# -*- coding: utf-8 -*-
"""
garage.py - Catalogo pezzi e logica di assemblaggio/salvataggio robot
per Robot Arena.

Il catalogo pezzi e' FISSO, definito qui dal gioco (non modificabile dal
giocatore). I robot salvati vivono su SQLite, nella stessa connessione
usata da accounts.py (vedi init_db in questo modulo, da chiamare insieme
a accounts.init_db).

Uso tipico:

    import accounts, garage

    conn = accounts.get_connection("robot_arena.db")
    accounts.init_db(conn)
    garage.init_db(conn)

    loadout = {
        "chassis_id": "chassis_light",
        "movement_id": "move_wheels",
        "weapon_ids": ["weap_laser", "weap_mg"],
        "armor_id": "armor_light",
        "core_id": "core_regen",
    }

    robot = garage.save_robot(conn, owner_id=1, name="Falco Rosso", loadout=loadout)
    full = garage.load_robot(conn, robot["id"])   # loadout + pezzi risolti + stat derivate
"""

import json
import sqlite3
import time


class GarageError(Exception):
    """Errore applicativo: loadout non valido, pezzo inesistente, slot
    incompatibile, robot non trovato, ecc. Messaggio pensato per l'utente."""
    pass


# ---------------------------------------------------------------------------
# Catalogo pezzi (FISSO - definito dal gioco)
# ---------------------------------------------------------------------------
# Ogni pezzo: id, name, slot, cost, stats (dict tipizzato per slot).
# 'weight' e' presente su chassis/movimento/armi/armatura (NON sul core,
# che e' elettronica leggera) e concorre al calcolo della velocita'
# effettiva del robot (vedi compute_derived_stats).

PIECES = {
    # --- CHASSIS: vita massima, n. slot arma, peso base -------------------
    "chassis_light": {
        "id": "chassis_light", "name": "Chassis Leggero", "slot": "chassis", "cost": 0,
        "stats": {"max_hp": 80, "weapon_slots": 1, "weight": 10},
    },
    "chassis_medium": {
        "id": "chassis_medium", "name": "Chassis Medio", "slot": "chassis", "cost": 150,
        "stats": {"max_hp": 130, "weapon_slots": 2, "weight": 18},
    },
    "chassis_heavy": {
        "id": "chassis_heavy", "name": "Chassis Pesante", "slot": "chassis", "cost": 350,
        "stats": {"max_hp": 200, "weapon_slots": 3, "weight": 30},
    },

    # --- MOVIMENTO: velocita' base, turn_rate, peso ------------------------
    "move_treads": {
        "id": "move_treads", "name": "Cingoli", "slot": "movement", "cost": 0,
        "stats": {"speed": 2.2, "turn_rate": 1.4, "weight": 12},
    },
    "move_wheels": {
        "id": "move_wheels", "name": "Ruote", "slot": "movement", "cost": 120,
        "stats": {"speed": 3.4, "turn_rate": 2.6, "weight": 6},
    },
    "move_legs": {
        "id": "move_legs", "name": "Gambe", "slot": "movement", "cost": 200,
        "stats": {"speed": 3.0, "turn_rate": 3.2, "weight": 8},
    },

    # --- ARMI: danno, cadenza di fuoco (colpi/sec), gittata (celle), peso -
    "weap_mg": {
        "id": "weap_mg", "name": "Mitragliatrice", "slot": "weapon", "cost": 0,
        "stats": {"damage": 6, "fire_rate": 4.0, "range": 8, "weight": 5},
    },
    "weap_laser": {
        "id": "weap_laser", "name": "Laser", "slot": "weapon", "cost": 180,
        "stats": {"damage": 14, "fire_rate": 1.5, "range": 12, "weight": 7},
    },
    "weap_missiles": {
        "id": "weap_missiles", "name": "Missili", "slot": "weapon", "cost": 220,
        "stats": {"damage": 25, "fire_rate": 0.8, "range": 10, "weight": 9},
    },
    "weap_melee": {
        "id": "weap_melee", "name": "Mazza da Mischia", "slot": "weapon", "cost": 100,
        "stats": {"damage": 30, "fire_rate": 1.2, "range": 1, "weight": 11},
    },

    # --- ARMATURA: riduzione danno (0-1), peso -----------------------------
    "armor_light": {
        "id": "armor_light", "name": "Armatura Leggera", "slot": "armor", "cost": 80,
        "stats": {"damage_reduction": 0.10, "weight": 6},
    },
    "armor_heavy": {
        "id": "armor_heavy", "name": "Armatura Pesante", "slot": "armor", "cost": 250,
        "stats": {"damage_reduction": 0.25, "weight": 16},
    },

    # --- CORE/REATTORE: bonus passivi, nessun peso -------------------------
    "core_regen": {
        "id": "core_regen", "name": "Core Rigenerante", "slot": "core", "cost": 200,
        "stats": {"hp_regen_per_sec": 1.5, "reload_bonus": 0.0, "weight": 0},
    },
    "core_overcharge": {
        "id": "core_overcharge", "name": "Core Overcharge", "slot": "core", "cost": 220,
        "stats": {"hp_regen_per_sec": 0.0, "reload_bonus": 0.20, "weight": 0},
    },
}

