"""
Pac-Man Arena 1vAll - server WebSocket

Questo file contiene la STESSA IDENTICA logica di gioco di server.py
(lobby, countdown, movimento, collisioni, condizioni di vittoria, timer,
codici stanza): nessuna regola e' stata cambiata rispetto a server.py, a
parte la rimozione del killer (vedi sotto).
L'UNICA differenza rispetto a server.py e' il trasporto di rete:
- server.py parla socket TCP grezzi (righe di JSON) pensati per il
  client da terminale (client.py), incompatibili con un browser.
- questo file parla il protocollo WebSocket vero, quello che il browser
  usa con `new WebSocket(...)`, cosi' il client web (index.html) puo'
  collegarsi davvero.

In piu', questo server invia la mappa scelta a caso tra le 10 disponibili
(maze/maze_w/maze_h/maze_name/theme) alla creazione della stanza e di nuovo,
con una nuova mappa casuale, ad ogni inizio round: il client da terminale la
legge in locale da common.py, il browser invece deve riceverla via rete.

Non esiste piu' un "killer" scelto a rotazione tra i giocatori: dopo il
countdown iniziale il round parte direttamente, e i giocatori si eliminano
a vicenda solo tramite i bonus ottenuti raggiungendo soglie di punti
(laser, mine, super assassino a 300 punti).

Avvio locale:      python3 main.py [porta]   (default 8765)
In hosting (Render/Railway/...): la porta arriva dalla variabile
d'ambiente PORT, impostata automaticamente dalla piattaforma.
"""
import asyncio
import json
import os
import pathlib
import random
import socket
import sys
import uuid
from collections import deque

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from common import (
    TICK_DT, STATE_BROADCAST_EVERY_N_TICKS, COUNTDOWN_SECONDS, ROUND_SECONDS,
    MAX_PLAYERS, MIN_PLAYERS, NORMAL_SPEED, ASSASSIN_SPEED_MULT,
    COLORS, CHARACTERS, DIRECTIONS, is_wall, ROOM_CODE_CHARS, SECONDARY_ONLY_COLORS,
    pick_random_maze, choose_power_pellet_cells, bfs_path,
    BONUS_THRESHOLDS, GHOST_SECONDS,
    PELLET_POINTS, POWER_PELLET_POINTS, POWER_PELLET_COUNT,
    PELLET_RESPAWN_SECONDS, MEGA_PELLET_POINTS, MEGA_PELLET_INTERVAL_SECONDS, SUPER_ASSASSIN_THRESHOLD,
    SUPER_ASSASSIN_DURATION_SECONDS, LASER_RANGE_CELLS,
    SPAWN_PROTECT_SECONDS, MIN_SPAWN_DISTANCE, LASER_INTERVAL_SECONDS, LASER_FIRST_DELAY_SECONDS,
    LASER_PROJECTILE_SPEED, LASER_BOUNCE_DISTANCE, MINES_COUNT, SUPERBOMB_COUNT,
    PORTAL_COOLDOWN_SECONDS, PORTAL_ON_SECONDS, PORTAL_OFF_SECONDS,
    MISSILE_SPEED_MULT, MISSILES_COUNT, MISSILE_RETARGET_SECONDS, MISSILE_LOCK_DISTANCE,
    TRAP_THRESHOLD, TRAP_DURATION_SECONDS, TRAP_RANGE, TRAP_MAX_USES,
    TURRET_THRESHOLD, TURRET_FIRE_INTERVAL_SECONDS,
    TURRET_RANGE_CELLS, KILL_STEAL_FRACTION,
    ARMOR_THRESHOLD, ARMOR_DURATION_SECONDS,
    LIGHTNING_THRESHOLD,
    PET_THRESHOLD, PET_RANGE_CELLS,
    PET_SPEED_MULT, PET_RETARGET_SECONDS, PET_STAY_RANGE,
    ROBOT_THRESHOLD, ROBOT_FIRE_INTERVAL_SECONDS, ROBOT_SPEED_MULT,
    ROBOT_WANDER_RETARGET_SECONDS, ROBOT_LEVELUP_DISPLAY_SECONDS,
    MORTAR_THRESHOLD, MORTAR_RANGE_CELLS, MORTAR_FIRE_INTERVAL_SECONDS,
    MORTAR_FLIGHT_SECONDS_PER_CELL, MORTAR_BLAST_RADIUS_CELLS,
    POISON_DURATION_SECONDS, POISON_TICK_SECONDS, POISON_RADIUS_CELLS,
    SUPERBOMB_THRESHOLD, SUPERBOMB_FUSE_SECONDS, SUPERBOMB_RADIUS_CELLS,
    BALLOON_THRESHOLD, BALLOON_SPEED, BALLOON_BOMB_INTERVAL_SECONDS,
    BALLOON_BOMB_RADIUS_CELLS, BALLOON_RETARGET_EPSILON,
    BLOB_THRESHOLD,
    BLOB_ALIVE_THRESHOLD, BLOB_ALIVE_SPEED_MULT,
    BLOB_POISON_DURATION_SECONDS, BLOB_EAT_RANGE_CELLS,
    SPIKE_WALL_THRESHOLD, SPIKE_WALL_HIT_RANGE,
    TESLA_THRESHOLD, TESLA_FIRE_INTERVAL_SECONDS, TESLA_RANGE_CELLS,
    TERRITORY_TRAP_THRESHOLD, TERRITORY_TILES_REQUIRED,
    BUSH_THRESHOLD, BUSH_GROW_INTERVAL_SECONDS, BUSH_HIT_RANGE, BUSH_MAX_EXPANSIONS,
    LIVES_EVERY_POINTS, LIVES_EVERY_AMOUNT,
    MUSHROOM_THRESHOLD, MUSHROOM_BLAST_RADIUS_CELLS,
    MUSHROOM_POISON_DURATION_SECONDS, MUSHROOM_VISIBILITY_RANGE,
    MUSHROOM_CLOUD_SECONDS,
    RTT_PING_INTERVAL_SECONDS, RTT_DEFAULT_SECONDS,
    REWIND_MAX_SECONDS, REWIND_HISTORY_SECONDS,
)
import math
import time

# Direzione opposta di ciascuna direzione: serve per l'inversione di marcia
# istantanea a meta' cella (stile Pac-Man originale, vedi update_movement).
OPPOSITE_DIR = {"up": "down", "down": "up", "left": "right", "right": "left"}

MAX_PLAYER_COLORS = 2  # colore primario + colore di dettaglio (opzionale)

DEFAULT_PORT = 8765

# Se index.html si trova nella stessa cartella di questo file, il server lo
# serve direttamente: cosi' un solo processo/hosting basta per tutto,
# niente Netlify separato. Pura comodita' di distribuzione, non tocca la
# logica di gioco: il client rimane identico, cambia solo da dove arriva.
CLIENT_HTML_PATH = pathlib.Path(__file__).parent / "index.html"
try:
    CLIENT_HTML = CLIENT_HTML_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    CLIENT_HTML = None

ROOMS = {}  # code -> Room


def encode_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Player:
    def __init__(self, pid, name, ws):
        self.id = pid
        self.name = name
        self.ws = ws
        # Fino a 2 colori: colors[0] = colore primario (corpo, univoco tra
        # i giocatori), colors[1] = colore di dettaglio opzionale (contorno,
        # denti, occhio a seconda del personaggio). Lista vuota = nessun
        # colore scelto ancora.
        self.colors = []
        self.character = "classic"
        self.host = False
        self.x = 0
        self.y = 0
        self.direction = None
        # Direzione "richiesta" dal giocatore ma non ancora applicabile (es.
        # muro nella cella successiva): viene tenuta in memoria e applicata
        # in automatico nel primo tick in cui diventa possibile, esattamente
        # come nel Pac-Man originale. Senza questa coda, premere una
        # direzione un istante troppo presto la faceva perdere del tutto,
        # dando la sensazione di comandi "poco precisi".
        self.next_direction = None
        self.move_accum = 0.0
        self.alive = True
        self.connected = True
        # ---- sistema punti / bonus (azzerato ad ogni round) ----
        self.points = 0
        self.lives = 3                 # si parte con 3 vite; a 50 punti diventa 4, ecc.: un'eliminazione non ti fa uscire finche' hai vite, fa respawnare
        self.claimed = set()           # soglie bonus gia' riscattate in questo round
        self.ghost_left = 0.0          # (bonus fantasma rimosso dal gioco: resta sempre a 0)
        # A SUPER_ASSASSIN_THRESHOLD punti (300): sblocca la modalita' ninja
        # (tasto "2"), attivabile a comando finche' il round e' in corso.
        # Una volta attivata: invisibile agli altri, piu' veloce (1.1x
        # rispetto a 1.0 dei giocatori normali) e uccide chiunque al solo
        # contatto. Dura solo SUPER_ASSASSIN_DURATION_SECONDS (10s) poi si
        # disattiva da sola (vedi il countdown in update_movement); si
        # disattiva anche prima se il giocatore viene ucciso (vedi
        # kill_player). E' UTILIZZABILE UNA SOLA VOLTA per round (vedi
        # ninja_used): a differenza di prima non si puo' piu' riattivare
        # dopo che e' scaduta o dopo un'eliminazione.
        self.has_ninja = False         # bonus sbloccato (una volta per round)
        self.is_assassin = False       # True mentre la modalita' ninja e' ATTIVA in questo istante
        self.assassin_left = 0.0       # secondi rimanenti da modalita' ninja attiva
        self.ninja_used = False        # True dopo l'unica attivazione consentita per round
        self.prot_left = 0.0           # invulnerabilita' temporanea dopo un respawn
        self.has_laser = False         # bonus 150 punti: laser frontale (arma principale). Resta sbloccato per TUTTA la partita una volta ottenuto (non scade piu'); spara solo quando un nemico e' entro LASER_RANGE_CELLS caselle (vedi update_lasers)
        self.laser_cd = 0.0            # countdown al prossimo colpo di laser
        self.has_bounce = False        # (non piu' assegnato da alcun bonus, resta sempre False)
        self.has_mines = False         # bonus 200 punti: puo' sganciare mine
        self.mines_left = 0            # mine ancora disponibili in questo round
        self.portal_cd = 0.0           # anti ping-pong dopo un teletrasporto
        # Ultima direzione di marcia nota: e' il "lato frontale" da cui parte
        # il laser anche se in questo istante si e' fermi contro un muro.
        self.facing = "right"
        # ---- compensazione latenza per le svolte (vedi Room._rewind_move) ----
        # Storico (timestamp, x, y, move_accum, direction) registrato ad ogni
        # tick: serve a "tornare indietro" fino al momento reale in cui un
        # tasto direzione e' stato premuto, cosi' la svolta puo' essere
        # applicata li' invece che al momento (in ritardo) in cui il
        # messaggio arriva sul server. Lunghezza limitata (REWIND_HISTORY_SECONDS)
        # per non accumulare memoria round dopo round.
        self.pos_history = deque()
        # Stima del round-trip-time di questo giocatore (aggiornata dai
        # pacchetti rtt_pong, vedi Room.update_rtt_pings): usata per sapere
        # di quanto "tornare indietro" quando arriva un comando di svolta.
        self.rtt = RTT_DEFAULT_SECONDS
        self.rtt_ping_cd = 0.0        # countdown al prossimo ping di misura RTT
        self.rtt_ping_sent_at = None  # timestamp dell'ultimo ping in attesa di risposta
        # ---- bonus 400 punti: missile guidato (tasto "2") ----
        self.has_missile = False
        self.missiles_left = 0
        # ---- bonus 500 punti: trappola (tasto "3") ----
        self.has_trap = False          # bonus sbloccato (una volta per round)
        self.trap_target = None        # id della vittima che QUESTO giocatore ha intrappolato
        self.trapped_left = 0.0        # se > 0, QUESTO giocatore e' intrappolato (immobile)
        self.trapped_by = None         # id di chi lo ha intrappolato (per pulizia alla scadenza/morte)
        self.trap_uses_left = 0        # quante volte puo' ancora INNESCARE la trappola (max TRAP_MAX_USES)
        # ---- bonus 600 punti: torretta automatica permanente (tasto "5") ----
        self.has_turret = False        # bonus sbloccato (una volta per round)
        self.turret_placed = False     # True dopo il piazzamento: il tasto "5" e' utilizzabile una sola volta
        # ---- bonus 700 punti: corazza laser (tasto "6") ----
        # Allo sblocco NON si attiva da sola: si attiva a comando col tasto
        # "6" (vedi try_activate_armor), UNA SOLA VOLTA per round, e dura
        # ARMOR_DURATION_SECONDS. Mentre e' attiva respinge ogni proiettile
        # che la colpisce, distrugge le torrette toccate e uccide chiunque
        # tocchi (a differenza del ninja resta pero' visibile a tutti).
        self.has_armor = False         # bonus sbloccato (una volta per round)
        self.armor_active = False      # True mentre la corazza e' ATTIVA in questo istante
        self.armor_left = 0.0          # secondi rimanenti di corazza attiva
        self.armor_used = False        # True dopo l'unica attivazione consentita per round
        # ---- bonus 800 punti: fulmine (tasto "7") ----
        # Allo sblocco NON scatta nulla in automatico: si attiva a comando
        # col tasto "7" (vedi try_activate_lightning), UNA SOLA VOLTA per
        # round. Colpisce all'istante tutti gli avversari vivi sulla mappa.
        self.has_lightning = False     # bonus sbloccato (una volta per round)
        self.lightning_used = False    # True dopo l'unica attivazione consentita per round
        # ---- bonus 900 punti: pet fedele permanente (tasto "8") ----
        # Allo sblocco NON scatta nulla in automatico: si evoca a comando col
        # tasto "8" (vedi try_summon_pet), UNA SOLA VOLTA per round, come la
        # torretta. Il pet vero e proprio vive in self.pets (lista della
        # Room), non qui: qui si tiene solo lo stato dello sblocco/utilizzo.
        self.has_pet = False           # bonus sbloccato (una volta per round)
        self.pet_summoned = False      # True dopo l'evocazione: il tasto "8" e' utilizzabile una sola volta
        # ---- bonus 1000 punti: evoluzione della torretta in robot (tasto "9") ----
        # Allo sblocco NON scatta nulla in automatico: si evolve a comando col
        # tasto "9" (vedi try_evolve_turret), UNA SOLA VOLTA per round, e solo
        # se la torretta (bonus 600 punti) e' ancora viva sulla mappa. Lo
        # stato vero e proprio dell'evoluzione (evolved/level_up_left/
        # wander_path/...) vive dentro il dict della torretta in
        # self.turrets, non qui: qui si tiene solo lo stato dello
        # sblocco/utilizzo, come per gli altri bonus a comando.
        self.has_robot = False         # bonus sbloccato (una volta per round)
        self.robot_used = False        # True dopo l'evoluzione: il tasto "9" e' utilizzabile una sola volta
        # ---- bonus 1200 punti: mortaio (tasto "1") ----
        # Allo sblocco NON scatta nulla in automatico: si schiera a comando
        # col tasto "1" (vedi try_place_mortar), UNA SOLA VOLTA per round,
        # come la torretta. Il mortaio vero e proprio vive in self.mortars
        # (lista della Room), non qui: qui si tiene solo lo stato dello
        # sblocco/utilizzo, come per gli altri bonus a comando.
        self.has_mortar = False        # bonus sbloccato (una volta per round)
        self.mortar_placed = False     # True dopo lo schieramento: il tasto "1" e' utilizzabile una sola volta
        # ---- bonus 1400 punti: bombolone ad area (tasto "1", DOPO il mortaio) ----
        # Allo sblocco NON scatta nulla in automatico: si piazza a comando
        # riusando il tasto "1" (vedi try_place_superbomb), UNA SOLA VOLTA
        # per round, ma solo DOPO che il mortaio (bonus 1200 punti) e' gia'
        # stato schierato. Il bombolone vero e proprio vive in
        # self.superbombs (lista della Room), non qui: qui si tiene solo lo
        # stato dello sblocco/utilizzo, come per gli altri bonus a comando.
        self.has_superbomb = False     # bonus sbloccato (una volta per round)
        self.superbomb_left = 0        # bomboloni ancora disponibili in questo round (SUPERBOMB_COUNT allo sblocco)

        # ---- bonus 1600 punti: mongolfiera vagante (tasto "1", DOPO il bombolone) ----
        self.has_balloon = False       # bonus sbloccato (una volta per round)
        self.balloon_launched = False  # True dopo il lancio: il tasto "1" (dopo mortaio+bombolone) e' utilizzabile una sola volta

        # ---- bonus 1800 punti: blob gelatinoso (tasto "1", DOPO la mongolfiera) ----
        self.has_blob = False          # bonus sbloccato (una volta per round)
        self.blob_placed = False       # True dopo il piazzamento: il tasto "1" (dopo mortaio+bombolone+mongolfiera) e' utilizzabile una sola volta

        # ---- bonus 2000 punti: blob VIVO/vagante (tasto "1", DOPO il blob fermo) ----
        # Allo sblocco NON scatta nulla in automatico: si risveglia a comando
        # riusando ancora il tasto "1" (vedi try_animate_blob), UNA SOLA
        # VOLTA per round, ma solo DOPO che il blob (bonus 1800 punti) e'
        # gia' stato piazzato. Lo stato vero e proprio del movimento (alive/
        # wander_path/...) vive dentro il dict del blob in self.blobs, non
        # qui: qui si tiene solo lo stato dello sblocco/utilizzo, come per
        # gli altri bonus a comando.
        self.has_blob_alive = False    # bonus sbloccato (una volta per round)
        self.blob_alive_used = False   # True dopo il risveglio: il tasto "1" (dopo mortaio+bombolone+mongolfiera+blob) e' utilizzabile una sola volta

        # ---- bonus 2200 punti: muro di spunzoni (tasto "1", DOPO il risveglio del blob) ----
        # Sbloccato a 2200 punti, si piazza a comando RIUSANDO ancora il
        # tasto "1" (vedi try_place_spike_wall), UNA SOLA VOLTA per round,
        # ma solo DOPO aver esaurito tutta la catena precedente del tasto
        # "1". Il muro vero e proprio (posizione, durata residua) vive nel
        # dict dentro self.spike_walls della Room, non qui.
        self.has_spike_wall = False    # bonus sbloccato (una volta per round)
        self.spike_wall_placed = False # True dopo il piazzamento: il tasto "1" (a fine catena) e' utilizzabile una sola volta

        # ---- bonus 2400 punti: Tesla laser (tasto "1", DOPO il muro di spunzoni) ----
        # Ultimo gradino della catena del tasto "1". Si piazza a comando
        # RIUSANDO ancora il tasto "1" (vedi try_place_tesla), UNA SOLA
        # VOLTA per round, ma solo DOPO aver esaurito tutta la catena
        # precedente (mine, mortaio, bombolone, mongolfiera, blob,
        # risveglio del blob, muro di spunzoni). La Tesla vera e propria
        # (posizione, cadenza di fuoco) vive nel dict dentro self.teslas
        # della Room, non qui: qui si tiene solo lo stato dello
        # sblocco/utilizzo, come per gli altri bonus a comando.
        self.has_tesla = False         # bonus sbloccato (una volta per round)
        self.tesla_placed = False      # True dopo il piazzamento: il tasto "1" (dopo il muro di spunzoni) e' utilizzabile una sola volta

        # ---- bonus 2600 punti: trappola territoriale a spunzoni (tasto "1", DOPO la Tesla) ----
        # Nuovo ultimo gradino della catena del tasto "1". La PRIMA
        # pressione (dopo la Tesla) avvia la selezione delle caselle
        # (vedi try_use_territory_trap/update_territory_marking), la
        # SECONDA le trasforma in spunzoni letali (vedi
        # trigger_territory_trap). Le celle marcate (territory_tiles)
        # NON vengono mai incluse nello stato pubblico: restano visibili
        # SOLO al proprietario tramite eventi privati dedicati.
        self.has_territory_trap = False  # bonus sbloccato
        self.territory_marking = False   # True durante la fase di selezione
        self.territory_ready = False     # True quando la selezione e' completa, in attesa della 2a pressione
        self.territory_used = False      # True dopo l'attivazione: tasto "1" (a fine catena) esaurito per sempre
        self.territory_tiles = set()     # celle (x, y) marcate finora, private

        # ---- bonus 2800 punti: arbusto spinoso (tasto "1", DOPO la trappola territoriale) ----
        # Sbloccato a BUSH_THRESHOLD punti, piazzabile UNA SOLA VOLTA per
        # round col tasto "1" a fine catena (vedi try_place_bush).
        # L'arbusto vero e proprio (celle occupate, timer di crescita) vive
        # come dict dentro self.bushes della Room, non qui.
        self.has_bush = False     # bonus sbloccato (una volta per round)
        self.bush_placed = False  # True dopo il piazzamento: il tasto "1" (a fine catena) e' utilizzabile una sola volta

        # ---- bonus 3000 punti: fungo atomico (tasto "1", DOPO l'arbusto spinoso) ----
        self.has_mushroom = False     # bonus sbloccato (una volta per round)
        self.mushroom_placed = False  # True dopo il piazzamento

        # ---- vite extra ricorrenti: ogni LIVES_EVERY_POINTS punti +LIVES_EVERY_AMOUNT vite ----
        # Prossimo traguardo da riscattare (1600, poi 3200, 4800, ...).
        self.next_lives_milestone = LIVES_EVERY_POINTS
        # Uccisioni fatte in questo round: ogni 2 kill si guadagna una vita extra.
        self.kills = 0

    def to_public(self):
        # Posizione "continua": la griglia interna (self.x/self.y, interi)
        # resta l'autorita' per collisioni/regole, ma al client mandiamo
        # anche l'avanzamento reale dentro la cella corrente (move_accum),
        # che il server gia' calcola ad ogni tick. Cosi' il client puo'
        # mostrare la posizione VERA in tempo reale invece di scoprire "e'
        # arrivato nella cella successiva" solo a cella completata e dover
        # inscenare un'animazione di recupero: e' questo che rendeva il
        # movimento degli altri giocatori percettibilmente in ritardo.
        dx, dy = DIRECTIONS.get(self.direction, (0, 0)) if self.direction else (0, 0)
        fx = self.x + dx * self.move_accum
        fy = self.y + dy * self.move_accum
        return {
            "id": self.id, "name": self.name, "colors": self.colors,
            "character": self.character,
            "host": self.host, "x": round(fx, 4), "y": round(fy, 4),
            "direction": self.direction,
            "alive": self.alive,
            # ---- nuovi campi per HUD/rendering ----
            "points": self.points, "lives": self.lives,
            "kills": self.kills,
            "ghost": self.ghost_left > 0,
            "assassin": self.is_assassin,
            "assassin_left": round(self.assassin_left, 1) if self.is_assassin else 0,
            "ninja": self.has_ninja,
            "ninja_used": self.ninja_used,
            "prot": self.prot_left > 0,
            "laser": self.has_laser,
            "bounce": self.has_bounce,
            "mines": self.has_mines,
            "mines_left": self.mines_left,
            "missile": self.has_missile,
            "missiles_left": self.missiles_left,
            "trap": self.has_trap,
            "trap_uses_left": self.trap_uses_left,
            "trapped": self.trapped_left > 0,
            "trapped_left": round(self.trapped_left, 1) if self.trapped_left > 0 else 0,
            "turret": self.has_turret,
            "turret_placed": self.turret_placed,
            "armor": self.has_armor,
            "armor_on": self.armor_active,
            "armor_left": round(self.armor_left, 1) if self.armor_active else 0,
            "armor_used": self.armor_used,
            "lightning": self.has_lightning,
            "lightning_used": self.lightning_used,
            "pet": self.has_pet,
            "pet_summoned": self.pet_summoned,
            "robot": self.has_robot,
            "robot_used": self.robot_used,
            "mortar": self.has_mortar,
            "mortar_placed": self.mortar_placed,
            "superbomb": self.has_superbomb,
            "superbomb_left": self.superbomb_left,
            "superbomb_placed": self.superbomb_left <= 0 and self.has_superbomb,
            "balloon": self.has_balloon,
            "balloon_launched": self.balloon_launched,
            "blob": self.has_blob,
            "blob_placed": self.blob_placed,
            "blob_alive_bonus": self.has_blob_alive,
            "blob_alive_used": self.blob_alive_used,
            "spike_wall": self.has_spike_wall,
            "spike_wall_placed": self.spike_wall_placed,
            "tesla": self.has_tesla,
            "tesla_placed": self.tesla_placed,
            "territory_trap": self.has_territory_trap,
            "territory_marking": self.territory_marking,
            "territory_ready": self.territory_ready,
            "territory_used": self.territory_used,
            "bush": self.has_bush,
            "bush_placed": self.bush_placed,
            "mushroom": self.has_mushroom,
            "mushroom_placed": self.mushroom_placed,
        }


