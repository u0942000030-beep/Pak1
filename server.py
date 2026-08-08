# -*- coding: utf-8 -*-
"""
server.py - Server WebSocket UNIFICATO (Garage + Arena) per il deploy
su Render.

PERCHE' QUESTO FILE ESISTE
---------------------------
In locale usiamo due processi separati (garage_server.py sulla porta
8766, arena_server.py sulla 8767), ognuno con la propria connessione
SQLite. Su Render, un Web Service espone UNA sola porta pubblica
($PORT) e due servizi diversi NON condividono lo stesso disco: se
tenessimo i due server separati, avremmo due file robot_arena.db
scollegati (account creati nel garage invisibili all'arena, robot
salvati non trovabili al join, ecc.).

Questo file unisce i due protocolli su un'unica connessione WebSocket,
un'unica connessione DB (accounts.py + garage.py, invariati) e
un'unica istanza di Arena (arena.py, invariato). Il client si collega
UNA VOLTA e puo' mandare sia i messaggi "da garage" (register, login,
get_catalog, list_robots, save_robot, delete_robot) sia quelli "da
arena" (join_arena, input, fire, leave_arena) sulla stessa socket.

Protocollo: identico all'unione di garage_server.py + arena_server.py.
Vedi i docstring di quei due file per il dettaglio messaggio per
messaggio: qui non e' cambiato nulla nel formato, solo che ora arrivano
tutti sulla stessa connessione.

Avvio locale:
    python3 server.py [porta]        # default 8766

Avvio su Render:
    Render imposta la variabile d'ambiente PORT automaticamente;
    questo script la legge in automatico (vedi main()).
"""

import asyncio
import json
import os
import pathlib
import sys
import uuid

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

import accounts
import garage
from arena import Arena
from common import TICK_DT

DB_PATH = os.environ.get("DB_PATH", "robot_arena.db")
DEFAULT_PORT = 8766
BROADCAST_EVERY_N_TICKS = 3  # stato broadcast a TICK_HZ/3 (~20Hz), fisica a 60Hz piena

# Stesso pattern di main.py (Pac-Man Arena): un solo processo/servizio
# basta per tutto, niente static site separato su Render. Le richieste
# HTTP GET normali (non upgrade a WebSocket) ricevono garage.html o
# arena.html a seconda del path; le vere richieste WebSocket del gioco
# proseguono normalmente (vedi serve_client sotto).
_HERE = pathlib.Path(__file__).parent


def _load_html(filename):
    path = _HERE / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


GARAGE_HTML = _load_html("garage.html")
ARENA_HTML = _load_html("arena.html")


