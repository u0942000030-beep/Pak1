# -*- coding: utf-8 -*-
"""
arena.py - Simulazione di combattimento robot per Robot Arena.

Riusa la mappa "Sala del Trono Violetta" dal catalogo condiviso
(common.py, MAZES) e le sue funzioni helper (is_wall, hitbox_hit,
TICK_HZ/TICK_DT), esattamente come fa Pac-Man Arena, cosi' la fisica
gira alla stessa cadenza e con le stesse convenzioni di coordinate
(celle continue, origine in alto a sinistra).

Questo modulo NON tocca la rete: e' pura simulazione, testabile in
isolamento (vedi self-test in fondo). Il collegamento WebSocket e'
in arena_server.py.

Uso tipico:

    arena = Arena()
    key = arena.add_robot("p1", "Falco Rosso", robot_derived_stats)
    arena.set_input(key, forward=True)
    for _ in range(60):
        arena.tick(1/60)
    print(arena.snapshot())
"""

import math
import random
import time

from common import MAZES, TICK_DT, TICK_HZ, is_wall, hitbox_hit

ARENA_MAP_NAME = "Sala del Trono Violetta"

ROBOT_RADIUS = 0.32          # celle - usato per collisioni muro e hitbox tra robot
RESPAWN_SECONDS = 3.0
BACKWARD_SPEED_FACTOR = 0.55  # muoversi all'indietro e' piu' lento (come quasi ogni arena shooter)
RAYCAST_STEP = 0.08           # celle - granularita' del raycast delle armi


def _load_arena_map(name=ARENA_MAP_NAME):
    for m in MAZES:
        if m["name"] == name:
            return m
    raise ValueError(f"Mappa '{name}' non trovata in MAZES.")


class RobotState:
    """Stato a runtime di un robot in arena. I valori di combattimento
    (max_hp, speed, damage_reduction, armi, ecc.) arrivano gia' calcolati
    da garage.compute_derived_stats - questo modulo non conosce i pezzi,
    solo le statistiche finali. Questo disaccoppia arena.py dal catalogo
    pezzi: se il garage cambia, l'arena non deve sapere nulla."""

    def __init__(self, key, account_id, robot_id, name, derived_stats, spawn, loadout=None):
        self.key = key                    # chiave di sessione (es. connessione WS)
        self.account_id = account_id
        self.robot_id = robot_id
        self.name = name
        # Il loadout (piece id per slot) serve SOLO al rendering lato
        # client (sapere quali pezzi disegnare): la simulazione qui usa
        # esclusivamente derived_stats, gia' calcolate da garage.py.
        self.loadout = loadout or {}

        self.max_hp = derived_stats["max_hp"]
        self.hp = self.max_hp
        self.speed = derived_stats["speed"]
        self.turn_rate = derived_stats["turn_rate"]
        self.damage_reduction = derived_stats["damage_reduction"]
        self.hp_regen_per_sec = derived_stats["hp_regen_per_sec"]
        self.reload_bonus = derived_stats["reload_bonus"]
        # Ogni arma tiene un cooldown_remaining indipendente.
        self.weapons = [
            {
                "id": w["id"], "name": w["name"], "damage": w["damage"],
                "fire_rate": w["fire_rate"] * (1.0 + self.reload_bonus),
                "range": w["range"], "cooldown_remaining": 0.0,
            }
            for w in derived_stats["weapons"]
        ]

        self.x, self.y = spawn
        self.angle = 0.0  # radianti, 0 = verso +x

        self.alive = True
        self.respawn_timer = 0.0

        # input correnti (impostati da set_input, consumati da tick)
        self.move_forward = False
        self.move_back = False
        self.turn_left = False
        self.turn_right = False

    def public_state(self):
        return {
            "id": self.key, "name": self.name,
            "x": round(self.x, 3), "y": round(self.y, 3), "angle": round(self.angle, 3),
            "hp": round(self.hp, 1), "max_hp": self.max_hp, "alive": self.alive,
            "weapons": [{"id": w["id"], "cooldown_remaining": round(w["cooldown_remaining"], 2)} for w in self.weapons],
        }