SLOTS = ("chassis", "movement", "weapon", "armor", "core")

# Costante di attrito peso->velocita': ogni unita' di peso oltre il peso
# "di riferimento" del movimento rallenta il robot. Valore da bilanciare
# in playtest, isolato qui per poterlo tarare in un punto solo.
WEIGHT_SPEED_FACTOR = 0.02
MIN_EFFECTIVE_SPEED = 0.5


def get_piece(piece_id):
    piece = PIECES.get(piece_id)
    if piece is None:
        raise GarageError(f"Pezzo inesistente: '{piece_id}'.")
    return piece


def pieces_by_slot(slot):
    if slot not in SLOTS:
        raise GarageError(f"Slot inesistente: '{slot}'.")
    return [p for p in PIECES.values() if p["slot"] == slot]


def catalog_snapshot():
    """Ritorna l'intero catalogo (per inviarlo al client garage)."""
    return {"pieces": list(PIECES.values()), "slots": list(SLOTS)}


# ---------------------------------------------------------------------------
# Validazione loadout + stat derivate
# ---------------------------------------------------------------------------

def validate_loadout(loadout: dict) -> dict:
    """Valida un loadout (dict con chassis_id/movement_id/weapon_ids/
    armor_id/core_id). Solleva GarageError con messaggio parlante al primo
    problema trovato. Ritorna i pezzi risolti (dict di oggetti pezzo)."""

    chassis_id = loadout.get("chassis_id")
    movement_id = loadout.get("movement_id")
    weapon_ids = loadout.get("weapon_ids") or []
    armor_id = loadout.get("armor_id")
    core_id = loadout.get("core_id")

    if not chassis_id:
        raise GarageError("Manca lo chassis.")
    if not movement_id:
        raise GarageError("Manca il modulo di movimento.")
    if not weapon_ids:
        raise GarageError("Serve almeno un'arma.")
    if not isinstance(weapon_ids, list):
        raise GarageError("weapon_ids deve essere una lista.")

    chassis = get_piece(chassis_id)
    if chassis["slot"] != "chassis":
        raise GarageError(f"'{chassis_id}' non e' uno chassis.")

    movement = get_piece(movement_id)
    if movement["slot"] != "movement":
        raise GarageError(f"'{movement_id}' non e' un modulo di movimento.")

    weapons = []
    for wid in weapon_ids:
        w = get_piece(wid)
        if w["slot"] != "weapon":
            raise GarageError(f"'{wid}' non e' un'arma.")
        weapons.append(w)

    max_slots = chassis["stats"]["weapon_slots"]
    if len(weapons) > max_slots:
        raise GarageError(
            f"Troppe armi: {chassis['name']} ha {max_slots} slot, "
            f"ne hai equipaggiate {len(weapons)}."
        )

    armor = None
    if armor_id:
        armor = get_piece(armor_id)
        if armor["slot"] != "armor":
            raise GarageError(f"'{armor_id}' non e' un'armatura.")

    core = None
    if core_id:
        core = get_piece(core_id)
        if core["slot"] != "core":
            raise GarageError(f"'{core_id}' non e' un core.")

    return {
        "chassis": chassis, "movement": movement, "weapons": weapons,
        "armor": armor, "core": core,
    }