async def serve_client(connection, request):
    """process_request: intercetta le richieste HTTP GET normali (aperture
    di link da browser, health check della piattaforma di hosting) e
    risponde con la pagina giusta in base al path. Le richieste di upgrade
    WebSocket (Upgrade: websocket) proseguono invece normalmente restituendo
    None. Vedi la nota identica in main.py di Pac-Man Arena sul motivo per
    cui la firma dev'essere esattamente (connection, request) -> Response|None
    con un vero oggetto websockets.http11.Response."""
    upgrade = request.headers.get("Upgrade", "")
    if upgrade.lower() == "websocket":
        return None  # lascia proseguire come WebSocket

    path = request.path.split("?", 1)[0]
    if path in ("/arena", "/arena.html") and ARENA_HTML is not None:
        body = ARENA_HTML.encode("utf-8")
    elif GARAGE_HTML is not None:
        # / , /garage, /garage.html o qualunque altro path -> officina
        # (schermata di partenza del gioco).
        body = GARAGE_HTML.encode("utf-8")
    else:
        body = b"Robot Arena server OK\n"
        headers = Headers([
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return Response(200, "OK", headers, body)

    headers = Headers([
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return Response(200, "OK", headers, body)


def make_error(message: str) -> dict:
    return {"type": "error", "message": message}


class Session:
    __slots__ = ("account_id", "username", "arena_key")

    def __init__(self):
        self.account_id = None
        self.username = None
        self.arena_key = None

    @property
    def logged_in(self):
        return self.account_id is not None

    @property
    def in_arena(self):
        return self.arena_key is not None


class GameServer:
    def __init__(self, db_path=DB_PATH):
        self.conn_db = accounts.get_connection(db_path)
        accounts.init_db(self.conn_db)
        garage.init_db(self.conn_db)

        self.arena = Arena()
        self.connections = {}  # arena_key -> websocket, per il broadcast mirato

    # -----------------------------------------------------------------
    # Dispatch messaggi
    # -----------------------------------------------------------------
    async def handle_message(self, websocket, session: Session, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(json.dumps(make_error("Messaggio non valido (JSON malformato).")))
            return

        msg_type = msg.get("type")

        # --- Azioni pubbliche (non richiedono login) ------------------------
        if msg_type == "register":
            try:
                account = accounts.create_account(
                    self.conn_db, msg.get("username", ""), msg.get("email", ""), msg.get("password", "")
                )
            except accounts.AccountError as e:
                await websocket.send(json.dumps(make_error(str(e))))
                return
            await websocket.send(json.dumps({"type": "register_ok", "account": account}))
            return

        if msg_type == "login":
            account = accounts.authenticate(self.conn_db, msg.get("username", ""), msg.get("password", ""))
            if account is None:
                await websocket.send(json.dumps(make_error("Username o password errati.")))
                return
            session.account_id = account["id"]
            session.username = account["username"]
            await websocket.send(json.dumps({"type": "login_ok", "account": account}))
            return

        if msg_type == "get_catalog":
            await websocket.send(json.dumps({"type": "catalog", **garage.catalog_snapshot()}))
            return

        # --- Da qui in poi serve essere loggati ------------------------------
        if not session.logged_in:
            await websocket.send(json.dumps(make_error("Devi effettuare il login prima.")))
            return

        if msg_type == "list_robots":
            try:
                robots = garage.list_robots(self.conn_db, session.account_id)
            except garage.GarageError as e:
                await websocket.send(json.dumps(make_error(str(e))))
                return
            await websocket.send(json.dumps({"type": "robots", "robots": robots}))
            return

        if msg_type == "save_robot":
            try:
                robot = garage.save_robot(
                    self.conn_db, session.account_id,
                    msg.get("name", ""), msg.get("loadout", {}),
                    robot_id=msg.get("robot_id"),
                )
            except garage.GarageError as e:
                await websocket.send(json.dumps(make_error(str(e))))
                return
            await websocket.send(json.dumps({"type": "robot_saved", "robot": robot}))
            return

        if msg_type == "delete_robot":
            try:
                garage.delete_robot(self.conn_db, session.account_id, msg.get("robot_id"))
            except garage.GarageError as e:
                await websocket.send(json.dumps(make_error(str(e))))
                return
            await websocket.send(json.dumps({"type": "robot_deleted", "robot_id": msg.get("robot_id")}))
            return

        if msg_type == "join_arena":
            if session.in_arena:
                await websocket.send(json.dumps(make_error("Sei gia' in arena.")))
                return
            robot_id = msg.get("robot_id")
            try:
                robot = garage.load_robot(self.conn_db, robot_id)
            except garage.GarageError as e:
                await websocket.send(json.dumps(make_error(str(e))))
                return
            if robot["owner_id"] != session.account_id:
                await websocket.send(json.dumps(make_error("Questo robot non appartiene a questo account.")))
                return

            arena_key = uuid.uuid4().hex[:8]
            loadout = {
                "chassis_id": robot["chassis_id"], "movement_id": robot["movement_id"],
                "weapon_ids": robot["weapon_ids"], "armor_id": robot["armor_id"], "core_id": robot["core_id"],
            }
            self.arena.add_robot(
                arena_key, session.account_id, robot["id"], robot["name"], robot["derived_stats"],
                loadout=loadout,
            )
            session.arena_key = arena_key
            self.connections[arena_key] = websocket

            snap = self.arena.snapshot()
            await websocket.send(json.dumps({
                "type": "joined_arena", "your_id": arena_key,
                "maze": snap["maze"], "w": snap["w"], "h": snap["h"],
                "name": snap["name"], "theme": snap["theme"],
                "roster": self.arena.roster(),
            }))
            await self._broadcast_raw(json.dumps({
                "type": "roster_update", "joined": {arena_key: {"name": robot["name"], "loadout": loadout}},
            }), exclude=arena_key)
            return

        # --- Da qui in poi serve essere in arena ------------------------------
        if not session.in_arena:
            await websocket.send(json.dumps(make_error("Devi entrare in arena prima (join_arena).")))
            return

        if msg_type == "input":
            self.arena.set_input(
                session.arena_key,
                forward=msg.get("forward", False), back=msg.get("back", False),
                left=msg.get("left", False), right=msg.get("right", False),
            )
            return

        if msg_type == "fire":
            self.arena.try_fire(session.arena_key, msg.get("weapon_index", 0))
            return

        if msg_type == "leave_arena":
            left_key = session.arena_key
            self.arena.remove_robot(left_key)
            self.connections.pop(left_key, None)
            session.arena_key = None
            await self._broadcast_raw(json.dumps({"type": "roster_update", "left": left_key}))
            return

        await websocket.send(json.dumps(make_error(f"Tipo di messaggio sconosciuto: '{msg_type}'.")))

    # -----------------------------------------------------------------
    async def handle_client(self, websocket):
        session = Session()
        try:
            async for raw in websocket:
                await self.handle_message(websocket, session, raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if session.in_arena:
                self.arena.remove_robot(session.arena_key)
                self.connections.pop(session.arena_key, None)

    # -----------------------------------------------------------------
    async def tick_loop(self):
        tick_count = 0
        while True:
            self.arena.tick(TICK_DT)
            events = self.arena.pop_events()
            tick_count += 1

            if events:
                await self._broadcast_raw(json.dumps({"type": "events", "events": events}))

            if tick_count % BROADCAST_EVERY_N_TICKS == 0:
                snap = self.arena.snapshot()
                await self._broadcast_raw(json.dumps({"type": "state", "robots": snap["robots"]}))

            await asyncio.sleep(TICK_DT)

    async def _broadcast_raw(self, payload: str, exclude=None):
        dead = []
        for key, ws in self.connections.items():
            if key == exclude:
                continue
            try:
                await ws.send(payload)
            except websockets.exceptions.ConnectionClosed:
                dead.append(key)
        for key in dead:
            self.arena.remove_robot(key)
            self.connections.pop(key, None)


async def main():
    # Render imposta $PORT automaticamente; in locale usiamo l'argomento
    # da riga di comando o il default.
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT))
    server = GameServer()

    tick_task = asyncio.create_task(server.tick_loop())

    print(f"Server unificato (garage+arena) in ascolto su ws://0.0.0.0:{port}  (mappa: {server.arena.name})")
    async with websockets.serve(
        server.handle_client, "0.0.0.0", port, process_request=serve_client,
        compression=None,
    ):
        await tick_task


if __name__ == "__main__":
    asyncio.run(main())