class Arena:
    def __init__(self, map_name=ARENA_MAP_NAME):
        m = _load_arena_map(map_name)
        self.maze = m["maze"]
        self.w = len(self.maze[0])
        self.h = len(self.maze)
        self.name = m["name"]
        self.theme = m["theme"]
        # Punti di spawn del labirinto originale (celle intere) -> centro
        # cella (vedi nota storica sul fix "+0.5 cell-center" nel progetto
        # sorella: senza l'offset gli spawn cadevano sull'angolo della
        # cella invece che al centro, con hitbox leggermente sballate).
        self.spawn_points = [(x + 0.5, y + 0.5) for x, y in m["spawn_points"]]

        self.robots: dict[object, RobotState] = {}
        self._spawn_cursor = 0
        self.events = []  # eventi dell'ultimo tick (hit/kill/respawn), consumati dal server

    # -----------------------------------------------------------------
    # Gestione robot
    # -----------------------------------------------------------------
    def _next_spawn(self):
        spawn = self.spawn_points[self._spawn_cursor % len(self.spawn_points)]
        self._spawn_cursor += 1
        return spawn

    def add_robot(self, key, account_id, robot_id, name, derived_stats, loadout=None):
        if key in self.robots:
            raise ValueError("Chiave robot gia' presente in arena.")
        spawn = self._next_spawn()
        robot = RobotState(key, account_id, robot_id, name, derived_stats, spawn, loadout=loadout)
        self.robots[key] = robot
        return robot

    def remove_robot(self, key):
        self.robots.pop(key, None)

    def roster(self):
        """Nome + loadout (piece id) di tutti i robot presenti, per il
        rendering lato client. Inviato una tantum al join, non ad ogni
        tick (a differenza di public_state, molto piu' leggero)."""
        return {
            key: {"name": r.name, "loadout": r.loadout}
            for key, r in self.robots.items()
        }

    def set_input(self, key, forward=False, back=False, left=False, right=False):
        r = self.robots.get(key)
        if r is None or not r.alive:
            return
        r.move_forward = bool(forward)
        r.move_back = bool(back)
        r.turn_left = bool(left)
        r.turn_right = bool(right)

    # -----------------------------------------------------------------
    # Collisioni
    # -----------------------------------------------------------------
    def _blocked(self, x, y):
        """Approssima la hitbox circolare del robot campionando 4 punti
        cardinali attorno al centro (stesso approccio economico usato per
        i personaggi in Pac-Man Arena, sufficiente perche' ROBOT_RADIUS
        e' piccolo rispetto alla cella)."""
        r = ROBOT_RADIUS
        for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
            if is_wall(self.maze, self.w, self.h, int(x + dx), int(y + dy)):
                return True
        return False

    def _try_move(self, robot, dx, dy):
        # Slide lungo i muri: prova prima solo X poi solo Y, cosi' un
        # robot che sbatte "di striscio" continua a scivolare invece di
        # fermarsi di colpo (stesso pattern del movimento giocatore in
        # Pac-Man Arena).
        nx = robot.x + dx
        if not self._blocked(nx, robot.y):
            robot.x = max(0.0, min(self.w - 1.0, nx))
        ny = robot.y + dy
        if not self._blocked(robot.x, ny):
            robot.y = max(0.0, min(self.h - 1.0, ny))

    # -----------------------------------------------------------------
    # Combattimento
    # -----------------------------------------------------------------
    def try_fire(self, key, weapon_index):
        """Tenta di sparare l'arma weapon_index del robot 'key'. Ritorna
        un dict-evento se lo sparo e' avvenuto (anche a vuoto), None se
        rifiutato (robot morto, arma inesistente, cooldown non pronto)."""
        shooter = self.robots.get(key)
        if shooter is None or not shooter.alive:
            return None
        if weapon_index < 0 or weapon_index >= len(shooter.weapons):
            return None
        weapon = shooter.weapons[weapon_index]
        if weapon["cooldown_remaining"] > 0:
            return None

        weapon["cooldown_remaining"] = 1.0 / weapon["fire_rate"]

        # Raycast lungo l'angolo corrente del robot, passo per passo:
        # si ferma al primo muro o al primo robot avversario colpito.
        dirx, diry = math.cos(shooter.angle), math.sin(shooter.angle)
        dist = 0.0
        hit_target = None
        while dist < weapon["range"]:
            dist += RAYCAST_STEP
            px = shooter.x + dirx * dist
            py = shooter.y + diry * dist
            if is_wall(self.maze, self.w, self.h, int(px), int(py)):
                break
            for other_key, other in self.robots.items():
                if other_key == key or not other.alive:
                    continue
                if hitbox_hit(px, py, other.x, other.y, ROBOT_RADIUS):
                    hit_target = other
                    break
            if hit_target:
                break

        event = {
            "type": "fire", "shooter_id": key, "weapon_id": weapon["id"],
            "angle": shooter.angle, "hit": False,
        }

        if hit_target is not None:
            raw_damage = weapon["damage"]
            actual_damage = raw_damage * (1.0 - hit_target.damage_reduction)
            hit_target.hp = max(0.0, hit_target.hp - actual_damage)
            event.update({
                "hit": True, "target_id": hit_target.key,
                "damage": round(actual_damage, 1),
            })
            if hit_target.hp <= 0:
                hit_target.alive = False
                hit_target.respawn_timer = RESPAWN_SECONDS
                event["killed"] = True

        self.events.append(event)
        return event

    # -----------------------------------------------------------------
    # Tick
    # -----------------------------------------------------------------
    def tick(self, dt=TICK_DT):
        for robot in self.robots.values():
            for weapon in robot.weapons:
                if weapon["cooldown_remaining"] > 0:
                    weapon["cooldown_remaining"] = max(0.0, weapon["cooldown_remaining"] - dt)

            if not robot.alive:
                robot.respawn_timer -= dt
                if robot.respawn_timer <= 0:
                    robot.x, robot.y = self._next_spawn()
                    robot.hp = robot.max_hp
                    robot.alive = True
                    self.events.append({"type": "respawn", "robot_id": robot.key})
                continue

            if robot.hp_regen_per_sec > 0:
                robot.hp = min(robot.max_hp, robot.hp + robot.hp_regen_per_sec * dt)

            turn_input = (1 if robot.turn_right else 0) - (1 if robot.turn_left else 0)
            robot.angle = (robot.angle + turn_input * robot.turn_rate * dt) % (2 * math.pi)

            move_input = (1 if robot.move_forward else 0) - (1 if robot.move_back else 0)
            if move_input != 0:
                speed = robot.speed if move_input > 0 else robot.speed * BACKWARD_SPEED_FACTOR
                dx = math.cos(robot.angle) * speed * dt * move_input
                dy = math.sin(robot.angle) * speed * dt * move_input
                self._try_move(robot, dx, dy)

    def pop_events(self):
        events, self.events = self.events, []
        return events

    def snapshot(self):
        return {
            "maze": self.maze, "w": self.w, "h": self.h, "name": self.name, "theme": self.theme,
            "robots": [r.public_state() for r in self.robots.values()],
        }


