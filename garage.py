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
    # --- BUSTO (torso): forma/dimensione del tronco. Definisce anche i
    # punti di aggancio (spalle/anche/collo) usati dal renderer 3D per
    # attaccare correttamente braccia, gambe e testa qualunque sia la
    # combinazione scelta. Nessun impatto sulle statistiche per ora
    # (arriva quando disegneremo bene il sistema di combattimento):
    # 'weight' resta a 0 e non concorre al calcolo di compute_derived_stats.
    "torso_slim": {
        "id": "torso_slim", "name": "Busto Slim", "slot": "torso", "cost": 0,
        "stats": {"weight": 0},
        "visual": {
            "shape": "slim", "dims": [0.42, 0.55, 0.30],
            "shoulder_w": 1.05, "hip_w": 0.75, "color": 0x2a1a4a,
        },
    },
    "torso_standard": {
        "id": "torso_standard", "name": "Busto Standard", "slot": "torso", "cost": 0,
        "stats": {"weight": 0},
        "visual": {
            "shape": "standard", "dims": [0.52, 0.60, 0.38],
            "shoulder_w": 1.0, "hip_w": 0.85, "color": 0x2a1a4a,
        },
    },
    "torso_heavy": {
        "id": "torso_heavy", "name": "Busto Corazzato", "slot": "torso", "cost": 0,
        "stats": {"weight": 0},
        "visual": {
            "shape": "heavy", "dims": [0.70, 0.64, 0.52],
            "shoulder_w": 1.05, "hip_w": 1.0, "color": 0x241633,
        },
    },
    "torso_barrel": {
        "id": "torso_barrel", "name": "Busto a Botte", "slot": "torso", "cost": 0,
        "stats": {"weight": 0},
        "visual": {
            "shape": "barrel", "dims": [0.58, 0.62, 0.58],
            "shoulder_w": 0.95, "hip_w": 0.9, "color": 0x2a1a4a,
        },
    },
    "torso_wide": {
        "id": "torso_wide", "name": "Busto Ampio", "slot": "torso", "cost": 0,
        "stats": {"weight": 0},
        "visual": {
            "shape": "wide", "dims": [0.56, 0.58, 0.36],
            "shoulder_w": 1.35, "hip_w": 0.6, "color": 0x2a1a4a,
        },
    },

    # --- TESTA -------------------------------------------------------------
    "head_round": {
        "id": "head_round", "name": "Testa Sferica", "slot": "head", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "round", "size": 0.15},
    },
    "head_visor": {
        "id": "head_visor", "name": "Testa a Visiera", "slot": "head", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "visor", "size": 0.15},
    },
    "head_angular": {
        "id": "head_angular", "name": "Testa Angolare", "slot": "head", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "angular", "size": 0.16},
    },
    "head_antenna": {
        "id": "head_antenna", "name": "Testa con Antenne", "slot": "head", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "antenna", "size": 0.12},
    },
    "head_cyclops": {
        "id": "head_cyclops", "name": "Testa Monocolo", "slot": "head", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "cyclops", "size": 0.16},
    },

    # --- COMPONENTI ARTI: pezzi sciolti da incatenare liberamente per
    # costruire braccia e gambe pezzo su pezzo (segmento -> giuntura/
    # ammortizzatore -> segmento -> ... -> terminale). Vedi build_limb_chain
    # per la logica di assemblaggio e validate_limb_chain per le regole.
    #
    # Campi comuni:
    #   "length": quanto il pezzo fa avanzare la catena lungo l'arto (m).
    #             I tendini hanno length 0: sono decorativi, si agganciano
    #             al segmento strutturale precedente senza allungare l'arto.
    #   "radius": spessore per il disegno 3D.
    #   "bend_deg": SOLO su giunture/ammortizzatori: quanto piegano in
    #               avanti la direzione dell'arto da quel punto in poi.
    #   "kind": SOLO sui terminali: "hand" | "foot" | "claw" | "universal".
    #           "universal" va bene sia in fondo a un braccio che a una gamba.

    # -- Segmenti (ossa) ------------------------------------------------
    "seg_short_thin": {
        "id": "seg_short_thin", "name": "Segmento Corto Sottile", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "straight", "length": 0.14, "radius": 0.035},
    },
    "seg_short_armored": {
        "id": "seg_short_armored", "name": "Segmento Corto Corazzato", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "armored", "length": 0.16, "radius": 0.09},
    },
    "seg_med_thin": {
        "id": "seg_med_thin", "name": "Segmento Medio Sottile", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "straight", "length": 0.22, "radius": 0.045},
    },
    "seg_med_standard": {
        "id": "seg_med_standard", "name": "Segmento Medio Standard", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "straight", "length": 0.22, "radius": 0.065},
    },
    "seg_med_tapered": {
        "id": "seg_med_tapered", "name": "Segmento Medio Rastremato", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "tapered", "length": 0.24, "radius": 0.075},
    },
    "seg_long_thin": {
        "id": "seg_long_thin", "name": "Segmento Lungo Sottile", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "straight", "length": 0.32, "radius": 0.04},
    },
    "seg_long_armored": {
        "id": "seg_long_armored", "name": "Segmento Lungo Corazzato", "slot": "limb_segment", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "armored", "length": 0.30, "radius": 0.09},
    },

    # -- Giunture ---------------------------------------------------------
    "joint_hinge_small": {
        "id": "joint_hinge_small", "name": "Cerniera Piccola", "slot": "limb_joint", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "hinge", "length": 0.05, "radius": 0.05, "bend_deg": 18},
    },
    "joint_hinge_large": {
        "id": "joint_hinge_large", "name": "Cerniera Grande", "slot": "limb_joint", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "hinge", "length": 0.07, "radius": 0.075, "bend_deg": 32},
    },
    "joint_ball_small": {
        "id": "joint_ball_small", "name": "Snodo Sferico Piccolo", "slot": "limb_joint", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "ball", "length": 0.05, "radius": 0.05, "bend_deg": 22},
    },
    "joint_ball_large": {
        "id": "joint_ball_large", "name": "Snodo Sferico Grande", "slot": "limb_joint", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "ball", "length": 0.07, "radius": 0.08, "bend_deg": 38},
    },

    # -- Ammortizzatori (pistoni): come un segmento ma non piegano l'arto -
    "actuator_small": {
        "id": "actuator_small", "name": "Ammortizzatore Piccolo", "slot": "limb_actuator", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "piston", "length": 0.14, "radius": 0.045, "bend_deg": 0},
    },
    "actuator_standard": {
        "id": "actuator_standard", "name": "Ammortizzatore Standard", "slot": "limb_actuator", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "piston", "length": 0.19, "radius": 0.06, "bend_deg": 0},
    },
    "actuator_heavy": {
        "id": "actuator_heavy", "name": "Ammortizzatore Pesante", "slot": "limb_actuator", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "piston", "length": 0.24, "radius": 0.085, "bend_deg": 0},
    },

    # -- Tendini artificiali: decorativi, si agganciano al segmento --
    # -- strutturale appena prima nella catena, non la allungano ------
    "tendon_single": {
        "id": "tendon_single", "name": "Tendine Singolo", "slot": "limb_tendon", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "single", "length": 0, "radius": 0.012},
    },
    "tendon_twin": {
        "id": "tendon_twin", "name": "Tendini Doppi", "slot": "limb_tendon", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "twin", "length": 0, "radius": 0.01},
    },
    "tendon_braided": {
        "id": "tendon_braided", "name": "Tendine Intrecciato", "slot": "limb_tendon", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "braided", "length": 0, "radius": 0.02},
    },

    # -- Terminali: chiudono la catena (mano/piede/artiglio/universale) --
    "term_hand_round": {
        "id": "term_hand_round", "name": "Mano Tonda", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "round_hand", "length": 0.07, "radius": 0.06, "kind": "hand"},
    },
    "term_hand_grip": {
        "id": "term_hand_grip", "name": "Mano a Presa", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "grip_hand", "length": 0.08, "radius": 0.07, "kind": "hand"},
    },
    "term_claw_pincer": {
        "id": "term_claw_pincer", "name": "Artiglio a Pinza", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "pincer", "length": 0.09, "radius": 0.05, "kind": "claw"},
    },
    "term_foot_flat": {
        "id": "term_foot_flat", "name": "Piede Piatto", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "flat_foot", "length": 0.05, "radius": 0.09, "kind": "foot"},
    },
    "term_foot_pad": {
        "id": "term_foot_pad", "name": "Piede a Cuscinetto", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "pad_foot", "length": 0.05, "radius": 0.08, "kind": "foot"},
    },
    "term_foot_talon": {
        "id": "term_foot_talon", "name": "Piede Artigliato", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "talon", "length": 0.06, "radius": 0.06, "kind": "foot"},
    },
    "term_universal_stub": {
        "id": "term_universal_stub", "name": "Terminale Universale", "slot": "limb_terminal", "cost": 0,
        "stats": {"weight": 0},
        "visual": {"style": "stub", "length": 0.05, "radius": 0.06, "kind": "universal"},
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

SLOTS = (
    "torso", "head",
    "limb_segment", "limb_joint", "limb_actuator", "limb_tendon", "limb_terminal",
    "weapon", "armor", "core",
)

# Slot che possono comparire in una catena arto (ordine libero, tranne il
# terminale che deve stare sempre e solo in fondo).
LIMB_CHAIN_SLOTS = ("limb_segment", "limb_joint", "limb_actuator", "limb_tendon")
LIMB_STRUCTURAL_SLOTS = ("limb_segment", "limb_joint", "limb_actuator")  # contano ai fini della lunghezza minima
MIN_CHAIN_LEN = 2      # almeno 1 pezzo strutturale + 1 terminale
MAX_CHAIN_LEN = 8       # tetto di buon senso per non degenerare in 3D

ARM_TERMINAL_KINDS = ("hand", "claw", "universal")
LEG_TERMINAL_KINDS = ("foot", "universal")

# torso/head/braccia/gambe sono per ora SOLO estetici (nessuna 'stats' che
# concorra al calcolo sotto): la personalizzazione umanoide serve a dare
# struttura/dimensione diversa al robot mentre decidiamo con calma come
# le stat di combattimento dovranno dipendere da questi pezzi.
# Fino ad allora ogni robot umanoide parte dalle stesse stat di base.
BASE_MAX_HP = 130
BASE_SPEED = 3.0
BASE_TURN_RATE = 2.8
BASE_WEAPON_SLOTS = 2

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

def validate_limb_chain(chain_ids: list, limb_kind: str) -> list:
    """Valida una catena di componenti per UN arto (braccio o gamba).
    limb_kind: 'arm' o 'leg', determina quali terminali sono ammessi.
    Ritorna la lista di pezzi risolti (nello stesso ordine). Solleva
    GarageError con messaggio parlante al primo problema."""
    label = "braccio" if limb_kind == "arm" else "gamba"

    if not chain_ids or not isinstance(chain_ids, list):
        raise GarageError(f"Manca la catena di componenti per un {label}.")
    if len(chain_ids) < MIN_CHAIN_LEN:
        raise GarageError(
            f"Catena troppo corta per un {label}: serve almeno 1 pezzo strutturale "
            f"(segmento/giuntura/ammortizzatore) + 1 terminale."
        )
    if len(chain_ids) > MAX_CHAIN_LEN:
        raise GarageError(f"Catena troppo lunga per un {label}: massimo {MAX_CHAIN_LEN} pezzi.")

    pieces = [get_piece(pid) for pid in chain_ids]

    for i, p in enumerate(pieces):
        is_last = (i == len(pieces) - 1)
        if is_last:
            if p["slot"] != "limb_terminal":
                raise GarageError(f"L'ultimo pezzo di un {label} deve essere un terminale.")
            allowed_kinds = ARM_TERMINAL_KINDS if limb_kind == "arm" else LEG_TERMINAL_KINDS
            if p["visual"]["kind"] not in allowed_kinds:
                raise GarageError(
                    f"'{p['name']}' non puo' chiudere un {label} "
                    f"(tipo terminale non compatibile)."
                )
        else:
            if p["slot"] not in LIMB_CHAIN_SLOTS:
                raise GarageError(
                    f"'{p['name']}' non puo' stare nel mezzo di un {label}: "
                    f"solo l'ultimo pezzo puo' essere un terminale."
                )

    structural_count = sum(1 for p in pieces if p["slot"] in LIMB_STRUCTURAL_SLOTS)
    if structural_count < 1:
        raise GarageError(
            f"Un {label} ha bisogno di almeno un segmento, giuntura o ammortizzatore "
            f"oltre al terminale (i tendini da soli sono solo decorativi)."
        )

    return pieces


def validate_loadout(loadout: dict) -> dict:
    """Valida un loadout (dict con torso_id/head_id, le 4 catene arto
    arm_left/arm_right/leg_left/leg_right, weapon_ids/armor_id/core_id).
    Solleva GarageError con messaggio parlante al primo problema trovato.
    Ritorna i pezzi risolti (dict di oggetti pezzo / liste di pezzi)."""

    torso_id = loadout.get("torso_id")
    head_id = loadout.get("head_id")
    weapon_ids = loadout.get("weapon_ids") or []
    armor_id = loadout.get("armor_id")
    core_id = loadout.get("core_id")

    if not torso_id:
        raise GarageError("Manca il busto.")
    if not head_id:
        raise GarageError("Manca la testa.")
    if not weapon_ids:
        raise GarageError("Serve almeno un'arma.")
    if not isinstance(weapon_ids, list):
        raise GarageError("weapon_ids deve essere una lista.")

    torso = get_piece(torso_id)
    if torso["slot"] != "torso":
        raise GarageError(f"'{torso_id}' non e' un busto.")

    head = get_piece(head_id)
    if head["slot"] != "head":
        raise GarageError(f"'{head_id}' non e' una testa.")

    arm_left = validate_limb_chain(loadout.get("arm_left"), "arm")
    arm_right = validate_limb_chain(loadout.get("arm_right"), "arm")
    leg_left = validate_limb_chain(loadout.get("leg_left"), "leg")
    leg_right = validate_limb_chain(loadout.get("leg_right"), "leg")

    weapons = []
    for wid in weapon_ids:
        w = get_piece(wid)
        if w["slot"] != "weapon":
            raise GarageError(f"'{wid}' non e' un'arma.")
        weapons.append(w)

    if len(weapons) > BASE_WEAPON_SLOTS:
        raise GarageError(
            f"Troppe armi: al momento sono disponibili {BASE_WEAPON_SLOTS} slot, "
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
        "torso": torso, "head": head,
        "arm_left": arm_left, "arm_right": arm_right,
        "leg_left": leg_left, "leg_right": leg_right,
        "weapons": weapons, "armor": armor, "core": core,
    }


def compute_derived_stats(resolved: dict) -> dict:
    """A partire dai pezzi risolti (output di validate_loadout), calcola
    le statistiche finali del robot pronte per l'arena.

    NOTA: torso/head/braccia/gambe sono per ora solo estetici (vedi
    commento su SLOTS) quindi max_hp/speed/turn_rate partono da una base
    fissa uguale per tutte le combinazioni umanoidi. Solo armi/armatura/
    core influenzano le stat, esattamente come prima."""
    weapons = resolved["weapons"]
    armor = resolved["armor"]
    core = resolved["core"]

    total_weight = (
        sum(w["stats"]["weight"] for w in weapons)
        + (armor["stats"]["weight"] if armor else 0)
    )

    effective_speed = max(
        MIN_EFFECTIVE_SPEED,
        BASE_SPEED - WEIGHT_SPEED_FACTOR * total_weight,
    )

    return {
        "max_hp": BASE_MAX_HP,
        "weight": total_weight,
        "speed": round(effective_speed, 3),
        "turn_rate": BASE_TURN_RATE,
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
    torso_id     TEXT NOT NULL,
    head_id      TEXT NOT NULL,
    arm_left     TEXT NOT NULL,   -- JSON array ordinata di piece id (catena)
    arm_right    TEXT NOT NULL,   -- JSON array ordinata di piece id (catena)
    leg_left     TEXT NOT NULL,   -- JSON array ordinata di piece id (catena)
    leg_right    TEXT NOT NULL,   -- JSON array ordinata di piece id (catena)
    weapon_ids   TEXT NOT NULL,   -- JSON array di piece id
    armor_id     TEXT,
    core_id      TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_robots_owner ON robots(owner_id);
"""
# NOTA MIGRAZIONE: questo schema sostituisce le vecchie colonne
# arms_id/legs_id (un pezzo unico) con 4 catene libere arm_left/arm_right/
# leg_left/leg_right (liste di piece id in JSON). Su un DB gia' esistente,
# CREATE TABLE IF NOT EXISTS non altera la vecchia tabella: cancella/
# rinomina il vecchio file .db prima di ripartire, i robot salvati in
# precedenza non sono comunque compatibili con il nuovo formato loadout.


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
        "torso_id": row["torso_id"],
        "head_id": row["head_id"],
        "arm_left": json.loads(row["arm_left"]),
        "arm_right": json.loads(row["arm_right"]),
        "leg_left": json.loads(row["leg_left"]),
        "leg_right": json.loads(row["leg_right"]),
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
    arm_left_json = json.dumps(loadout["arm_left"])
    arm_right_json = json.dumps(loadout["arm_right"])
    leg_left_json = json.dumps(loadout["leg_left"])
    leg_right_json = json.dumps(loadout["leg_right"])

    if robot_id is None:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM robots WHERE owner_id = ?", (owner_id,)
        ).fetchone()["n"]
        if count >= MAX_ROBOTS_PER_ACCOUNT:
            raise GarageError(f"Hai gia' {MAX_ROBOTS_PER_ACCOUNT} robot salvati (limite massimo).")

        cur = conn.execute(
            "INSERT INTO robots "
            "(owner_id, name, torso_id, head_id, arm_left, arm_right, leg_left, leg_right, "
            " weapon_ids, armor_id, core_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, name, loadout["torso_id"], loadout["head_id"],
             arm_left_json, arm_right_json, leg_left_json, leg_right_json,
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
            "UPDATE robots SET name=?, torso_id=?, head_id=?, arm_left=?, arm_right=?, "
            "leg_left=?, leg_right=?, weapon_ids=?, armor_id=?, core_id=?, updated_at=? WHERE id=?",
            (name, loadout["torso_id"], loadout["head_id"],
             arm_left_json, arm_right_json, leg_left_json, leg_right_json,
             weapon_ids_json, loadout.get("armor_id"), loadout.get("core_id"), now, robot_id),
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
            "torso_id": "torso_standard",
            "head_id": "head_visor",
            "arm_left": ["seg_med_standard", "joint_hinge_small", "seg_med_thin", "term_hand_round"],
            "arm_right": ["actuator_standard", "seg_med_tapered", "term_hand_grip"],
            "leg_left": ["seg_long_thin", "joint_hinge_large", "seg_med_standard", "term_foot_flat"],
            "leg_right": ["seg_long_thin", "joint_hinge_large", "seg_med_standard", "term_foot_flat"],
            "weapon_ids": ["weap_laser", "weap_mg"],
            "armor_id": "armor_light",
            "core_id": "core_regen",
        }
        robot = save_robot(conn, owner_id, "Falco Rosso", loadout)
        print("Robot creato:", json.dumps(robot, indent=2, ensure_ascii=False))
        assert robot["derived_stats"]["max_hp"] == BASE_MAX_HP

        # Troppe armi rispetto agli slot disponibili -> deve fallire
        bad_loadout = dict(loadout, weapon_ids=["weap_laser", "weap_mg", "weap_missiles"])
        try:
            save_robot(conn, owner_id, "Errore", bad_loadout)
            print("ERRORE: doveva fallire (troppe armi)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Pezzo inesistente -> deve fallire
        try:
            save_robot(conn, owner_id, "Errore2", dict(loadout, torso_id="torso_fantasma"))
            print("ERRORE: doveva fallire (pezzo inesistente)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Catena arto senza terminale in fondo -> deve fallire
        try:
            save_robot(conn, owner_id, "Errore3",
                       dict(loadout, arm_left=["seg_med_standard", "seg_med_thin"]))
            print("ERRORE: doveva fallire (arto senza terminale)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Terminale piede in fondo a un braccio -> deve fallire (kind incompatibile)
        try:
            save_robot(conn, owner_id, "Errore4",
                       dict(loadout, arm_left=["seg_med_standard", "term_foot_flat"]))
            print("ERRORE: doveva fallire (piede su un braccio)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Solo tendine + terminale, nessun pezzo strutturale -> deve fallire
        try:
            save_robot(conn, owner_id, "Errore5",
                       dict(loadout, arm_left=["tendon_single", "term_hand_round"]))
            print("ERRORE: doveva fallire (arto senza struttura)")
        except GarageError as e:
            print("OK, rifiutato:", e)

        # Catena troppo lunga -> deve fallire
        try:
            save_robot(conn, owner_id, "Errore6",
                       dict(loadout, arm_left=["seg_short_thin"] * 8 + ["term_hand_round"]))
            print("ERRORE: doveva fallire (catena troppo lunga)")
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