def compute_derived_stats(resolved: dict) -> dict:
    """A partire dai pezzi risolti (output di validate_loadout), calcola
    le statistiche finali del robot pronte per l'arena."""
    chassis = resolved["chassis"]
    movement = resolved["movement"]
    weapons = resolved["weapons"]
    armor = resolved["armor"]
    core = resolved["core"]

    total_weight = (
        chassis["stats"]["weight"]
        + movement["stats"]["weight"]
        + sum(w["stats"]["weight"] for w in weapons)
        + (armor["stats"]["weight"] if armor else 0)
    )

    effective_speed = max(
        MIN_EFFECTIVE_SPEED,
        movement["stats"]["speed"] - WEIGHT_SPEED_FACTOR * total_weight,
    )

    return {
        "max_hp": chassis["stats"]["max_hp"],
        "weight": total_weight,
        "speed": round(effective_speed, 3),
        "turn_rate": movement["stats"]["turn_rate"],
        "damage_reduction": armor["stats"]["damage_reduction"] if armor else 0.0,
        "hp_regen_per_sec": core["stats"]["hp_regen_per_sec"] if core else 0.0,
        "reload_bonus": core["stats"]["reload_bonus"] if core else 0.0,
        "weapons": [
            {
                "id": w["id"], "name": w["name"],
                "damage": w["stats"]["damage"],
                "fire_rate": w["stats"]["fire_rate"],
                "range": w["stats"]["range"],
            }
            for w in weapons
        ],
    }


# ---------------------------------------------------------------------------
# Persistenza robot
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS robots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    chassis_id   TEXT NOT NULL,
    movement_id  TEXT NOT NULL,
    weapon_ids   TEXT NOT NULL,   -- JSON array di piece id
    armor_id     TEXT,
    core_id      TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_robots_owner ON robots(owner_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


MAX_ROBOT_NAME_LEN = 24
MAX_ROBOTS_PER_ACCOUNT = 6


def _row_to_robot(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "chassis_id": row["chassis_id"],
        "movement_id": row["movement_id"],
        "weapon_ids": json.loads(row["weapon_ids"]),
        "armor_id": row["armor_id"],
        "core_id": row["core_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_robot(conn: sqlite3.Connection, owner_id: int, name: str, loadout: dict,
               robot_id: int | None = None) -> dict:
    """Valida il loadout e lo salva. Se robot_id e' dato, AGGIORNA quel
    robot (deve appartenere a owner_id). Altrimenti CREA un nuovo robot,
    a patto che l'account non abbia gia' raggiunto MAX_ROBOTS_PER_ACCOUNT.
    Ritorna il record salvato (via load_robot)."""

    name = (name or "").strip()
    if not name:
        raise GarageError("Il robot deve avere un nome.")
    if len(name) > MAX_ROBOT_NAME_LEN:
        raise GarageError(f"Nome troppo lungo (max {MAX_ROBOT_NAME_LEN} caratteri).")

    validate_loadout(loadout)  # solleva GarageError se qualcosa non torna
    now = time.time()
    weapon_ids_json = json.dumps(loadout.get("weapon_ids") or [])

    if robot_id is None:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM robots WHERE owner_id = ?", (owner_id,)
        ).fetchone()["n"]
        if count >= MAX_ROBOTS_PER_ACCOUNT:
            raise GarageError(f"Hai gia' {MAX_ROBOTS_PER_ACCOUNT} robot salvati (limite massimo).")

        cur = conn.execute(
            "INSERT INTO robots "
            "(owner_id, name, chassis_id, movement_id, weapon_ids, armor_id, core_id, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, name, loadout["chassis_id"], loadout["movement_id"],
             weapon_ids_json, loadout.get("armor_id"), loadout.get("core_id"), now, now),
        )
        conn.commit()
        robot_id = cur.lastrowid
    else:
        existing = conn.execute(
            "SELECT owner_id FROM robots WHERE id = ?", (robot_id,)
        ).fetchone()
        if existing is None:
            raise GarageError("Robot non trovato.")
        if existing["owner_id"] != owner_id:
            raise GarageError("Questo robot non appartiene a questo account.")

        conn.execute(
            "UPDATE robots SET name=?, chassis_id=?, movement_id=?, weapon_ids=?, "
            "armor_id=?, core_id=?, updated_at=? WHERE id=?",
            (name, loadout["chassis_id"], loadout["movement_id"], weapon_ids_json,
             loadout.get("armor_id"), loadout.get("core_id"), now, robot_id),
        )
        conn.commit()

    return load_robot(conn, robot_id)