# ---------------------------------------------------------------------------
# Self-test rapido (python3 arena.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    derived_stats_a = {
        "max_hp": 130, "speed": 3.0, "turn_rate": 3.0, "damage_reduction": 0.0,
        "hp_regen_per_sec": 0.0, "reload_bonus": 0.0,
        "weapons": [{"id": "weap_laser", "name": "Laser", "damage": 14, "fire_rate": 1.5, "range": 12}],
    }
    derived_stats_b = {
        "max_hp": 80, "speed": 3.4, "turn_rate": 2.6, "damage_reduction": 0.25,
        "hp_regen_per_sec": 1.0, "reload_bonus": 0.0,
        "weapons": [{"id": "weap_mg", "name": "Mitragliatrice", "damage": 6, "fire_rate": 4.0, "range": 8}],
    }

    arena = Arena()
    print(f"Mappa: {arena.name}  {arena.w}x{arena.h}  spawn={arena.spawn_points}")

    a = arena.add_robot("A", account_id=1, robot_id=1, name="Falco Rosso", derived_stats=derived_stats_a)
    b = arena.add_robot("B", account_id=2, robot_id=2, name="Vespa Blu", derived_stats=derived_stats_b)
    print("Spawn A:", (a.x, a.y), "Spawn B:", (b.x, b.y))

    # --- Test 1: collisione coi muri contro il bordo della mappa --------
    arena.set_input("A", left=True)  # A ruota verso il muro esterno
    for _ in range(200):
        arena.tick(TICK_DT)
    assert 0.0 <= a.x <= arena.w - 1 and 0.0 <= a.y <= arena.h - 1
    print("OK: il robot non esce mai dalla mappa dopo 200 tick di rotazione.")

    # --- Test 2: sparo a vuoto (bersaglio troppo lontano/fuori linea) ---
    arena.set_input("A", left=False)
    a.angle = 0.0
    a.x, a.y = 5.0, 5.0
    b.x, b.y = 5.0, 30.0  # fuori mappa in y volutamente per forzare "nessun hit" pulito
    b.x, b.y = 30.0, 5.0
    a.angle = math.pi  # punta lontano da B
    ev = arena.try_fire("A", 0)
    assert ev is not None and ev["hit"] is False
    print("OK: sparo a vuoto quando il bersaglio non e' sulla traiettoria.")

    # --- Test 3: colpo diretto con danno ridotto dall'armatura -----------
    for w in a.weapons:
        w["cooldown_remaining"] = 0.0
    a.x, a.y = 5.0, 5.0
    b.x, b.y = 7.0, 5.0
    a.angle = 0.0  # punta dritto verso B
    hp_before = b.hp
    ev = arena.try_fire("A", 0)
    assert ev["hit"] is True and ev["target_id"] == "B"
    expected_damage = 14 * (1 - 0.25)
    assert abs(ev["damage"] - expected_damage) < 0.01, ev
    assert abs((hp_before - b.hp) - expected_damage) < 0.01
    print(f"OK: colpo diretto, danno atteso {expected_damage}, danno applicato {ev['damage']}.")

    # --- Test 4: cooldown blocca lo sparo immediato successivo -----------
    ev2 = arena.try_fire("A", 0)
    assert ev2 is None
    print("OK: cooldown impedisce lo sparo immediato ripetuto.")

    # --- Test 5: uccisione e respawn ---------------------------------------
    a.x, a.y = 5.0, 5.0
    b.x, b.y = 6.0, 5.0  # a distanza ravvicinata, dentro gittata
    b.hp = 3.0  # quasi morto
    for w in a.weapons:
        w["cooldown_remaining"] = 0.0
    ev3 = arena.try_fire("A", 0)
    assert ev3["hit"] is True and ev3.get("killed") is True
    assert b.alive is False
    print("OK: robot ucciso quando hp <= 0.")

    for _ in range(int(RESPAWN_SECONDS / TICK_DT) + 5):
        arena.tick(TICK_DT)
    assert b.alive is True and b.hp == b.max_hp
    print("OK: respawn automatico con vita piena dopo RESPAWN_SECONDS.")

    # --- Test 6: rigenerazione HP (core rigenerante) ----------------------
    b.hp = 50.0
    hp_before = b.hp
    for _ in range(TICK_HZ):  # 1 secondo simulato
        arena.tick(TICK_DT)
    assert b.hp > hp_before
    print(f"OK: rigenerazione HP nel tempo, {hp_before} -> {round(b.hp,2)}.")

    print("\nTutti i test passati.")