class Room:
    def __init__(self, code):
        self.code = code
        self.players: dict[str, Player] = {}
        self.state = "LOBBY"  # LOBBY, COUNTDOWN, PLAYING, ENDED
        self.countdown_left = 0.0
        self.timer_left = 0.0
        self.loop_task = None
        self.last_result = None
        # Contatore dei tick di gioco, usato per limitare la frequenza di
        # invio dello stato COMPLETO (vedi STATE_BROADCAST_EVERY_N_TICKS in
        # common.py): il movimento/la logica restano a TICK_HZ pieno, ma lo
        # snapshot pesante (con tutti gli oggetti permanenti: torrette,
        # mortai, pet, arbusti, Tesla, ecc.) viene ricostruito e inviato
        # meno spesso per non far crescere il costo di CPU/rete col passare
        # del round, quando i giocatori sbloccano ed usano piu' gadget.
        self._snapshot_tick = 0
        # La vittoria e' "ultimo giocatore vivo": per decidere il titolo di
        # fine round teniamo traccia di com'e' avvenuta l'ultima eliminazione.
        self.last_kill = None
        # Eventi una-tantum (uccisioni, bonus, laser, teletrasporti, pallini
        # mangiati) accumulati durante il tick e trasmessi subito dopo: sono
        # cio' che permette al client effetti/suoni precisi senza dover
        # "indovinare" confrontando snapshot consecutivi.
        self.events = []
        # Eventi privati (bonus 2600 punti, trappola territoriale): stessa
        # idea di self.events ma recapitati SOLO al giocatore indicato,
        # mai in broadcast (vedi push_private_event/drain_private_events).
        # Lista di tuple (player_id, evento).
        self.private_events = []
        # Proiettili laser in volo e mine posate sulla mappa: entrambi sono
        # liste di dict semplici (niente classi dedicate, sono pochi campi)
        # azzerate ad ogni nuovo round.
        self.lasers = []
        self.mines = []
        # Torrette automatiche piazzate (bonus 600 punti): permanenti, non
        # vengono mai svuotate durante il round, solo ad ogni nuovo round
        # (vedi assign_spawns/reset_to_lobby).
        self.turrets = []
        # Pet fedeli evocati (bonus 900 punti): permanenti come le torrette,
        # non vengono mai svuotati durante il round, solo ad ogni nuovo
        # round (vedi assign_spawns/reset_to_lobby).
        self.pets = []
        # Bomboloni piazzati (bonus 1400 punti): permanenti fino
        # all'esplosione (SUPERBOMB_FUSE_SECONDS dopo il piazzamento),
        # azzerati ad ogni nuovo round (vedi assign_spawns/reset_to_lobby).
        self.superbombs = []
        # Mortai schierati (bonus 1200 punti): permanenti come le torrette.
        # self.bombs sono le bombe attualmente "in volo" sparate dai mortai,
        # svuotate anch'esse solo ad ogni nuovo round.
        self.mortars = []
        self.bombs = []
        self.poison_zones = []  # nuvole velenose lasciate a terra dagli impatti del mortaio
        # Mongolfiere in volo (bonus 1600 punti): permanenti come mortai e
        # torrette, vagano a caso su tutta la mappa sganciando bombe a
        # intervalli regolari, azzerate ad ogni nuovo round.
        self.balloons = []
        # Blob gelatinosi piazzati (bonus 1800 punti): permanenti come
        # mortai e torrette, immobili, bloccano la cella in cui sono
        # piazzati e mangiano chiunque ci passi sopra. Non si consumano
        # mangiando (a differenza delle mine): l'unico modo per rimuoverli
        # e' colpirli con un laser o un missile guidato (vedi move_lasers/
        # move_missiles). Azzerati ad ogni nuovo round.
        self.blobs = []
        # Muri di spunzoni piazzati (bonus 2200 punti): a differenza degli
        # altri gadget "permanenti" hanno una durata (1 minuto ciascuno,
        # vedi update_spike_walls), dopo la quale si sgretolano da soli.
        # Azzerati ad ogni nuovo round.
        self.spike_walls = []
        # Tesla laser piazzate (bonus 2400 punti): permanenti per tutto il
        # round come torrette/mortai/pet, a differenza dei muri di spunzoni
        # non scadono mai (vedi update_teslas/tesla_zap). Azzerate ad ogni
        # nuovo round.
        self.teslas = []
        # Arbusti spinosi piazzati (bonus 2800 punti): permanenti finche'
        # non vengono eliminati DEL TUTTO (vedi update_bushes), crescono di
        # una casella al minuto per sempre. Azzerati ad ogni nuovo round.
        self.bushes = []
        # Funghi atomici piazzati (bonus 3000 punti): restano a terra come
        # mine in attesa di essere calpestati (vedi update_mushrooms).
        # Azzerati ad ogni nuovo round.
        self.mushrooms = []
        # Mappa corrente della stanza: viene ripescata a caso tra le 10
        # disponibili a OGNI inizio round (vedi run_round), cosi' ogni
        # partita puo' capitare su una mappa diversa per forma/colore/misura.
        self.pick_new_map()

    def pick_new_map(self):
        map_data = pick_random_maze()
        self.maze = map_data["maze"]
        self.maze_w = map_data["w"]
        self.maze_h = map_data["h"]
        self.maze_name = map_data["name"]
        self.spawn_points = map_data["spawn_points"]
        self.theme = map_data["theme"]
        self.compute_portals()
        # 10 celle (una per angolo/estremita' della mappa) con un pallino
        # grosso arancione che vale 10 punti invece di 1.
        self.power_pellets = set(
            choose_power_pellet_cells(self.maze, self.maze_w, self.maze_h, POWER_PELLET_COUNT)
        )
        self.reset_pellets()
        self.reset_pellets()
        # Pallino mega (100 punti): spawna una volta al minuto, sempre
        # nella stessa cella, la piu' vicina al centro esatto della mappa
        # (vedi _nearest_open_cell/update_mega_pellet/eat_mega_pellet).
        # Azzerato e ricalcolato ad ogni nuovo round/mappa.
        self.mega_pellet_spot = self._nearest_open_cell(self.maze_w // 2, self.maze_h // 2)
        self.mega_pellet_cell = None
        self.mega_pellet_timer = MEGA_PELLET_INTERVAL_SECONDS
        # Tutte le celle libere (non muro) della mappa: servono al robot
        # (torretta evoluta, bonus 1000 punti) per scegliere a caso una
        # meta' da raggiungere mentre pattuglia (vedi update_robot_wander).
        # Calcolate una sola volta per mappa, non ad ogni ricalcolo.
        self.free_cells = [
            (x, y)
            for y, row in enumerate(self.maze)
            for x, ch in enumerate(row)
            if ch != "#"
        ]

    def reset_pellets(self):
        """Ricrea l'insieme dei pallini: ogni cella libera della mappa ne
        contiene uno (1 punto, o 10 se e' una delle celle "power" scelte in
        pick_new_map). Il server e' l'autorita' (prima erano solo
        decorativi lato client): cosi' i punti sono uguali per tutti."""
        self.pellets = {
            (x, y)
            for y, row in enumerate(self.maze)
            for x, ch in enumerate(row)
            if ch == "."
        }
        # Cella -> secondi residui prima che un pallino mangiato ricompaia.
        self.pellet_respawns = {}

    def _nearest_open_cell(self, tx, ty):
        """BFS che trova la cella libera raggiungibile piu' vicina a
        (tx, ty): riutilizzata sia dai portali (compute_portals) sia dal
        pallino mega (pick_new_map/mega_pellet_spot), per il caso in cui il
        punto esatto cercato cada su un muro."""
        if self.maze[ty][tx] == ".":
            return (tx, ty)
        seen = {(tx, ty)}
        frontier = deque([(tx, ty)])
        while frontier:
            x, y = frontier.popleft()
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < self.maze_w and 0 <= ny < self.maze_h and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    if self.maze[ny][nx] == ".":
                        return (nx, ny)
                    frontier.append((nx, ny))
        return (tx, ty)

    def compute_portals(self):
        """Due portali su una coppia di angoli diagonalmente opposti della
        mappa: in alto a sinistra e in basso a destra. Tutte le mappe
        standard (39x19) hanno quei due angoli esatti gia' aperti per
        costruzione (vedi commento sopra MAZES in common.py); per sicurezza,
        se un angolo fosse un muro, si cerca con una BFS la cella libera
        raggiungibile piu' vicina all'angolo (vedi _nearest_open_cell).
        Entrare in un portale teletrasporta all'altro (vedi try_portal)."""
        top_left = self._nearest_open_cell(1, 1)
        bottom_right = self._nearest_open_cell(self.maze_w - 2, self.maze_h - 2)
        if top_left != bottom_right:
            self.portals = [top_left, bottom_right]
        else:
            self.portals = []
        # I portali partono accesi ad ogni nuova mappa/round, poi si
        # alternano acceso/spento ogni PORTAL_ON_SECONDS/PORTAL_OFF_SECONDS
        # (vedi update_portal_cycle, chiamato una volta per tick).
        self.portal_on = True
        self.portal_cycle_left = PORTAL_ON_SECONDS

    def map_payload(self):
        return {
            "maze": self.maze, "maze_w": self.maze_w, "maze_h": self.maze_h,
            "maze_name": self.maze_name, "theme": self.theme,
            "portals": [list(p) for p in self.portals],
            "power_pellets": [list(c) for c in self.power_pellets],
        }

    # ---------- lobby ----------

    def add_player(self, player):
        player.host = (len(self.players) == 0)
        self.players[player.id] = player

    def taken_primary_colors(self):
        # Solo il colore PRIMARIO (corpo) resta univoco tra i giocatori, per
        # non avere due pedine indistinguibili a colpo d'occhio: il colore
        # di dettaglio (contorno/denti/occhio) puo' invece essere condiviso
        # liberamente.
        return {p.colors[0] for p in self.players.values() if p.colors}

    async def broadcast(self, obj):
        dead = []
        text = encode_text(obj)
        for p in list(self.players.values()):
            if not p.connected:
                continue
            try:
                await p.ws.send(text)
            except websockets.exceptions.ConnectionClosed:
                dead.append(p.id)
        for pid in dead:
            p = self.players.get(pid)
            if p:
                p.connected = False

    async def broadcast_lobby(self):
        await self.broadcast({
            "type": "lobby_state",
            "code": self.code,
            "players": [
                {
                    "id": p.id, "name": p.name, "colors": p.colors,
                    "character": p.character, "host": p.host,
                }
                for p in self.players.values()
            ],
            "min_players": MIN_PLAYERS,
            "max_players": MAX_PLAYERS,
        })

    # ---------- round setup ----------

    def is_floor(self, x, y):
        """Vero solo se (x,y) e' dentro i confini della mappa corrente ED
        e' una cella di pavimento ('.'), mai un muro ('#'). Usata come rete
        di sicurezza ESPLICITA ogni volta che si sceglie dove far comparire
        un giocatore: non ci si fida ciecamente di self.free_cells (che pure
        e' gia' costruita correttamente), la si ricontrolla cella per
        cella."""
        return (
            0 <= y < len(self.maze)
            and 0 <= x < len(self.maze[y])
            and self.maze[y][x] != "#"
        )

    def get_free_spawn(self, exclude_player=None):
        """Sceglie una cella libera CASUALE di TUTTA la mappa (non piu' solo
        i 4 angoli + centro) che non sia gia' occupata da un altro
        giocatore vivo, cosi' non spawnano mai due giocatori sulla stessa
        casella. self.free_cells contiene gia' tutte le celle non-muro
        della mappa corrente (calcolato una sola volta in pick_new_map), ma
        qui viene comunque ri-filtrata con is_floor: se per qualunque
        motivo la cache fosse disallineata dalla mappa attuale, un
        giocatore non spawnera' MAI su un muro."""
        occupied = {
            (q.x, q.y)
            for q in self.players.values()
            if q.alive and q is not exclude_player
        }
        free = [c for c in self.free_cells if c not in occupied and self.is_floor(c[0], c[1])]
        if free:
            return random.choice(free)
        # Fallback 1: ignora l'occupazione altrui (capita solo con troppi
        # giocatori vivi per le celle libere rimaste), ma resta SEMPRE su
        # pavimento vero.
        floor_only = [c for c in self.free_cells if self.is_floor(c[0], c[1])]
        if floor_only:
            return random.choice(floor_only)
        # Fallback 2 (non dovrebbe mai capitare: significherebbe cache
        # vuota/corrotta): riscansiona la mappa da zero, cella per cella.
        scanned = [(x, y) for y, row in enumerate(self.maze) for x, ch in enumerate(row) if ch == "."]
        if scanned:
            return random.choice(scanned)
        # Rete di sicurezza estrema: ogni mappa e' verificata a tempo di
        # generazione per avere pavimento raggiungibile, quindi questo
        # ramo e' teoricamente irraggiungibile; se mai capitasse, restiamo
        # comunque espliciti invece di far esplodere il server.
        raise RuntimeError("Nessuna cella di pavimento disponibile sulla mappa corrente")

    def pick_spaced_spawn(self, occupied_cells, min_dist=MIN_SPAWN_DISTANCE):
        """Sceglie una cella di PAVIMENTO libera (mai un muro: filtrata con
        is_floor esattamente come get_free_spawn) che sia ad almeno
        min_dist caselle (distanza euclidea) da OGNUNA delle celle in
        occupied_cells. Usata sia per lo spawn iniziale di tutti i
        giocatori sia per i respawn singoli, cosi' nessuno spawna mai
        troppo vicino a un altro (ne', tantomeno, dentro un muro).

        Se la mappa/il numero di giocatori non permettono di rispettare la
        distanza minima (caso raro: mappa piccola o troppi giocatori vivi),
        si sceglie comunque la cella che MASSIMIZZA la distanza minima dagli
        altri, cosi' i giocatori restano il piu' lontano possibile anche in
        questo caso limite, invece di far fallire lo spawn."""
        candidates = [c for c in self.free_cells if self.is_floor(c[0], c[1]) and c not in occupied_cells]
        if not candidates:
            # Stessa rete di sicurezza di get_free_spawn: ri-scansiona la
            # mappa da zero se la cache di free_cells fosse disallineata.
            candidates = [
                (x, y) for y, row in enumerate(self.maze)
                for x, ch in enumerate(row) if ch != "#"
            ]
        if not candidates:
            raise RuntimeError("Nessuna cella di pavimento disponibile sulla mappa corrente")
        if not occupied_cells:
            return random.choice(candidates)

        def min_dist_to_occupied(c):
            return min(math.hypot(c[0] - o[0], c[1] - o[1]) for o in occupied_cells)

        far_enough = [c for c in candidates if min_dist_to_occupied(c) >= min_dist]
        if far_enough:
            return random.choice(far_enough)
        # Best-effort: nessuna cella rispetta la distanza minima richiesta,
        # si prende quella che la massimizza (puo' capitare solo con mappe
        # molto piccole rispetto al numero di giocatori vivi).
        best = max(min_dist_to_occupied(c) for c in candidates)
        best_candidates = [c for c in candidates if min_dist_to_occupied(c) == best]
        return random.choice(best_candidates)

    def assign_spawns(self):
        # Ogni giocatore riceve una cella di pavimento libera, casuale e
        # distante almeno MIN_SPAWN_DISTANCE caselle da quella di TUTTI gli
        # altri giocatori (mai due giocatori sullo stesso spawn, mai vicini,
        # e mai dentro un muro: vedi pick_spaced_spawn).
        occupied_cells = []
        for p in self.players.values():
            x, y = self.pick_spaced_spawn(occupied_cells)
            occupied_cells.append((x, y))
            p.x, p.y = x, y
            p.direction = None
            p.next_direction = None
            p.move_accum = 0.0
            p.alive = True
            # Storico posizioni azzerato: dopo un teletrasporto di spawn
            # non deve restare traccia della posizione precedente, altrimenti
            # un riavvolgimento (vedi _rewind_move) potrebbe usarla per
            # errore e "resuscitare" una posizione ormai non valida.
            p.pos_history.clear()
            # reset del sistema punti/bonus per il nuovo round
            p.points = 0
            p.lives = 3             # si parte con 3 vite ad ogni nuovo round
            p.claimed = set()
            p.has_ninja = False
            p.is_assassin = False
            p.assassin_left = 0.0
            p.ninja_used = False
            p.ghost_left = 0.0
            p.prot_left = SPAWN_PROTECT_SECONDS  # immune per qualche secondo anche allo spawn iniziale
            p.has_laser = False
            p.laser_cd = 0.0
            p.has_bounce = False
            p.has_mines = False
            p.mines_left = 0
            p.portal_cd = 0.0
            p.facing = "right"
            p.has_missile = False
            p.missiles_left = 0
            p.has_trap = False
            p.trap_target = None
            p.trapped_left = 0.0
            p.trapped_by = None
            p.trap_uses_left = 0
            p.has_turret = False
            p.turret_placed = False
            p.has_armor = False
            p.armor_active = False
            p.armor_left = 0.0
            p.armor_used = False
            p.has_lightning = False
            p.lightning_used = False
            p.has_pet = False
            p.pet_summoned = False
            p.has_robot = False
            p.robot_used = False
            p.has_mortar = False
            p.mortar_placed = False
            p.has_superbomb = False
            p.superbomb_left = 0
            p.has_balloon = False
            p.balloon_launched = False
            p.has_blob = False
            p.blob_placed = False
            p.has_blob_alive = False
            p.blob_alive_used = False
            p.has_spike_wall = False
            p.spike_wall_placed = False
            p.has_tesla = False
            p.tesla_placed = False
            p.has_territory_trap = False
            p.territory_marking = False
            p.territory_ready = False
            p.territory_used = False
            p.territory_tiles = set()
            p.has_bush = False
            p.bush_placed = False
            p.has_mushroom = False
            p.mushroom_placed = False
            p.next_lives_milestone = LIVES_EVERY_POINTS
            p.kills = 0
        self.lasers = []
        self.mines = []
        self.missiles = []
        self.turrets = []
        self.pets = []
        self.mortars = []
        self.superbombs = []
        self.balloons = []
        self.blobs = []
        self.spike_walls = []
        self.teslas = []
        self.bushes = []
        self.mushrooms = []
        self.bombs = []
        self.poison_zones = []  # nuvole velenose lasciate a terra dagli impatti del mortaio

    def begin_playing(self):
        """Fine countdown iniziale: il round entra nel vivo. Non c'e' piu'
        alcuna scelta del killer: i giocatori partono tutti sullo stesso
        piano, e si eliminano a vicenda solo tramite i bonus (laser, mine,
        super assassino a 300 punti)."""
        self.state = "PLAYING"
        self.timer_left = ROUND_SECONDS

    # ---------- game tick ----------

    def push_event(self, ev):
        """Accoda un evento una-tantum da trasmettere a fine tick."""
        self.events.append({"type": "event", **ev})

    def push_private_event(self, player_id, ev):
        """Come push_event, ma recapitato SOLO al giocatore indicato (mai
        incluso nel broadcast pubblico): usato dalla trappola territoriale
        (bonus 2600 punti) per far vedere al proprietario, e a lui solo,
        quali caselle sta marcando durante la fase di selezione (vedi
        update_territory_marking/try_use_territory_trap). Stesso identico
        formato "type: event" degli eventi pubblici, cosi' il client lo
        gestisce con lo stesso identico switch (onGameEvent), senza sapere
        (ne doversi preoccupare) che stavolta e' arrivato privatamente."""
        self.private_events.append((player_id, {"type": "event", **ev}))

    async def drain_private_events(self):
        """Invia (e svuota) la coda degli eventi privati accumulati nel
        tick, uno per uno al SOLO destinatario indicato (vedi
        push_private_event): a differenza di drain_events/broadcast, qui
        ogni evento va a un singolo websocket, mai a tutti."""
        if not self.private_events:
            return
        pending, self.private_events = self.private_events, []
        for player_id, ev in pending:
            player = self.players.get(player_id)
            if not player or not player.connected:
                continue
            try:
                await player.ws.send(encode_text(ev))
            except websockets.exceptions.ConnectionClosed:
                player.connected = False

    def update_movement(self):
        prev_positions = {p.id: (p.x, p.y) for p in self.players.values()}
        for p in self.players.values():
            if not p.alive:
                continue

            # Timer personali dei bonus/protezioni: scorrono qui, una volta
            # per tick, cosi' restano sincronizzati con la fisica.
            if p.ghost_left > 0:
                p.ghost_left = max(0.0, p.ghost_left - TICK_DT)
            if p.prot_left > 0:
                p.prot_left = max(0.0, p.prot_left - TICK_DT)
            if p.portal_cd > 0:
                p.portal_cd = max(0.0, p.portal_cd - TICK_DT)
            # Bonus 300 punti (super assassino): dura solo un tempo limitato
            # (30s), poi si disattiva da solo. Il laser (bonus 150) non ha
            # piu' un timer di scadenza: resta sbloccato per tutta la
            # partita una volta ottenuto, e la sua attivazione dipende solo
            # dalla vicinanza di un nemico (vedi update_lasers).
            if p.assassin_left > 0:
                p.assassin_left = max(0.0, p.assassin_left - TICK_DT)
                if p.assassin_left <= 0 and p.is_assassin:
                    p.is_assassin = False
                    self.push_event({"kind": "assassin_off", "player": p.id})
            # Bonus 700 punti (corazza laser): dura solo ARMOR_DURATION_SECONDS,
            # poi si disattiva da sola.
            if p.armor_left > 0:
                p.armor_left = max(0.0, p.armor_left - TICK_DT)
                if p.armor_left <= 0 and p.armor_active:
                    p.armor_active = False
                    self.push_event({"kind": "armor_off", "player": p.id})

            # Bonus 500 punti (trappola): chi e' intrappolato resta bloccato
            # sul punto esatto in cui si trovava, per TRAP_DURATION_SECONDS.
            # Scaduto il tempo senza detonazione, torna libero da solo.
            if p.trapped_left > 0:
                p.trapped_left = max(0.0, p.trapped_left - TICK_DT)
                if p.trapped_left <= 0:
                    trapper = self.players.get(p.trapped_by)
                    if trapper is not None and trapper.trap_target == p.id:
                        trapper.trap_target = None
                    p.trapped_by = None
                    self.push_event({"kind": "trap_expired", "player": p.id})
                # Immobile: niente movimento/pellet/portale in questo tick.
                continue

            # La svolta in coda si applica SUBITO, ad ogni tick, non solo
            # quando si e' esattamente su un incrocio: aspettare l'incrocio
            # sembrava piu' pulito, ma introduceva un problema peggiore,
            # cioe' client e server non attraversano MAI il confine della
            # cella nello stesso identico istante (millisecondo), quindi ad
            # ogni curva le due posizioni previste divergevano abbastanza
            # da far scattare la correzione forte lato client: e' quello il
            # teletrasporto. Girando subito, client e server seguono la
            # STESSA regola semplice ("se non e' muro, gira ora") e restano
            # sincronizzati sulla stessa logica deterministica.
            #
            # Resta pero' il problema originale che l'attesa dell'incrocio
            # doveva risolvere: se si cambia asse (es. da orizzontale a
            # verticale) a meta' cella, non si puo' riusare lo stesso
            # move_accum sul nuovo asse, altrimenti il personaggio "salta"
            # di una frazione di cella nella direzione sbagliata. Per
            # questo, ogni volta che la direzione cambia davvero, si azzera
            # l'avanzamento frazionario: il movimento nella nuova direzione
            # riparte pulito dal centro della cella corrente. E' comunque
            # uno scatto piccolo e deterministico (identico su client e
            # server), non il grosso teletrasporto dovuto al disallineamento
            # di rete.
            # ---- MOVIMENTO "VERO PAC-MAN" (automa a sotto-passi) ----
            # La logica vera e propria vive in _advance_state, condivisa con
            # Room._rewind_move: cosi' un tick normale e un "riavvolgimento"
            # per compensare la latenza (vedi sopra il messaggio "move")
            # usano ESATTAMENTE la stessa fisica, e non possono divergere.
            speed = NORMAL_SPEED
            if p.is_assassin:
                # Il super assassino (300 punti) e' piu' veloce dei
                # giocatori normali (stesso moltiplicatore 1.0 -> 1.1).
                speed *= ASSASSIN_SPEED_MULT
            (p.x, p.y, p.move_accum, p.direction, p.next_direction,
             facing) = self._advance_state(
                p.x, p.y, p.move_accum, p.direction, p.next_direction,
                TICK_DT, speed,
            )
            if facing is not None:
                p.facing = facing

            # Registra lo stato di fine tick nello storico: e' quello che
            # _rewind_move usera' per "tornare indietro" fino al momento
            # reale in cui un tasto direzione e' stato premuto (vedi sopra).
            now = time.monotonic()
            p.pos_history.append((now, p.x, p.y, p.move_accum, p.direction))
            cutoff = now - REWIND_HISTORY_SECONDS
            while p.pos_history and p.pos_history[0][0] < cutoff:
                p.pos_history.popleft()

            # Pallini e portali si valutano sulla cella in cui ci si trova
            # ORA (anche da fermi: copre lo spawn su un pallino).
            self.eat_pellet(p)
            self.eat_mega_pellet(p)
            self.try_portal(p)
        return prev_positions

    def _advance_state(self, x, y, accum, direction, next_direction, dt, speed):
        """Avanza UNA sola entita' (posizione+direzione) di dt secondi,
        applicando le stesse regole "vero Pac-Man" di sempre:
         1. Inversione di marcia (direzione opposta): applicata SUBITO,
            anche a meta' cella, senza scatti - la posizione continua
            viene conservata ribaltando l'avanzamento (accum -> 1-accum
            sulla cella successiva).
         2. Svolta perpendicolare in coda: applicata solo AL CENTRO della
            cella (accum == 0), come nel Pac-Man originale.
         3. Se la svolta e' bloccata da un muro, la coda RESTA in attesa e
            scatta da sola al primo incrocio utile.

        Pura funzione di stato (non tocca self.players ne' side-effect come
        pallini/eventi): usata sia dal tick normale (update_movement) sia
        dal riavvolgimento per compensare la latenza (_rewind_move), cosi'
        le due strade producono sempre lo stesso identico risultato per lo
        stesso input, e non possono disallinearsi tra loro.

        Ritorna (x, y, accum, direction, next_direction, facing) dove facing
        e' l'ultima direzione di marcia attraversata (None se l'entita' era
        gia' ferma e non si e' mai mossa in questo intervallo).
        """
        facing = None
        remaining = dt
        while remaining > 1e-9:
            if next_direction is not None:
                if (direction is not None
                        and next_direction == OPPOSITE_DIR[direction]
                        and accum > 1e-9):
                    dx, dy = DIRECTIONS[direction]
                    x += dx
                    y += dy
                    accum = 1.0 - accum
                    direction = next_direction
                    next_direction = None
                elif accum <= 1e-9:
                    ndx, ndy = DIRECTIONS[next_direction]
                    if not is_wall(self.maze, self.maze_w, self.maze_h,
                                   x + ndx, y + ndy):
                        direction = next_direction
                        next_direction = None
                    # se e' muro: la coda resta in memoria (regola 3)
            if direction is None:
                break
            facing = direction
            dx, dy = DIRECTIONS[direction]
            nx, ny = x + dx, y + dy
            if is_wall(self.maze, self.maze_w, self.maze_h, nx, ny):
                accum = 0.0
                break
            step = min(remaining, (1.0 - accum) / speed)
            accum += speed * step
            remaining -= step
            if accum >= 1.0 - 1e-6:
                accum = 0.0
                x, y = nx, ny
            else:
                break
        return x, y, accum, direction, next_direction, facing

    def _rewind_move(self, p, requested_dir):
        """Applica una richiesta di svolta compensando la latenza di rete.

        Invece di limitarsi ad accodare `requested_dir` nella posizione
        ATTUALE del giocatore (che sul server e' gia' "nel futuro" rispetto
        al momento reale in cui il tasto e' stato premuto, per via del
        tempo di viaggio del pacchetto), si cerca nello storico lo stato
        registrato a meta' del round-trip-time stimato fa, si applica li'
        la richiesta con le stesse regole di sempre (_advance_state), e si
        "riavvolge in avanti" fino ad ora. Il risultato e' la posizione
        fisicamente corretta: se al centro-cella di allora la svolta era
        valida, il giocatore la ottiene, senza dover prima sbattere contro
        un muro e senza alcuno scatto visibile (la correzione e' al massimo
        di REWIND_MAX_SECONDS di percorso, meno di mezza cella a velocita'
        normale).

        IMPORTANTE: il riavvolgimento tocca p.x/p.y/p.move_accum, quindi va
        usato SOLO quando serve davvero. Se la pressione e' gia' in tempo
        (il giocatore e' fermo, sta invertendo la marcia, o e' gia' esattamente
        al centro-cella) la svolta scatterebbe comunque, subito e senza
        alcun problema, con il solito accodamento "leggero" (next_direction),
        esattamente come prima di questa modifica: quel percorso NON tocca
        mai la posizione, quindi resta perfettamente fluido. Il
        riavvolgimento e' riservato al solo caso realmente problematico
        (svolta perpendicolare richiesta mentre non si e' al centro), che
        prima di questa fix veniva accodata e rischiava di far sbattere il
        personaggio contro un muro. Fare passare OGNI pressione dal
        ricalcolo, invece, introduceva un piccolo scarto numerico ad ogni
        singolo tasto premuto (il riavvolgimento ricompone la posizione in
        un solo blocco di tempo, il tick normale la accumula a incrementi
        di TICK_DT: gli arrotondamenti non coincidono mai esattamente) e con
        tasti premuti di continuo durante il gioco normale, quello scarto
        continuo e' quello che si percepiva come "a scatti".
        """
        if (
            p.direction is None
            or p.move_accum <= 1e-9
            or requested_dir == OPPOSITE_DIR[p.direction]
        ):
            # Percorso semplice, invariato: nessun tocco a x/y/move_accum.
            p.next_direction = requested_dir
            return

        now = time.monotonic()
        delay = min(p.rtt / 2.0, REWIND_MAX_SECONDS)
        if delay <= 1e-9 or not p.pos_history:
            # Nessuna stima di ritardo utile o storico vuoto (es. appena
            # spawnato): nessun rischio, ma nessun beneficio nemmeno.
            # Si torna al comportamento semplice, sempre corretto.
            p.next_direction = requested_dir
            return

        target_t = now - delay
        # Cerca l'ultimo campione dello storico CON timestamp <= target_t
        # (dal piu' recente al piu' vecchio: di solito e' tra gli ultimi).
        snapshot = None
        for entry in reversed(p.pos_history):
            if entry[0] <= target_t:
                snapshot = entry
                break
        if snapshot is None:
            # Storico troppo corto per coprire il ritardo stimato (partita
            # appena iniziata): fallback sicuro, nessun riavvolgimento.
            p.next_direction = requested_dir
            return

        snap_t, sx, sy, saccum, sdir = snapshot
        replay_dt = now - snap_t
        speed = NORMAL_SPEED * (ASSASSIN_SPEED_MULT if p.is_assassin else 1.0)
        nx, ny, naccum, ndir, nnext, facing = self._advance_state(
            sx, sy, saccum, sdir, requested_dir, replay_dt, speed,
        )

        # Rete di sicurezza: se nel frattempo e' successo qualcosa che
        # _advance_state non conosce (respawn, teletrasporto via portale,
        # trappola, eliminazione...) la posizione ricalcolata puo' finire
        # molto lontana da quella attuale. In quel caso si scarta il
        # riavvolgimento e si torna al comportamento semplice, invece di
        # rischiare di teletrasportare il giocatore per errore.
        if math.hypot(nx - p.x, ny - p.y) > 1.5:
            p.next_direction = requested_dir
            return

        p.x, p.y, p.move_accum, p.direction, p.next_direction = nx, ny, naccum, ndir, nnext
        if facing is not None:
            p.facing = facing

    def update_rtt_pings(self):
        """Manda un ping di misura RTT a ciascun giocatore ogni
        RTT_PING_INTERVAL_SECONDS, e aggiorna p.rtt quando arriva il pong
        (vedi il branch 'rtt_pong' nel loop messaggi). Chiamato una volta
        per tick da run_round, con costo trascurabile (un controllo
        temporale per giocatore, invio solo ogni ~2s)."""
        for p in self.players.values():
            if not p.connected:
                continue
            p.rtt_ping_cd -= TICK_DT
            if p.rtt_ping_cd > 0:
                continue
            p.rtt_ping_cd = RTT_PING_INTERVAL_SECONDS
            p.rtt_ping_sent_at = time.monotonic()
            asyncio.ensure_future(self._safe_send(p.ws, encode_text(
                {"type": "rtt_ping", "t": p.rtt_ping_sent_at}
            )))

    @staticmethod
    async def _safe_send(ws, payload):
        try:
            await ws.send(payload)
        except websockets.exceptions.ConnectionClosed:
            pass

    def eat_pellet(self, p):
        cell = (p.x, p.y)
        if cell not in self.pellets:
            return
        self.pellets.discard(cell)
        is_power = cell in self.power_pellets
        gained = POWER_PELLET_POINTS if is_power else PELLET_POINTS
        p.points += gained
        # Il pallino ricompare da solo dopo PELLET_RESPAWN_SECONDS (stesso
        # tipo, normale o "power", di quello appena mangiato).
        self.pellet_respawns[cell] = PELLET_RESPAWN_SECONDS
        self.push_event({
            "kind": "pellet", "cells": [[p.x, p.y]], "by": p.id,
            "power": is_power, "points": gained,
        })
        self.check_bonuses(p)

    def update_pellet_respawns(self):
        """Fa ricomparire i pallini mangiati dopo PELLET_RESPAWN_SECONDS."""
        if not self.pellet_respawns:
            return
        done = []
        for cell, left in self.pellet_respawns.items():
            left -= TICK_DT
            if left <= 0:
                done.append(cell)
            else:
                self.pellet_respawns[cell] = left
        for cell in done:
            del self.pellet_respawns[cell]
            self.pellets.add(cell)
            self.push_event({
                "kind": "pellet_respawn", "cells": [[cell[0], cell[1]]],
                "power": cell in self.power_pellets,
            })

    def update_mega_pellet(self):
        """Ogni MEGA_PELLET_INTERVAL_SECONDS (un minuto) fa comparire il
        pallino mega da MEGA_PELLET_POINTS punti nella cella fissa al
        centro della mappa (self.mega_pellet_spot, calcolata in
        pick_new_map), ma SOLO se non ce n'e' gia' uno in attesa di essere
        mangiato: a differenza dei pallini normali, non ricompare da solo
        subito dopo essere stato mangiato, bisogna aspettare il giro
        successivo."""
        self.mega_pellet_timer -= TICK_DT
        if self.mega_pellet_timer > 0:
            return
        self.mega_pellet_timer = MEGA_PELLET_INTERVAL_SECONDS
        if self.mega_pellet_cell is not None:
            return
        self.mega_pellet_cell = self.mega_pellet_spot
        self.push_event({
            "kind": "mega_pellet_spawn",
            "x": self.mega_pellet_spot[0], "y": self.mega_pellet_spot[1],
            "points": MEGA_PELLET_POINTS,
        })

    def eat_mega_pellet(self, p):
        """Se il giocatore si trova sulla cella del pallino mega attivo, lo
        mangia: guadagna MEGA_PELLET_POINTS punti in un colpo solo e il
        pallino sparisce fino al prossimo giro di
        MEGA_PELLET_INTERVAL_SECONDS (vedi update_mega_pellet)."""
        if self.mega_pellet_cell is None or (p.x, p.y) != self.mega_pellet_cell:
            return
        self.mega_pellet_cell = None
        p.points += MEGA_PELLET_POINTS
        self.push_event({
            "kind": "mega_pellet_eaten", "by": p.id,
            "x": p.x, "y": p.y, "points": MEGA_PELLET_POINTS,
        })
        self.check_bonuses(p)

    def check_bonuses(self, p):
        """Riscatta i traguardi appena superati (una volta sola per round).

        Il ninja (300 punti) e la trappola (500 punti) sono traguardi a
        parte rispetto a BONUS_THRESHOLDS (soglie fisse, non configurabili
        per-mappa). Allo sblocco NON si attivano da soli: segnano solo che
        il bonus e' disponibile (has_ninja / has_trap). L'attivazione vera
        e propria scatta solo quando il giocatore preme il tasto
        corrispondente (vedi try_activate_ninja e try_activate_trap), e
        resta disponibile per tutto il round (si puo' riattivare piu'
        volte, a differenza di laser/mine/missili che si consumano)."""
        for threshold, kind in BONUS_THRESHOLDS:
            if p.points < threshold or threshold in p.claimed:
                continue
            p.claimed.add(threshold)
            if kind == "extra_life":
                p.lives += 1
            elif kind == "extra_life_3":
                p.lives += 3
            elif kind == "laser":
                p.has_laser = True
                p.laser_cd = LASER_FIRST_DELAY_SECONDS
            elif kind == "mines":
                p.has_mines = True
                p.mines_left = MINES_COUNT
            elif kind == "missile":
                p.has_missile = True
                p.missiles_left = MISSILES_COUNT
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": kind, "points": threshold,
            })

        # Traguardo RICORRENTE: ogni LIVES_EVERY_POINTS punti (1600, 3200,
        # 4800, ...) si guadagnano LIVES_EVERY_AMOUNT vite extra in un
        # colpo solo, senza limite. Il while gestisce anche il caso di un
        # balzo di punti che scavalca piu' traguardi in un colpo.
        while p.alive and p.points >= p.next_lives_milestone:
            p.lives += LIVES_EVERY_AMOUNT
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "extra_life_3", "points": p.next_lives_milestone,
            })
            p.next_lives_milestone += LIVES_EVERY_POINTS
        # Bonus 300 punti: sblocca la modalita' ninja (invisibilita' +
        # velocita' + uccisione al contatto), ma NON la attiva. Si attiva
        # a comando col tasto "2" (vedi try_activate_ninja).
        if (
            p.alive
            and p.points >= SUPER_ASSASSIN_THRESHOLD
            and SUPER_ASSASSIN_THRESHOLD not in p.claimed
        ):
            p.claimed.add(SUPER_ASSASSIN_THRESHOLD)
            p.has_ninja = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "ninja", "points": SUPER_ASSASSIN_THRESHOLD,
            })
        # Bonus 500 punti: sblocca la trappola, ma NON intrappola subito
        # nessuno. Si attiva a comando col tasto "4" (vedi try_activate_trap).
        if (
            p.alive
            and p.points >= TRAP_THRESHOLD
            and TRAP_THRESHOLD not in p.claimed
        ):
            p.claimed.add(TRAP_THRESHOLD)
            p.has_trap = True
            p.trap_uses_left = TRAP_MAX_USES
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "trap", "points": TRAP_THRESHOLD,
            })
        # Bonus 600 punti: sblocca la torretta automatica permanente, ma NON
        # la piazza subito. Si piazza a comando col tasto "5" (vedi
        # try_place_turret), una sola volta per giocatore.
        if (
            p.alive
            and p.points >= TURRET_THRESHOLD
            and TURRET_THRESHOLD not in p.claimed
        ):
            p.claimed.add(TURRET_THRESHOLD)
            p.has_turret = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "turret", "points": TURRET_THRESHOLD,
            })
        # Bonus 700 punti: sblocca la corazza laser, ma NON la attiva. Si
        # attiva a comando col tasto "6" (vedi try_activate_armor), UNA SOLA
        # VOLTA per round (come il ninja).
        if (
            p.alive
            and p.points >= ARMOR_THRESHOLD
            and ARMOR_THRESHOLD not in p.claimed
        ):
            p.claimed.add(ARMOR_THRESHOLD)
            p.has_armor = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "armor", "points": ARMOR_THRESHOLD,
            })
        # Bonus 800 punti: sblocca il fulmine, ma NON lo scatena subito. Si
        # attiva a comando col tasto "7" (vedi try_activate_lightning), UNA
        # SOLA VOLTA per round (come ninja e corazza).
        if (
            p.alive
            and p.points >= LIGHTNING_THRESHOLD
            and LIGHTNING_THRESHOLD not in p.claimed
        ):
            p.claimed.add(LIGHTNING_THRESHOLD)
            p.has_lightning = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "lightning", "points": LIGHTNING_THRESHOLD,
            })
        # Bonus 900 punti: sblocca il pet fedele, ma NON lo evoca subito. Si
        # evoca a comando col tasto "8" (vedi try_summon_pet), una sola volta
        # per giocatore, per round.
        if (
            p.alive
            and p.points >= PET_THRESHOLD
            and PET_THRESHOLD not in p.claimed
        ):
            p.claimed.add(PET_THRESHOLD)
            p.has_pet = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "pet", "points": PET_THRESHOLD,
            })
        # Bonus 1000 punti: sblocca l'evoluzione della torretta in robot
        # mobile, ma NON la evolve subito. Si evolve a comando col tasto "9"
        # (vedi try_evolve_turret), UNA SOLA VOLTA per round, e solo se la
        # torretta e' ancora viva sulla mappa in quel momento.
        if (
            p.alive
            and p.points >= ROBOT_THRESHOLD
            and ROBOT_THRESHOLD not in p.claimed
        ):
            p.claimed.add(ROBOT_THRESHOLD)
            p.has_robot = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "robot", "points": ROBOT_THRESHOLD,
            })
        # Bonus 1200 punti: sblocca il mortaio, ma NON lo schiera subito. Si
        # schiera a comando col tasto "1" (vedi try_place_mortar), UNA SOLA
        # VOLTA per giocatore, per round.
        if (
            p.alive
            and p.points >= MORTAR_THRESHOLD
            and MORTAR_THRESHOLD not in p.claimed
        ):
            p.claimed.add(MORTAR_THRESHOLD)
            p.has_mortar = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "mortar", "points": MORTAR_THRESHOLD,
            })
        # Bonus 1400 punti: sblocca il bombolone ad area, ma NON lo piazza
        # subito. Si piazza a comando RIUSANDO il tasto "1" (vedi
        # try_place_superbomb), UNA SOLA VOLTA per giocatore per round, ma
        # solo DOPO aver gia' schierato il mortaio (bonus 1200 punti): finche'
        # il mortaio non e' stato piazzato, il tasto "1" resta dedicato a
        # quello (vedi il dispatch del messaggio "place_mortar").
        if (
            p.alive
            and p.points >= SUPERBOMB_THRESHOLD
            and SUPERBOMB_THRESHOLD not in p.claimed
        ):
            p.claimed.add(SUPERBOMB_THRESHOLD)
            p.has_superbomb = True
            p.superbomb_left = SUPERBOMB_COUNT
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "superbomb", "points": SUPERBOMB_THRESHOLD,
            })
        # Bonus 1600 punti: sblocca la mongolfiera vagante, ma NON la fa
        # librare subito. Si libra a comando RIUSANDO ancora il tasto "1"
        # (vedi try_launch_balloon), UNA SOLA VOLTA per giocatore per round,
        # ma solo DOPO aver gia' piazzato sia il mortaio (1200) sia il
        # bombolone (1400): finche' entrambi non sono stati piazzati, il
        # tasto "1" resta dedicato a quelli (vedi il dispatch del messaggio
        # "place_mortar").
        if (
            p.alive
            and p.points >= BALLOON_THRESHOLD
            and BALLOON_THRESHOLD not in p.claimed
        ):
            p.claimed.add(BALLOON_THRESHOLD)
            p.has_balloon = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "balloon", "points": BALLOON_THRESHOLD,
            })
        # Bonus 1800 punti: sblocca il blob gelatinoso, ma NON lo piazza
        # subito. Si piazza a comando RIUSANDO ancora il tasto "1" (vedi
        # try_place_blob), UNA SOLA VOLTA per giocatore per round, ma solo
        # DOPO aver gia' piazzato mortaio (1200), bombolone (1400) e
        # mongolfiera (1600): finche' non sono stati piazzati tutti e tre,
        # il tasto "1" resta dedicato a quelli (vedi il dispatch del
        # messaggio "place_mortar").
        if (
            p.alive
            and p.points >= BLOB_THRESHOLD
            and BLOB_THRESHOLD not in p.claimed
        ):
            p.claimed.add(BLOB_THRESHOLD)
            p.has_blob = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "blob", "points": BLOB_THRESHOLD,
            })

        # Bonus 2000 punti: sblocca il risveglio del blob, ma NON lo anima
        # subito. Si risveglia a comando RIUSANDO ancora il tasto "1" (vedi
        # try_animate_blob), UNA SOLA VOLTA per giocatore per round, ma solo
        # DOPO aver gia' piazzato mortaio (1200), bombolone (1400),
        # mongolfiera (1600) e blob (1800): finche' non sono stati piazzati
        # tutti e quattro, il tasto "1" resta dedicato a quelli (vedi il
        # dispatch del messaggio "place_mortar").
        if (
            p.alive
            and p.points >= BLOB_ALIVE_THRESHOLD
            and BLOB_ALIVE_THRESHOLD not in p.claimed
        ):
            p.claimed.add(BLOB_ALIVE_THRESHOLD)
            p.has_blob_alive = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "blob_alive", "points": BLOB_ALIVE_THRESHOLD,
            })

        # Bonus 2200 punti: sblocca il muro di spunzoni, ma NON lo piazza
        # subito. Si piazza a comando RIUSANDO ancora il tasto "1" (vedi
        # try_place_spike_wall), UNA SOLA VOLTA per giocatore per round, ma
        # solo DOPO aver esaurito tutta la catena precedente del tasto "1"
        # (mortaio, bombolone, mongolfiera, blob e risveglio del blob):
        # finche' la catena non e' esaurita, il tasto "1" resta dedicato a
        # quella (vedi il dispatch del messaggio "place_mortar").
        if (
            p.alive
            and p.points >= SPIKE_WALL_THRESHOLD
            and SPIKE_WALL_THRESHOLD not in p.claimed
        ):
            p.claimed.add(SPIKE_WALL_THRESHOLD)
            p.has_spike_wall = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "spike_wall", "points": SPIKE_WALL_THRESHOLD,
            })

        # Bonus 2400 punti: sblocca la Tesla laser, ma NON la piazza
        # subito. Si piazza a comando RIUSANDO ancora il tasto "1" (vedi
        # try_place_tesla), UNA SOLA VOLTA per giocatore per round, ma solo
        # DOPO aver esaurito tutta la catena precedente del tasto "1"
        # (mine, mortaio, bombolone, mongolfiera, blob, risveglio del blob
        # e muro di spunzoni): finche' la catena non e' esaurita, il tasto
        # "1" resta dedicato a quella (vedi il dispatch del messaggio
        # "place_mortar").
        if (
            p.alive
            and p.points >= TESLA_THRESHOLD
            and TESLA_THRESHOLD not in p.claimed
        ):
            p.claimed.add(TESLA_THRESHOLD)
            p.has_tesla = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "tesla", "points": TESLA_THRESHOLD,
            })

        # Bonus 2600 punti: sblocca la trappola territoriale a spunzoni, ma
        # NON avvia subito la selezione. Si usa a comando RIUSANDO ancora
        # il tasto "1" (vedi try_use_territory_trap), UNA SOLA VOLTA per
        # giocatore per round, ma solo DOPO aver esaurito tutta la catena
        # precedente del tasto "1" (mine, mortaio, bombolone, mongolfiera,
        # blob, risveglio del blob, muro di spunzoni e Tesla): finche' la
        # catena non e' esaurita, il tasto "1" resta dedicato a quella
        # (vedi il dispatch del messaggio "place_mortar").
        if (
            p.alive
            and p.points >= TERRITORY_TRAP_THRESHOLD
            and TERRITORY_TRAP_THRESHOLD not in p.claimed
        ):
            p.claimed.add(TERRITORY_TRAP_THRESHOLD)
            p.has_territory_trap = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "territory_trap", "points": TERRITORY_TRAP_THRESHOLD,
            })

        # Bonus 2800 punti: sblocca l'arbusto spinoso, ma NON lo piazza
        # subito. Si piazza a comando RIUSANDO ancora il tasto "1" (vedi
        # try_place_bush), UNA SOLA VOLTA per giocatore per round, ma solo
        # DOPO aver esaurito tutta la catena precedente del tasto "1"
        # (mine, mortaio, bombolone, mongolfiera, blob, risveglio del blob,
        # muro di spunzoni, Tesla e trappola territoriale): finche' la
        # catena non e' esaurita, il tasto "1" resta dedicato a quella
        # (vedi il dispatch del messaggio "place_mortar").
        if (
            p.alive
            and p.points >= BUSH_THRESHOLD
            and BUSH_THRESHOLD not in p.claimed
        ):
            p.claimed.add(BUSH_THRESHOLD)
            p.has_bush = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "bush", "points": BUSH_THRESHOLD,
            })

        # Bonus 3000 punti: sblocca il fungo atomico, ma NON lo piazza
        # subito. Si piazza a comando RIUSANDO ancora il tasto "1" (vedi
        # try_place_mushroom), UNA SOLA VOLTA per giocatore per round, ma
        # solo DOPO aver esaurito tutta la catena precedente del tasto "1"
        # (arbusto spinoso compreso): finche' la catena non e' esaurita,
        # il tasto "1" resta dedicato a quella (vedi il dispatch del
        # messaggio "place_mortar").
        if (
            p.alive
            and p.points >= MUSHROOM_THRESHOLD
            and MUSHROOM_THRESHOLD not in p.claimed
        ):
            p.claimed.add(MUSHROOM_THRESHOLD)
            p.has_mushroom = True
            self.push_event({
                "kind": "bonus", "player": p.id,
                "bonus": "mushroom", "points": MUSHROOM_THRESHOLD,
            })

    def try_portal(self, p):
        """Se il giocatore e' su un portale (e non e' appena arrivato da un
        teletrasporto), lo sposta al portale opposto mantenendo la direzione.
        Funziona solo mentre i portali sono ACCESI (vedi update_portal_cycle):
        da spenti, stare sulla cella del portale non ha alcun effetto."""
        if not self.portals or p.portal_cd > 0 or not self.portal_on:
            return
        pos = (p.x, p.y)
        if pos == self.portals[0]:
            dest = self.portals[1]
        elif pos == self.portals[1]:
            dest = self.portals[0]
        else:
            return
        src = pos
        p.x, p.y = dest
        p.move_accum = 0.0
        p.portal_cd = PORTAL_COOLDOWN_SECONDS
        # Vedi assign_spawns: dopo un salto di posizione non dovuto al
        # normale movimento, lo storico va azzerato per non offrire a
        # _rewind_move dati di "prima del teletrasporto".
        p.pos_history.clear()
        self.push_event({
            "kind": "teleport", "player": p.id,
            "from": [src[0], src[1]], "to": [dest[0], dest[1]],
        })

    def update_portal_cycle(self):
        """Alterna i portali tra acceso (PORTAL_ON_SECONDS) e spento
        (PORTAL_OFF_SECONDS), avanti e indietro per tutto il round."""
        if not self.portals:
            return
        self.portal_cycle_left -= TICK_DT
        if self.portal_cycle_left <= 0:
            self.portal_on = not self.portal_on
            self.portal_cycle_left = PORTAL_ON_SECONDS if self.portal_on else PORTAL_OFF_SECONDS
            self.push_event({"kind": "portal_toggle", "on": self.portal_on})

    def respawn_player(self, p):
        """Rimette in gioco chi aveva una vita extra: se un super assassino
        e' attivo, nella cella libera piu' lontana da lui su tutta la
        mappa, con qualche secondo di protezione; altrimenti in una cella
        libera casuale, ovunque sulla mappa (non piu' solo negli angoli).
        In ogni caso mai su una casella gia' occupata da un altro
        giocatore vivo (mai due giocatori sullo stesso spawn)."""
        assassins = [q for q in self.players.values() if q.alive and q.is_assassin]
        occupied = {
            (q.x, q.y)
            for q in self.players.values()
            if q.alive and q is not p
        }
        if assassins:
            # Stessa rete di sicurezza di get_free_spawn: ri-filtriamo con
            # is_floor invece di fidarci ciecamente di self.free_cells.
            free = [c for c in self.free_cells if c not in occupied and self.is_floor(c[0], c[1])]
            if not free:
                free = [c for c in self.free_cells if self.is_floor(c[0], c[1])]
            if not free:
                free = [(x, y) for y, row in enumerate(self.maze) for x, ch in enumerate(row) if ch == "."]
            def min_dist(s):
                return min(abs(s[0] - a.x) + abs(s[1] - a.y) for a in assassins)
            x, y = max(free, key=min_dist)
        else:
            x, y = self.pick_spaced_spawn(occupied)
        p.x, p.y = x, y
        p.direction = None
        p.next_direction = None
        p.move_accum = 0.0
        p.prot_left = SPAWN_PROTECT_SECONDS
        p.portal_cd = 0.5  # se lo spawn fosse vicino a un portale, niente teletrasporto istantaneo
        p.pos_history.clear()  # vedi assign_spawns

    def kill_player(self, victim, cause, shooter_id=None):
        """Unica via per togliere una vita: usata dal tocco del super
        assassino, dal laser e dalle mine. Con vite extra si respawna,
        altrimenti si e' fuori.

        Chi uccide GUADAGNA il 10% dei punti della vittima come bonus
        (KILL_STEAL_FRACTION, arrotondato per difetto): la vittima
        CONSERVA tutti i suoi punti, nessuno le viene piu' sottratto -
        un'eliminazione fa guadagnare il killer ma non penalizza piu' chi
        viene ucciso."""
        self.last_kill = {"cause": cause, "by": shooter_id}
        killer_player = self.players.get(shooter_id) if shooter_id else None
        stolen = 0
        if killer_player is not None and killer_player.id != victim.id:
            stolen = int(victim.points * KILL_STEAL_FRACTION)
            if stolen > 0:
                killer_player.points += stolen
                self.check_bonuses(killer_player)
            # Ogni 2 uccisioni fatte, il killer guadagna una vita extra
            # (indipendente dalle soglie punti: conta solo il numero di kill).
            killer_player.kills += 1
            if killer_player.kills % 2 == 0:
                killer_player.lives += 1
                self.push_event({
                    "kind": "kill_life_bonus", "player": killer_player.id,
                    "kills": killer_player.kills, "lives": killer_player.lives,
                })
        # Pulizia stato trappola: sia se la vittima era intrappolata, sia se
        # la vittima stessa aveva qualcuno intrappolato (il bersaglio torna
        # libero, dato che chi lo teneva e' stato eliminato).
        if victim.trapped_by:
            trapper = self.players.get(victim.trapped_by)
            if trapper is not None and trapper.trap_target == victim.id:
                trapper.trap_target = None
            victim.trapped_by = None
        victim.trapped_left = 0.0
        if victim.trap_target:
            freed = self.players.get(victim.trap_target)
            if freed is not None and freed.trapped_by == victim.id:
                freed.trapped_left = 0.0
                freed.trapped_by = None
                self.push_event({"kind": "trap_expired", "player": freed.id})
            victim.trap_target = None
        victim.lives -= 1
        died_at = [victim.x, victim.y]
        if victim.lives > 0:
            self.respawn_player(victim)
            respawned = True
        else:
            victim.alive = False
            victim.direction = None
            victim.next_direction = None
            victim.move_accum = 0.0
            respawned = False
        if victim.is_assassin:
            victim.is_assassin = False
            victim.assassin_left = 0.0
            self.push_event({"kind": "assassin_off", "player": victim.id})
        self.push_event({
            "kind": "kill", "victim": victim.id, "cause": cause,
            "by": shooter_id, "at": died_at,
            "respawn": respawned, "lives": max(victim.lives, 0),
            "stolen": stolen,
        })

    def check_collisions(self, prev_positions):
        """A SUPER_ASSASSIN_THRESHOLD punti un giocatore diventa "super
        assassino" per SUPER_ASSASSIN_DURATION_SECONDS (vedi check_bonuses):
        invisibile agli altri, piu' veloce, e uccide chiunque tocchi. Allo
        stesso modo, a ARMOR_THRESHOLD punti la corazza laser (bonus 700,
        visibile a tutti) uccide chiunque tocchi mentre e' attiva. Due
        giocatori "letali" (ninja e/o corazza, in qualsiasi combinazione)
        non si uccidono a vicenda toccandosi."""
        lethal = [p for p in self.players.values() if p.alive and (p.is_assassin or p.armor_active)]
        if not lethal:
            return
        killed_ids = set()
        for L in lethal:
            if L.id in killed_ids or not L.alive:
                continue
            for p in list(self.players.values()):
                if p.id == L.id or not p.alive or p.id in killed_ids:
                    continue
                if p.is_assassin or p.armor_active:
                    continue  # due giocatori "letali" non si eliminano a vicenda
                # Il tocco non puo' nulla contro la protezione post-respawn
                # (il bonus fantasma e' stato rimosso dal gioco, ghost_left
                # resta sempre 0). Laser e mine ignorano comunque entrambe.
                if p.ghost_left > 0 or p.prot_left > 0:
                    continue
                same_cell = (p.x == L.x and p.y == L.y)
                swapped = (
                    prev_positions.get(p.id) == (L.x, L.y)
                    and prev_positions.get(L.id) == (p.x, p.y)
                )
                if same_cell or swapped:
                    cause = "assassin" if L.is_assassin else "armor"
                    self.kill_player(p, cause, shooter_id=L.id)
                    killed_ids.add(p.id)

    def update_lasers(self):
        """Bonus 150 punti: arma principale, sbloccata UNA VOLTA e attiva
        per TUTTA la partita da quel momento (non scade piu'). Ogni
        LASER_INTERVAL_SECONDS (1 secondo) parte un singolo colpo
        (proiettile) dal lato frontale del personaggio, con la stessa
        identica meccanica di sempre (spawn_laser) - ma SOLO quando almeno
        un avversario vivo si trova entro LASER_RANGE_CELLS caselle
        (distanza Manhattan), esattamente come il raggio d'azione della
        torretta (vedi update_turrets). Se nessuno e' abbastanza vicino resta
        carico (cd fermo a zero) e spara ISTANTANEAMENTE appena qualcuno
        entra nel raggio, invece di sprecare colpi a vuoto o di accumulare
        colpi arretrati. Il super assassino (300 punti, invisibile agli
        altri) NON spara: il proiettile e' visibile a tutti e rivelerebbe
        subito la sua posizione, vanificando l'invisibilita'. Allo stesso
        modo, chi e' intrappolato dalla trappola di un avversario
        (trapped_left > 0) non spara: e' completamente bloccato, laser
        compreso, finche' non torna libero di muoversi."""
        for p in list(self.players.values()):
            if not p.alive or not p.has_laser or p.is_assassin or p.trapped_left > 0:
                continue
            nearest = self.nearest_alive(p.x, p.y, {p.id})
            in_range = (
                nearest is not None
                and abs(nearest.x - p.x) + abs(nearest.y - p.y) <= LASER_RANGE_CELLS
            )
            p.laser_cd -= TICK_DT
            if p.laser_cd > 0:
                continue
            if not in_range:
                p.laser_cd = 0.0
                continue
            p.laser_cd = LASER_INTERVAL_SECONDS
            self.spawn_laser(p)

    def spawn_laser(self, shooter):
        """Crea un nuovo proiettile laser (singolo colpo) che parte dalla
        cella dello sparatore e viaggia nella sua direzione frontale. Il
        proiettile vero e proprio avanza poi, un tick alla volta, dentro
        move_lasers()."""
        dx, dy = DIRECTIONS.get(shooter.facing, (1, 0))
        laser = {
            "id": uuid.uuid4().hex[:8],
            "owner": shooter.id,
            "x": shooter.x, "y": shooter.y,   # cella intera corrente
            "dx": dx, "dy": dy,
            "move_accum": 0.0,
            "bounce_left": None,  # None finche' non ha ancora rimbalzato
        }
        self.lasers.append(laser)
        self.push_event({
            "kind": "laser_fire", "id": laser["id"], "shooter": shooter.id,
            "x": shooter.x, "y": shooter.y, "dir": shooter.facing,
        })

    def move_lasers(self):
        """Avanza tutti i proiettili laser attivi di un tick. Un proiettile
        elimina QUALSIASI giocatore colpito (protezioni incluse, come il
        vecchio raggio istantaneo) e si estingue sul primo muro incontrato,
        a meno che lo sparatore abbia sbloccato il rimbalzo (bonus 150
        punti): in quel caso rimbalza in una direzione libera scelta a caso
        e prosegue per altre LASER_BOUNCE_DISTANCE celle prima di sparire."""
        if not self.lasers:
            return
        survivors = []
        for lz in self.lasers:
            lz["move_accum"] += LASER_PROJECTILE_SPEED * TICK_DT
            destroyed = False
            while lz["move_accum"] >= 1.0 and not destroyed:
                nx, ny = lz["x"] + lz["dx"], lz["y"] + lz["dy"]
                if is_wall(self.maze, self.maze_w, self.maze_h, nx, ny):
                    shooter = self.players.get(lz["owner"])
                    can_bounce = (
                        shooter is not None and shooter.has_bounce
                        and (lz["bounce_left"] is None or lz["bounce_left"] > 0)
                    )
                    if not can_bounce:
                        destroyed = True
                        break
                    # Sceglie una direzione libera a caso (diversa da quella
                    # che ha appena portato al muro, se possibile).
                    options = []
                    for ddx, ddy in DIRECTIONS.values():
                        if (ddx, ddy) == (-lz["dx"], -lz["dy"]):
                            continue  # evita di tornare indietro sui propri passi
                        tx, ty = lz["x"] + ddx, lz["y"] + ddy
                        if not is_wall(self.maze, self.maze_w, self.maze_h, tx, ty):
                            options.append((ddx, ddy))
                    if not options:
                        # Vicolo cieco: nessuna via libera nemmeno tornando
                        # indietro, il proiettile si estingue qui.
                        destroyed = True
                        break
                    lz["dx"], lz["dy"] = random.choice(options)
                    first_bounce = lz["bounce_left"] is None
                    if first_bounce:
                        lz["bounce_left"] = LASER_BOUNCE_DISTANCE
                        self.push_event({
                            "kind": "laser_bounce", "id": lz["id"],
                            "x": lz["x"], "y": lz["y"],
                        })
                    continue  # riprova subito nella nuova direzione, stesso tick
                # Bonus 2200 punti: un muro di spunzoni AVVERSARIO ferma il
                # proiettile esattamente come un muro vero (niente rimbalzo:
                # gli spunzoni lo "infilzano"). I proiettili del PROPRIETARIO
                # del muro invece lo attraversano liberamente.
                if self.spike_wall_blocking(nx, ny, lz["owner"]) is not None:
                    destroyed = True
                    break
                # Cella libera: avanza di una cella.
                lz["move_accum"] -= 1.0
                lz["x"], lz["y"] = nx, ny
                if lz["bounce_left"] is not None:
                    lz["bounce_left"] -= 1
                victims = [
                    q for q in self.players.values()
                    if q.alive and q.id != lz["owner"] and q.x == nx and q.y == ny
                    and q.ghost_left <= 0 and q.prot_left <= 0
                ]
                if victims:
                    armored = [v for v in victims if v.armor_active]
                    if armored:
                        # Bonus 700 punti: la corazza laser RESPINGE il
                        # colpo invece di subirlo. Il proiettile inverte
                        # direzione e riparte come "sparato" dal portatore
                        # della corazza, quindi puo' colpire chiunque trovi
                        # sulla via del ritorno, compreso lo sparatore
                        # originale. Il portatore della corazza non subisce
                        # alcun danno.
                        lz["dx"], lz["dy"] = -lz["dx"], -lz["dy"]
                        lz["owner"] = armored[0].id
                        self.push_event({
                            "kind": "laser_reflect", "id": lz["id"],
                            "x": nx, "y": ny, "by": armored[0].id,
                        })
                        continue  # riprova subito nella direzione invertita, stesso tick
                    for v in victims:
                        self.kill_player(v, "laser", lz["owner"])
                    destroyed = True
                    break
                # Bonus 900 punti: un colpo laser NEMICO (di un altro
                # giocatore o di un'altra torretta/pet) distrugge il pet che
                # trova sulla sua strada, cosi' come un giocatore.
                pet_victims = [
                    pet for pet in self.pets
                    if pet["owner"] != lz["owner"] and pet["x"] == nx and pet["y"] == ny
                ]
                if pet_victims:
                    for pet in pet_victims:
                        self.destroy_pet(pet, "laser", lz["owner"])
                    destroyed = True
                    break
                # Bonus 1800 punti: il blob gelatinoso e' immune al laser
                # (sia amico che avversario) - l'UNICO modo per rimuoverlo
                # dalla strada resta il bombolone (vedi explode_superbomb).
                if lz["bounce_left"] is not None and lz["bounce_left"] <= 0:
                    destroyed = True
                    break
            if destroyed:
                self.push_event({"kind": "laser_end", "id": lz["id"], "x": lz["x"], "y": lz["y"]})
            else:
                survivors.append(lz)
        self.lasers = survivors

    def try_place_mine(self, player):
        """Bonus 200 punti: sgancia una mina nella cella corrente del
        giocatore (finche' ne ha ancora disponibili). Chiamato dalla
        pressione del tasto "1" lato client.

        Se il giocatore e' intrappolato dalla trappola di un avversario
        (bonus 500 punti), NON puo' usare alcun bonus finche' non torna
        libero di muoversi (vedi player.trapped_left)."""
        if not player.alive or player.trapped_left > 0 or not player.has_mines or player.mines_left <= 0 or player.is_assassin or player.armor_active:
            return
        if any(m["x"] == player.x and m["y"] == player.y for m in self.mines):
            return  # niente due mine sulla stessa cella
        player.mines_left -= 1
        mine = {"id": uuid.uuid4().hex[:8], "owner": player.id, "x": player.x, "y": player.y}
        self.mines.append(mine)
        self.push_event({
            "kind": "mine_place", "id": mine["id"], "player": player.id,
            "x": player.x, "y": player.y, "left": player.mines_left,
        })

    def check_mines(self):
        """Fa esplodere le mine calpestate: elimina chiunque le tocchi
        (proprietario escluso), ignorando protezioni, come il laser. Se sulla
        stessa cella c'e' il pet (bonus 900 punti) di un altro giocatore, la
        mina distrugge anche lui.

        Eccezione: la modalita' ninja (300 punti) rende immuni alle mine,
        esattamente come rende invisibili e letali al tocco - camminarci
        sopra da ninja non la fa esplodere (la mina resta innescata, pronta
        per chi non e' ninja).

        Eccezione 2: chi ha la corazza laser ATTIVA (bonus 700 punti) e'
        immune al contatto con una mina AVVERSARIA - non la fa esplodere e
        non subisce danno, la mina resta sul posto pronta per essere
        disinnescata subito dopo da check_armor_effects (stesso tick)."""
        if not self.mines:
            return
        remaining = []
        for m in self.mines:
            victims = [
                q for q in self.players.values()
                if q.alive and not q.is_assassin and not q.armor_active and q.id != m["owner"]
                and q.ghost_left <= 0 and q.prot_left <= 0
                and q.x == m["x"] and q.y == m["y"]
            ]
            pet_victims = [
                pet for pet in self.pets
                if pet["owner"] != m["owner"] and pet["x"] == m["x"] and pet["y"] == m["y"]
            ]
            if victims or pet_victims:
                for v in victims:
                    self.kill_player(v, "mine", m["owner"])
                for pet in pet_victims:
                    self.destroy_pet(pet, "mine", m["owner"])
                self.push_event({"kind": "mine_boom", "id": m["id"], "x": m["x"], "y": m["y"]})
            else:
                remaining.append(m)
        self.mines = remaining

    def destroy_pet(self, pet, cause, by=None):
        """Unica via per rimuovere un pet (bonus 900 punti) dalla mappa: lo
        toglie da self.pets e notifica i client, esattamente come
        kill_player fa per i giocatori. Il proprietario NON puo' rievocarlo:
        pet_summoned resta True per tutto il resto del round."""
        if pet in self.pets:
            self.pets.remove(pet)
        self.push_event({
            "kind": "pet_destroyed", "id": pet["id"], "owner": pet["owner"],
            "x": pet["x"], "y": pet["y"], "cause": cause, "by": by,
        })

    def pet_public(self, pt):
        """Come Player.to_public: la griglia interna (pt['x']/pt['y'],
        interi) resta l'autorita' per collisioni, ma al client mandiamo
        anche l'avanzamento reale dentro la cella corrente (move_accum,
        verso la prossima cella del percorso), cosi' il pet si muove in
        modo fluido come un giocatore invece di scattare da una cella
        intera alla successiva solo quando il movimento e' completato."""
        dx = dy = 0
        path = pt.get("path")
        if path:
            nx, ny = path[0]
            dx, dy = nx - pt["x"], ny - pt["y"]
        accum = pt.get("move_accum", 0.0)
        fx = pt["x"] + dx * accum
        fy = pt["y"] + dy * accum
        dir_name = next((k for k, v in DIRECTIONS.items() if v == (dx, dy)), None)
        return {
            "id": pt["id"], "x": round(fx, 4), "y": round(fy, 4),
            "owner": pt["owner"], "aim": pt.get("aim"), "dir": dir_name,
        }

    def missile_public(self, mz):
        """Come pet_public: la griglia interna (mz['x']/mz['y'], interi)
        resta l'autorita' per collisioni, ma al client mandiamo anche
        l'avanzamento reale dentro la cella corrente (move_accum, verso la
        prossima cella del percorso) piu' la direzione corrente, cosi' il
        missile si disegna in modo fluido come un giocatore/pet invece di
        scattare da una cella intera alla successiva ad ogni tick."""
        dx = dy = 0
        if mz.get("locked"):
            dx, dy = mz.get("dir", (0, 0))
        else:
            path = mz.get("path")
            if path:
                nx, ny = path[0]
                dx, dy = nx - mz["x"], ny - mz["y"]
        accum = mz.get("move_accum", 0.0)
        fx = mz["x"] + dx * accum
        fy = mz["y"] + dy * accum
        dir_name = next((k for k, v in DIRECTIONS.items() if v == (dx, dy)), None)
        return {
            "id": mz["id"], "x": round(fx, 4), "y": round(fy, 4),
            "owner": mz["owner"], "target": mz["target"], "dir": dir_name,
        }

    def nearest_alive(self, x, y, exclude_ids):
        """Giocatore vivo piu' vicino (distanza Manhattan) a (x, y), tra
        quelli il cui id non e' in exclude_ids. Esclude anche chi e'
        attualmente immune (protezione post-respawn, prot_left > 0): un
        bersaglio invulnerabile non va nemmeno inseguito o preso di mira.
        None se nessuno qualifica."""
        candidates = [
            q for q in self.players.values()
            if q.alive and q.id not in exclude_ids and q.prot_left <= 0
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda q: abs(q.x - x) + abs(q.y - y))

    def nearest_alive_non_ninja(self, x, y, exclude_ids):
        """Come nearest_alive, ma esclude anche chi e' attualmente in
        modalita' ninja (invisibile): usata dal missile guidato, che non
        puo' agganciare un bersaglio che non vede."""
        candidates = [
            q for q in self.players.values()
            if q.alive and not q.is_assassin and q.id not in exclude_ids and q.prot_left <= 0
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda q: abs(q.x - x) + abs(q.y - y))

    # ---- bonus 300 punti: modalita' ninja (tasto "2") ----

    def try_activate_ninja(self, player):
        """Tasto '2': se il bonus e' sbloccato (300 punti) e non e' ancora
        stato usato in questo round, attiva la modalita' ninja per
        SUPER_ASSASSIN_DURATION_SECONDS (10s: invisibile agli altri, 1.1x
        piu' veloce, uccide chiunque tocchi). UTILIZZABILE UNA SOLA VOLTA
        per round: una volta terminata (scaduto il tempo o dopo
        un'eliminazione) non si puo' piu' riattivare, a differenza di
        prima.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi.
        Mentre la modalita' ninja e' attiva, NESSUN altro gadget e'
        utilizzabile (e viceversa, non si puo' attivare il ninja se la
        corazza e' gia' attiva): sono mutuamente esclusivi."""
        if not player.alive or player.trapped_left > 0 or not player.has_ninja or player.is_assassin or player.ninja_used or player.armor_active:
            return
        player.ninja_used = True
        player.is_assassin = True
        player.assassin_left = SUPER_ASSASSIN_DURATION_SECONDS
        self.push_event({
            "kind": "assassin_on", "player": player.id,
            "bonus": "ninja", "points": SUPER_ASSASSIN_THRESHOLD,
        })

    # ---- bonus 700 punti: corazza laser (tasto "6") ----

    def try_activate_armor(self, player):
        """Tasto '6': se il bonus e' sbloccato (700 punti) e non e' ancora
        stato usato in questo round, attiva la corazza laser per
        ARMOR_DURATION_SECONDS: respinge ogni proiettile che la colpisce
        (vedi move_lasers/move_missiles), distrugge le torrette toccate
        (vedi check_armor_effects) e uccide chiunque tocchi (vedi
        check_collisions). Resta visibile a tutti (niente invisibilita').
        UTILIZZABILE UNA SOLA VOLTA per round.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi.
        Mentre la corazza e' attiva, NESSUN altro gadget e' utilizzabile
        (e viceversa, non si puo' attivare la corazza se il ninja e' gia'
        attivo): sono mutuamente esclusivi."""
        if not player.alive or player.trapped_left > 0 or not player.has_armor or player.armor_active or player.armor_used or player.is_assassin:
            return
        player.armor_used = True
        player.armor_active = True
        player.armor_left = ARMOR_DURATION_SECONDS
        self.push_event({
            "kind": "armor_on", "player": player.id,
            "bonus": "armor", "points": ARMOR_THRESHOLD,
        })

    # ---- bonus 800 punti: fulmine (tasto "7") ----

    def try_activate_lightning(self, player):
        """Tasto '7': se il bonus e' sbloccato (800 punti) e non e' ancora
        stato usato in questo round, scatena un fulmine che colpisce
        ISTANTANEAMENTE UN SOLO avversario vivo scelto A CASO tra quelli
        presenti sulla mappa, ovunque si trovi (nessun raggio d'azione, a
        differenza della torretta): perde una vita tramite kill_player (la
        stessa unica via usata da laser/mine/missili/trappola), con lo
        stesso bonus del 10% dei punti guadagnato dal killer (la vittima
        non perde nulla) e lo stesso conteggio
        kill/vita-extra-ogni-2-uccisioni del killer. UTILIZZABILE UNA SOLA
        VOLTA per round.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_lightning or player.lightning_used or player.is_assassin or player.armor_active:
            return
        player.lightning_used = True
        candidates = [
            q for q in self.players.values()
            if q.alive and q.id != player.id
            and q.ghost_left <= 0 and q.prot_left <= 0
        ]
        # NERF: il fulmine ora ha solo 1 possibilita' su 3 di colpire
        # davvero qualcuno (in precedenza colpiva sempre, all'attivazione,
        # un avversario a caso). Se il tiro casuale fallisce, l'uso del
        # bonus viene comunque consumato (lightning_used resta True: e'
        # UTILIZZABILE UNA SOLA VOLTA per round anche in caso di "cilecca"),
        # ma nessuno viene colpito.
        victim = random.choice(candidates) if candidates and random.random() < (1 / 3) else None
        self.push_event({
            "kind": "lightning_on", "player": player.id,
            "bonus": "lightning", "points": LIGHTNING_THRESHOLD,
            "targets": [victim.id] if victim else [],
        })
        if victim is None:
            return
        self.kill_player(victim, "lightning", player.id)
        # Bonus 900 punti: il fulmine distrugge anche il pet dell'unico
        # avversario colpito (il proprio pet, se ne hai uno, resta illeso,
        # e cosi' anche quello degli altri giocatori NON colpiti).
        for pet in [pt for pt in list(self.pets) if pt["owner"] == victim.id]:
            self.destroy_pet(pet, "lightning", player.id)

    # ---- bonus 400 punti: missile guidato (tasto "3") ----

    def try_fire_missile(self, player):
        """Spara un missile (finche' ne restano) verso il nemico piu' vicino
        in questo istante. Il missile e' 'guidato': segue i corridoi via
        pathfinding (vedi move_missiles), non attraversa mai i muri, e si
        aggancia di nuovo al bersaglio piu' vicino se quello originale muore
        prima dell'impatto.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_missile or player.missiles_left <= 0 or player.is_assassin or player.armor_active:
            return
        # Un ninja e' invisibile: il missile non puo' agganciarlo nemmeno al
        # lancio (vedi anche move_missiles per il riaggancio in volo).
        target = self.nearest_alive_non_ninja(player.x, player.y, {player.id})
        if target is None:
            return
        player.missiles_left -= 1
        missile = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "move_accum": 0.0,
            "target": target.id,
            "path": [],
            "retarget_cd": 0.0,
        }
        self.missiles.append(missile)
        self.push_event({
            "kind": "missile_fire", "id": missile["id"], "owner": player.id,
            "x": player.x, "y": player.y, "left": player.missiles_left,
        })

    def move_missiles(self):
        """Avanza tutti i missili in volo di un tick: ognuno ricalcola il
        percorso verso il bersaglio ad intervalli regolari (il bersaglio si
        muove) e procede una cella alla volta lungo quel percorso, quindi non
        si schianta mai contro un muro. Al contatto col bersaglio (o con
        chiunque altro capiti sulla sua strada, escluso lo sparatore),
        quella vittima perde una vita.

        NERF: il missile a ricerca ora e' SCHIVABILE. Appena la distanza
        (Manhattan) dal bersaglio agganciato scende a MISSILE_LOCK_DISTANCE
        (2) caselle o meno, il missile smette per sempre di ricalcolare la
        rotta e prosegue SOLO in linea retta nell'ultima direzione che stava
        percorrendo in quel momento (mz['locked'] = True, direzione
        congelata in mz['dir']): da quel punto in poi non insegue piu' le
        svolte del bersaglio, quindi un giocatore abbastanza lesto a
        cambiare direzione all'ultimo istante puo' schivarlo. Se in quella
        direzione trova un muro, il missile si schianta e si estingue (non
        gira mai attorno all'ostacolo una volta agganciata la rotta finale).
        Il "lock" si azzera solo se il missile perde il bersaglio e si
        riaggancia a uno nuovo (torna a inseguire normalmente finche' non
        rientra di nuovo entro le 2 caselle)."""
        if not self.missiles:
            return
        survivors = []
        for mz in self.missiles:
            destroyed = False
            target = self.players.get(mz["target"])
            # La modalita' ninja (300 punti) rende invisibili: un missile
            # guidato che aveva agganciato un giocatore diventato ninja
            # DEVE perdere il bersaglio (non puo' colpire un nemico che non
            # vede piu') e riagganciarsi subito al nemico vivo, non-ninja,
            # piu' vicino, esattamente come quando il bersaglio originale
            # muore prima dell'impatto.
            if target is None or not target.alive or target.is_assassin:
                target = self.nearest_alive_non_ninja(mz["x"], mz["y"], {mz["owner"]})
                if target is None:
                    destroyed = True
                else:
                    mz["target"] = target.id
                    mz["path"] = []
                    mz["retarget_cd"] = 0.0
                    mz["locked"] = False  # nuovo bersaglio: torna a inseguire normalmente

            if not destroyed:
                # Appena entra entro MISSILE_LOCK_DISTANCE caselle dal
                # bersaglio, congela la rotta una volta per tutte nell'ultima
                # direzione nota, invece di continuare a ricalcolarla.
                if not mz.get("locked") and target is not None \
                        and abs(target.x - mz["x"]) + abs(target.y - mz["y"]) <= MISSILE_LOCK_DISTANCE:
                    mz["locked"] = True
                    if mz.get("path"):
                        lx, ly = mz["path"][0]
                        mz["dir"] = (lx - mz["x"], ly - mz["y"])
                    elif not mz.get("dir"):
                        mz["dir"] = (0, 0)
                    mz["path"] = []

                if mz.get("locked"):
                    dx, dy = mz.get("dir", (0, 0))
                else:
                    mz["retarget_cd"] -= TICK_DT
                    if mz["retarget_cd"] <= 0 or not mz["path"]:
                        mz["retarget_cd"] = MISSILE_RETARGET_SECONDS
                        path = bfs_path(
                            self.maze, self.maze_w, self.maze_h,
                            (mz["x"], mz["y"]), (target.x, target.y),
                        )
                        mz["path"] = path or []

                speed = NORMAL_SPEED * MISSILE_SPEED_MULT
                mz["move_accum"] += speed * TICK_DT
                while mz["move_accum"] >= 1.0 and not destroyed:
                    if mz.get("locked"):
                        dx, dy = mz.get("dir", (0, 0))
                        if (dx, dy) == (0, 0):
                            destroyed = True
                            break
                        nx, ny = mz["x"] + dx, mz["y"] + dy
                        if is_wall(self.maze, self.maze_w, self.maze_h, nx, ny):
                            # Rotta congelata: niente svolte, si schianta
                            # dritto contro l'ostacolo invece di aggirarlo.
                            destroyed = True
                            break
                    else:
                        if not mz["path"]:
                            break
                        nx, ny = mz["path"].pop(0)
                    # Bonus 2200 punti: un muro di spunzoni AVVERSARIO ferma
                    # anche il missile guidato, che ci si schianta contro
                    # come contro un muro vero (bfs_path non lo conosce,
                    # quindi il controllo va fatto qui a ogni passo). I
                    # missili del PROPRIETARIO del muro lo attraversano.
                    if self.spike_wall_blocking(nx, ny, mz["owner"]) is not None:
                        destroyed = True
                        break
                    mz["move_accum"] -= 1.0
                    mz["x"], mz["y"] = nx, ny
                    # Un ninja e' immune: il missile lo attraversa senza
                    # detonare, invece di colpirlo (coerente col riaggancio
                    # automatico al bersaglio piu' vicino non-ninja sopra).
                    victims = [
                        q for q in self.players.values()
                        if q.alive and not q.is_assassin
                        and q.ghost_left <= 0 and q.prot_left <= 0
                        and q.id != mz["owner"] and q.x == nx and q.y == ny
                    ]
                    if victims:
                        armored = [v for v in victims if v.armor_active]
                        other_victims = [v for v in victims if not v.armor_active]
                        if armored:
                            # Bonus 700 punti: la corazza laser respinge
                            # anche il missile guidato (che non puo' essere
                            # rimandato indietro come il laser, dato che e'
                            # a ricerca automatica: viene semplicemente
                            # distrutto all'impatto, senza fare danno).
                            self.push_event({
                                "kind": "missile_reflect", "id": mz["id"],
                                "x": nx, "y": ny, "by": armored[0].id,
                            })
                        for v in other_victims:
                            self.kill_player(v, "missile", mz["owner"])
                        destroyed = True
                    # Bonus 900 punti: il missile guidato distrugge anche il
                    # pet nemico che trova sulla sua strada.
                    pet_victims = [
                        pet for pet in self.pets
                        if pet["owner"] != mz["owner"] and pet["x"] == nx and pet["y"] == ny
                    ]
                    if pet_victims:
                        for pet in pet_victims:
                            self.destroy_pet(pet, "missile", mz["owner"])
                        destroyed = True
                    # Bonus 1800 punti: il blob gelatinoso e' immune al
                    # missile guidato (sia amico che avversario) - l'UNICO
                    # modo per rimuoverlo dalla strada resta il bombolone
                    # (vedi explode_superbomb).

            if destroyed:
                self.push_event({"kind": "missile_end", "id": mz["id"], "x": mz["x"], "y": mz["y"]})
            else:
                survivors.append(mz)
        self.missiles = survivors

    # ---- bonus 500 punti: trappola (tasto "4") ----

    def try_activate_trap(self, player):
        """Tasto '4': un solo tasto per tutto il meccanismo della trappola.

        - Se questo giocatore non ha ancora nessuno intrappolato (o la sua
          vittima precedente e' scappata/scaduta), intrappola SUBITO il
          nemico piu' vicino: resta bloccato sul posto per
          TRAP_DURATION_SECONDS.
        - Se invece ha gia' una vittima intrappolata ed e' abbastanza
          vicino (TRAP_RANGE celle), la fa detonare con una piccola
          esplosione (perde una vita).
        L'INNESCO (intrappolare un nuovo bersaglio) e' limitato a
        TRAP_MAX_USES volte per giocatore, per round: la detonazione di una
        trappola gia' innescata non consuma un uso extra.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus (nemmeno far detonare una sua trappola
        gia' innescata) finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_trap or player.is_assassin or player.armor_active:
            return

        if player.trap_target:
            victim = self.players.get(player.trap_target)
            if victim is not None and victim.alive and victim.trapped_left > 0:
                dist = max(abs(victim.x - player.x), abs(victim.y - player.y))
                if dist <= TRAP_RANGE:
                    self.push_event({"kind": "trap_boom", "x": victim.x, "y": victim.y})
                    # La protezione post-respawn (prot_left) rende immune
                    # anche a una trappola gia' innescata: la vittima
                    # scampa alla detonazione (nearest_alive non la
                    # sceglierebbe piu' come nuovo bersaglio, ma potrebbe
                    # essere respawnata proprio mentre era gia' intrappolata).
                    if victim.prot_left <= 0:
                        self.kill_player(victim, "trap", player.id)
                    player.trap_target = None
                    return
                return
            player.trap_target = None

        # Nessuna vittima attualmente intrappolata: per innescarne una nuova
        # serve almeno un uso residuo (ne restano al massimo TRAP_MAX_USES
        # per round).
        if player.trap_uses_left <= 0:
            return

        target = self.nearest_alive(player.x, player.y, {player.id})
        if target is None:
            return
        player.trap_uses_left -= 1
        target.trapped_left = TRAP_DURATION_SECONDS
        target.trapped_by = player.id
        player.trap_target = target.id
        self.push_event({
            "kind": "trap_start", "player": player.id, "victim": target.id,
            "seconds": TRAP_DURATION_SECONDS, "uses_left": player.trap_uses_left,
        })

    # ---- bonus 600 punti: torretta automatica permanente (tasto "5") ----

    def try_place_turret(self, player):
        """Tasto '5': piazza UNA SOLA VOLTA (per tutto il round) una
        torretta nella cella corrente del giocatore. Da quel momento la
        torretta e' permanente (resta sulla mappa fino a fine round, anche
        se il proprietario muore o si disconnette) e spara da sola verso il
        nemico vivo piu' vicino, con la stessa cadenza del laser (vedi
        update_turrets).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_turret or player.turret_placed or player.is_assassin or player.armor_active:
            return
        player.turret_placed = True
        turret = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "cd": LASER_FIRST_DELAY_SECONDS,
        }
        self.turrets.append(turret)
        self.push_event({
            "kind": "turret_place", "id": turret["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    def try_evolve_turret(self, player):
        """Tasto '9': se il giocatore ha sbloccato il bonus 1000 punti, non
        lo ha ancora usato in questo round, e la sua torretta (bonus 600
        punti) e' ANCORA VIVA sulla mappa (non distrutta dalla corazza di un
        avversario), la fa evolvere in un robot mobile su 3 gambe. Da quel
        momento il robot smette di restare fermo: pattuglia la mappa a caso
        cercando nemici (vedi update_robot_wander), con cadenza di fuoco
        raddoppiata (vedi update_turrets) e velocita' di camminata pari a
        NORMAL_SPEED * ROBOT_SPEED_MULT. Utilizzabile una sola volta per
        round, come la torretta stessa.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_robot or player.robot_used or player.is_assassin or player.armor_active:
            return
        turret = next((t for t in self.turrets if t["owner"] == player.id), None)
        if turret is None:
            # La torretta non e' mai stata piazzata, oppure e' gia' stata
            # distrutta dalla corazza di un avversario: niente da evolvere.
            return
        player.robot_used = True
        turret["evolved"] = True
        turret["level_up_left"] = ROBOT_LEVELUP_DISPLAY_SECONDS
        turret["wander_path"] = []
        turret["wander_cd"] = 0.0
        turret["move_accum"] = 0.0
        self.push_event({
            "kind": "turret_evolve", "id": turret["id"], "player": player.id,
            "x": turret["x"], "y": turret["y"],
        })

    def update_robot_wander(self, t):
        """Bonus 1000 punti: la navicella (torretta evoluta) non pattuglia
        piu' a caso, insegue ATTIVAMENTE il nemico vivo piu' vicino, esatta-
        mente come il missile guidato (vedi move_missiles): ogni
        ROBOT_WANDER_RETARGET_SECONDS ricalcola il percorso verso la
        posizione corrente del bersaglio (che si muove) via bfs_path, quindi
        non attraversa mai i muri, alla velocita' dimezzata
        NORMAL_SPEED * ROBOT_SPEED_MULT. Se al momento non c'e' nessun
        nemico vivo, resta ferma sull'ultimo percorso residuo invece di
        vagare senza meta."""
        target = self.nearest_alive(t["x"], t["y"], {t["owner"]})
        t["wander_cd"] = t.get("wander_cd", 0.0) - TICK_DT
        if target is not None and (t["wander_cd"] <= 0 or not t.get("wander_path")):
            t["wander_cd"] = ROBOT_WANDER_RETARGET_SECONDS
            path = bfs_path(self.maze, self.maze_w, self.maze_h, (t["x"], t["y"]), (target.x, target.y))
            t["wander_path"] = path or []
        speed = NORMAL_SPEED * ROBOT_SPEED_MULT
        t["move_accum"] = t.get("move_accum", 0.0) + speed * TICK_DT
        while t["move_accum"] >= 1.0 and t["wander_path"]:
            t["move_accum"] -= 1.0
            nx, ny = t["wander_path"].pop(0)
            t["x"], t["y"] = nx, ny

    def turret_public(self, t):
        """Come pet_public: se il robot (torretta evoluta) si sta muovendo
        lungo il proprio percorso di pattugliamento, manda anche
        l'avanzamento reale dentro la cella corrente (move_accum), cosi' il
        client lo disegna scivolare in modo fluido invece di scattare da una
        cella intera alla successiva. Una torretta non ancora evoluta resta
        ferma (dx=dy=0), esattamente come prima."""
        dx = dy = 0
        if t.get("evolved") and t.get("wander_path"):
            nx, ny = t["wander_path"][0]
            dx, dy = nx - t["x"], ny - t["y"]
        accum = t.get("move_accum", 0.0)
        fx = t["x"] + dx * accum
        fy = t["y"] + dy * accum
        return {
            "id": t["id"], "x": round(fx, 4), "y": round(fy, 4),
            "owner": t["owner"], "aim": t.get("aim"),
            "evolved": t.get("evolved", False),
            "level_up": t.get("level_up_left", 0) > 0,
        }

    def update_turrets(self):
        """Ogni torretta piazzata spara automaticamente verso il nemico
        vivo piu' vicino ogni TURRET_FIRE_INTERVAL_SECONDS (stessa cadenza
        del laser): riusa esattamente la stessa meccanica dei proiettili
        laser (self.lasers / move_lasers), scegliendo la direzione cardinale
        piu' vicina al bersaglio dato che la torretta non si muove.

        Se la torretta si e' evoluta in robot (bonus 1000 punti, tasto "9"):
        pattuglia la mappa a caso (vedi update_robot_wander) invece di
        restare ferma, e spara con cadenza raddoppiata
        (ROBOT_FIRE_INTERVAL_SECONDS invece di TURRET_FIRE_INTERVAL_SECONDS)."""
        if not self.turrets:
            return
        for t in self.turrets:
            if t.get("level_up_left", 0) > 0:
                t["level_up_left"] = max(0.0, t["level_up_left"] - TICK_DT)
            evolved = t.get("evolved", False)
            if evolved:
                self.update_robot_wander(t)
            # Tracciamento continuo: ad OGNI tick la torretta individua il
            # nemico vivo piu' vicino e, se e' entro TURRET_RANGE_CELLS (10
            # caselle, distanza Manhattan), gli punta contro la canna. La
            # mira (t["aim"]) finisce nello snapshot cosi' il client la
            # disegna che ruota verso il bersaglio in tempo reale.
            target = self.nearest_alive(t["x"], t["y"], {t["owner"]})
            in_range = (
                target is not None
                and abs(target.x - t["x"]) + abs(target.y - t["y"]) <= TURRET_RANGE_CELLS
            )
            t["aim"] = [target.x, target.y] if in_range else None
            t["cd"] -= TICK_DT
            if t["cd"] > 0:
                continue
            if not in_range:
                # Nessuno nel raggio: la torretta resta carica (cd fermo a
                # zero) e spara ISTANTANEAMENTE appena qualcuno entra nelle
                # 10 caselle, invece di sprecare colpi a vuoto.
                t["cd"] = 0.0
                continue
            t["cd"] = ROBOT_FIRE_INTERVAL_SECONDS if evolved else TURRET_FIRE_INTERVAL_SECONDS
            ddx, ddy = target.x - t["x"], target.y - t["y"]
            # Scelta della direzione di fuoco: prima l'asse con lo scarto
            # maggiore, ma se quella canna e' subito contro un muro si prova
            # l'altro asse (il colpo esce nel corridoio libero invece di
            # morire sul muro adiacente).
            cand = []
            horiz = (1, 0) if ddx >= 0 else (-1, 0)
            vert = (0, 1) if ddy >= 0 else (0, -1)
            cand = [horiz, vert] if abs(ddx) >= abs(ddy) else [vert, horiz]
            dx, dy = cand[0]
            if is_wall(self.maze, self.maze_w, self.maze_h, t["x"] + dx, t["y"] + dy) \
                    and not is_wall(self.maze, self.maze_w, self.maze_h,
                                    t["x"] + cand[1][0], t["y"] + cand[1][1]):
                dx, dy = cand[1]
            dir_name = next((k for k, v in DIRECTIONS.items() if v == (dx, dy)), "right")
            laser = {
                "id": uuid.uuid4().hex[:8],
                "owner": t["owner"],
                "x": t["x"], "y": t["y"],
                "dx": dx, "dy": dy,
                "move_accum": 0.0,
                "bounce_left": None,
            }
            self.lasers.append(laser)
            self.push_event({
                "kind": "laser_fire", "id": laser["id"], "shooter": t["owner"],
                "x": t["x"], "y": t["y"], "dir": dir_name, "turret": True,
            })

    # ---- bonus 1200 punti: mortaio (tasto "1") ----

    def try_place_mortar(self, player):
        """Tasto '0': schiera UNA SOLA VOLTA (per tutto il round) un
        mortaio nella cella corrente del giocatore. Da quel momento il
        mortaio e' permanente (resta sulla mappa fino a fine round, anche
        se il proprietario muore o si disconnette) e spara da solo bombe
        ad arco contro il nemico vivo piu' vicino entro MORTAR_RANGE_CELLS
        caselle (vedi update_mortars).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_mortar or player.mortar_placed or player.is_assassin or player.armor_active:
            return
        player.mortar_placed = True
        mortar = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "cd": LASER_FIRST_DELAY_SECONDS,
        }
        self.mortars.append(mortar)
        self.push_event({
            "kind": "mortar_place", "id": mortar["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    # ---- bonus 1400 punti: bombolone ad area (tasto "1", DOPO il mortaio) ----

    def try_place_superbomb(self, player):
        """Tasto '0', RIUSATO: viene chiamato dal dispatch del messaggio
        "place_mortar" solo quando player.mortar_placed e' gia' True (finche'
        il mortaio non e' stato piazzato, quella stessa pressione richiama
        invece try_place_mortar). Piazza un bombolone nella cella corrente
        del giocatore: un ordigno rotondo, grande quanto una casella, dello
        stesso colore del proprietario e visibile a TUTTI. Disponibili
        SUPERBOMB_COUNT bomboloni per round (come le mine): finche'
        player.superbomb_left non arriva a zero, ogni nuova pressione del
        tasto piazza un altro bombolone nella cella corrente. Ognuno resta
        a terra per SUPERBOMB_FUSE_SECONDS, poi esplode (vedi
        update_superbombs/explode_superbomb) con un'onda concentrica che
        distrugge/neutralizza tutto cio' che si trova entro
        SUPERBOMB_RADIUS_CELLS caselle. Solo quando ENTRAMBI i bomboloni
        sono stati piazzati (superbomb_left arriva a 0) la stessa pressione
        del tasto passa allo step successivo della catena (mongolfiera).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_superbomb or player.superbomb_left <= 0 or player.is_assassin or player.armor_active:
            return
        player.superbomb_left -= 1
        bomb = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "t": 0.0,
        }
        self.superbombs.append(bomb)
        self.push_event({
            "kind": "superbomb_place", "id": bomb["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    def superbomb_public(self, bomb):
        return {
            "id": bomb["id"], "x": bomb["x"], "y": bomb["y"],
            "owner": bomb["owner"],
            "fuse_left": round(max(SUPERBOMB_FUSE_SECONDS - bomb["t"], 0), 2),
        }

    def update_superbombs(self):
        """Avanza il conto alla rovescia di ogni bombolone piazzato: dopo
        SUPERBOMB_FUSE_SECONDS esplode (vedi explode_superbomb) e viene
        rimosso dalla mappa. Un bombolone puo' anche venire neutralizzato
        PRIMA che scada la sua miccia, colpito dall'esplosione di un altro
        bombolone o di una bomba di mongolfiera avversari: in quel caso
        viene marcato bomb["destroyed"]=True (vedi explode_superbomb/
        explode_balloon_bomb) e va comunque rimosso qui, senza farlo
        esplodere a sua volta. Si itera su una copia della lista perche'
        neutralizzare un bombolone rivale dentro explode_superbomb
        modifica self.superbombs mentre lo stiamo scorrendo."""
        if not self.superbombs:
            return
        for bomb in list(self.superbombs):
            if bomb.get("destroyed"):
                continue
            bomb["t"] += TICK_DT
            if bomb["t"] >= SUPERBOMB_FUSE_SECONDS:
                bomb["destroyed"] = True
                self.explode_superbomb(bomb)
        self.superbombs = [b for b in self.superbombs if not b.get("destroyed")]

    def explode_superbomb(self, bomb):
        """Esplosione del bombolone (bonus 1400 punti): onda concentrica di
        SUPERBOMB_RADIUS_CELLS caselle (distanza Manhattan) che distrugge o
        neutralizza TUTTO cio' che trova nel raggio, tranne le cose del
        proprietario stesso. E' l'ordigno piu' potente del gioco: a
        differenza della bomba di mongolfiera, non risparmia nessuno:
          - fa perdere una vita a ogni avversario vivo nel raggio (stessa
            immunita' ghost/protezione post-respawn di mortaio/mine/laser);
          - disinnesca ogni mina avversaria nel raggio;
          - distrugge ogni torretta/robot avversario nel raggio;
          - distrugge ogni mortaio avversario nel raggio;
          - distrugge ogni pet avversario nel raggio (vedi destroy_pet);
          - fa esplodere a sua volta (reazione a catena, con onda d'urto
            indipendente) ogni altro bombolone avversario ancora inesploso
            nel raggio - NON lo disinnesca soltanto;
          - fa sganciare a sua volta la propria bomba (reazione a catena,
            con onda d'urto indipendente) a ogni mongolfiera avversaria in
            volo nel raggio - NON la abbatte soltanto;
          - distrugge anche il blob gelatinoso avversario nel raggio (vedi
            destroy_blob): l'UNICO ordigno del gioco capace di farlo. Il
            blob e' immune a qualsiasi altra arma, incluso il fuoco amico
            (un bombolone non tocca mai il proprio blob);
          - distrugge anche ogni Tesla laser avversaria nel raggio: l'UNICA
            arma del gioco capace di abbattere una Tesla, che altrimenti
            resterebbe indistruttibile fino a fine round (la Tesla, dal
            canto suo, non e' invece in grado di fare nulla contro il
            bombolone: vedi tesla_zap)."""
        ox, oy = bomb["x"], bomb["y"]
        owner = bomb["owner"]
        self.push_event({
            "kind": "superbomb_explode", "id": bomb["id"],
            "x": ox, "y": oy, "by": owner, "radius": SUPERBOMB_RADIUS_CELLS,
        })

        victims = [
            p for p in self.players.values()
            if p.alive and p.id != owner
            and p.ghost_left <= 0 and p.prot_left <= 0
            and abs(p.x - ox) + abs(p.y - oy) <= SUPERBOMB_RADIUS_CELLS
        ]
        for victim in victims:
            self.kill_player(victim, "superbomb", shooter_id=owner)

        # Arbusti spinosi avversari (bonus 2800 punti): il bombolone e' una
        # delle tre armi capaci di potarli. Tutte le celle dell'arbusto nel
        # raggio d'urto vengono distrutte; se non resta nulla, l'arbusto e'
        # eliminato del tutto e smette per sempre di crescere.
        for b in list(self.bushes):
            if b["owner"] == owner:
                continue
            hit = [c for c in b["cells"]
                   if abs(c[0] - ox) + abs(c[1] - oy) <= SUPERBOMB_RADIUS_CELLS]
            self.prune_bush_cells(b, hit, owner, "superbomb")

        if self.mines:
            remaining_mines = []
            for m in self.mines:
                if m["owner"] != owner and abs(m["x"] - ox) + abs(m["y"] - oy) <= SUPERBOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "mine_destroyed", "id": m["id"],
                        "x": m["x"], "y": m["y"], "by": owner, "cause": "superbomb",
                    })
                else:
                    remaining_mines.append(m)
            self.mines = remaining_mines

        if self.turrets:
            remaining_turrets = []
            for t in self.turrets:
                if t["owner"] != owner and abs(t["x"] - ox) + abs(t["y"] - oy) <= SUPERBOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "turret_destroyed", "id": t["id"],
                        "x": t["x"], "y": t["y"], "by": owner, "cause": "superbomb",
                        "evolved": t.get("evolved", False),
                    })
                else:
                    remaining_turrets.append(t)
            self.turrets = remaining_turrets

        if self.mortars:
            remaining_mortars = []
            for mt in self.mortars:
                if mt["owner"] != owner and abs(mt["x"] - ox) + abs(mt["y"] - oy) <= SUPERBOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "mortar_destroyed", "id": mt["id"],
                        "x": mt["x"], "y": mt["y"], "by": owner, "cause": "superbomb",
                    })
                else:
                    remaining_mortars.append(mt)
            self.mortars = remaining_mortars

        for pet in list(self.pets):
            if pet["owner"] != owner and abs(pet["x"] - ox) + abs(pet["y"] - oy) <= SUPERBOMB_RADIUS_CELLS:
                self.destroy_pet(pet, "superbomb", owner)

        # Ogni Tesla laser avversaria nel raggio viene distrutta: e' l'UNICA
        # arma del gioco capace di farlo (la Tesla, altrimenti, resterebbe
        # sulla mappa fino a fine round, vedi try_place_tesla/tesla_zap).
        if self.teslas:
            remaining_teslas = []
            for tesla in self.teslas:
                if tesla["owner"] != owner and abs(tesla["x"] - ox) + abs(tesla["y"] - oy) <= SUPERBOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "tesla_destroyed", "id": tesla["id"],
                        "x": tesla["x"], "y": tesla["y"], "by": owner, "cause": "superbomb",
                    })
                else:
                    remaining_teslas.append(tesla)
            self.teslas = remaining_teslas

        # Ogni altro bombolone avversario ancora inesploso nel raggio NON
        # viene "annullato": esplode a sua volta (reazione a catena), con
        # la propria onda d'urto indipendente. Il flag "destroyed" viene
        # marcato PRIMA di richiamare explode_superbomb() su di lui, cosi'
        # da evitare che una catena si richiami all'infinito avanti e
        # indietro tra due bomboloni che si trovano entrambi nel raggio
        # l'uno dell'altro.
        for other in list(self.superbombs):
            if (other is not bomb and other["owner"] != owner and not other.get("destroyed")
                    and abs(other["x"] - ox) + abs(other["y"] - oy) <= SUPERBOMB_RADIUS_CELLS):
                other["destroyed"] = True
                self.explode_superbomb(other)

        # Ogni mongolfiera avversaria in volo nel raggio non viene abbattuta
        # in silenzio: sgancia la propria bomba (con la propria onda
        # d'urto indipendente) esattamente come se il suo timer fosse
        # scaduto in quell'istante, poi esce di scena.
        for bal in list(self.balloons):
            if (bal["owner"] != owner and not bal.get("destroyed")
                    and abs(bal["x"] - ox) + abs(bal["y"] - oy) <= SUPERBOMB_RADIUS_CELLS):
                bal["destroyed"] = True
                self.explode_balloon_bomb(bal)

        # Distrugge anche il blob gelatinoso avversario nel raggio: a
        # differenza della bomba di mongolfiera, il bombolone non risparmia
        # nemmeno il blob (vedi destroy_blob).
        blob_victims = [
            blob for blob in self.blobs
            if blob["owner"] != owner
            and abs(blob["x"] - ox) + abs(blob["y"] - oy) <= SUPERBOMB_RADIUS_CELLS
        ]
        for blob in blob_victims:
            self.destroy_blob(blob, "superbomb", owner)

    # ---- bonus 1600 punti: mongolfiera vagante (tasto "1", DOPO il bombolone) ----

    def try_launch_balloon(self, player):
        """Tasto '0', RIUSATO una terza volta: viene chiamato dal dispatch
        del messaggio "place_mortar" solo quando sia player.mortar_placed
        sia player.superbomb_left <= 0 (bomboloni entrambi piazzati, finche' non lo sono
        entrambi, quella stessa pressione richiama invece
        try_place_mortar/try_place_superbomb). Fa librare in aria, UNA SOLA
        VOLTA per round, DUE mongolfiere (con teschio disegnato sul pallone,
        lato client) che nascono entrambe sulla cella corrente del
        giocatore: da quel momento vagano a caso su TUTTA la mappa (vedi
        update_balloons), volando sopra ogni muro senza alcun bersaglio, e
        sgancia una bomba ogni BALLOON_BOMB_INTERVAL_SECONDS nella propria
        posizione corrente, che esplode ISTANTANEAMENTE (vedi
        explode_balloon_bomb) con un raggio di BALLOON_BOMB_RADIUS_CELLS
        caselle. E' permanente: resta in volo per tutto il resto del round,
        anche se il proprietario muore o si disconnette (come il mortaio).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi.

        Da questo bonus vengono ora fatte librare in aria DUE mongolfiere
        contemporaneamente (invece di una sola), entrambe con teschio
        disegnato sul pallone lato client: nascono nello stesso punto ma
        puntano SUBITO verso due meta' opposte della mappa (scelto a caso
        se la divisione e' sinistra/destra o alto/basso, e a caso quale
        mongolfiera va in quale meta'), cosi' si separano davvero invece
        di rischiare - come con due bersagli scelti a caso senza vincoli -
        di restare vicine per un bel po'. Dopo aver raggiunto questo primo
        bersaglio, vagano di nuovo del tutto a caso su tutta la mappa
        (vedi update_balloons), esattamente come prima."""
        if not player.alive or player.trapped_left > 0 or not player.has_balloon or player.balloon_launched or player.is_assassin or player.armor_active:
            return
        player.balloon_launched = True
        # Scelto UNA SOLA volta per il lancio (non per singola mongolfiera):
        # split_axis decide se la mappa si divide in meta' sinistra/destra
        # ("x") o alto/basso ("y"); flip decide a caso quale delle due
        # mongolfiere finisce in quale meta'.
        split_axis = random.choice(("x", "y"))
        flip = random.random() < 0.5
        for i in range(2):
            half = i if not flip else 1 - i  # 0 = prima meta', 1 = seconda meta'
            if split_axis == "x":
                if half == 0:
                    tx = random.uniform(0, max(self.maze_w / 2 - 1, 0))
                else:
                    tx = random.uniform(self.maze_w / 2, self.maze_w - 1)
                ty = random.uniform(0, self.maze_h - 1)
            else:
                if half == 0:
                    ty = random.uniform(0, max(self.maze_h / 2 - 1, 0))
                else:
                    ty = random.uniform(self.maze_h / 2, self.maze_h - 1)
                tx = random.uniform(0, self.maze_w - 1)
            balloon = {
                "id": uuid.uuid4().hex[:8],
                "owner": player.id,
                "x": float(player.x), "y": float(player.y),
                "tx": tx, "ty": ty,
                "bomb_cd": BALLOON_BOMB_INTERVAL_SECONDS,
            }
            self.balloons.append(balloon)
            self.push_event({
                "kind": "balloon_launch", "id": balloon["id"], "player": player.id,
                "x": player.x, "y": player.y,
            })

    def balloon_public(self, b):
        return {
            "id": b["id"], "x": round(b["x"], 3), "y": round(b["y"], 3),
            "owner": b["owner"],
        }

    def update_balloons(self):
        """Ogni mongolfiera in volo non ha alcun bersaglio: vaga a caso su
        tutta la mappa, scegliendo una nuova meta' casuale (in linea d'aria,
        MAI attraverso bfs_path/corridoi) ogni volta che raggiunge quella
        corrente, esattamente come vola sopra i muri una bomba di mortaio in
        volo. Ogni BALLOON_BOMB_INTERVAL_SECONDS sgancia una bomba nella
        propria posizione attuale (vedi explode_balloon_bomb).

        Una mongolfiera puo' anche venire abbattuta in anticipo da un
        bombolone o da un'altra bomba di mongolfiera avversari: in quel
        caso viene marcata b["destroyed"]=True (vedi explode_superbomb/
        explode_balloon_bomb) e va rimossa qui. Si itera su una copia
        della lista perche' abbattere una mongolfiera rivale dentro
        explode_balloon_bomb modifica self.balloons mentre lo stiamo
        scorrendo."""
        if not self.balloons:
            return
        for b in list(self.balloons):
            if b.get("destroyed"):
                continue
            dx, dy = b["tx"] - b["x"], b["ty"] - b["y"]
            dist = math.hypot(dx, dy)
            if dist <= BALLOON_RETARGET_EPSILON:
                b["tx"] = random.uniform(0, self.maze_w - 1)
                b["ty"] = random.uniform(0, self.maze_h - 1)
            else:
                step = BALLOON_SPEED * TICK_DT
                if step >= dist:
                    b["x"], b["y"] = b["tx"], b["ty"]
                else:
                    b["x"] += dx / dist * step
                    b["y"] += dy / dist * step
            b["bomb_cd"] -= TICK_DT
            if b["bomb_cd"] <= 0:
                b["bomb_cd"] = BALLOON_BOMB_INTERVAL_SECONDS
                self.explode_balloon_bomb(b)
        self.balloons = [bal for bal in self.balloons if not bal.get("destroyed")]

    def explode_balloon_bomb(self, b):
        """Bomba sganciata dalla mongolfiera (bonus 1600 punti): a
        differenza del bombolone NON ha alcuna miccia, esplode
        ISTANTANEAMENTE nel punto di sgancio con un raggio di
        BALLOON_BOMB_RADIUS_CELLS caselle (distanza Manhattan), colpendo
        dall'alto (come l'impatto del mortaio) e neutralizzando tutto cio'
        che trova nel raggio tranne le cose del proprietario stesso:
          - fa perdere una vita a ogni avversario vivo nel raggio (stessa
            immunita' ghost/protezione post-respawn degli altri ordigni);
          - disinnesca ogni mina avversaria nel raggio;
          - distrugge ogni torretta/robot avversario nel raggio;
          - distrugge ogni mortaio avversario nel raggio;
          - distrugge ogni pet avversario nel raggio (vedi destroy_pet);
          - fa esplodere a sua volta (reazione a catena, con onda d'urto
            indipendente) ogni bombolone avversario ancora inesploso nel
            raggio - NON lo disinnesca soltanto;
          - fa sganciare a sua volta la propria bomba (reazione a catena,
            con onda d'urto indipendente) a ogni altra mongolfiera
            avversaria in volo nel raggio - NON la abbatte soltanto.
        Il blob gelatinoso (bonus 1800 punti) e' IMMUNE alla bomba di
        mongolfiera, che lo sorvola senza alcun effetto - resta
        distruttibile solo dal bombolone avversario (vedi
        explode_superbomb), immune a qualsiasi altra arma e al fuoco
        amico."""
        ox, oy = b["x"], b["y"]
        owner = b["owner"]
        self.push_event({
            "kind": "balloon_bomb_drop", "id": b["id"],
            "x": ox, "y": oy, "by": owner, "radius": BALLOON_BOMB_RADIUS_CELLS,
        })

        victims = [
            p for p in self.players.values()
            if p.alive and p.id != owner
            and p.ghost_left <= 0 and p.prot_left <= 0
            and abs(p.x - ox) + abs(p.y - oy) <= BALLOON_BOMB_RADIUS_CELLS
        ]
        for victim in victims:
            self.kill_player(victim, "balloon", shooter_id=owner)

        # Arbusti spinosi avversari (bonus 2800 punti): anche la bomba di
        # mongolfiera li pota, con lo stesso raggio d'urto dell'esplosione.
        for bsh in list(self.bushes):
            if bsh["owner"] == owner:
                continue
            hit = [c for c in bsh["cells"]
                   if abs(c[0] - ox) + abs(c[1] - oy) <= BALLOON_BOMB_RADIUS_CELLS]
            self.prune_bush_cells(bsh, hit, owner, "balloon")

        if self.mines:
            remaining_mines = []
            for m in self.mines:
                if m["owner"] != owner and abs(m["x"] - ox) + abs(m["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "mine_destroyed", "id": m["id"],
                        "x": m["x"], "y": m["y"], "by": owner, "cause": "balloon",
                    })
                else:
                    remaining_mines.append(m)
            self.mines = remaining_mines

        if self.turrets:
            remaining_turrets = []
            for t in self.turrets:
                if t["owner"] != owner and abs(t["x"] - ox) + abs(t["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "turret_destroyed", "id": t["id"],
                        "x": t["x"], "y": t["y"], "by": owner, "cause": "balloon",
                        "evolved": t.get("evolved", False),
                    })
                else:
                    remaining_turrets.append(t)
            self.turrets = remaining_turrets

        if self.mortars:
            remaining_mortars = []
            for mt in self.mortars:
                if mt["owner"] != owner and abs(mt["x"] - ox) + abs(mt["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS:
                    self.push_event({
                        "kind": "mortar_destroyed", "id": mt["id"],
                        "x": mt["x"], "y": mt["y"], "by": owner, "cause": "balloon",
                    })
                else:
                    remaining_mortars.append(mt)
            self.mortars = remaining_mortars

        for pet in list(self.pets):
            if pet["owner"] != owner and abs(pet["x"] - ox) + abs(pet["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS:
                self.destroy_pet(pet, "balloon", owner)

        # Ogni bombolone avversario ancora inesploso nel raggio NON viene
        # "annullato": esplode a sua volta (reazione a catena), con la
        # propria onda d'urto indipendente.
        for other in list(self.superbombs):
            if (other["owner"] != owner and not other.get("destroyed")
                    and abs(other["x"] - ox) + abs(other["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS):
                other["destroyed"] = True
                self.explode_superbomb(other)

        # Ogni altra mongolfiera avversaria in volo nel raggio non viene
        # abbattuta in silenzio: sgancia a sua volta la propria bomba
        # (reazione a catena), poi esce di scena.
        for bal in list(self.balloons):
            if (bal is not b and bal["owner"] != owner and not bal.get("destroyed")
                    and abs(bal["x"] - ox) + abs(bal["y"] - oy) <= BALLOON_BOMB_RADIUS_CELLS):
                bal["destroyed"] = True
                self.explode_balloon_bomb(bal)

        # NOTA: il blob (bonus 1800 punti) e' volutamente escluso qui -
        # resta immune alla bomba di mongolfiera (vedi docstring sopra).

    # ---- bonus 1800 punti: blob gelatinoso (tasto "1", DOPO la mongolfiera) ----

    def try_place_blob(self, player):
        """Tasto '1', RIUSATO una quarta volta: viene chiamato dal dispatch
        del messaggio "place_mortar" solo quando player.mortar_placed,
        player.superbomb_left <= 0 e player.balloon_launched sono gia' tutti e
        tre True (finche' non lo sono tutti, quella stessa pressione
        richiama invece try_place_mortar/try_place_superbomb/
        try_launch_balloon). Piazza UNA SOLA VOLTA (per tutto il round) un
        blob gelatinoso nella cella corrente del giocatore, in mezzo alla
        strada: un omino di gelatina colante, immobile, dello stesso colore
        del proprietario e visibile a TUTTI. Da quel momento blocca quella
        cella e "mangia" (fa perdere una vita) chiunque non sia il
        proprietario ci passi sopra (vedi check_blobs) - senza pero'
        consumarsi: resta li' pronto a mangiare anche il prossimo che ci
        passa, finche' qualcuno non gli spara (vedi move_lasers/
        move_missiles).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_blob or player.blob_placed or player.is_assassin or player.armor_active:
            return
        player.blob_placed = True
        blob = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
        }
        self.blobs.append(blob)
        self.push_event({
            "kind": "blob_place", "id": blob["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    def blob_public(self, b):
        # Come turret_public/pet_public: se il blob e' "vivo" (bonus 2000
        # punti) e si sta muovendo lungo il proprio percorso di
        # vagabondaggio, manda anche l'avanzamento reale dentro la cella
        # corrente (move_accum), cosi' il client lo disegna scivolare in
        # modo fluido invece di scattare da una cella intera alla
        # successiva. Un blob non ancora risvegliato resta fermo (dx=dy=0),
        # esattamente come prima.
        dx = dy = 0
        if b.get("alive") and b.get("wander_path"):
            nx, ny = b["wander_path"][0]
            dx, dy = nx - b["x"], ny - b["y"]
        accum = b.get("move_accum", 0.0)
        fx = b["x"] + dx * accum
        fy = b["y"] + dy * accum
        return {
            "id": b["id"], "x": round(fx, 4), "y": round(fy, 4),
            "owner": b["owner"],
            "alive": b.get("alive", False),
        }

    def check_blobs(self):
        """Fa "mangiare" al blob chiunque si trovi sulla sua cella o su una
        cella adiacente (entro BLOB_EAT_RANGE_CELLS caselle, stile
        scacchi/Chebyshev): elimina chiunque sia abbastanza vicino
        (proprietario escluso), ignorando protezioni, come una mina. A
        differenza della mina pero' il blob NON si consuma mangiando: resta
        sulla mappa (fermo, o vagante se risvegliato dal bonus 2000 punti,
        vedi update_blobs_wander) pronto a mangiare il prossimo che gli
        capita vicino. Se una cella adiacente ospita il pet (bonus 900
        punti) di un altro giocatore, il blob mangia anche lui (ma il pet,
        a differenza del giocatore, sparisce per sempre).

        Eccezione: la modalita' ninja (300 punti) rende immuni al blob,
        esattamente come alle mine - stare vicino da ninja non attiva
        nulla.

        Eccezione 2: chi ha la corazza laser ATTIVA (bonus 700 punti) e'
        immune al contatto con un blob AVVERSARIO, come con una mina: non
        viene mangiato, ma (a differenza della mina) il contatto non
        distrugge nemmeno il blob - l'unico modo per rimuoverlo resta
        sparargli."""
        if not self.blobs:
            return
        for b in self.blobs:
            victims = [
                q for q in self.players.values()
                if q.alive and not q.is_assassin and not q.armor_active and q.id != b["owner"]
                and q.ghost_left <= 0 and q.prot_left <= 0
                and abs(q.x - b["x"]) <= BLOB_EAT_RANGE_CELLS
                and abs(q.y - b["y"]) <= BLOB_EAT_RANGE_CELLS
            ]
            pet_victims = [
                pet for pet in self.pets
                if pet["owner"] != b["owner"]
                and abs(pet["x"] - b["x"]) <= BLOB_EAT_RANGE_CELLS
                and abs(pet["y"] - b["y"]) <= BLOB_EAT_RANGE_CELLS
            ]
            for v in victims:
                self.kill_player(v, "blob", b["owner"])
            for pet in pet_victims:
                self.destroy_pet(pet, "blob", b["owner"])

    # ---- bonus 2000 punti: blob VIVO/vagante (tasto "1", DOPO il blob fermo) ----

    def try_animate_blob(self, player):
        """Tasto '1', RIUSATO una quinta volta: viene chiamato dal dispatch
        del messaggio "place_mortar" solo quando player.mortar_placed,
        player.superbomb_left <= 0, player.balloon_launched e
        player.blob_placed sono gia' tutti e quattro True (finche' non lo
        sono, quella stessa pressione richiama invece
        try_place_mortar/try_place_superbomb/try_launch_balloon/
        try_place_blob). Risveglia, UNA SOLA VOLTA per round, il blob gia'
        piazzato da questo giocatore - a patto che sia ancora vivo sulla
        mappa (non distrutto nel frattempo da un laser/missile avversario,
        vedi destroy_blob). Da quel momento il blob smette di restare
        fermo: vaga a caso per tutta la mappa (vedi update_blobs_wander)
        alla stessa velocita' della torretta evoluta, lasciando una scia di
        gas velenoso su ogni casella che calpesta camminando.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_blob_alive or player.blob_alive_used or player.is_assassin or player.armor_active:
            return
        blob = next((b for b in self.blobs if b["owner"] == player.id), None)
        if blob is None:
            # Il blob non e' mai stato piazzato, oppure e' gia' stato
            # distrutto da un laser/missile avversario: niente da risvegliare.
            return
        player.blob_alive_used = True
        blob["alive"] = True
        blob["wander_path"] = []
        blob["wander_cd"] = 0.0
        blob["move_accum"] = 0.0
        self.push_event({
            "kind": "blob_animate", "id": blob["id"], "player": player.id,
            "x": blob["x"], "y": blob["y"],
        })

    def update_blob_wander(self, b):
        """Bonus 2000 punti: un blob risvegliato (vedi try_animate_blob) non
        pattuglia verso un bersaglio come il robot: vaga DAVVERO a caso su
        tutta la mappa, scegliendo ogni volta una cella libera a caso tra
        self.free_cells come nuova meta' e raggiungendola via bfs_path (mai
        attraverso i muri), alla velocita' NORMAL_SPEED * BLOB_ALIVE_SPEED_MULT
        (identica alla torretta evoluta). Ogni volta che entra in una nuova
        casella vi lascia a terra una nuvola di gas velenoso larga UNA SOLA
        casella (raggio 0, a differenza di quella - piu' larga - lasciata
        dagli impatti del mortaio) che dura BLOB_POISON_DURATION_SECONDS
        (vedi update_poison_zones, che gestisce entrambe le nuvole allo
        stesso modo)."""
        if not b.get("wander_path"):
            target = random.choice(self.free_cells)
            path = bfs_path(self.maze, self.maze_w, self.maze_h, (b["x"], b["y"]), target)
            b["wander_path"] = path or []
        speed = NORMAL_SPEED * BLOB_ALIVE_SPEED_MULT
        b["move_accum"] = b.get("move_accum", 0.0) + speed * TICK_DT
        while b["move_accum"] >= 1.0 and b["wander_path"]:
            b["move_accum"] -= 1.0
            nx, ny = b["wander_path"].pop(0)
            b["x"], b["y"] = nx, ny
            poison = {
                "id": uuid.uuid4().hex[:8],
                "owner": b["owner"],
                "x": nx, "y": ny,
                "left": BLOB_POISON_DURATION_SECONDS,
                "tick_cd": POISON_TICK_SECONDS,
                "radius": 0,  # solo la casella calpestata, non un'area come l'impatto del mortaio
            }
            self.poison_zones.append(poison)
            self.push_event({
                "kind": "poison_spawn", "id": poison["id"],
                "x": nx, "y": ny,
                "duration": BLOB_POISON_DURATION_SECONDS, "radius": 0,
            })

    def update_blobs_wander(self):
        """Fa avanzare, ogni tick, tutti i blob "vivi" (bonus 2000 punti,
        vedi update_blob_wander). I blob non ancora risvegliati restano
        fermi come prima: non vengono toccati qui."""
        if not self.blobs:
            return
        for b in self.blobs:
            if b.get("alive"):
                self.update_blob_wander(b)

    def destroy_blob(self, blob, cause, by=None):
        """Unica via per rimuovere un blob dalla mappa: colpito dal
        bombolone (bonus 1400 punti) di un giocatore AVVERSARIO (vedi
        explode_superbomb). Il blob e' immune al fuoco amico e a
        qualsiasi altra arma o esplosione del gioco (laser, missile
        guidato, mortaio, mina, fulmine, mongolfiera, torretta...)."""
        if blob in self.blobs:
            self.blobs.remove(blob)
        self.push_event({
            "kind": "blob_destroyed", "id": blob["id"],
            "x": blob["x"], "y": blob["y"], "by": by, "cause": cause,
        })

    # ---- bonus 2200 punti: muro di spunzoni (tasto "1", DOPO il risveglio del blob) ----

    def try_place_spike_wall(self, player):
        """Tasto '1', RIUSATO una sesta volta: viene chiamato dal dispatch
        del messaggio "place_mortar" solo quando TUTTA la catena precedente
        del tasto "1" e' esaurita (mortaio, bombolone, mongolfiera, blob e
        risveglio del blob - o blob ormai distrutto, che rende il risveglio
        impossibile per sempre). Piazza UNA SOLA VOLTA (per tutto il round)
        un blocco di muro grande esattamente quanto un muro normale (una
        casella) nella cella corrente del giocatore: da quel momento, per
        tutto il round, quella cella e' un muro di
        spunzoni acuminati che SOLO il proprietario e i suoi gadget possono
        attraversare. Qualsiasi avversario che ci sbatte contro MUORE
        all'impatto (vedi update_spike_walls), i proiettili avversari
        (laser/missili) si schiantano come contro un muro vero (vedi
        move_lasers/move_missiles) e pet/torrette-navicella avversari che
        lo toccano vengono distrutti. Il muro e' PERMANENTE: non si
        sgretola piu' da solo.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_spike_wall or player.spike_wall_placed or player.is_assassin or player.armor_active:
            return
        player.spike_wall_placed = True
        # Il muro va ESATTAMENTE sulla casella in cui il giocatore si
        # trova in questo istante. player.x/player.y e' pero' la cella di
        # PARTENZA mentre si sta scivolando verso la successiva
        # (move_accum > 0): a meta' scivolamento il personaggio appare
        # gia' nella cella dopo, e il muro finirebbe visivamente alle sue
        # spalle. Si usa quindi la posizione frazionaria reale (la stessa
        # di to_public) arrotondata alla cella piu' vicina: la cella di
        # destinazione e' per costruzione libera (il movimento scivola
        # solo verso celle aperte), quindi l'arrotondamento e' sicuro.
        dx, dy = DIRECTIONS.get(player.direction, (0, 0)) if player.direction else (0, 0)
        wx = int(round(player.x + dx * player.move_accum))
        wy = int(round(player.y + dy * player.move_accum))
        wall = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": wx, "y": wy,
        }
        self.spike_walls.append(wall)
        self.push_event({
            "kind": "spike_wall_place", "id": wall["id"], "player": player.id,
            "x": wx, "y": wy,
        })

    def spike_wall_public(self, w):
        return {
            "id": w["id"], "x": w["x"], "y": w["y"],
            "owner": w["owner"],
        }

    def spike_wall_blocking(self, x, y, owner_id):
        """Ritorna il muro di spunzoni presente nella cella (x, y) che
        BLOCCA chi appartiene a `owner_id` (cioe' un muro piazzato da un
        ALTRO giocatore), oppure None. Il proprietario e tutti i suoi
        gadget attraversano liberamente i propri muri, quindi per loro
        questa funzione ritorna sempre None."""
        for w in self.spike_walls:
            if w["x"] == x and w["y"] == y and w["owner"] != owner_id:
                return w
        return None

    def update_spike_walls(self):
        """Fa avanzare, ogni tick, tutti i muri di spunzoni piazzati
        (bonus 2200 punti):
          - uccide all'impatto QUALSIASI giocatore avversario che prova ad
            attraversarlo (via kill_player, causa "spike_wall"): il
            contatto si misura sulla posizione frazionaria reale (cella +
            move_accum), cosi' la morte scatta appena il personaggio tocca
            gli spunzoni, non solo a cella completata. Ninja e corazza NON
            proteggono (sono spunzoni fisici, non un'arma respingibile);
            l'unica eccezione e' la protezione post-respawn (prot_left),
            per evitare morti a catena a chi e' appena rinato;
          - distrugge pet e torrette/navicelle avversari che finiscono
            sulla cella del muro (i gadget del PROPRIETARIO invece lo
            attraversano liberamente, come richiesto)."""
        if not self.spike_walls:
            return
        for w in list(self.spike_walls):
            # PERMANENTE: nessun conto alla rovescia, il muro resta in
            # piedi per tutto il round (puo' abbatterlo solo un fulmine di
            # Tesla avversaria o un fungo atomico).
            wx, wy = w["x"], w["y"]
            owner = w["owner"]
            # Giocatori avversari all'impatto: posizione frazionaria reale
            # (stessa formula di to_public), cosi' il contatto scatta
            # appena si tocca la superficie del muro.
            for q in self.players.values():
                if not q.alive or q.id == owner or q.prot_left > 0:
                    continue
                dx, dy = DIRECTIONS.get(q.direction, (0, 0)) if q.direction else (0, 0)
                fx = q.x + dx * q.move_accum
                fy = q.y + dy * q.move_accum
                if abs(fx - wx) < SPIKE_WALL_HIT_RANGE and abs(fy - wy) < SPIKE_WALL_HIT_RANGE:
                    self.kill_player(q, "spike_wall", shooter_id=owner)
            # Pet avversari che toccano il muro: distrutti.
            for pet in [pt for pt in self.pets if pt["owner"] != owner
                        and pt["x"] == wx and pt["y"] == wy]:
                self.destroy_pet(pet, "spike_wall", owner)
            # Torrette/navicelle avversarie sulla cella del muro (una
            # navicella mobile puo' finirci sopra camminando): distrutte.
            doomed_turrets = [t for t in self.turrets if t["owner"] != owner
                              and t["x"] == wx and t["y"] == wy]
            for t in doomed_turrets:
                self.turrets.remove(t)
                self.push_event({
                    "kind": "turret_destroyed", "id": t["id"],
                    "x": t["x"], "y": t["y"], "by": owner,
                    "cause": "spike_wall",
                    "evolved": t.get("evolved", False),
                })

    # ---- bonus 2400 punti: Tesla laser (tasto "1", DOPO il muro di spunzoni) ----

    def try_place_tesla(self, player):
        """Tasto '1', RIUSATO una settima volta: viene chiamato dal
        dispatch del messaggio "place_mortar" solo quando TUTTA la catena
        precedente del tasto "1" e' esaurita (mine, mortaio, bombolone,
        mongolfiera, blob, risveglio del blob e muro di spunzoni). Piazza
        UNA SOLA VOLTA (per tutto il round) una Tesla laser nella cella
        corrente del giocatore: una torre fissa in stile "Tesla" di Clash
        Royale, grande quanto una sola casella ma visivamente PIU' ALTA di
        un muro normale. Da quel momento resta sulla mappa fino a fine
        round (anche se il proprietario muore o si disconnette, esattamente
        come torretta/mortaio/pet) - A MENO CHE un bombolone avversario non
        esploda nel suo raggio: e' l'UNICA arma del gioco capace di
        distruggere una Tesla (vedi explode_superbomb). Finche' resta in
        piedi fulmina da sola, ogni TESLA_FIRE_INTERVAL_SECONDS, TUTTO cio'
        che appartiene alla squadra avversaria entro TESLA_RANGE_CELLS
        caselle, IGNORANDO i muri della mappa (vedi update_teslas/tesla_zap)
        - CON L'ECCEZIONE del bombolone, che la Tesla non e' in grado di
        toccare (ne' di far esplodere, ne' di disinnescare).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_tesla or player.tesla_placed or player.is_assassin or player.armor_active:
            return
        player.tesla_placed = True
        tesla = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "cd": TESLA_FIRE_INTERVAL_SECONDS,
        }
        self.teslas.append(tesla)
        self.push_event({
            "kind": "tesla_place", "id": tesla["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    def tesla_public(self, t):
        return {
            "id": t["id"], "x": t["x"], "y": t["y"], "owner": t["owner"],
            # Frazione di carica (0 -> 1) verso il prossimo fulmine: il
            # client la usa per animare la bobina che pulsa sempre piu'
            # in fretta man mano che si ricarica, esattamente come la
            # mira continua della torretta (t["aim"]).
            "charge": round(max(0.0, 1 - t["cd"] / TESLA_FIRE_INTERVAL_SECONDS), 2),
        }

    def update_teslas(self):
        """Ogni Tesla piazzata (bonus 2400 punti) fulmina automaticamente,
        ogni TESLA_FIRE_INTERVAL_SECONDS, TUTTO cio' che appartiene alla
        squadra avversaria entro TESLA_RANGE_CELLS caselle (distanza
        Manhattan): a differenza della torretta normale non spara un
        proiettile che vola, puo' mancare il bersaglio o schiantarsi sui
        muri, ma colpisce ISTANTANEAMENTE e ad AREA (vedi tesla_zap),
        ignorando i muri della mappa esattamente come l'esplosione del
        bombolone o della bomba di mongolfiera - solo che, a differenza di
        quelle, non si esaurisce mai: resta li' a ripetere il colpo per
        tutto il resto del round."""
        if not self.teslas:
            return
        for t in self.teslas:
            t["cd"] -= TICK_DT
            if t["cd"] > 0:
                continue
            t["cd"] = TESLA_FIRE_INTERVAL_SECONDS
            self.tesla_zap(t)

    def tesla_zap(self, t):
        """Il fulmine vero e proprio di una Tesla: colpisce, tutti insieme
        nello stesso istante, TUTTI i bersagli avversari entro
        TESLA_RANGE_CELLS caselle (distanza Manhattan) dalla torre,
        IGNORANDO i muri della mappa (la Tesla e' piu' alta e spara "da
        sopra"), esattamente come fa il bombolone alla sua esplosione:
          - fa perdere una vita a ogni avversario vivo nel raggio (stessa
            immunita' ghost/protezione post-respawn delle altre armi);
          - disinnesca ogni mina avversaria nel raggio;
          - distrugge ogni torretta/robot avversario nel raggio;
          - distrugge ogni mortaio avversario nel raggio;
          - distrugge ogni pet avversario nel raggio (vedi destroy_pet);
          - fa sganciare a sua volta la propria bomba (reazione a catena,
            con onda d'urto indipendente) a ogni mongolfiera avversaria in
            volo nel raggio;
          - distrugge anche il blob gelatinoso avversario nel raggio (vedi
            destroy_blob), come solo il bombolone sapeva fare finora;
          - sgretola anche ogni muro di spunzoni avversario nel raggio.
        NON tocca invece il bombolone (non lo fa esplodere ne' lo
        disinnesca): e' l'UNICA minaccia avversaria a cui la Tesla non puo'
        fare nulla - e non a caso, visto che e' il bombolone stesso l'UNICA
        arma capace di distruggere una Tesla (vedi explode_superbomb).
        Manda un unico evento "tesla_fire" con la lista di tutte le celle
        colpite, cosi' il client disegna un fulmine separato dalla torre
        verso OGNUNO dei bersagli nello stesso istante (niente evento se
        non c'era nessun nemico nel raggio: la torre resta silenziosa)."""
        ox, oy = t["x"], t["y"]
        owner = t["owner"]
        hits = []

        victims = [
            p for p in self.players.values()
            if p.alive and p.id != owner
            and p.ghost_left <= 0 and p.prot_left <= 0
            and abs(p.x - ox) + abs(p.y - oy) <= TESLA_RANGE_CELLS
        ]
        for victim in victims:
            hits.append([victim.x, victim.y])
            self.kill_player(victim, "tesla", shooter_id=owner)

        if self.mines:
            remaining_mines = []
            for m in self.mines:
                if m["owner"] != owner and abs(m["x"] - ox) + abs(m["y"] - oy) <= TESLA_RANGE_CELLS:
                    hits.append([m["x"], m["y"]])
                    self.push_event({
                        "kind": "mine_destroyed", "id": m["id"],
                        "x": m["x"], "y": m["y"], "by": owner, "cause": "tesla",
                    })
                else:
                    remaining_mines.append(m)
            self.mines = remaining_mines

        if self.turrets:
            remaining_turrets = []
            for tt in self.turrets:
                if tt["owner"] != owner and abs(tt["x"] - ox) + abs(tt["y"] - oy) <= TESLA_RANGE_CELLS:
                    hits.append([tt["x"], tt["y"]])
                    self.push_event({
                        "kind": "turret_destroyed", "id": tt["id"],
                        "x": tt["x"], "y": tt["y"], "by": owner, "cause": "tesla",
                        "evolved": tt.get("evolved", False),
                    })
                else:
                    remaining_turrets.append(tt)
            self.turrets = remaining_turrets

        if self.mortars:
            remaining_mortars = []
            for mt in self.mortars:
                if mt["owner"] != owner and abs(mt["x"] - ox) + abs(mt["y"] - oy) <= TESLA_RANGE_CELLS:
                    hits.append([mt["x"], mt["y"]])
                    self.push_event({
                        "kind": "mortar_destroyed", "id": mt["id"],
                        "x": mt["x"], "y": mt["y"], "by": owner, "cause": "tesla",
                    })
                else:
                    remaining_mortars.append(mt)
            self.mortars = remaining_mortars

        for pet in list(self.pets):
            if pet["owner"] != owner and abs(pet["x"] - ox) + abs(pet["y"] - oy) <= TESLA_RANGE_CELLS:
                hits.append([pet["x"], pet["y"]])
                self.destroy_pet(pet, "tesla", owner)

        # Nota: a differenza di torretta/mortaio/mina/pet/blob/mongolfiera,
        # il bombolone avversario NON viene toccato dalla Tesla (non lo fa
        # esplodere ne' lo disinnesca): e' l'unica minaccia a cui la Tesla
        # e' "cieca" (vedi docstring sopra) - non a caso e' anche l'unica
        # arma capace di distruggerla (vedi explode_superbomb).

        for bal in list(self.balloons):
            if (bal["owner"] != owner and not bal.get("destroyed")
                    and abs(bal["x"] - ox) + abs(bal["y"] - oy) <= TESLA_RANGE_CELLS):
                hits.append([bal["x"], bal["y"]])
                bal["destroyed"] = True
                self.explode_balloon_bomb(bal)

        blob_victims = [
            blob for blob in self.blobs
            if blob["owner"] != owner
            and abs(blob["x"] - ox) + abs(blob["y"] - oy) <= TESLA_RANGE_CELLS
        ]
        for blob in blob_victims:
            hits.append([blob["x"], blob["y"]])
            self.destroy_blob(blob, "tesla", owner)

        if self.spike_walls:
            remaining_walls = []
            for w in self.spike_walls:
                if w["owner"] != owner and abs(w["x"] - ox) + abs(w["y"] - oy) <= TESLA_RANGE_CELLS:
                    hits.append([w["x"], w["y"]])
                    self.push_event({
                        "kind": "spike_wall_expired", "id": w["id"],
                        "x": w["x"], "y": w["y"],
                    })
                else:
                    remaining_walls.append(w)
            self.spike_walls = remaining_walls

        if hits:
            self.push_event({
                "kind": "tesla_fire", "id": t["id"], "player": owner,
                "x": ox, "y": oy, "targets": hits, "radius": TESLA_RANGE_CELLS,
            })

    # ---- bonus 2600 punti: trappola territoriale a spunzoni (tasto "1", DOPO la Tesla) ----

    def try_use_territory_trap(self, player):
        """Tasto '1', RIUSATO un'ottava volta: viene chiamato dal dispatch
        del messaggio "place_mortar" solo quando TUTTA la catena precedente
        del tasto "1" e' esaurita (mine, mortaio, bombolone, mongolfiera,
        blob, risveglio del blob, muro di spunzoni e Tesla). Un solo tasto
        per tutto il meccanismo, esattamente come la trappola normale
        (bonus 500 punti, vedi try_activate_trap):
          - PRIMA pressione: avvia la fase di selezione. Da questo momento
            (vedi update_territory_marking, chiamato ad ogni tick) ogni
            cella di strada NON ancora marcata che il giocatore calpesta
            si illumina del suo colore - ma SOLO ai suoi occhi, tramite un
            evento privato (push_private_event) mai incluso nello stato
            pubblico: l'avversario non ha modo di scoprire in anticipo
            dove scattera' la trappola.
          - Una volta completata la selezione (TERRITORY_TILES_REQUIRED
            caselle marcate), la pressione SUCCESSIVA innesca la trappola
            vera e propria (vedi trigger_territory_trap): a quel punto il
            bonus e' consumato, UNA SOLA VOLTA per round.
        Se la selezione e' gia' in corso, una nuova pressione non fa
        nulla: si completa da sola camminando, non serve ripremere.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if (not player.alive or player.trapped_left > 0 or not player.has_territory_trap
                or player.territory_used or player.is_assassin or player.armor_active):
            return
        if player.territory_ready:
            self.trigger_territory_trap(player)
            return
        if player.territory_marking:
            return  # selezione gia' in corso: si completa da sola camminando
        player.territory_marking = True
        player.territory_tiles = set()
        self.push_private_event(player.id, {
            "kind": "territory_start", "required": TERRITORY_TILES_REQUIRED,
        })

    def update_territory_marking(self):
        """Ad ogni tick, per ogni giocatore con la selezione in corso
        (bonus 2600 punti), marca la cella corrente se non era gia' stata
        marcata: notifica SOLO il proprietario (evento privato), mai
        pubblicamente, cosi' gli avversari non possono scoprire dove sta
        preparando la trappola. Al raggiungimento di
        TERRITORY_TILES_REQUIRED celle nuove, chiude da sola la selezione
        (le celle restano illuminate, sempre solo per il proprietario,
        finche' non arriva la seconda pressione del tasto "1", vedi
        try_use_territory_trap)."""
        for p in self.players.values():
            if not p.territory_marking or not p.alive:
                continue
            cell = (p.x, p.y)
            if cell in p.territory_tiles:
                continue
            p.territory_tiles.add(cell)
            remaining = TERRITORY_TILES_REQUIRED - len(p.territory_tiles)
            self.push_private_event(p.id, {
                "kind": "territory_tile", "x": p.x, "y": p.y,
                "remaining": max(0, remaining),
            })
            if len(p.territory_tiles) >= TERRITORY_TILES_REQUIRED:
                p.territory_marking = False
                p.territory_ready = True
                self.push_private_event(p.id, {"kind": "territory_ready"})

    def trigger_territory_trap(self, player):
        """La SECONDA pressione del tasto "1" (bonus 2600 punti): da OGNI
        cella marcata durante la selezione sputano all'istante spunzoni
        acuminati dal pavimento, nel colore del proprietario, che uccidono
        chiunque - avversario vivo, senza protezioni attive (ghost/post-
        respawn) - si trovi sopra in quel preciso momento, esattamente
        come un'esplosione istantanea ad area (vedi tesla_zap/
        explode_superbomb) ma sagomata sulle caselle scelte invece che su
        un raggio. A differenza della Tesla o del muro di spunzoni non e'
        permanente: e' un colpo secco, UNA SOLA VOLTA per round, poi lo
        stesso tasto resta muto per il resto della partita. L'evento
        "territory_trigger" e', stavolta, PUBBLICO: e' proprio nell'istante
        dell'attivazione che la trappola si rivela a tutti, non prima."""
        player.territory_ready = False
        player.territory_used = True
        tiles = list(player.territory_tiles)
        hits = []
        for q in self.players.values():
            if (q.alive and q.id != player.id and q.ghost_left <= 0 and q.prot_left <= 0
                    and (q.x, q.y) in player.territory_tiles):
                hits.append(q.id)
                self.kill_player(q, "territory_trap", shooter_id=player.id)
        player.territory_tiles = set()
        self.push_event({
            "kind": "territory_trigger", "player": player.id,
            "tiles": [[x, y] for (x, y) in tiles], "hits": hits,
        })

    # ---- bonus 2800 punti: arbusto spinoso (tasto "1", DOPO la trappola territoriale) ----

    def try_place_bush(self, player):
        """Tasto '1', RIUSATO un'ennesima volta: viene chiamato dal
        dispatch del messaggio "place_mortar" solo quando TUTTA la catena
        precedente del tasto "1" e' esaurita (mine, mortaio, bombolone,
        mongolfiera, blob, risveglio del blob, muro di spunzoni, Tesla e
        trappola territoriale gia' consumata). Piazza UNA SOLA VOLTA (per
        tutto il round) un piccolo arbusto spinoso, del colore del
        proprietario, nella cella corrente: da quel momento l'arbusto
        UCCIDE AL CONTATTO qualsiasi avversario (vedi update_bushes) e
        ogni BUSH_GROW_INTERVAL_SECONDS (1 minuto) si espande TUTTO
        INTORNO A SE', occupando in un colpo solo le caselle adiacenti
        (anche in diagonale) a quelle gia' occupate (1 casella -> 3x3 ->
        5x5 -> ...), inghiottendo anche i muri, fino a un massimo di
        BUSH_MAX_EXPANSIONS anelli di crescita, dopodiche' smette di
        espandersi ma resta comunque letale finche' l'arbusto non viene
        eliminato del tutto (bombolone, bomba di mongolfiera o corazza).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_bush or player.bush_placed or player.is_assassin or player.armor_active:
            return
        player.bush_placed = True
        bush = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "cells": [(player.x, player.y)],
            # Conto alla rovescia per la PROSSIMA espansione ad anello.
            "grow_left": BUSH_GROW_INTERVAL_SECONDS,
            # Quante espansioni ad anello ha gia' fatto (max BUSH_MAX_EXPANSIONS).
            "expansions": 0,
        }
        self.bushes.append(bush)
        self.push_event({
            "kind": "bush_place", "id": bush["id"], "player": player.id,
            "x": player.x, "y": player.y,
            "grow_interval": BUSH_GROW_INTERVAL_SECONDS,
        })

    def bush_public(self, b):
        return {
            "id": b["id"], "owner": b["owner"],
            "cells": [[c[0], c[1]] for c in b["cells"]],
            "grow_left": round(b["grow_left"], 1),
        }

    def grow_bush(self, b):
        """Fa espandere l'arbusto TUTTO INTORNO A SE' in un colpo solo:
        ogni casella gia' occupata si allarga simultaneamente sulle 8
        caselle adiacenti (comprese le diagonali) non ancora occupate e
        dentro il bordo esterno della mappa, formando un anello
        concentrico che si allarga di volta in volta (1 casella -> 3x3 ->
        5x5 -> ...). La crescita e' CIECA rispetto ai muri: l'arbusto li
        inghiotte (la cella-muro resta invalicabile sotto i rami, ma viene
        ricoperta; se poi quella cella viene potata, il muro riappare).
        Per ogni nuova casella viene comunque emesso un evento bush_grow
        separato (stesso schema di prima), cosi' il client continua ad
        animare la crescita casella per casella invece che di colpo."""
        occupied = set(b["cells"])
        new_cells = set()
        for (cx, cy) in b["cells"]:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    # Il bordo esterno (cornice della mappa) resta intoccabile:
                    # inghiottirlo aprirebbe "buchi" verso il nulla.
                    if 1 <= nx <= self.maze_w - 2 and 1 <= ny <= self.maze_h - 2 \
                            and (nx, ny) not in occupied:
                        new_cells.add((nx, ny))
        if not new_cells:
            return
        for (nx, ny) in sorted(new_cells):
            b["cells"].append((nx, ny))
            self.push_event({
                "kind": "bush_grow", "id": b["id"], "x": nx, "y": ny,
                "owner": b["owner"], "cells": len(b["cells"]),
            })

    def prune_bush_cells(self, b, cells, by, cause):
        """Rimuove dall'arbusto le celle indicate (potatura da bombolone,
        bomba di mongolfiera o corazza). Se non resta piu' nulla,
        l'arbusto e' eliminato DEL TUTTO e smette (ovviamente) di
        crescere: evento bush_destroyed dedicato per suono/effetto."""
        if not cells:
            return
        removed = [c for c in b["cells"] if c in set(cells)]
        if not removed:
            return
        b["cells"] = [c for c in b["cells"] if c not in set(removed)]
        self.push_event({
            "kind": "bush_cells_destroyed", "id": b["id"],
            "cells": [[c[0], c[1]] for c in removed],
            "by": by, "cause": cause, "owner": b["owner"],
        })
        if not b["cells"] and b in self.bushes:
            self.bushes.remove(b)
            self.push_event({
                "kind": "bush_destroyed", "id": b["id"],
                "by": by, "cause": cause, "owner": b["owner"],
            })

    def update_bushes(self):
        """Fa avanzare, ogni tick, tutti gli arbusti spinosi piazzati
        (bonus 2800 punti):
          - scala il conto alla rovescia della crescita: ogni minuto
            un'espansione ad anello tutto intorno a se' (vedi grow_bush),
            fino a un massimo di BUSH_MAX_EXPANSIONS volte, dopodiche'
            smette di crescere ma resta comunque letale;
          - uccide all'impatto QUALSIASI giocatore avversario che tocca
            una casella dell'arbusto (via kill_player, causa "bush"):
            stesso contatto frazionario del muro di spunzoni. Il ninja
            NON protegge (sono spine fisiche); la protezione post-respawn
            (prot_left) e il ghost si';
          - ECCEZIONE corazza/scudo: un avversario con la corazza attiva
            che tocca una casella dell'arbusto la SPEZZA invece di morire
            (vedi prune_bush_cells): e' una delle tre armi - con bombolone
            e bomba di mongolfiera - capaci di distruggere l'arbusto;
          - distrugge pet e torrette/navicelle avversari che finiscono su
            una casella dell'arbusto (i gadget del PROPRIETARIO invece lo
            attraversano liberamente)."""
        if not self.bushes:
            return
        for b in list(self.bushes):
            if b.get("expansions", 0) < BUSH_MAX_EXPANSIONS:
                b["grow_left"] -= TICK_DT
                if b["grow_left"] <= 0:
                    b["grow_left"] += BUSH_GROW_INTERVAL_SECONDS
                    self.grow_bush(b)
                    b["expansions"] = b.get("expansions", 0) + 1
            owner = b["owner"]
            cells = set(b["cells"])
            # Giocatori avversari all'impatto (posizione frazionaria
            # reale, come per il muro di spunzoni).
            for q in self.players.values():
                if not q.alive or q.id == owner or q.prot_left > 0 or q.ghost_left > 0:
                    continue
                dx, dy = DIRECTIONS.get(q.direction, (0, 0)) if q.direction else (0, 0)
                fx = q.x + dx * q.move_accum
                fy = q.y + dy * q.move_accum
                touched = [c for c in b["cells"]
                           if abs(fx - c[0]) < BUSH_HIT_RANGE and abs(fy - c[1]) < BUSH_HIT_RANGE]
                if not touched:
                    continue
                if q.armor_active:
                    # Lo scudo spezza i rami toccati invece di far morire.
                    self.prune_bush_cells(b, touched, q.id, "armor")
                else:
                    self.kill_player(q, "bush", shooter_id=owner)
            # Pet avversari su una casella dell'arbusto: distrutti.
            for pet in [pt for pt in self.pets if pt["owner"] != owner
                        and (pt["x"], pt["y"]) in cells]:
                self.destroy_pet(pet, "bush", owner)
            # Torrette/navicelle avversarie su una casella dell'arbusto.
            doomed_turrets = [t for t in self.turrets if t["owner"] != owner
                              and (t["x"], t["y"]) in cells]
            for t in doomed_turrets:
                self.turrets.remove(t)
                self.push_event({
                    "kind": "turret_destroyed", "id": t["id"],
                    "x": t["x"], "y": t["y"], "by": owner,
                    "cause": "bush",
                    "evolved": t.get("evolved", False),
                })

    # ---- bonus 3000 punti: fungo atomico (tasto "1", DOPO l'arbusto spinoso) ----

    def try_place_mushroom(self, player):
        """Tasto '1', RIUSATO come VERO ultimo gradino della catena: viene
        chiamato dal dispatch del messaggio "place_mortar" solo quando
        TUTTA la catena precedente e' esaurita (arbusto spinoso compreso).
        Piazza UNA SOLA VOLTA (per tutto il round) un piccolo fungo
        atomico, del colore del proprietario, nella cella corrente: resta
        a terra come una mina, visibile agli avversari solo entro
        MUSHROOM_VISIBILITY_RANGE caselle (lato client), finche' un
        avversario (o un suo pet) non lo CALPESTA facendolo esplodere
        (vedi update_mushrooms/explode_mushroom).

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_mushroom or player.mushroom_placed or player.is_assassin or player.armor_active:
            return
        player.mushroom_placed = True
        m = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
        }
        self.mushrooms.append(m)
        self.push_event({
            "kind": "mushroom_place", "id": m["id"], "player": player.id,
            "x": m["x"], "y": m["y"],
        })

    def mushroom_public(self, m):
        return {"id": m["id"], "x": m["x"], "y": m["y"], "owner": m["owner"]}

    def update_mushrooms(self):
        """Fa esplodere i funghi atomici CALPESTATI: come per le mine, il
        contatto e' sulla cella esatta. Lo innescano un avversario vivo
        (corazza e ninja NON lo disinnescano: lo fanno esplodere comunque,
        il fungo distrugge tutto; solo ghost e protezione post-respawn
        non lo innescano) oppure un pet avversario che ci passa sopra."""
        if not self.mushrooms:
            return
        for m in list(self.mushrooms):
            stepped = any(
                q.alive and q.id != m["owner"]
                and q.ghost_left <= 0 and q.prot_left <= 0
                and q.x == m["x"] and q.y == m["y"]
                for q in self.players.values()
            ) or any(
                pet["owner"] != m["owner"] and pet["x"] == m["x"] and pet["y"] == m["y"]
                for pet in self.pets
            )
            if stepped:
                self.mushrooms.remove(m)
                self.explode_mushroom(m)

    def explode_mushroom(self, m):
        """Esplosione del fungo atomico (bonus 3000 punti): l'ordigno piu'
        devastante del gioco. Con un GROSSO BOATO uccide e distrugge
        LETTERALMENTE TUTTO cio' che si trova entro
        MUSHROOM_BLAST_RADIUS_CELLS caselle (distanza Manhattan), tranne
        le cose del proprietario:
          - fa perdere una vita a ogni avversario vivo nel raggio (corazza
            e ninja NON proteggono; ghost e protezione post-respawn si');
          - distrugge mine, torrette/robot, mortai, pet avversari;
          - fa esplodere a catena bomboloni avversari e fa sganciare a
            catena le bombe delle mongolfiere avversarie;
          - distrugge blob, muri di spunzoni, Tesla e arbusti spinosi
            avversari (pota TUTTE le celle dell'arbusto nel raggio);
          - lascia sull'epicentro un'area concentrica AVVELENATA di pari
            raggio per MUSHROOM_POISON_DURATION_SECONDS (1 minuto), con la
            stessa logica del veleno del mortaio (una vita di danno al
            secondo, vedi update_poison_zones) ma nel COLORE del
            proprietario (flag "atomic" nella zona).
        Il client, all'evento mushroom_explode, disegna la classica nube a
        fungo gassosa nel colore del proprietario per
        MUSHROOM_CLOUD_SECONDS (2 secondi)."""
        ox, oy = m["x"], m["y"]
        owner = m["owner"]
        self.push_event({
            "kind": "mushroom_explode", "id": m["id"],
            "x": ox, "y": oy, "by": owner,
            "radius": MUSHROOM_BLAST_RADIUS_CELLS,
            "cloud": MUSHROOM_CLOUD_SECONDS,
        })

        # Giocatori: corazza e ninja NON proteggono.
        victims = [
            p for p in self.players.values()
            if p.alive and p.id != owner
            and p.ghost_left <= 0 and p.prot_left <= 0
            and abs(p.x - ox) + abs(p.y - oy) <= MUSHROOM_BLAST_RADIUS_CELLS
        ]
        for victim in victims:
            self.kill_player(victim, "mushroom", shooter_id=owner)

        # Mine avversarie: distrutte.
        remaining_mines = []
        for mn in self.mines:
            if mn["owner"] != owner and abs(mn["x"] - ox) + abs(mn["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.push_event({
                    "kind": "mine_destroyed", "id": mn["id"],
                    "x": mn["x"], "y": mn["y"], "by": owner, "cause": "mushroom",
                })
            else:
                remaining_mines.append(mn)
        self.mines = remaining_mines

        # Torrette/robot avversari: distrutti.
        remaining_turrets = []
        for t in self.turrets:
            if t["owner"] != owner and abs(t["x"] - ox) + abs(t["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.push_event({
                    "kind": "turret_destroyed", "id": t["id"],
                    "x": t["x"], "y": t["y"], "by": owner, "cause": "mushroom",
                    "evolved": t.get("evolved", False),
                })
            else:
                remaining_turrets.append(t)
        self.turrets = remaining_turrets

        # Mortai avversari: distrutti.
        remaining_mortars = []
        for mt in self.mortars:
            if mt["owner"] != owner and abs(mt["x"] - ox) + abs(mt["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.push_event({
                    "kind": "mortar_destroyed", "id": mt["id"],
                    "x": mt["x"], "y": mt["y"], "by": owner, "cause": "mushroom",
                })
            else:
                remaining_mortars.append(mt)
        self.mortars = remaining_mortars

        # Pet avversari: distrutti.
        for pet in list(self.pets):
            if pet["owner"] != owner and abs(pet["x"] - ox) + abs(pet["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.destroy_pet(pet, "mushroom", owner)

        # Bomboloni avversari inesplosi: esplosione a catena.
        for other in list(self.superbombs):
            if (other["owner"] != owner and not other.get("destroyed")
                    and abs(other["x"] - ox) + abs(other["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS):
                other["destroyed"] = True
                self.explode_superbomb(other)

        # Mongolfiere avversarie in volo: sgancio a catena.
        for bal in list(self.balloons):
            if (bal["owner"] != owner and not bal.get("destroyed")
                    and abs(bal["x"] - ox) + abs(bal["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS):
                bal["destroyed"] = True
                self.explode_balloon_bomb(bal)

        # Blob avversari: distrutti.
        for blob in list(self.blobs):
            if blob["owner"] != owner and abs(blob["x"] - ox) + abs(blob["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.destroy_blob(blob, "mushroom", owner)

        # Muri di spunzoni avversari: sgretolati (stesso evento del
        # fulmine di Tesla, riusato dal client per suono/effetto).
        remaining_walls = []
        for w in self.spike_walls:
            if w["owner"] != owner and abs(w["x"] - ox) + abs(w["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.push_event({
                    "kind": "spike_wall_expired", "id": w["id"],
                    "x": w["x"], "y": w["y"],
                })
            else:
                remaining_walls.append(w)
        self.spike_walls = remaining_walls

        # Tesla avversarie: distrutte.
        remaining_teslas = []
        for tesla in self.teslas:
            if tesla["owner"] != owner and abs(tesla["x"] - ox) + abs(tesla["y"] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS:
                self.push_event({
                    "kind": "tesla_destroyed", "id": tesla["id"],
                    "x": tesla["x"], "y": tesla["y"], "by": owner, "cause": "mushroom",
                })
            else:
                remaining_teslas.append(tesla)
        self.teslas = remaining_teslas

        # Arbusti spinosi avversari: pota TUTTE le celle nel raggio.
        for bsh in list(self.bushes):
            if bsh["owner"] == owner:
                continue
            hit = [c for c in bsh["cells"]
                   if abs(c[0] - ox) + abs(c[1] - oy) <= MUSHROOM_BLAST_RADIUS_CELLS]
            self.prune_bush_cells(bsh, hit, owner, "mushroom")

        # Area concentrica avvelenata: stessa logica del veleno del
        # mortaio (update_poison_zones), ma raggio 10, durata 1 minuto e
        # flag "atomic" per farla colorare del colore del proprietario.
        poison = {
            "id": uuid.uuid4().hex[:8],
            "owner": owner,
            "x": ox, "y": oy,
            "left": MUSHROOM_POISON_DURATION_SECONDS,
            "tick_cd": POISON_TICK_SECONDS,
            "radius": MUSHROOM_BLAST_RADIUS_CELLS,
            "atomic": True,
        }
        self.poison_zones.append(poison)
        self.push_event({
            "kind": "poison_spawn", "id": poison["id"],
            "x": ox, "y": oy,
            "duration": MUSHROOM_POISON_DURATION_SECONDS,
            "radius": MUSHROOM_BLAST_RADIUS_CELLS,
            "atomic": True,
        })

    def mortar_public(self, mt):
        return {
            "id": mt["id"], "x": mt["x"], "y": mt["y"],
            "owner": mt["owner"], "aim": mt.get("aim"),
        }

    def bomb_public(self, bomb):
        """Posizione "in volo" della bomba: interpolazione lineare (in
        linea d'aria, NON sui corridoi) tra il punto di lancio e il punto
        di impatto, in base alla frazione di tempo di volo trascorsa. Serve
        al client per disegnare l'arco e l'ombra proiettata a terra."""
        frac = min(1.0, bomb["t"] / bomb["duration"]) if bomb["duration"] > 0 else 1.0
        fx = bomb["x0"] + (bomb["x1"] - bomb["x0"]) * frac
        fy = bomb["y0"] + (bomb["y1"] - bomb["y0"]) * frac
        return {
            "id": bomb["id"], "x": round(fx, 4), "y": round(fy, 4),
            "tx": bomb["x1"], "ty": bomb["y1"], "owner": bomb["owner"],
            "frac": round(frac, 4),
        }

    def update_mortars(self):
        """Ogni mortaio schierato individua ad OGNI tick il nemico vivo
        piu' vicino e, se e' entro MORTAR_RANGE_CELLS (15 caselle,
        distanza Manhattan), gli spara contro una bomba ogni
        MORTAR_FIRE_INTERVAL_SECONDS (cadenza piu' lenta della torretta,
        e' un'arma d'area molto piu' potente). La bomba non segue i
        corridoi come laser/missili: vola in linea retta SOPRA la mappa
        (vedi update_bombs/bomb_public), scavalcando qualsiasi muro, e
        ricade esplodendo sul bersaglio."""
        if not self.mortars:
            return
        for mt in self.mortars:
            target = self.nearest_alive(mt["x"], mt["y"], {mt["owner"]})
            in_range = (
                target is not None
                and abs(target.x - mt["x"]) + abs(target.y - mt["y"]) <= MORTAR_RANGE_CELLS
            )
            mt["aim"] = [target.x, target.y] if in_range else None
            mt["cd"] -= TICK_DT
            if mt["cd"] > 0:
                continue
            if not in_range:
                # Nessuno nel raggio: il mortaio resta carico (cd fermo a
                # zero) e spara ISTANTANEAMENTE appena qualcuno entra nelle
                # 15 caselle, invece di sprecare colpi a vuoto.
                mt["cd"] = 0.0
                continue
            mt["cd"] = MORTAR_FIRE_INTERVAL_SECONDS
            dist = abs(target.x - mt["x"]) + abs(target.y - mt["y"])
            duration = max(0.15, dist * MORTAR_FLIGHT_SECONDS_PER_CELL)
            bomb = {
                "id": uuid.uuid4().hex[:8],
                "owner": mt["owner"],
                "x0": mt["x"], "y0": mt["y"],
                "x1": target.x, "y1": target.y,
                "t": 0.0, "duration": duration,
            }
            self.bombs.append(bomb)
            self.push_event({
                "kind": "mortar_fire", "id": bomb["id"], "shooter": mt["owner"],
                "x0": mt["x"], "y0": mt["y"], "x1": target.x, "y1": target.y,
                "duration": duration,
            })

    def update_bombs(self):
        """Avanza il tempo di volo di ogni bomba in aria; quando raggiunge
        il punto di impatto esplode (vedi land_bomb) e viene rimossa."""
        if not self.bombs:
            return
        remaining = []
        for bomb in self.bombs:
            bomb["t"] += TICK_DT
            if bomb["t"] >= bomb["duration"]:
                self.land_bomb(bomb)
            else:
                remaining.append(bomb)
        self.bombs = remaining

    def land_bomb(self, bomb):
        """Impatto di una bomba di mortaio (bonus 1200 punti): colpendo
        dall'alto non le importa cosa c'e' nella cella (muro compreso), e
        fa perdere una vita a chiunque si trovi entro MORTAR_BLAST_RADIUS_CELLS
        caselle (distanza Manhattan) dal punto di impatto - un solo colpo
        puo' quindi coinvolgere piu' avversari vicini tra loro. Come per il
        tocco del ninja/corazza, la protezione post-respawn resta immune.

        Oltre al colpo diretto, sul punto di impatto resta a terra una
        nuvola di gas velenoso (vedi update_poison_zones) che continua a
        fare danno ad area nel tempo per POISON_DURATION_SECONDS."""
        self.push_event({
            "kind": "mortar_impact", "id": bomb["id"],
            "x": bomb["x1"], "y": bomb["y1"], "by": bomb["owner"],
        })
        victims = [
            p for p in self.players.values()
            if p.alive and p.id != bomb["owner"]
            and p.ghost_left <= 0 and p.prot_left <= 0
            and abs(p.x - bomb["x1"]) + abs(p.y - bomb["y1"]) <= MORTAR_BLAST_RADIUS_CELLS
        ]
        for victim in victims:
            self.kill_player(victim, "mortar", shooter_id=bomb["owner"])

        # NERF pet: l'impatto del mortaio distrugge anche il pet AVVERSARIO
        # (non il proprio) che si trova nel raggio dello scoppio, come gia'
        # succede con mine/missili/torrette.
        for pet in list(self.pets):
            if pet["owner"] != bomb["owner"] \
                    and abs(pet["x"] - bomb["x1"]) + abs(pet["y"] - bomb["y1"]) <= MORTAR_BLAST_RADIUS_CELLS:
                self.destroy_pet(pet, "mortar", bomb["owner"])

        poison = {
            "id": uuid.uuid4().hex[:8],
            "owner": bomb["owner"],
            "x": bomb["x1"], "y": bomb["y1"],
            "left": POISON_DURATION_SECONDS,
            "tick_cd": POISON_TICK_SECONDS,
            "radius": POISON_RADIUS_CELLS,
        }
        self.poison_zones.append(poison)
        self.push_event({
            "kind": "poison_spawn", "id": poison["id"],
            "x": poison["x"], "y": poison["y"],
            "duration": POISON_DURATION_SECONDS, "radius": POISON_RADIUS_CELLS,
        })

    def update_poison_zones(self):
        """Avanza ogni nuvola velenosa lasciata a terra: dagli impatti del
        mortaio (raggio POISON_RADIUS_CELLS, dura POISON_DURATION_SECONDS)
        o dalla scia del blob vivo (bonus 2000 punti: raggio 0, una sola
        casella, dura BLOB_POISON_DURATION_SECONDS, vedi
        update_blob_wander). Ogni POISON_TICK_SECONDS toglie una vita a
        chiunque (avversario del proprietario) si trovi ancora entro il
        raggio proprio di QUELLA nuvola dal centro, esattamente come il
        colpo diretto (stessa immunita' di ghost/protezione post-respawn).
        Ciascuna nuvola svanisce da sola scaduto il proprio tempo."""
        if not self.poison_zones:
            return
        remaining = []
        for pz in self.poison_zones:
            pz["left"] -= TICK_DT
            pz["tick_cd"] -= TICK_DT
            if pz["tick_cd"] <= 0:
                pz["tick_cd"] += POISON_TICK_SECONDS
                radius = pz.get("radius", POISON_RADIUS_CELLS)
                victims = [
                    p for p in self.players.values()
                    if p.alive and p.id != pz["owner"]
                    and p.ghost_left <= 0 and p.prot_left <= 0
                    and abs(p.x - pz["x"]) + abs(p.y - pz["y"]) <= radius
                ]
                for victim in victims:
                    self.kill_player(victim, "poison", shooter_id=pz["owner"])
            if pz["left"] > 0:
                remaining.append(pz)
            else:
                self.push_event({"kind": "poison_expire", "id": pz["id"]})
        self.poison_zones = remaining

    def check_armor_effects(self):
        """Bonus 700 punti: chi ha la corazza laser ATTIVA distrugge ogni
        torretta AVVERSARIA (di un altro giocatore, NON la propria) la cui
        cella tocca - normale O evoluta (robot mobile, bonus 1000 punti)
        indifferentemente - e allo stesso modo disinnesca ogni mina
        AVVERSARIA calpestata (anche qui, la propria resta intatta)."""
        armored = [p for p in self.players.values() if p.alive and p.armor_active]
        if not armored:
            return
        if self.turrets:
            remaining = []
            for t in self.turrets:
                # La corazza laser distrugge la torretta a contatto sia
                # nella sua versione normale sia in quella evoluta (robot
                # mobile, bonus 1000 punti): il resto della logica
                # (mine/mortai/pet) resta invariato.
                destroyer = next(
                    (a for a in armored if a.id != t["owner"] and a.x == t["x"] and a.y == t["y"]),
                    None,
                )
                if destroyer is not None:
                    self.push_event({
                        "kind": "turret_destroyed", "id": t["id"],
                        "x": t["x"], "y": t["y"], "by": destroyer.id,
                        "evolved": t.get("evolved", False),
                    })
                else:
                    remaining.append(t)
            self.turrets = remaining
        if self.mines:
            remaining_mines = []
            for m in self.mines:
                destroyer = next(
                    (a for a in armored if a.id != m["owner"] and a.x == m["x"] and a.y == m["y"]),
                    None,
                )
                if destroyer is not None:
                    self.push_event({
                        "kind": "mine_destroyed", "id": m["id"],
                        "x": m["x"], "y": m["y"], "by": destroyer.id,
                    })
                else:
                    remaining_mines.append(m)
            self.mines = remaining_mines
        if self.mortars:
            remaining_mortars = []
            for mt in self.mortars:
                destroyer = next(
                    (a for a in armored if a.id != mt["owner"] and a.x == mt["x"] and a.y == mt["y"]),
                    None,
                )
                if destroyer is not None:
                    self.push_event({
                        "kind": "mortar_destroyed", "id": mt["id"],
                        "x": mt["x"], "y": mt["y"], "by": destroyer.id,
                    })
                else:
                    remaining_mortars.append(mt)
            self.mortars = remaining_mortars
        # Bonus 900 punti: chi ha la corazza distrugge ogni pet AVVERSARIO
        # (di un altro giocatore, NON il proprio) la cui cella tocca, stessa
        # regola di mine/torrette/mortai qui sopra. NOTA: il proprietario va
        # escluso (a.id != pet["owner"]), altrimenti il proprio pet, che
        # nasce esattamente sulla cella del giocatore, si autodistrugge
        # all'istante se la corazza e' gia' attiva al momento dell'evocazione
        # (bug corretto: il pet "spariva" prima ancora di essere visibile).
        if self.pets:
            for pet in list(self.pets):
                destroyer = next(
                    (a for a in armored if a.id != pet["owner"] and a.x == pet["x"] and a.y == pet["y"]),
                    None,
                )
                if destroyer is not None:
                    self.destroy_pet(pet, "armor", destroyer.id)

    # ---- bonus 900 punti: pet fedele permanente (tasto "8") ----

    def try_summon_pet(self, player):
        """Tasto '8': evoca UNA SOLA VOLTA (per tutto il round) un piccolo
        Pac-Man "pet" dello stesso colore del proprietario, nella sua cella
        corrente. Da quel momento il pet e' permanente: segue il
        proprietario per tutto il resto del round (anche se muore e
        respawna altrove) finche' non aggancia un nemico entro
        PET_RANGE_CELLS caselle, nel qual caso lo insegue attivamente fino
        al contatto (vedi update_pets), finche' non viene distrutto (vedi
        check_mines/move_missiles/move_lasers/try_activate_lightning/
        check_armor_effects): a quel punto sparisce per il resto del round
        e NON si puo' rievocare.

        Se il giocatore e' intrappolato dalla trappola di un avversario,
        NON puo' usare alcun bonus finche' non torna libero di muoversi."""
        if not player.alive or player.trapped_left > 0 or not player.has_pet or player.pet_summoned or player.is_assassin or player.armor_active:
            return
        player.pet_summoned = True
        pet = {
            "id": uuid.uuid4().hex[:8],
            "owner": player.id,
            "x": player.x, "y": player.y,
            "move_accum": 0.0,
            "path": [],
            "retarget_cd": 0.0,
            "target_id": None,
            "aim": None,
        }
        self.pets.append(pet)
        self.push_event({
            "kind": "pet_summon", "id": pet["id"], "player": player.id,
            "x": player.x, "y": player.y,
        })

    def update_pets(self):
        """NERF: il pet non uccide piu' al contatto. Ora spara come una
        piccola torretta mobile (riusa esattamente la meccanica dei
        proiettili laser di self.lasers, stessa di torretta/laser
        principale) contro il nemico vivo piu' vicino entro PET_RANGE_CELLS
        (6) caselle, con la stessa cadenza della torretta - e puo' farlo in
        QUALSIASI direzione lo separi dal bersaglio, anche mentre si sta
        muovendo nello stesso istante.

        NERF: il pet NON si allontana piu' dal proprietario per inseguire un
        nemico: il suo movimento segue SEMPRE e SOLO il proprietario
        (fermandosi entro PET_STAY_RANGE caselle da lui), esattamente come
        quando non aveva alcun bersaglio agganciato. Il fuoco e' del tutto
        indipendente dal movimento.

        Il pet resta vulnerabile come prima a mine/missili
        guidati/torrette/mortai/fulmine/corazza/bombolone/mongolfiera/blob
        (vedi check_mines/move_missiles/move_lasers/land_bomb/
        try_activate_lightning/check_armor_effects/explode_superbomb/...)."""
        if not self.pets:
            return
        for pet in list(self.pets):
            owner = self.players.get(pet["owner"])
            if owner is None:
                continue

            # ---- mantenimento/selezione del bersaglio da COLPIRE (non da inseguire) ----
            target = self.players.get(pet.get("target_id"))
            if target is not None and not target.alive:
                target = None
            if target is None:
                pet["target_id"] = None
                candidate = self.nearest_alive(pet["x"], pet["y"], {pet["owner"]})
                if candidate is not None and \
                        abs(candidate.x - pet["x"]) + abs(candidate.y - pet["y"]) <= PET_RANGE_CELLS:
                    target = candidate
                    pet["target_id"] = candidate.id
            pet["aim"] = [target.x, target.y] if target is not None else None

            # ---- movimento: segue SEMPRE e SOLO il proprietario, mai un nemico ----
            chase_x, chase_y = owner.x, owner.y
            stay_range = PET_STAY_RANGE
            dist = abs(chase_x - pet["x"]) + abs(chase_y - pet["y"])
            if dist > stay_range:
                pet["retarget_cd"] -= TICK_DT
                if pet["retarget_cd"] <= 0 or not pet["path"]:
                    pet["retarget_cd"] = PET_RETARGET_SECONDS
                    path = bfs_path(
                        self.maze, self.maze_w, self.maze_h,
                        (pet["x"], pet["y"]), (chase_x, chase_y),
                    )
                    pet["path"] = path or []
                speed = NORMAL_SPEED * PET_SPEED_MULT
                pet["move_accum"] += speed * TICK_DT
                while pet["move_accum"] >= 1.0 and pet["path"]:
                    pet["move_accum"] -= 1.0
                    nx, ny = pet["path"].pop(0)
                    pet["x"], pet["y"] = nx, ny
                    # Appena e' arrivato abbastanza vicino si ferma subito,
                    # invece di continuare a scavalcare il proprietario ad
                    # ogni tick.
                    if abs(chase_x - pet["x"]) + abs(chase_y - pet["y"]) <= stay_range:
                        pet["path"] = []
                        break
            else:
                pet["path"] = []
                pet["move_accum"] = 0.0

            # ---- fuoco: spara verso il bersaglio agganciato, indipendentemente dal movimento ----
            pet["fire_cd"] = pet.get("fire_cd", 0.0) - TICK_DT
            if target is not None and pet["fire_cd"] <= 0:
                pet["fire_cd"] = TURRET_FIRE_INTERVAL_SECONDS
                ddx, ddy = target.x - pet["x"], target.y - pet["y"]
                horiz = (1, 0) if ddx >= 0 else (-1, 0)
                vert = (0, 1) if ddy >= 0 else (0, -1)
                cand = [horiz, vert] if abs(ddx) >= abs(ddy) else [vert, horiz]
                dx, dy = cand[0]
                if is_wall(self.maze, self.maze_w, self.maze_h, pet["x"] + dx, pet["y"] + dy) \
                        and not is_wall(self.maze, self.maze_w, self.maze_h,
                                        pet["x"] + cand[1][0], pet["y"] + cand[1][1]):
                    dx, dy = cand[1]
                if not is_wall(self.maze, self.maze_w, self.maze_h, pet["x"] + dx, pet["y"] + dy):
                    dir_name = next((k for k, v in DIRECTIONS.items() if v == (dx, dy)), "right")
                    laser = {
                        "id": uuid.uuid4().hex[:8],
                        "owner": pet["owner"],
                        "x": pet["x"], "y": pet["y"],
                        "dx": dx, "dy": dy,
                        "move_accum": 0.0,
                        "bounce_left": None,
                    }
                    self.lasers.append(laser)
                    self.push_event({
                        "kind": "laser_fire", "id": laser["id"], "shooter": pet["owner"],
                        "x": pet["x"], "y": pet["y"], "dir": dir_name, "pet": True,
                    })

    def check_win(self):
        """Il round finisce quando resta UN SOLO giocatore vivo (tra
        ALMENO due in stanza): quello e' il vincitore. Con le vite extra e
        i bonus (laser, mine, super assassino) chiunque puo' essere
        eliminato, quindi il conteggio giusto e' sui vivi totali.

        ECCEZIONE per la partita in solitaria (utile per provare il gioco
        senza dover accendere un secondo dispositivo): con un solo
        giocatore in stanza non ha senso dichiararlo subito "ultimo
        sopravvissuto" appena parte il round (sarebbe l'unico vivo fin dal
        primo istante), quindi quel caso viene escluso qui sotto e il round
        prosegue normalmente. Se pero' quell'unico giocatore esaurisce
        tutte le vite ed elimina anche se stesso, il round finisce
        comunque regolarmente (nessun sopravvissuto)."""
        alive = [p for p in self.players.values() if p.alive]
        if len(alive) == 0:
            # Puo' capitare sia in solitaria (l'unico giocatore ha esaurito
            # le vite) sia in casi limite come disconnessioni multiple: si
            # chiude il round senza vincitori "veri".
            return [], "no_survivors"
        if len(alive) == 1 and len(self.players) > 1:
            return [alive[0].id], "last_survivor"
        return None, None

    def state_snapshot(self):
        return {
            "type": "state",
            "phase": self.state.lower(),
            "countdown": round(max(self.countdown_left, 0), 1),
            "timer": round(max(self.timer_left, 0), 1),
            "players": [p.to_public() for p in self.players.values()],
            "lasers": [
                {"id": lz["id"], "x": lz["x"], "y": lz["y"], "dir": [lz["dx"], lz["dy"]]}
                for lz in self.lasers
            ],
            "mines": [{"id": m["id"], "x": m["x"], "y": m["y"], "owner": m["owner"]} for m in self.mines],
            "turrets": [self.turret_public(t) for t in self.turrets],
            "pets": [self.pet_public(pt) for pt in self.pets],
            "mortars": [self.mortar_public(mt) for mt in self.mortars],
            "poison_zones": [
                {
                    "id": pz["id"], "x": pz["x"], "y": pz["y"],
                    "left": round(pz["left"], 2),
                    "radius": pz.get("radius", POISON_RADIUS_CELLS),
                    # Il proprietario serve al client per colorare la scia
                    # del blob vivo (radius 0) nel colore di chi ha piazzato
                    # il blob, invece del verde veleno generico.
                    "owner": pz.get("owner"),
                    # Le zone del fungo atomico vengono colorate dal
                    # client nel colore del proprietario (vedi
                    # drawPoisonZones in index.html).
                    "atomic": pz.get("atomic", False),
                }
                for pz in self.poison_zones
            ],
            "bombs": [self.bomb_public(bomb) for bomb in self.bombs],
            "superbombs": [self.superbomb_public(b) for b in self.superbombs],
            "balloons": [self.balloon_public(b) for b in self.balloons],
            "blobs": [self.blob_public(b) for b in self.blobs],
            "spike_walls": [self.spike_wall_public(w) for w in self.spike_walls],
            "bushes": [self.bush_public(b) for b in self.bushes],
            "mushrooms": [self.mushroom_public(m) for m in self.mushrooms],
            "teslas": [self.tesla_public(t) for t in self.teslas],
            "portal_on": self.portal_on,
            "portal_cycle_left": round(max(self.portal_cycle_left, 0), 1),
            "missiles": [self.missile_public(mz) for mz in self.missiles],
            "mega_pellet": (
                {"x": self.mega_pellet_cell[0], "y": self.mega_pellet_cell[1], "points": MEGA_PELLET_POINTS}
                if self.mega_pellet_cell is not None else None
            ),
        }

    async def drain_events(self):
        """Invia (e svuota) la coda degli eventi accumulati nel tick."""
        if not self.events:
            return
        pending, self.events = self.events, []
        for ev in pending:
            await self.broadcast(ev)

    def reset_to_lobby(self):
        self.state = "LOBBY"
        self.last_kill = None
        self.events = []
        self.lasers = []
        self.mines = []
        self.missiles = []
        self.turrets = []
        self.pets = []
        self.mortars = []
        self.superbombs = []
        self.balloons = []
        self.blobs = []
        self.spike_walls = []
        self.teslas = []
        self.bushes = []
        self.mushrooms = []
        self.bombs = []
        self.poison_zones = []  # nuvole velenose lasciate a terra dagli impatti del mortaio
        for p in self.players.values():
            p.alive = True
            p.direction = None
            p.has_ninja = False
            p.is_assassin = False
            p.assassin_left = 0.0
            p.ninja_used = False
            p.ghost_left = 0.0
            p.prot_left = 0.0
            p.has_laser = False
            p.has_bounce = False
            p.has_mines = False
            p.mines_left = 0
            p.has_missile = False
            p.missiles_left = 0
            p.has_trap = False
            p.trap_target = None
            p.trapped_left = 0.0
            p.trapped_by = None
            p.trap_uses_left = 0
            p.has_turret = False
            p.turret_placed = False
            p.has_armor = False
            p.armor_active = False
            p.armor_left = 0.0
            p.armor_used = False
            p.has_lightning = False
            p.lightning_used = False
            p.has_pet = False
            p.pet_summoned = False
            p.has_robot = False
            p.robot_used = False
            p.has_mortar = False
            p.mortar_placed = False
            p.has_superbomb = False
            p.superbomb_left = 0
            p.has_balloon = False
            p.balloon_launched = False
            p.has_blob = False
            p.blob_placed = False
            p.has_blob_alive = False
            p.blob_alive_used = False
            p.has_spike_wall = False
            p.spike_wall_placed = False
            p.has_tesla = False
            p.tesla_placed = False
            p.has_territory_trap = False
            p.territory_marking = False
            p.territory_ready = False
            p.territory_used = False
            p.territory_tiles = set()
            p.has_bush = False
            p.bush_placed = False
            p.has_mushroom = False
            p.mushroom_placed = False
            p.next_lives_milestone = LIVES_EVERY_POINTS
            p.kills = 0

    # ---------- main loop ----------

    async def run_round(self):
        # Ad ogni nuova partita si pesca a caso una delle 10 mappe: forma,
        # colori e dimensioni cambiano, ma la giocabilita' e' garantita (ogni
        # mappa e' verificata per connettivita' totale al momento della
        # generazione).
        self.pick_new_map()
        self.state = "COUNTDOWN"
        self.countdown_left = COUNTDOWN_SECONDS
        self.assign_spawns()
        await self.broadcast({"type": "round_start", **self.map_payload()})
        await self.broadcast(self.state_snapshot())

        while self.state in ("COUNTDOWN", "PLAYING"):
            await asyncio.sleep(TICK_DT)
            if not self.players:
                return

            # Il movimento e' attivo sia in countdown che in gioco: ci si puo'
            # muovere subito, ancora prima che il round entri nel vivo.
            prev = self.update_movement()
            self.update_rtt_pings()
            self.update_pellet_respawns()
            self.update_portal_cycle()   # accende/spegne i portali ogni 30s
            self.check_collisions(prev)  # no-op finche' nessuno e' super assassino

            if self.state == "COUNTDOWN":
                self.countdown_left -= TICK_DT
                if self.countdown_left <= 0:
                    self.begin_playing()

            elif self.state == "PLAYING":
                self.timer_left -= TICK_DT
                self.update_lasers()  # bonus 150 punti: arma principale permanente, un colpo al secondo se un nemico e' entro 12 caselle
                self.update_turrets() # bonus 600 punti: torretta automatica, stessa cadenza del laser
                self.update_pets()    # bonus 900 punti: il pet insegue il proprietario e attacca chi si avvicina
                self.update_mortars() # bonus 1200 punti: il mortaio spara bombe ad arco contro il nemico piu' vicino entro 15 caselle
                self.move_lasers()    # avanza i proiettili laser in volo (con eventuale rimbalzo)
                self.check_mines()    # bonus 200 punti: fa esplodere le mine calpestate
                self.update_blobs_wander()  # bonus 2000 punti: fa vagare i blob "vivi" lasciando la scia velenosa
                self.check_blobs()    # bonus 1800/2000 punti: fa mangiare al blob (fermo o vivo) chiunque tocchi o gli sia adiacente
                self.update_spike_walls()  # bonus 2200 punti: scala la durata dei muri di spunzoni (1 minuto) e uccide gli avversari che ci sbattono contro
                self.update_teslas()  # bonus 2400 punti: la Tesla fulmina ad area, ogni 2.5s, tutto cio' che appartiene al nemico entro 8 caselle, ignorando i muri
                self.update_territory_marking()  # bonus 2600 punti: marca (in privato) le caselle calpestate durante la fase di selezione della trappola territoriale
                self.update_bushes()  # bonus 2800 punti: cresce gli arbusti spinosi (una casella al minuto), uccide al contatto, la corazza li spezza
                self.update_mushrooms()  # bonus 3000 punti: fa esplodere i funghi atomici calpestati (distruzione totale raggio 10 + area avvelenata 1 minuto)
                self.move_missiles()  # bonus 400 punti: avanza i missili guidati verso il bersaglio
                self.update_bombs()   # bonus 1200 punti: avanza le bombe di mortaio in volo e le fa esplodere all'impatto
                self.update_poison_zones()  # bonus 1200 punti: le nuvole velenose lasciate dagli impatti continuano a fare danno nel tempo
                self.update_superbombs()  # bonus 1400 punti: avanza il conto alla rovescia dei bomboloni piazzati e li fa esplodere dopo 2 secondi
                self.update_balloons()    # bonus 1600 punti: fa vagare a caso le mongolfiere in volo e sganciano bombe istantanee ogni 3 secondi
                self.update_mega_pellet()  # pallino mega da 100 punti: spawna una volta al minuto al centro esatto della mappa
                self.check_armor_effects()  # bonus 700 punti: la corazza distrugge torrette/mine/pet/mortai avversari toccati
                winners, reason = self.check_win()
                if winners is None and self.timer_left <= 0:
                    alive = [p for p in self.players.values() if p.alive]
                    if alive:
                        best = max(p.points for p in alive)
                        winners = [p.id for p in alive if p.points == best]
                    else:
                        winners = []
                    reason = "time_up"
                if winners is not None:
                    self.state = "ENDED"
                    self.last_result = {"winners": winners, "reason": reason}
                    await self.drain_events()
                    await self.drain_private_events()
                    await self.broadcast(self.state_snapshot())
                    await self.broadcast({
                        "type": "round_end",
                        "winners": winners,
                        "reason": reason,
                        "names": {p.id: p.name for p in self.players.values()},
                        "scores": {p.id: p.points for p in self.players.values()},
                    })
                    break

            await self.drain_events()
            await self.drain_private_events()
            # Gli eventi (uccisioni, esplosioni, suoni...) restano in tempo
            # reale ad ogni tick perche' sono leggeri e vanno ad effetto
            # immediato. Lo snapshot COMPLETO invece e' pesante (cresce con
            # tutti i gadget permanenti piazzati durante il round) e non ha
            # bisogno di essere ricostruito/inviato 60 volte al secondo per
            # sembrare fluido: lo si manda ogni STATE_BROADCAST_EVERY_N_TICKS
            # tick per tenere sotto controllo CPU e banda man mano che la
            # partita si riempie di torrette, mortai, arbusti, ecc.
            self._snapshot_tick += 1
            if self._snapshot_tick % STATE_BROADCAST_EVERY_N_TICKS == 0:
                await self.broadcast(self.state_snapshot())
        if self.code in ROOMS:
            self.reset_to_lobby()
            await self.broadcast_lobby()


def gen_room_code():
    while True:
        code = "".join(random.choice(ROOM_CODE_CHARS) for _ in range(6))
        if code not in ROOMS:
            return code


async def send_error(ws, message):
    await ws.send(encode_text({"type": "error", "message": message}))


def disable_nagle(ws):
    """Disattiva l'algoritmo di Nagle sulla connessione TCP sottostante.

    Di default il sistema operativo raggruppa i pacchetti piccoli prima di
    inviarli, per usare la rete in modo piu' efficiente: ottimo per
    trasferimenti di file, pessimo per un gioco in tempo reale, dove ogni
    messaggio (mossa, stato) e' piccolo e deve arrivare il prima possibile.
    L'interazione tra l'algoritmo di Nagle e gli ACK ritardati del sistema
    ricevente puo' introdurre decine di millisecondi di attesa "invisibile"
    per ogni messaggio: esattamente il tipo di latenza che va eliminato per
    un feeling reattivo come quello richiesto (vedi commenti su TICK_HZ in
    common.py). Va fatto per connessione, non a livello globale, quindi si
    applica al momento in cui il client si collega.
    """
    try:
        sock = ws.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass  # Piattaforme/transport senza socket TCP diretto (raro): ok, si prosegue senza


async def handle_client(ws):
    disable_nagle(ws)
    player = None
    room = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mtype = msg.get("type")

            if mtype == "create_room":
                name = (msg.get("name") or "Player")[:16]
                room = Room(gen_room_code())
                ROOMS[room.code] = room
                player = Player(uuid.uuid4().hex[:8], name, ws)
                room.add_player(player)
                await ws.send(encode_text({
                    "type": "room_created", "code": room.code, "player_id": player.id,
                    **room.map_payload(),
                }))
                await room.broadcast_lobby()

            elif mtype == "join_room":
                code = (msg.get("code") or "").upper().strip()
                name = (msg.get("name") or "Player")[:16]
                room = ROOMS.get(code)
                if room is None:
                    await send_error(ws, "Codice stanza non trovato.")
                    room = None
                    continue
                if room.state != "LOBBY":
                    await send_error(ws, "Partita gia' in corso, riprova piu' tardi.")
                    room = None
                    continue
                if len(room.players) >= MAX_PLAYERS:
                    await send_error(ws, "Stanza piena (max 5 giocatori).")
                    room = None
                    continue
                player = Player(uuid.uuid4().hex[:8], name, ws)
                room.add_player(player)
                await ws.send(encode_text({
                    "type": "joined", "code": room.code, "player_id": player.id,
                    **room.map_payload(),
                }))
                await room.broadcast_lobby()

            elif mtype == "select_color":
                if not room or not player:
                    continue
                raw = msg.get("colors")
                if not isinstance(raw, list):
                    continue
                # Dedup mantenendo l'ordine (colors[0] = primario), max 2,
                # solo nomi validi. I colori in SECONDARY_ONLY_COLORS (il
                # nero) non possono MAI finire in posizione primaria: da
                # soli, su sfondo quasi nero, sarebbero pressoche' invisibili
                # (vedi anche il blocco lato client in toggleMyColor).
                seen = []
                for c in raw:
                    if c not in COLORS or c in seen:
                        continue
                    if c in SECONDARY_ONLY_COLORS and len(seen) == 0:
                        continue
                    seen.append(c)
                    if len(seen) >= MAX_PLAYER_COLORS:
                        break
                if not seen:
                    continue
                primary = seen[0]
                others_primary = {
                    p.colors[0] for p in room.players.values()
                    if p.colors and p.id != player.id
                }
                if primary in others_primary:
                    await send_error(ws, "Colore primario gia' scelto da un altro giocatore.")
                    continue
                player.colors = seen
                await room.broadcast_lobby()

            elif mtype == "select_character":
                if not room or not player:
                    continue
                character = msg.get("character")
                if character not in CHARACTERS:
                    continue
                player.character = character
                await room.broadcast_lobby()

            elif mtype == "start_game":
                if not room or not player or not player.host:
                    continue
                if room.state != "LOBBY":
                    continue
                if len(room.players) < MIN_PLAYERS:
                    await send_error(ws, f"Servono almeno {MIN_PLAYERS} giocatori.")
                    continue
                if any(not p.colors for p in room.players.values()):
                    await send_error(ws, "Tutti i giocatori devono scegliere un colore.")
                    continue
                room.loop_task = asyncio.create_task(room.run_round())

            elif mtype == "move":
                if not room or not player:
                    continue
                direction = msg.get("direction")
                if direction in DIRECTIONS:
                    # Invece di limitarsi ad accodare la direzione nella
                    # posizione ATTUALE (gia' "nel futuro" per via del
                    # ritardo di rete del pacchetto), si compensa la
                    # latenza: vedi Room._rewind_move per i dettagli. Un
                    # solo input "in attesa" resta comunque possibile alla
                    # volta: una nuova pressione sostituisce sempre quella
                    # precedente.
                    room._rewind_move(player, direction)

            elif mtype == "rtt_pong":
                # Risposta al ping periodico di misura latenza (vedi
                # Room.update_rtt_pings): usata per stimare quanto
                # "tornare indietro" quando arriva una svolta (vedi
                # Room._rewind_move). Media mobile esponenziale per non
                # farsi destabilizzare da un singolo pacchetto in ritardo.
                if not player:
                    continue
                sent_at = msg.get("t")
                if isinstance(sent_at, (int, float)) and sent_at == player.rtt_ping_sent_at:
                    measured = time.monotonic() - sent_at
                    # Clamp difensivo: oltre 1s e' quasi certamente un
                    # outlier di rete, non un vero ritardo costante.
                    measured = max(0.0, min(measured, 1.0))
                    player.rtt = 0.7 * player.rtt + 0.3 * measured

            elif mtype == "place_mine":
                # Bonus 200 punti: pressione del tasto "1" lato client.
                # Il server resta l'autorita' su quante mine restano
                # e su dove vengono posate.
                if not room or not player:
                    continue
                room.try_place_mine(player)

            elif mtype == "activate_ninja":
                # Bonus 300 punti: pressione del tasto "2" lato client.
                if not room or not player:
                    continue
                room.try_activate_ninja(player)

            elif mtype == "fire_missile":
                # Bonus 400 punti: pressione del tasto "3" lato client.
                if not room or not player:
                    continue
                room.try_fire_missile(player)

            elif mtype == "activate_trap":
                # Bonus 500 punti: pressione del tasto "4" lato client
                # (sia per intrappolare che per far detonare).
                if not room or not player:
                    continue
                room.try_activate_trap(player)

            elif mtype == "place_turret":
                # Bonus 600 punti: pressione del tasto "5" lato client.
                # Utilizzabile una sola volta per giocatore (vedi
                # try_place_turret): il server resta l'autorita'.
                if not room or not player:
                    continue
                room.try_place_turret(player)

            elif mtype == "activate_armor":
                # Bonus 700 punti: pressione del tasto "6" lato client.
                if not room or not player:
                    continue
                room.try_activate_armor(player)

            elif mtype == "activate_lightning":
                # Bonus 800 punti: pressione del tasto "7" lato client.
                if not room or not player:
                    continue
                room.try_activate_lightning(player)

            elif mtype == "activate_pet":
                # Bonus 900 punti: pressione del tasto "8" lato client.
                # Utilizzabile una sola volta per giocatore (vedi
                # try_summon_pet): il server resta l'autorita'.
                if not room or not player:
                    continue
                room.try_summon_pet(player)

            elif mtype == "evolve_turret":
                # Bonus 1000 punti: pressione del tasto "9" lato client.
                # Utilizzabile una sola volta per giocatore, e solo se la
                # torretta e' ancora viva (vedi try_evolve_turret): il
                # server resta l'autorita'.
                if not room or not player:
                    continue
                room.try_evolve_turret(player)

            elif mtype == "place_mortar":
                # Tasto "1" lato client: la PRIMA pressione schiera il
                # mortaio (bonus 1200 punti). Una volta che il mortaio e'
                # gia' stato piazzato, la stessa pressione innesca invece il
                # bombolone (bonus 1400 punti, vedi try_place_superbomb):
                # ce ne sono DUE disponibili (SUPERBOMB_COUNT), quindi le
                # DUE pressioni successive ne piazzano uno ciascuna. Solo
                # una volta che ENTRAMBI i bomboloni sono stati piazzati la
                # stessa pressione fa librare in aria la mongolfiera (bonus
                # 1600 punti, vedi try_launch_balloon). Una volta che ANCHE
                # la mongolfiera e' gia' stata lanciata, la stessa pressione
                # piazza il blob gelatinoso (bonus 1800 punti, vedi
                # try_place_blob). Una volta che ANCHE il blob e' gia' stato
                # piazzato, la stessa pressione lo risveglia infine
                # facendolo vagare per la mappa (bonus 2000 punti, vedi
                # try_animate_blob), poi piazza il muro di spunzoni (bonus
                # 2200 punti, vedi try_place_spike_wall) e infine, ultimo
                # gradino della catena, la Tesla laser (bonus 2400 punti,
                # vedi try_place_tesla). Ogni step e' utilizzabile una sola
                # volta per giocatore (i due bomboloni fanno eccezione, ne
                # hanno due) (vedi try_place_mortar/try_place_superbomb/
                # try_launch_balloon/try_place_blob/try_animate_blob/
                # try_place_spike_wall/try_place_tesla): il server resta
                # l'autorita'.
                if not room or not player:
                    continue
                superbomb_done = player.has_superbomb and player.superbomb_left <= 0
                if player.mortar_placed and superbomb_done and player.balloon_launched and player.blob_placed:
                    # Fine catena: se il risveglio del blob e' gia' stato
                    # usato - oppure e' diventato IMPOSSIBILE per sempre
                    # (blob distrutto da un bombolone avversario prima del
                    # risveglio) - la stessa pressione piazza il muro di
                    # spunzoni (bonus 2200 punti, vedi try_place_spike_wall)
                    # e, una volta che ANCHE quello e' gia' stato piazzato,
                    # la Tesla laser (bonus 2400 punti, vedi
                    # try_place_tesla). Altrimenti risveglia il blob come
                    # prima.
                    blob_still_there = any(
                        b["owner"] == player.id for b in room.blobs
                    )
                    if player.blob_alive_used or not blob_still_there:
                        if player.spike_wall_placed:
                            if player.tesla_placed:
                                # Fine catena (nuovo, bonus 2600 punti): la
                                # Tesla e' gia' stata piazzata, quindi la
                                # stessa pressione gestisce ora la
                                # trappola territoriale (vedi
                                # try_use_territory_trap): prima pressione
                                # avvia la selezione, seconda la innesca.
                                # Trappola territoriale gia' CONSUMATA:
                                # la stessa pressione piazza infine
                                # l'arbusto spinoso (bonus 2800 punti,
                                # vedi try_place_bush), nuovo, vero ultimo
                                # gradino della catena.
                                if player.territory_used:
                                    # Arbusto gia' piantato: la stessa
                                    # pressione piazza infine il fungo
                                    # atomico (bonus 3000 punti, vedi
                                    # try_place_mushroom), VERO ultimo
                                    # gradino della catena.
                                    if player.bush_placed:
                                        room.try_place_mushroom(player)
                                    else:
                                        room.try_place_bush(player)
                                else:
                                    room.try_use_territory_trap(player)
                            else:
                                room.try_place_tesla(player)
                        else:
                            room.try_place_spike_wall(player)
                    else:
                        room.try_animate_blob(player)
                elif player.mortar_placed and superbomb_done and player.balloon_launched:
                    room.try_place_blob(player)
                elif player.mortar_placed and superbomb_done:
                    room.try_launch_balloon(player)
                elif player.mortar_placed:
                    room.try_place_superbomb(player)
                else:
                    room.try_place_mortar(player)

            elif mtype == "stop":
                # Il tasto/direzione e' stato rilasciato: il personaggio si
                # ferma subito, non continua da solo nell'ultima direzione
                # premuta. Si ferma alla cella corrente (non completa
                # l'eventuale scivolamento verso la cella successiva),
                # esattamente come ci si aspetta rilasciando il tasto.
                if not room or not player:
                    continue
                player.direction = None
                player.next_direction = None
                player.move_accum = 0.0

            elif mtype == "ping":
                await ws.send(encode_text({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if player and room:
            player.connected = False
            was_host = player.host
            room.players.pop(player.id, None)
            if not room.players:
                ROOMS.pop(room.code, None)
            else:
                if was_host:
                    new_host = next(iter(room.players.values()))
                    new_host.host = True
                await room.broadcast_lobby()


async def health_check(connection, request):
    """
    Un GET HTTP normale (dal browser che apre il link, o dagli 'health
    check' delle piattaforme di hosting) riceve la pagina del gioco
    (index.html), se presente accanto a questo file. Le vere richieste
    WebSocket del gioco proseguono invece normalmente.

    NB: dalla versione 13 di 'websockets' la firma di process_request e' 
    process_request(connection, request) -> Response | None (non piu'
    process_request(path, request_headers) -> tuple | None come nelle
    versioni vecchie), e il valore di ritorno deve essere un vero oggetto
    websockets.http11.Response (non una tupla): usare la firma/i tipi
    sbagliati fa fallire silenziosamente l'intercettazione delle richieste
    HTTP normali, che finiscono nella pagina d'errore di default del
    protocollo WebSocket ("Non e' riuscito ad aprire una connessione
    WebSocket") anche quando il server e' online e funzionante.
    """
    upgrade = request.headers.get("Upgrade", "")
    if upgrade.lower() == "websocket":
        return None  # lascia proseguire come WebSocket
    if CLIENT_HTML is not None:
        body = CLIENT_HTML.encode("utf-8")
        headers = Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        return Response(200, "OK", headers, body)
    body = b"Pac-Man Arena server OK\n"
    headers = Headers([
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return Response(200, "OK", headers, body)


async def main():
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT))
    async with websockets.serve(
        handle_client, "0.0.0.0", port, process_request=health_check,
        # compression=None: i pacchetti di gioco sono piccoli (poche centinaia
        # di byte) e frequentissimi (fino a 60/s per stanza). La compressione
        # permessage-deflate ha un costo CPU fisso per messaggio che, su
        # payload cosi' piccoli, supera quasi sempre il risparmio di banda
        # ottenuto: per un gioco in tempo reale conviene spendere quella CPU
        # per spedire prima, non per comprimere meglio.
        compression=None,
    ):
        print(f"Pac-Man Arena (WebSocket) in ascolto sulla porta {port}")
        await asyncio.Future()  # resta acceso per sempre


if __name__ == "__main__":
    asyncio.run(main())