def load_robot(conn: sqlite3.Connection, robot_id: int) -> dict:
    """Carica un robot e lo arricchisce con i pezzi risolti e le stat
    derivate (pronto per essere spedito al client arena)."""
    row = conn.execute("SELECT * FROM robots WHERE id = ?", (robot_id,)).fetchone()
    if row is None:
        raise GarageError("Robot non trovato.")
    robot = _row_to_robot(row)
    resolved = validate_loadout(robot)  # ri-valida: cataloga sempre coerente col salvato
    robot["derived_stats"] = compute_derived_stats(resolved)
    return robot


def list_robots(conn: sqlite3.Connection, owner_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id FROM robots WHERE owner_id = ? ORDER BY updated_at DESC", (owner_id,)
    ).fetchall()
    return [load_robot(conn, r["id"]) for r in rows]


def delete_robot(conn: sqlite3.Connection, owner_id: int, robot_id: int) -> None:
    row = conn.execute("SELECT owner_id FROM robots WHERE id = ?", (robot_id,)).fetchone()
    if row is None:
        raise GarageError("Robot non trovato.")
    if row["owner_id"] != owner_id:
        raise GarageError("Questo robot non appartiene a questo account.")
    conn.execute("DELETE FROM robots WHERE id = ?", (robot_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Self-test rapido (python3 garage.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    import accounts

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        conn = accounts.get_connection(tmp.name)
        accounts.init_db(conn)
        init_db(conn)

        acc = accounts.create_account(conn, "savvynick", "nick@example.com", "supersegreta")
        owner_id = acc["id"]

        loadout = {
            "chassis_id": "chassis_medium",
            "movement_id": "move_wheels",
            "weapon_ids": ["weap_laser", "weap_mg"],
            "armor_id": "armor_light",
            "core_id": "core_regen",
        }
        robot = save_robot(conn, owner_id, "Falco Rosso", loadout)
        print("Robot creato:", json.dumps(robot, indent=2, ensure_ascii=False))
        assert robot["derived_stats"]["max_hp"] == 130

        # Troppe armi per lo slot disponibile -> deve fallire
        bad_loadout = dict(loadout, chassis_id="chassis_light",
                            weapon_ids=["weap_laser", "weap_mg"])
        try:
            save_robot(conn, owner_id, "Errore", bad_loadout)
            print("ERRORE: doveva fallire (troppe armi)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Pezzo inesistente -> deve fallire
        try:
            save_robot(conn, owner_id, "Errore2", dict(loadout, chassis_id="chassis_fantasma"))
            print("ERRORE: doveva fallire (pezzo inesistente)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Update robot esistente
        updated_loadout = dict(loadout, weapon_ids=["weap_missiles"])
        updated = save_robot(conn, owner_id, "Falco Rosso Mk2", updated_loadout, robot_id=robot["id"])
        assert updated["id"] == robot["id"]
        assert updated["name"] == "Falco Rosso Mk2"
        print("OK, update robot esistente:", updated["name"])

        # Owner sbagliato -> deve fallire
        try:
            save_robot(conn, owner_id + 999, "Furto", loadout, robot_id=robot["id"])
            print("ERRORE: doveva fallire (owner sbagliato)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Limite robot per account
        for i in range(MAX_ROBOTS_PER_ACCOUNT - 1):
            save_robot(conn, owner_id, f"Bot{i}", loadout)
        try:
            save_robot(conn, owner_id, "Troppi", loadout)
            print("ERRORE: doveva fallire (limite robot)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        robots = list_robots(conn, owner_id)
        print(f"Robot totali per l'account: {len(robots)}")
        assert len(robots) == MAX_ROBOTS_PER_ACCOUNT

        delete_robot(conn, owner_id, robot["id"])
        robots = list_robots(conn, owner_id)
        assert len(robots) == MAX_ROBOTS_PER_ACCOUNT - 1
        print("OK, delete robot")

        print("\nTutti i test passati.")
    finally:
        os.unlink(tmp.name)
