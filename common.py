"""
Costanti condivise, mappe e helper di protocollo per Pac-Man Arena 1vAll.
"""
import json
import random
import string
from collections import deque

DEFAULT_PORT = 8765

# 60Hz invece di 30Hz: raddoppia la frequenza con cui il server calcola
# fisica/collisioni e manda correzioni di stato ai client. Le velocita' sono
# espresse in celle/secondo quindi il bilanciamento del gioco NON cambia
# (a 60Hz ogni tick avanza semplicemente la meta' di spazio rispetto a
# prima); a beneficiarne sono la precisione delle collisioni tra giocatori
# (la cella "attraversata" viene controllata il doppio delle volte, quindi
# si notano meno gli "attraversamenti fantasma" ad alta velocita') e la
# riconciliazione client-side, che deve correggere scarti piu' piccoli e
# piu' spesso invece di scarti piu' grandi e piu' radi: e' proprio questo
# che si traduce in un movimento remoto percepito come piu' fluido, oltre
# ad avvicinare il tickrate del server al refresh rate tipico di un monitor
# desktop (60/120/144Hz), a cui il client renderizza gia' via
# requestAnimationFrame.
TICK_HZ = 60
TICK_DT = 1.0 / TICK_HZ
# La logica di gioco (movimento, collisioni, timer dei bonus) gira sempre a
# TICK_HZ pieno per restare precisa e reattiva. Lo stato COMPLETO (tutti gli
# oggetti permanenti: torrette, mortai, pet, arbusti con le loro caselle,
# Tesla, mine, ecc.) e' pero' molto piu' pesante da ricostruire/serializzare
# e cresce col passare del round; per evitare che il gioco rallenti via via
# che si sbloccano ed usano piu' gadget, lo si invia solo ogni N tick invece
# che ad ogni tick. 2 = 30 volte al secondo, ancora fluidissimo per un gioco
# a caselle come questo. Alzato da 2 a 4 (15/s invece di 30/s): lo stato
# completo cresce col passare del round (torrette, mortai, arbusti,
# spunzoni, mine...) e serializzarlo/spedirlo troppo spesso puo' occupare
# l'event loop abbastanza a lungo da far scattare il timeout di keepalive
# interno della libreria "websockets" su ALTRE connessioni (vedi anche
# RECONNECT_GRACE_SECONDS qui sotto e ping_interval/ping_timeout in
# websockets.serve, main.py): 15/s resta fluidissimo per un gioco a
# caselle, ma dimezza il lavoro sincrono per tick.
STATE_BROADCAST_EVERY_N_TICKS = 4

COUNTDOWN_SECONDS = 15
ROUND_SECONDS = 1200  # durata di un round: 20 minuti
MAX_PLAYERS = 5
MIN_PLAYERS = 1

# Un giocatore la cui connessione cade (blip di rete, telefono che va in
# background, passaggio WiFi/4G, timeout di ping interno) NON viene tolto
# subito dalla stanza: resta "in attesa" per questa finestra di tempo,
# durante la quale puo' riconnettersi e riprendere esattamente il suo
# posto (stesso player_id, stesso personaggio, stessi punti/vite/gadget)
# mandando un messaggio "rejoin". Solo se il tempo scade senza che si sia
# ripresentato viene rimosso per davvero (vedi sweep_disconnected in
# main.py). In LOBBY la stessa finestra vale anche per l'host.
RECONNECT_GRACE_SECONDS = 25.0

# Una stanza in LOBBY (appena creata, o tornata li' dopo la fine di un
# round) dove nessuno preme mai "Avvia partita" - ne' fa alcun'altra azione
# di lobby (join, scelta colore/personaggio, cambio modalita') - resta
# altrimenti viva PER SEMPRE finche' almeno un websocket resta aperto:
# nessun timer di gioco la tocca mai, perche' quei timer partono solo a
# partita avviata. Trascorso questo periodo di INATTIVITA' (non di
# semplice permanenza: ogni azione di lobby lo fa ripartire da zero) senza
# che lo stato sia cambiato, la stanza viene chiusa d'ufficio (vedi
# Room._lobby_watchdog in main.py) cosi' come un giocatore disconnesso
# scade dopo RECONNECT_GRACE_SECONDS. Volutamente molto piu' lungo di
# quella finestra: qui non c'e' nessuna fretta, serve solo evitare che
# stanze dimenticate aperte accumulino codici e memoria all'infinito.
LOBBY_IDLE_TIMEOUT_SECONDS = 900.0  # 15 minuti

NORMAL_SPEED = 4.5          # celle al secondo
ASSASSIN_SPEED_MULT = 1.1   # il super assassino (bonus 300 punti) e' 1.1x rispetto a 1.0 dei giocatori normali

# ---- compensazione della latenza per le svolte (vedi Room._rewind_move
# e Room._advance_state in main.py) ----
# La svolta perpendicolare puo' scattare solo esattamente al centro-cella,
# una finestra larga un solo tick server (TICK_DT, ~16ms su 60Hz). Un
# messaggio "move" arriva pero' sempre con un ritardo di rete rispetto al
# momento reale in cui il tasto e' stato premuto: se quel ritardo supera la
# finestra, la svolta viene "persa" per quell'incrocio e il personaggio
# deve percorrere un'altra cella intera prima di riprovare, sbattendo
# contro il muro se quella cella e' un vicolo cieco. Per questo, invece di
# limitarsi ad accodare la direzione richiesta, il server la applica
# retroattivamente nel punto in cui sarebbe scattata davvero, poi
# "riavvolge in avanti" la traiettoria fino ad ora: niente scatti visibili,
# perche' la posizione finale e' quella fisicamente corretta, non una
# posizione arbitraria.
RTT_PING_INTERVAL_SECONDS = 2.0   # ogni quanto il server misura il ping di ciascun giocatore
RTT_DEFAULT_SECONDS = 0.06        # stima prudente usata finche' non arriva la prima misura reale
REWIND_MAX_SECONDS = 0.20         # tetto massimo di riavvolgimento (oltre, si rinuncia: troppo rischioso/sfruttabile)
REWIND_HISTORY_SECONDS = 1.0      # quanta storia posizione/direzione si tiene in memoria per giocatore

# ---- sistema punti e bonus a traguardi ----
# Ogni pallino normale vale 1 punto. In 10 punti (angoli/estremita') della
# mappa si trovano pallini piu' grossi e arancioni che valgono 10 punti.
# Ogni pallino mangiato ricompare da solo dopo PELLET_RESPAWN_SECONDS.
# Al raggiungimento di ogni soglia (una sola volta per round) scatta il
# bonus corrispondente.
BONUS_THRESHOLDS = [
    (50,  "extra_life"),    # +1 vita: se vieni eliminato, respawni invece di uscire
    (100, "extra_life"),    # +1 seconda vita extra (stesso effetto, soglia diversa)
    (150, "laser"),         # sblocca il laser (un colpo/secondo): resta attivo per tutta la partita, ma spara solo quando un nemico e' entro LASER_RANGE_CELLS caselle
    (200, "mines"),         # sblocca 1 mina sganciabile sulla mappa (si attiva col tasto "1")
    (400, "missile"),       # sblocca 1 missile guidato (si spara col tasto "3")
    (750, "extra_life_3"),  # +3 vite extra in un colpo solo, tra la corazza (700) e il fulmine (800)
    (1100, "extra_life_3"), # +3 vite extra in un colpo solo, tra la torretta-navicella (1000) e il mortaio (1200)
]
PELLET_POINTS = 1                  # valore di un pallino normale
POWER_PELLET_POINTS = 10           # valore di un pallino grosso/arancione
POWER_PELLET_COUNT = 10            # quanti pallini grossi su ciascuna mappa
PELLET_RESPAWN_SECONDS = 20.0      # tempo prima che un pallino mangiato ricompaia
# Pallino mega (ancora piu' grosso del power pellet arancione): spawna UNA
# SOLA VOLTA al minuto, sempre nella cella libera piu' vicina all'esatto
# centro della mappa, e vale 100 punti. A differenza dei pallini normali
# non ricompare da solo dopo essere stato mangiato: bisogna aspettare il
# prossimo giro di MEGA_PELLET_INTERVAL_SECONDS (vedi update_mega_pellet/
# eat_mega_pellet in main.py).
MEGA_PELLET_POINTS = 100
MEGA_PELLET_INTERVAL_SECONDS = 60.0
SUPER_ASSASSIN_THRESHOLD = 300     # punti oltre i quali si sblocca la modalita' ninja
# La modalita' ninja dura 45 secondi (aumentata da 30) ed e' utilizzabile
# UNA SOLA VOLTA per round: una volta terminata (scaduto il tempo o dopo
# un'eliminazione) non si puo' piu' riattivare (vedi Player.ninja_used e
# try_activate_ninja in main.py).
SUPER_ASSASSIN_DURATION_SECONDS = 10.0
LASER_RANGE_CELLS = 12          # bonus 150 punti: il laser (arma principale, sbloccata per tutta la partita) spara SOLO quando un avversario vivo e' entro questa distanza (caselle, stile Manhattan, come TURRET_RANGE_CELLS)
GHOST_SECONDS = 10.0            # (bonus rimosso dal gioco, costante tenuta per compatibilita')
SPAWN_PROTECT_SECONDS = 5.0    # invulnerabilita' temporanea dopo un respawn
MIN_SPAWN_DISTANCE = 12        # distanza minima (in caselle, euclidea) richiesta tra due spawn
LASER_INTERVAL_SECONDS = 1.0   # ogni quanto il laser spara un colpo, una volta sbloccato (1 al secondo)
LASER_FIRST_DELAY_SECONDS = 1.0  # attesa del primo colpo dopo lo sblocco
LASER_PROJECTILE_SPEED = 20.0  # celle al secondo percorse dal proiettile laser (raddoppiata: e' un proiettile vero, deve sentirsi veloce)
LASER_BOUNCE_DISTANCE = 12     # celle percorribili dopo il primo rimbalzo su una parete (bonus 150 punti)
MINES_COUNT = 1                # numero di mine disponibili una volta sbloccato il bonus 200 punti (ridotto da 2 a 1)
MINE_DOUBLE_TAP_MS = 350       # finestra (ms) del doppio tocco freccia destra/D che sgancia una mina (uso lato client)
PORTAL_COOLDOWN_SECONDS = 1.2  # anti ping-pong: dopo un teletrasporto i portali si ignorano per un attimo

# ---- ciclo acceso/spento dei portali di teletrasporto ----
# I portali non sono piu' sempre attivi: si accendono per PORTAL_ON_SECONDS,
# poi si spengono per PORTAL_OFF_SECONDS, e cosi' via per tutto il round.
# Da spenti, entrarci non ha alcun effetto (vedi try_portal in main.py).
PORTAL_ON_SECONDS = 30.0
PORTAL_OFF_SECONDS = 30.0

# ---- bonus 400 punti: missile guidato (tasto "3") ----
MISSILE_SPEED_MULT = 1.1        # velocita' del missile = NORMAL_SPEED * 1.1 (di poco piu' veloce di un giocatore normale)
MISSILES_COUNT = 1              # missili disponibili una volta sbloccato il bonus 400 punti (solo 1)
MISSILE_RETARGET_SECONDS = 0.15  # ogni quanto il missile ricalcola il percorso verso il bersaglio (che si muove)
MISSILE_LOCK_DISTANCE = 2        # NERF: entro questa distanza (caselle, Manhattan) dal bersaglio il missile smette di correggere la rotta e prosegue dritto (schivabile)

# ---- bonus 500 punti: trappola (tasto "4") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "4" il
# giocatore intrappola il nemico piu' vicino (bloccato sul posto) per
# TRAP_DURATION_SECONDS. Se ci si avvicina entro TRAP_RANGE celle e si
# preme di nuovo "4" in tempo, l'avversario viene distrutto da una piccola
# esplosione (perde una vita). Se scade il tempo, la trappola si disinnesca
# da sola e l'avversario torna libero.
TRAP_THRESHOLD = 500
TRAP_DURATION_SECONDS = 5.0    # la trappola immobilizza il bersaglio per 5 secondi (aumentata da 3)
TRAP_RANGE = 1  # distanza massima (in celle, stile scacchi/Chebyshev) per far detonare la trappola
TRAP_MAX_USES = 1              # la trappola si puo' innescare UNA SOLA VOLTA per giocatore, per round (ridotta da 3)

# ---- bonus 600 punti: torretta automatica piazzabile (tasto "5") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "5" UNA
# SOLA VOLTA il giocatore piazza una torretta nella cella in cui si trova
# in quel momento. La torretta e' permanente (resta sulla mappa per tutto
# il resto del round, anche se il proprietario muore) e spara da sola verso
# il nemico vivo piu' vicino con la STESSA cadenza di fuoco del laser
# (un colpo ogni LASER_INTERVAL_SECONDS), riusando la stessa meccanica dei
# proiettili laser (stessa velocita', si ferma sul primo muro).
TURRET_THRESHOLD = 600
TURRET_FIRE_INTERVAL_SECONDS = LASER_INTERVAL_SECONDS  # stessa cadenza di fuoco del laser
# Raggio d'azione della torretta: traccia e spara SOLO ai nemici entro
# questa distanza (in caselle, distanza Manhattan). Fuori raggio la
# torretta resta in attesa e riprende a sparare appena qualcuno rientra.
TURRET_RANGE_CELLS = 10
# Percentuale di punti che chi uccide GUADAGNA come bonus, calcolata sul
# totale della vittima (10%): NON viene piu' sottratta alla vittima, che
# conserva sempre tutti i suoi punti - e' un premio per il killer, non un
# furto.
KILL_STEAL_FRACTION = 0.1

# ---- bonus 700 punti: corazza laser (tasto "6") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "6" il
# giocatore attiva la corazza per ARMOR_DURATION_SECONDS. Mentre e' attiva:
# respinge (rimbalza indietro) qualsiasi proiettile laser/missile la
# colpisca, distrugge ogni torretta che tocca e uccide chiunque tocchi
# (stessa meccanica di contatto del ninja). E' visibile a TUTTI (a
# differenza del ninja, non da' invisibilita') ed e' utilizzabile UNA SOLA
# VOLTA per round, come la modalita' ninja.
ARMOR_THRESHOLD = 700
ARMOR_DURATION_SECONDS = 10.0

# ---- bonus 800 punti: fulmine (tasto "7") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "7" il
# giocatore scatena un fulmine che colpisce ISTANTANEAMENTE tutti gli
# avversari vivi presenti sulla mappa (ovunque si trovino, niente raggio
# d'azione), facendo perdere loro una vita ciascuno (stessa unica via
# kill_player usata da laser/mine/missili/trappola). UTILIZZABILE UNA SOLA
# VOLTA per round, come il ninja e la corazza.
LIGHTNING_THRESHOLD = 800

# ---- bonus 900 punti: pet fedele permanente (tasto "8") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "8" il
# giocatore evoca UNA SOLA VOLTA (per round) un piccolo Pac-Man "pet", dello
# stesso colore del proprietario e grande la meta', che lo segue per tutto
# il resto del round. Il pet NON spara piu': appena un nemico vivo entra
# entro PET_RANGE_CELLS caselle lo aggancia e lo insegue attivamente
# (bfs_path, come il missile guidato) finche' non lo raggiunge, ovunque
# vada, poi gli fa perdere una vita al solo contatto (stessa meccanica del
# ninja/corazza). Resta sulla mappa finche' non viene distrutto da una
# mina, un missile guidato, un colpo laser nemico, un fulmine o il contatto
# con la corazza laser di un avversario: a quel punto sparisce per il resto
# del round e NON si puo' rievocare.
PET_THRESHOLD = 900
PET_RANGE_CELLS = 6             # raggio (in caselle, distanza Manhattan) entro cui il pet aggancia un nemico da inseguire
PET_SPEED_MULT = 1.15           # leggermente piu' veloce del proprietario, per riuscire a stargli dietro
PET_RETARGET_SECONDS = 0.15     # ogni quanto il pet ricalcola il percorso (verso il proprietario o verso il bersaglio agganciato)
PET_STAY_RANGE = 1              # entro questa distanza (a scacchi) dal proprietario il pet smette di muoversi quando non sta inseguendo nessuno

# ---- bonus 1000 punti: evoluzione della torretta in robot (tasto "9") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "9" il
# giocatore fa evolvere, UNA SOLA VOLTA per round, la propria torretta
# automatica (bonus 600 punti) in una navicella spaziale mobile, MA SOLO SE
# la torretta e' ancora viva sulla mappa (non distrutta dalla corazza di un
# avversario). Da quel momento la navicella smette di restare ferma: insegue
# ATTIVAMENTE il nemico vivo piu' vicino (stesso bfs_path/ricalcolo periodico
# del missile guidato, mai attraverso i muri) invece di pattugliare a caso,
# con la cadenza di fuoco raddoppiata rispetto a una torretta normale e una
# velocita' di movimento dimezzata pari a NORMAL_SPEED * ROBOT_SPEED_MULT
# (per restare bilanciata nonostante il fuoco doppio e l'inseguimento attivo).
ROBOT_THRESHOLD = 1000
ROBOT_FIRE_INTERVAL_SECONDS = TURRET_FIRE_INTERVAL_SECONDS / 2  # cadenza di fuoco raddoppiata
ROBOT_SPEED_MULT = 0.4          # velocita' di movimento della navicella = NORMAL_SPEED * 0.4 (dimezzata rispetto a prima: 0.8 -> 0.4)
ROBOT_WANDER_RETARGET_SECONDS = 0.15  # ogni quanto ricalcola il percorso verso il nemico piu' vicino (che si muove, stessa cadenza del missile)
ROBOT_LEVELUP_DISPLAY_SECONDS = 1.0  # durata della scritta "LEVEL UP" mostrata sopra alla navicella appena evoluta

# ---- bonus 1200 punti: mortaio (tasto "0") ----
# Allo sblocco NON scatta nulla in automatico: premendo il tasto "0" UNA
# SOLA VOLTA il giocatore schiera un mortaio nella cella in cui si trova in
# quel momento. Il mortaio e' permanente (resta sulla mappa per tutto il
# resto del round, anche se il proprietario muore) e individua da solo il
# nemico vivo piu' vicino entro MORTAR_RANGE_CELLS (15) caselle: quando lo
# trova gli spara contro una bomba "in aria" ad arco, che NON segue i
# corridoi e scavalca qualsiasi muro (a differenza di laser/missili/torretta)
# perche' viaggia in linea retta sopra la mappa per MORTAR_FLIGHT_SECONDS_PER_CELL
# secondi per casella percorsa, per poi ricadere ed esplodere sul bersaglio,
# uccidendo (colpendola dall'alto) chiunque si trovi entro
# MORTAR_BLAST_RADIUS_CELLS caselle dal punto di impatto.
MORTAR_THRESHOLD = 1200
MORTAR_RANGE_CELLS = 15                    # raggio (in caselle, distanza Manhattan) entro cui il mortaio individua i nemici
MORTAR_FIRE_INTERVAL_SECONDS = 2.5         # cadenza di fuoco: piu' lenta di laser/torretta, e' un'arma d'area molto piu' potente
MORTAR_FLIGHT_SECONDS_PER_CELL = 0.09      # tempo di volo della bomba per casella di distanza in linea d'aria (arco sopra i muri)
MORTAR_BLAST_RADIUS_CELLS = 1              # raggio dell'esplosione (caselle, distanza Manhattan) intorno al punto di impatto

# ---- bonus 1400 punti: bombolone ad area (tasto "0", DOPO il mortaio) ----
# Il tasto "0" e' lo STESSO usato per il mortaio (bonus 1200 punti): la
# PRIMA pressione schiera il mortaio (vedi try_place_mortar); una volta che
# il mortaio e' gia' stato schierato, la pressione SUCCESSIVA del tasto "0"
# innesca invece, UNA SOLA VOLTA per round, questo bombolone (vedi
# try_place_superbomb). Viene piazzato nella cella corrente del giocatore:
# un ordigno rotondo, grande quanto una casella, dello stesso colore del
# proprietario e visibile a TUTTI i giocatori (niente invisibilita', come la
# corazza). Resta a terra per SUPERBOMB_FUSE_SECONDS, poi esplode con
# un'onda concentrica che distrugge/neutralizza TUTTO cio' che si trova
# entro SUPERBOMB_RADIUS_CELLS caselle (distanza Manhattan) dal centro:
# uccide gli avversari vivi nel raggio (stessa immunita' ghost/protezione
# post-respawn di mortaio/mine/laser) e neutralizza anche mine, torrette,
# robot, pet e mortai avversari trovati nel raggio (vedi explode_superbomb
# in main.py). Le cose del proprietario stesso restano illese.
SUPERBOMB_THRESHOLD = 1400
SUPERBOMB_COUNT = 2                 # numero di bomboloni disponibili una volta sbloccato il bonus 1400 punti (come per le mine)
SUPERBOMB_FUSE_SECONDS = 2.0        # tempo (secondi) prima che il bombolone esploda dopo il piazzamento
SUPERBOMB_RADIUS_CELLS = 8          # raggio dell'esplosione concentrica (caselle, distanza Manhattan)

# ---- bonus 1600 punti: mongolfiera vagante (tasto "0", DOPO il bombolone) ----
# Il tasto "0" e' lo STESSO usato per mortaio (1200) e bombolone (1400): una
# volta che ENTRAMBI sono gia' stati piazzati, la pressione SUCCESSIVA del
# tasto "0" fa librare in aria, UNA SOLA VOLTA per round, questa mongolfiera
# (vedi try_launch_balloon in main.py). Non ha alcun bersaglio: vaga a caso
# su TUTTA la mappa volando sopra ogni muro (esattamente come le bombe di
# mortaio, mai bloccata dal labirinto) e sgancia una bomba ogni
# BALLOON_BOMB_INTERVAL_SECONDS nella propria posizione corrente. A
# differenza del bombolone la bomba sganciata NON ha alcuna miccia: esplode
# ISTANTANEAMENTE con un raggio di BALLOON_BOMB_RADIUS_CELLS caselle
# (distanza Manhattan). La mongolfiera resta in volo per tutto il resto del
# round, anche se il proprietario muore o si disconnette (come mortaio e
# torretta).
BALLOON_THRESHOLD = 1600
BALLOON_SPEED = 1.1                       # celle al secondo: vaga lentamente su tutta la mappa (dimezzata: il doppio piu' lenta)
BALLOON_BOMB_INTERVAL_SECONDS = 3.0       # cadenza di sgancio bombe
BALLOON_BOMB_RADIUS_CELLS = 4             # raggio dell'esplosione istantanea (caselle, distanza Manhattan)
BALLOON_RETARGET_EPSILON = 0.15           # sotto questa distanza dalla meta' ne sceglie subito una nuova a caso

# ---- bonus 1800 punti: blob gelatinoso (tasto "1", DOPO la mongolfiera) ----
# Il tasto "1" e' lo STESSO usato per mortaio (1200), bombolone (1400) e
# mongolfiera (1600): una volta che TUTTI E TRE sono gia' stati piazzati, la
# pressione SUCCESSIVA del tasto "1" piazza, UNA SOLA VOLTA per round,
# questo blob (vedi try_place_blob in main.py). Viene piazzato nella cella
# corrente del giocatore, in mezzo a una strada: un omino di gelatina
# colante, immobile, che blocca fisicamente il passaggio e "mangia" (fa
# perdere una vita, ignorando le protezioni, come una mina) chiunque non
# sia il proprietario ci finisca sopra - senza pero' consumarsi come una
# mina: resta li' pronto a mangiare anche il prossimo che ci passa sopra.
# E' permanente: resta sulla mappa per tutto il resto del round, anche se il
# proprietario muore o si disconnette (come mortaio/torretta/mongolfiera).
# L'UNICO modo per rimuoverlo dalla strada e' sparargli: un colpo laser o un
# missile guidato che lo colpiscono lo distruggono all'istante (vedi
# move_lasers/move_missiles in main.py); niente altro lo scalfisce (non le
# esplosioni di bombolone/mongolfiera, non la corazza laser).
BLOB_THRESHOLD = 1800

# ---- bonus 2000 punti: blob VIVO/vagante (tasto "1", DOPO il blob fermo) ----
# Il tasto "1" e' lo STESSO usato per mortaio (1200), bombolone (1400),
# mongolfiera (1600) e blob (1800): una volta che TUTTI E QUATTRO sono gia'
# stati piazzati/usati, la pressione SUCCESSIVA del tasto "1" risveglia,
# UNA SOLA VOLTA per round, il blob gia' piazzato dal giocatore (vedi
# try_animate_blob in main.py) - a patto che sia ancora vivo sulla mappa
# (non distrutto da un laser/missile nemico, vedi destroy_blob). Da quel
# momento il blob smette di restare fermo: vaga a caso per tutta la mappa
# (via bfs_path, come il robot: mai attraverso i muri) alla stessa velocita'
# della torretta evoluta (NORMAL_SPEED * ROBOT_SPEED_MULT), lasciando dietro
# di se' una nuvola di gas velenoso su OGNI singola casella che calpesta
# camminando (a differenza di quella lasciata dagli impatti del mortaio,
# larga MORTAR_BLAST_RADIUS_CELLS caselle, questa e' larga una sola casella:
# raggio 0), che resta attiva BLOB_POISON_DURATION_SECONDS. E' permanente
# per il resto del round, come tutti gli altri bonus "a comando" da 600
# punti in su.
BLOB_ALIVE_THRESHOLD = 2000
BLOB_ALIVE_SPEED_MULT = ROBOT_SPEED_MULT          # stessa velocita' della torretta evoluta (bonus 1000 punti)

# ---- bonus 2200 punti: muro di spunzoni (tasto "1", DOPO il risveglio del blob) ----
# Il tasto "1" e' lo STESSO della catena mortaio (1200) -> bombolone (1400)
# -> mongolfiera (1600) -> blob (1800) -> risveglio blob (2000): una volta
# esaurita TUTTA la catena precedente, la pressione SUCCESSIVA del tasto "1"
# piazza, UNA SOLA VOLTA per round, un blocco di muro grande esattamente
# quanto un muro normale (una casella) nella cella corrente del giocatore
# (vedi try_place_spike_wall in main.py). Disegnato lato client come
# bombolone/mongolfiera (corpo scuro, strisce nel colore del proprietario,
# teschio bianco) ma con in piu' SPUNZONI ACUMINATI su tutte le superfici
# visibili. E' PERMANENTE per tutto il round (non si sgretola piu' da
# solo). Lo attraversano SOLO il proprietario e i suoi gadget: qualsiasi
# giocatore avversario che ci sbatte contro MUORE all'impatto (vedi
# update_spike_walls), i proiettili avversari (laser/missili) si schiantano
# come contro un muro vero, e pet/torrette-navicella avversari che lo
# toccano vengono distrutti.
SPIKE_WALL_THRESHOLD = 2200
# Il muro di spunzoni e' PERMANENTE: resta in piedi per tutto il round
# (niente piu' durata di 1 minuto). Puo' essere abbattuto solo da un
# fulmine di Tesla avversaria (vedi tesla_zap) o da un fungo atomico.
SPIKE_WALL_HIT_RANGE = 0.6           # distanza (frazione di cella, per asse) sotto la quale un avversario e' considerato "all'impatto" con gli spunzoni


# ---- bonus 2400 punti: Tesla laser (tasto "1", DOPO il muro di spunzoni) ----
# Ultimo gradino della catena del tasto "1". Ispirata alla torre "Tesla" di
# Clash Royale: una struttura fissa, grande quanto una sola casella ma
# visivamente PIU' ALTA di un muro normale (spunta oltre i muri della
# mappa). Proprio perche' e' piu' alta, ignora i muri quando spara: non
# lancia un proiettile che puo' schiantarsi o mancare il bersaglio come la
# torretta normale, ma fulmina ISTANTANEAMENTE, ogni
# TESLA_FIRE_INTERVAL_SECONDS, TUTTO cio' che appartiene alla squadra
# avversaria entro TESLA_RANGE_CELLS caselle (distanza Manhattan, come le
# altre armi ad area del gioco): giocatori vivi, mine, torrette/robot,
# mortai, pet, bomboloni (li fa esplodere a catena), mongolfiere (le fa
# sganciare la bomba a catena), blob gelatinosi e muri di spunzoni
# avversari. E' permanente per tutto il round, come torretta/mortaio/pet.
TESLA_THRESHOLD = 2400
TESLA_FIRE_INTERVAL_SECONDS = 2.5    # cadenza dei fulmini ad area
TESLA_RANGE_CELLS = 8                # raggio d'azione (distanza Manhattan), ignora i muri

# ---- bonus 2600 punti: trappola territoriale a spunzoni (tasto "1", DOPO la Tesla) ----
# Nuovo, ultimo gradino della catena del tasto "1", dopo la Tesla (2400).
# La PRIMA pressione del tasto "1" (una volta esaurita tutta la catena
# precedente) avvia la FASE DI SELEZIONE (vedi try_use_territory_trap in
# main.py): da quel momento, ogni casella di strada NON ancora marcata che
# il giocatore calpesta si illumina del suo colore - ma SOLO ai suoi occhi
# (evento privato, mai incluso nello stato pubblico), cosi' l'avversario
# non puo' scoprire in anticipo dove scattera' la trappola. La selezione
# si chiude da sola non appena sono state marcate TERRITORY_TILES_REQUIRED
# caselle nuove (vedi update_territory_marking): da quel momento le
# caselle restano illuminate (sempre solo per il proprietario) finche' la
# SECONDA pressione del tasto "1" non attiva la trappola vera e propria
# (vedi trigger_territory_trap): in quell'istante, da OGNI casella marcata
# sputano spunzoni acuminati dal pavimento (nel colore del proprietario,
# stavolta visibili a TUTTI) che uccidono all'istante chiunque - avversario
# vivo, senza protezioni attive - si trovi sopra in quel preciso momento.
# Esaurita l'attivazione, il bonus e' consumato per il resto del round,
# come tutti gli altri gradini della catena.
TERRITORY_TRAP_THRESHOLD = 2600
TERRITORY_TILES_REQUIRED = 20        # caselle nuove da calpestare per completare la selezione

# ---- bonus 2800 punti: arbusto spinoso (tasto "1", DOPO la trappola territoriale) ----
# Nuovo, ultimo gradino della catena del tasto "1". Appena piazzato e' un
# piccolo arbusto del colore di chi lo piazza, con le spine e 6 rami, che
# UCCIDE AL CONTATTO qualsiasi avversario (il proprietario e i suoi gadget
# lo attraversano liberamente). Ogni BUSH_GROW_INTERVAL_SECONDS (1 minuto)
# i rami si espandono e si intrecciano occupando UNA nuova casella scelta
# a caso tra quelle adiacenti alle caselle gia' occupate (in tutte le
# direzioni): la crescita non si ferma MAI da sola, e piano piano
# l'arbusto INGHIOTTE anche i muri e tutto cio' che trova nel suo
# tragitto (una cella-muro inghiottita resta invalicabile, ma viene
# ricoperta dai rami; se poi quella cella dell'arbusto viene distrutta,
# il muro sottostante riappare intatto). Il lato client anima ogni nuova
# casella con una crescita GRADUALE dei rami (evento bush_grow), mai
# "all'improvviso".
# L'arbusto puo' essere distrutto SOLO da: bombolone (explode_superbomb),
# bomba di mongolfiera (explode_balloon_bomb) - che nel raggio d'urto
# potano le caselle colpite - e scudo/corazza (un avversario con la
# corazza attiva che tocca una casella dell'arbusto la SPEZZA invece di
# morire, vedi update_bushes). Smette di crescere solo quando e' stato
# eliminato DEL TUTTO (zero caselle rimaste).
BUSH_THRESHOLD = 2800
BUSH_GROW_INTERVAL_SECONDS = 60.0   # un'espansione ad anello al minuto
BUSH_HIT_RANGE = 0.6                # stessa distanza d'impatto del muro di spunzoni (frazione di cella, per asse)
BUSH_MAX_EXPANSIONS = 6             # numero massimo di anelli di crescita (1 casella -> 3x3 -> 5x5 -> 7x7 -> 9x9 -> 11x11 -> 13x13, poi si ferma): dimensione massima raggiunta dopo 6 minuti dal piazzamento

# ---- vite extra ricorrenti ----
# OGNI LIVES_EVERY_POINTS punti (1600, 3200, 4800, ...) si guadagnano
# LIVES_EVERY_AMOUNT vite extra in un colpo solo, senza limite: e' un
# traguardo RICORRENTE, a differenza delle soglie fisse di
# BONUS_THRESHOLDS (vedi Player.next_lives_milestone in main.py).
LIVES_EVERY_POINTS = 1600
LIVES_EVERY_AMOUNT = 3

# ---- bonus 3000 punti: fungo atomico (tasto "1", DOPO l'arbusto spinoso) ----
# Nuovo, vero ultimo gradino della catena del tasto "1". Un piccolo fungo
# (forma classica a cappella + gambo, un po' piu' grande di una mina) nel
# colore di chi lo piazza. Come una mina, resta a terra in attesa: se un
# avversario (o un suo pet) lo CALPESTA, esplode con un GROSSO BOATO
# uccidendo e distruggendo LETTERALMENTE TUTTO cio' che si trova entro
# MUSHROOM_BLAST_RADIUS_CELLS caselle (distanza Manhattan): giocatori
# (corazza e ninja NON proteggono; solo la protezione post-respawn si'),
# mine, torrette/robot, mortai, pet, bomboloni (esplosione a catena),
# mongolfiere (sgancio a catena), blob, muri di spunzoni, Tesla e arbusti
# spinosi avversari. Sull'epicentro resta poi un'area concentrica
# AVVELENATA di pari raggio per MUSHROOM_POISON_DURATION_SECONDS (1
# minuto), con la STESSA logica del veleno del mortaio (una vita di danno
# al secondo a chi ci resta dentro) ma nel COLORE del proprietario.
# Il fungo e' VISIBILE solo entro MUSHROOM_VISIBILITY_RANGE caselle
# (distanza a scacchi/Chebyshev, come le mine ma piu' corta): da piu'
# lontano resta nascosto agli avversari (il proprietario lo vede sempre).
# All'esplosione il client disegna il classico fungo atomico gassoso, nel
# colore del proprietario, per MUSHROOM_CLOUD_SECONDS (2 secondi).
MUSHROOM_THRESHOLD = 3000
MUSHROOM_BLAST_RADIUS_CELLS = 4         # raggio di distruzione (caselle, Manhattan)
MUSHROOM_POISON_DURATION_SECONDS = 60.0 # l'area resta avvelenata per 1 minuto
MUSHROOM_VISIBILITY_RANGE = 3           # visibile solo entro 3 caselle (Chebyshev); il proprietario lo vede sempre
MUSHROOM_CLOUD_SECONDS = 2.0            # durata della nube a fungo (client)
MUSHROOM_RESPAWN_INTERVAL_SECONDS = 60.0 # il fungo "originale" ne genera un altro a caso sulla mappa ogni minuto, finche' non viene fatto esplodere
MUSHROOM_MAX_ACTIVE_PER_OWNER = 5        # tetto massimo di funghi (originale + generati) contemporaneamente vivi per proprietario

# ---- bonus 3200 punti: Tesla occulta (tasto "1", DOPO il fungo atomico) ----
# Nuovo, vero ultimo gradino della catena del tasto "1". A differenza di
# tutti gli altri gradini NON piazza nulla di nuovo sulla mappa: e' un
# POTENZIAMENTO che si applica alla Tesla laser (bonus 2400 punti) gia'
# piazzata dallo stesso giocatore, sempre che sia ancora in piedi (non
# distrutta da un bombolone avversario). Alla pressione del tasto "1"
# (vedi try_activate_occult_tesla in main.py) sotto la Tesla si apre una
# botola di legno che la fa sparire sotto terra: da quel momento la Tesla
# entra in un ciclo perpetuo, per il resto del round, alternando
# OCCULT_TESLA_ATTACK_SECONDS in "modalita' attacco" (fulmina normalmente,
# come prima, ogni TESLA_FIRE_INTERVAL_SECONDS) a OCCULT_TESLA_HIDDEN_SECONDS
# nascosta sottoterra (non fulmina e non puo' essere colpita), per poi
# riemergere in un punto CASUALE della mappa, sempre a esattamente
# OCCULT_TESLA_TELEPORT_DISTANCE_CELLS caselle (distanza Manhattan) da dove
# si trovava prima di sprofondare. Come lo sblocco/utilizzo degli altri
# gradini della catena, e' consumato UNA SOLA VOLTA per giocatore per round
# (vedi Player.occult_tesla_used).
OCCULT_TESLA_THRESHOLD = 3200
OCCULT_TESLA_ATTACK_SECONDS = 8.0          # durata di ogni fase "in superficie", a fulminare come al solito
OCCULT_TESLA_HIDDEN_SECONDS = 1.0          # durata di ogni fase "sottoterra", invisibile e inattiva
OCCULT_TESLA_TELEPORT_DISTANCE_CELLS = 10  # distanza (Manhattan) esatta a cui riemerge rispetto a dove si trovava

# ---- bonus 3400 punti: pozione terremoto (tasto "1", DOPO la Tesla occulta) ----
# Nuovo, vero ultimo gradino della catena del tasto "1", 200 punti dopo la
# Tesla occulta (3200). Una boccetta di pozione QUADRATA che si usa in due
# tempi (vedi try_use_potion in main.py):
#   1) PRIMA pressione del tasto "1" (a fine catena): si entra in modalita'
#      mira; il client mostra un mirino (stile spunzoni dal pavimento) a
#      POTION_THROW_RANGE_CELLS caselle davanti al giocatore, nella
#      direzione in cui sta guardando, cosi' si vede DOVE si sta puntando.
#   2) SECONDA pressione: la boccetta viene LANCIATA verso quel punto,
#      volando SOPRA i muri (come le bombe di mortaio, mai bloccata dal
#      labirinto). L'effetto parte esattamente nel punto d'impatto:
#      un TERREMOTO circolare (raggio POTION_RADIUS_CELLS, distanza
#      Manhattan) che resta attivo per
#      POTION_EFFECT_SECONDS: il client disegna il cerchio del raggio
#      d'azione a terra con crepe animate (terra che si spacca), nel COLORE
#      del giocatore che l'ha lanciata. Per tutta la durata:
#        - i giocatori dentro il cerchio sono RALLENTATI del 50%
#          (POTION_SLOW_MULT, vedi quake_slow_mult in main.py);
#        - TUTTE le strutture e i gadget dentro il cerchio (proprio tutte,
#          anche quelle del proprietario) vengono DISTRUTTE dal terremoto
#          (vedi quake_destroy in main.py): mine, torrette/robot, mortai,
#          pet, bomboloni (esplosione a catena), mongolfiere (sgancio a
#          catena), blob, muri di spunzoni, Tesla (tranne quelle occulte
#          sottoterra in quel momento), arbusti e funghi atomici.
# Come gli altri gradini della catena, e' consumata UNA SOLA VOLTA per
# giocatore per round (vedi Player.potion_used).
POTION_THRESHOLD = 3400
POTION_THROW_RANGE_CELLS = 5     # distanza (caselle) del lancio, oltre i muri
POTION_RADIUS_CELLS = 5          # raggio del terremoto (caselle, distanza Manhattan)
POTION_EFFECT_SECONDS = 5.0      # durata del terremoto a terra
POTION_SLOW_MULT = 0.5           # rallentamento dei giocatori dentro il cerchio (-50%)

# ---- bonus 3600 punti: golem spaccapietra (tasto "1", DOPO la pozione terremoto) ----
# Nuovo, vero ultimo gradino della catena del tasto "1", 200 punti dopo la
# pozione terremoto (3400). Un GROSSO golem di pietra (stile mostro
# roccioso di Clash of Clans: corpo grigio scuro con placche piu' chiare,
# ma occhi e gemme sulla schiena nel COLORE di chi lo piazza), affamato di
# oggetti. Piazzato nella cella corrente del giocatore, DORME per
# GOLEM_WAKE_SECONDS (30 secondi); una volta sveglio punta dritto (via
# bfs_path, mai attraverso i muri) verso il gadget AVVERSARIO piu' vicino
# rimasto sulla mappa, uno alla volta, a GOLEM_SPEED celle al secondo,
# MANGIANDO ogni genere di gadget AVVERSARIO che trova a portata
# (GOLEM_EAT_RANGE_CELLS, distanza a scacchi): mine, torrette/robot,
# mortai, pet, bomboloni (li ingoia SENZA farli esplodere), blob, muri di
# spunzoni, Tesla (tranne quelle occulte sottoterra), arbusti (potati) e
# funghi atomici (senza innescarli). Appena finisce un bersaglio si
# rimette subito a caccia del prossimo piu' vicino; solo quando non resta
# piu' nessun gadget nemico sulla mappa torna a vagare a caso. NON uccide
# i giocatori: li BLOCCA come un muro, ostruendo il passaggio (solo il
# proprietario lo attraversa). Ha una barra della vita sopra la testa: per
# ucciderlo servono GOLEM_HP colpi in totale, con QUALSIASI arma (laser,
# missile, fulmine di Tesla, esplosioni di bombolone/mongolfiera/fungo,
# impatto di mortaio: una vita a colpo); il VELENO invece gli toglie una
# vita al secondo finche' ci resta dentro (GOLEM_POISON_TICK_SECONDS), e
# lo stesso vale per il terremoto della pozione.
GOLEM_THRESHOLD = 3600
GOLEM_HP = 15                    # colpi totali necessari per abbatterlo
GOLEM_WAKE_SECONDS = 30.0        # dorme cosi' a lungo dopo il piazzamento
GOLEM_SPEED = 0.5                # celle al secondo (lento, e' un macigno)
GOLEM_EAT_RANGE_CELLS = 1        # distanza (a scacchi/Chebyshev) a cui divora i gadget
GOLEM_POISON_TICK_SECONDS = 1.0  # il veleno (e il terremoto) gli tolgono una vita al secondo

# ---- bonus 3800 punti: fungo madre magnetico (tasto "1", DOPO il golem) ----
# Nuovo, vero ultimo gradino della catena del tasto "1". NON piazza nulla
# di nuovo: e' un POTENZIAMENTO che si applica al fungo atomico MADRE (il
# "generatore" originale del bonus 3000 punti, vedi try_place_mushroom),
# SE E SOLO SE e' ancora vivo (non calpestato/distrutto). Alla pressione
# del tasto "1" (vedi try_activate_mega_mushroom in main.py) il fungo
# madre si TRASFORMA: diventa alto quanto una Tesla, VISIBILE A TUTTI
# (niente piu' occultamento a 3 caselle), smette di generare altri funghi
# e di esplodere se calpestato, e diventa una vera e propria arma: emette
# onde elettromagnetiche APPENA VISIBILI per MEGA_MUSHROOM_RANGE_CELLS
# caselle che, come un BUCO NERO, attirano verso di lui tutto cio' che e'
# nemico:
#   - i GIOCATORI avversari nel raggio vengono trascinati lungo i
#     corridoi verso il fungo (le onde pilotano il loro movimento) e
#     UCCISI AL CONTATTO (solo ghost e protezione post-respawn salvano);
#   - i GADGET avversari nel raggio vengono risucchiati e INGHIOTTITI
#     (bomboloni e funghi senza esplodere, mongolfiere senza sganciare);
#   - i GOLEM avversari, troppo pesanti per essere inghiottiti, subiscono
#     una vita al secondo finche' restano nel raggio (come col veleno).
# Come gli altri gradini, e' consumato UNA SOLA VOLTA per round.
MEGA_MUSHROOM_THRESHOLD = 3800
MEGA_MUSHROOM_RANGE_CELLS = 3     # raggio delle onde elettromagnetiche (distanza Manhattan)
MEGA_MUSHROOM_KILL_RANGE = 0.8    # distanza (per asse) sotto la quale il contatto e' letale
MEGA_MUSHROOM_GOLEM_TICK_SECONDS = 1.0  # danno ai golem nel raggio: una vita al secondo

# ---- bonus 4000 punti: attacco aereo (tasto "1", DOPO il fungo madre magnetico) ----
# Nuovo, vero ultimo gradino della catena del tasto "1". Si usa in DUE
# tempi (vedi try_use_airstrike in main.py):
#   1) PRIMA pressione: il giocatore diventa IMMOBILE e TOTALMENTE NERO
#      ed entra in modalita' selezione della fila. Il MIRINO e' il muro
#      perimetrale SINISTRO della fila scelta (si parte dal primo muro in
#      basso a sinistra) e si sposta su/giu' con le frecce (il server
#      reinterpreta i normali messaggi "move", vedi airstrike_adjust).
#   2) SECONDA pressione: parte il VERO attacco. Un aereo nel colore del
#      giocatore, col teschio della mongolfiera dipinto sulla fusoliera,
#      attraversa TUTTA la fila da sinistra verso destra bombardando
#      dall'alto verso il basso: elimina OGNI cosa nemica su quella fila
#      (giocatori - ghost e protezione post-respawn esclusi - mine,
#      torrette/robot, mortai, pet, bomboloni ed esplosioni a catena,
#      mongolfiere, blob, muri di spunzoni, Tesla non occulte-sottoterra,
#      arbusti potati, funghi senza innescarli; i golem, come sempre,
#      incassano un colpo). Le cose del giocatore restano illese, e in
#      2v2 anche quelle dei compagni (niente fuoco amico).
AIRSTRIKE_THRESHOLD = 4000
AIRSTRIKE_SPEED = 10.0   # celle al secondo percorse dall'aereo lungo la fila
BLOB_POISON_DURATION_SECONDS = 4.0                # quanto resta a terra ciascuna nuvola della scia del blob vivo
BLOB_EAT_RANGE_CELLS = 1                          # distanza (caselle, stile scacchi/Chebyshev): il blob mangia anche chi non e' esattamente sopra di lui, ma solo adiacente

# All'impatto, oltre al colpo diretto, la bomba lascia a terra una nuvola di
# gas velenoso (stile "pozione veleno" di Clash Royale) che resta attiva per
# POISON_DURATION_SECONDS: chiunque (avversario) si trovi entro
# POISON_RADIUS_CELLS caselle dal centro subisce una vita di danno ogni
# POISON_TICK_SECONDS finche' resta nella nuvola o finche' questa svanisce.
POISON_DURATION_SECONDS = 3.0              # quanto resta a terra la nuvola avvelenata dopo l'impatto
POISON_TICK_SECONDS = 1.0                  # ogni quanto la nuvola toglie una vita a chi vi si trova dentro
POISON_RADIUS_CELLS = MORTAR_BLAST_RADIUS_CELLS  # stesso raggio dell'esplosione diretta (caselle, distanza Manhattan)

# Distanza (in caselle, stile scacchi/Chebyshev) entro la quale le mine
# ALTRUI diventano visibili: da piu' lontano restano nascoste finche' non
# esplodono. Le proprie mine restano sempre visibili a se stessi.
MINE_VISIBILITY_RANGE = 5

# Nome colore (mostrato all'utente, in italiano) -> id colore interno.
# Elenco esteso: ogni giocatore puo' scegliere fino a 2 colori (primario +
# dettaglio/contorno), vedi Player.colors in main.py e COLOR_HEX nel
# client (index.html) per i valori esadecimali corrispondenti.
COLORS = [
    "azzurro", "giallo", "verde", "bianco", "rosa",
    "arancione", "rosso", "viola", "lime", "oro",
    "ciano", "magenta", "grigio", "marrone", "blu_notte", "corallo",
    "nero",
]

# Il nero e' selezionabile SOLO come colore secondario (dettaglio/contorno,
# vedi Player.colors[1]): come colore primario (corpo) sarebbe pressoche'
# invisibile sullo sfondo quasi nero dell'arena. Deve restare in sincronia
# con SECONDARY_ONLY_COLORS in index.html (client). Applicato server-side
# in modo autoritativo nell'handler "select_color" di main.py.
SECONDARY_ONLY_COLORS = {"nero"}

# Personaggi selezionabili in lobby. La forma/dettagli di ciascuno sono
# disegnati lato client (index.html); qui serve solo l'elenco degli id
# validi per la validazione server-side.
CHARACTERS = ["classic", "shark", "hex", "cyclops", "angry", "skull_mask"]

DIRECTIONS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

ROOM_CODE_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")

# 10 mappe distinte, tutte della STESSA dimensione standard 39x19 (stessa
# griglia totale, 741 celle), ma con LOGICHE STRUTTURALI diverse tra loro
# invece della densita' di muri pressoche' uniforme di prima:
#   - labirintiche pure (Lava Cremisi, Ghiaccio Ciano, Foresta Notte):
#     recursive backtracker asimmetrico con pochissimo "braiding" (6-14%),
#     tanti corridoi stretti e serpeggianti, vicoli ciechi veri;
#   - arcade classiche simmetriche (Neon Blu, Rosa Arcade, Indaco
#     Profondo): backtracker su META' griglia poi specchiato in
#     orizzontale (come il Pac-Man originale) con braiding medio
#     (28-42%) per una maglia piu' regolare e riconoscibile;
#   - squadrate/aperte (Giungla Smeraldo, Sabbia Ambra, Corallo
#     Tramonto): come sopra ma con braiding alto (36-55%) e alcune
#     stanze rettangolari scavate a mano (radure/caverne) per ampi
#     spazi aperti;
#   - Violetto Regale: simmetrica con una sala centrale a cornice (stile
#     "sala del trono").
# Ognuna e' stata verificata via flood-fill (tutte le celle libere
# raggiungibili tra loro, nessuna zona isolata => sempre giocabile) e via
# BFS tra tutti gli spawn point. I 4 angoli (1,1) / (w-2,1) / (1,h-2) /
# (w-2,h-2) sono garantiti aperti: servono come sede dei portali
# diagonali (vedi compute_portals in main.py). Ogni tema ha, oltre ai
# colori e alle particelle atmosferiche ("fx"), anche un set di
# decorazioni cotte sui MURI ("decor", vedi drawWallDecor in index.html):
# non tocca mai il pavimento per non disturbare la vista dei personaggi.
MAZES = [
    {
        "name": 'Cripta Cremisi',
        "maze": [
            '#######################################',
            '#.......#...............#.............#',
            '#######.###.###.#########.#.#########.#',
            '#.....#...#.#.#.#.........#.#.#.......#',
            '#.###.###.#.#.#.#.#########.#.#.#######',
            '#.#.#...#.#.#.#.#...#.......#...#.....#',
            '#.#.###.#.#.#.#.###.#.#######.###.#.#.#',
            '#.....#...#...#.....#...#...#.#...#.#.#',
            '#####.#########.#######.#.#.#.#####.#.#',
            '#...#.#.......#.......#...#.#.....#.#.#',
            '#.#.#.#.#####.#############.#####.#.###',
            '#.#.#...#.....#.....#.......#...#.#...#',
            '#.#######.#####.###.#.#########.#.###.#',
            '#.......#...#.....#.#...#...#...#.#...#',
            '#.###.#####.#.###.#.###.#.#.#.#.#.#.#.#',
            '#.#...#.....#...#.#.....#.#...#.#...#.#',
            '#.#.###.#########.#######.###########.#',
            '#.#...............#...................#',
            '#######################################',
        ],
        "spawn_points": [[2, 1], [36, 1], [3, 15], [37, 15], [19, 9]],
        "theme": {
            'wall': '#2a0606', 'edge': '#c81e2e', 'glow': '#ff5a4d', 'pellet': '#ffb35c', 'bg': '#0d0202', 'fx': 'embers', 'decor': 'lava',
        },
    },
    {
        "name": 'Ciano Arcade',
        "maze": [
            '#######################################',
            '#...#.......#...#.....#...#.......#...#',
            '###.#.###.#.#.#.#.#.#.#.#.#.#.###.#.###',
            '#.#.#...#.#.#.#.#.#.#.#.#.#.#.#...#.#.#',
            '#.#.###.#.#.#.#.#.#.#.#.#.#.#.#.###.#.#',
            '#.#.#...#.#.#.#...#.#...#.#.#.#...#.#.#',
            '#.#.###.#.#.#.###########.#.#.#.###.#.#',
            '#.#...#.#.#.#.............#.#.#.#...#.#',
            '#.###.#.#.#.#######.#######.#.#.#.###.#',
            '#.....#.#.#.#.............#.#.#.#.....#',
            '#.#####.#.#.#.#####.#####.#.#.#.#####.#',
            '#.#...#.#.#...#...#.#...#...#.#.#...#.#',
            '#.#.#.#.#.#####.###.###.#####.#.#.#.#.#',
            '#.#.#...#...#.............#...#...#.#.#',
            '#.#.#######.#.#.#######.#.#.#######.#.#',
            '#.....#.....#.#.........#.#.....#.....#',
            '#####.#.#####.#####.#####.#####.#.#####',
            '#.......#.....................#.......#',
            '#######################################',
        ],
        "spawn_points": [[2, 1], [36, 1], [2, 15], [36, 15], [19, 9]],
        "theme": {
            'wall': '#04222c', 'edge': '#17c9d6', 'glow': '#7df2ff', 'pellet': '#eaffb0', 'bg': '#010b0f', 'fx': 'neon', 'decor': 'crystal',
        },
    },
    {
        "name": 'Giungla Smeraldo',
        "maze": [
            '#######################################',
            '#.#.......#.............#.............#',
            '#.#.#####.#.###########.#.#######.###.#',
            '#.........#.#.........#.#...#...#...#.#',
            '###.......###.#######.#.#.#.###.###.###',
            '#...............#...#.#...#...#...#...#',
            '#.#.......#####.#.#.#.#.#####.#.#.###.#',
            '#.#.....#...............#...#.#.#...#.#',
            '#.#.###.#.#.###.........#.#.#.#.###.#.#',
            '#.#...#...#.#...........#.#.#...#...#.#',
            '#.###.###.#.#.#.........#.#.###.#####.#',
            '#...#.#.#.#.#.#...........#.....#.....#',
            '###.#.#.#.#.#.###.#########.#.......#.#',
            '#.....#...#.#.....#.......#.#.......#.#',
            '#.#########.#####.#.#####.###.......#.#',
            '#.#.........#.....#...#.#...#.......#.#',
            '#.#.#################.#.###.#.#.###.#.#',
            '#.....................#.......#.....#.#',
            '#######################################',
        ],
        "spawn_points": [[3, 1], [36, 1], [3, 15], [37, 15], [19, 9]],
        "theme": {
            'wall': '#062b12', 'edge': '#1fb35a', 'glow': '#8cffb0', 'pellet': '#eaff8f', 'bg': '#01130a', 'fx': 'leaves', 'decor': 'vines',
        },
    },
    {
        "name": 'Bastioni Ambra',
        "maze": [
            '#######################################',
            '#..#################################..#',
            '##................###................##',
            '##.#..#..#..#..#..###.#..#..#..#..#..##',
            '##................###................##',
            '##................###................##',
            '##.#..#..#..#..#..###.#..#..#..#..#..##',
            '##................###................##',
            '#################.....#################',
            '#################.....#################',
            '##................###................##',
            '##.#..#..#..#..#..###.#..#..#..#..#..##',
            '##................###................##',
            '##................###................##',
            '##.#..#..#..#..#..###.#..#..#..#..#..##',
            '##................###................##',
            '##.#################################.##',
            '#..#################################..#',
            '#######################################',
        ],
        "spawn_points": [[2, 1], [36, 1], [2, 15], [36, 15], [19, 9]],
        "theme": {
            'wall': '#3a2405', 'edge': '#e0932c', 'glow': '#ffd27a', 'pellet': '#fff2c8', 'bg': '#150c00', 'fx': 'sand', 'decor': 'hieroglyph',
        },
    },
    {
        "name": 'Sala del Trono Violetta',
        "maze": [
            '#######################################',
            '#.....................................#',
            '#.....................................#',
            '#...##############################....#',
            '#...#.........#...#.#...........#.....#',
            '#...#########.#.#.#.#.#.#######.#.....#',
            '#...#.....#...#.........#.....#.......#',
            '#...#.#####.###.........###.#.###.....#',
            '#...#.......#....#...#....#.#...#.....#',
            '#...#.#######....#...#....#.###.#.....#',
            '#...#.....................#.#...#.....#',
            '#...###########.........###.#.###.....#',
            '#...#.................#.....#.........#',
            '#...###############.##############....#',
            '#...##############################....#',
            '#.....................................#',
            '#.....................................#',
            '#.....................................#',
            '#######################################',
        ],
        "spawn_points": [[2, 1], [36, 1], [2, 15], [36, 15], [19, 9]],
        "theme": {
            'wall': '#210a3a', 'edge': '#a24bff', 'glow': '#e3c6ff', 'pellet': '#ffe9ff', 'bg': '#0a0016', 'fx': 'sparkle', 'decor': 'gems',
        },
    },
]
EXTRA_LANES_PER_SIDE = 2  # mappe allargate: 2 nuove corsie di corridoio su OGNI lato


def expand_maze(rows, lanes=EXTRA_LANES_PER_SIDE):
    """Allarga una mappa di 'lanes' corsie su OGNI lato CONTINUANDO la
    logica del labirinto, senza anelli vuoti: le nuove corsie sono la
    RIFLESSIONE del disegno del labirinto stesso (le prime/ultime righe e
    colonne interne, specchiate verso l'esterno), quindi muri e corridoi
    proseguono lo stesso pattern della mappa originale. La riflessione
    garantisce anche il collegamento: ogni corridoio che tocca il vecchio
    bordo continua nel suo specchio, esattamente come faceva all'interno.
    In piu' vengono scavati due brevi passaggi d'angolo verso (1,1) e
    (w-2,h-2), le celle-sede dei portali diagonali, che devono restare
    aperte come nelle mappe originali. La mappa cresce di 2*lanes celle
    in larghezza e altezza."""
    inner = [r[1:-1] for r in rows[1:-1]]      # il labirinto senza il vecchio bordo

    # Riflessione ORIZZONTALE: ogni riga viene estesa a sinistra e a
    # destra con lo specchio delle proprie prime/ultime 'lanes' colonne.
    def pad_row(r):
        return r[:lanes][::-1] + r + r[-lanes:][::-1]

    padded = [pad_row(r) for r in inner]
    # Riflessione VERTICALE: sopra e sotto si aggiungono gli specchi delle
    # prime/ultime 'lanes' righe (gia' estese in orizzontale), in ordine
    # rovesciato cosi' la riga adiacente al labirinto e' il suo specchio.
    top = [padded[i] for i in range(lanes)][::-1]
    bottom = [padded[-1 - i] for i in range(lanes)]
    core = top + padded + bottom

    new_w = len(core[0]) + 2
    out = ["#" * new_w] + ["#" + r + "#" for r in core] + ["#" * new_w]

    # Le celle (1,1) e (w-2,h-2) DEVONO restare aperte (sede dei portali
    # diagonali, come nelle mappe originali): si scava un breve passaggio
    # d'angolo che le collega alle vecchie celle d'angolo del labirinto -
    # (1,1) e (w-2,h-2) originali, garantite aperte - traslate di 'lanes'.
    grid = [list(r) for r in out]
    h = len(grid)
    w = new_w
    for x in range(1, lanes + 2):
        grid[1][x] = "."                        # tratto orizzontale in alto a sinistra
    for y in range(1, lanes + 2):
        grid[y][lanes + 1] = "."                # tratto verticale fino alla vecchia (1,1)
    for x in range(w - lanes - 3, w - 1):
        grid[h - 2][x] = "."                    # tratto orizzontale in basso a destra
    for y in range(h - lanes - 3, h - 1):
        grid[y][w - lanes - 2] = "."            # tratto verticale fino alla vecchia (w-2,h-2)
    return ["".join(r) for r in grid]


def pick_random_maze():
    """Sceglie casualmente una delle 10 mappe e la ALLARGA di
    EXTRA_LANES_PER_SIDE corsie per lato (vedi expand_maze); gli spawn
    point vengono traslati di conseguenza. Ritorna un dict con
    maze/w/h/spawn_points/theme/name pronto da assegnare a una Room."""
    m = random.choice(MAZES)
    lanes = EXTRA_LANES_PER_SIDE
    rows = expand_maze(m["maze"], lanes)
    return {
        "name": m["name"],
        "maze": rows,
        "w": len(rows[0]),
        "h": len(rows),
        "spawn_points": [[x + lanes, y + lanes] for x, y in m["spawn_points"]],
        "theme": m["theme"],
    }


def is_wall(maze, w, h, x, y):
    if x < 0 or y < 0 or y >= h or x >= w:
        return True
    return maze[y][x] == "#"


def bfs_path(maze, w, h, start, goal):
    """Percorso piu' breve (in celle, esclusa quella di partenza) da start a
    goal dentro il labirinto, via breadth-first search: e' cio' che rende il
    missile del bonus 400 punti "guidato" (segue i corridoi, non attraversa
    mai un muro) invece che un proiettile a linea retta come il laser.
    Ritorna None se il bersaglio non e' raggiungibile (non dovrebbe mai
    succedere: tutte le mappe sono garantite completamente connesse)."""
    if start == goal:
        return []
    frontier = deque([start])
    came_from = {start: None}
    while frontier:
        cur = frontier.popleft()
        if cur == goal:
            break
        cx, cy = cur
        for ddx, ddy in DIRECTIONS.values():
            nxt = (cx + ddx, cy + ddy)
            if nxt in came_from:
                continue
            if is_wall(maze, w, h, nxt[0], nxt[1]):
                continue
            came_from[nxt] = cur
            frontier.append(nxt)
    if goal not in came_from:
        return None
    path = []
    cur = goal
    while cur != start:
        path.append(cur)
        cur = came_from[cur]
    path.reverse()
    return path


def build_distance_field(maze, w, h, goal):
    """BFS "invertita": invece di cercare un percorso da UN punto a un
    goal, parte dal goal e calcola in un solo giro O(V+E) la distanza (in
    celle) verso OGNI cella raggiungibile del labirinto. Il risultato e'
    un "flow field": chiunque insegua quel goal non deve piu' rifare una
    bfs_path completa ad ogni replan, gli basta guardare le proprie 4
    celle adiacenti e muoversi verso quella con distanza minore (vedi
    next_step_from_field, O(1)/O(4)). Il vantaggio si accumula soprattutto
    quando piu' entita' (missili, pet, robot, golem...) inseguono lo
    stesso bersaglio nello stesso tick: il campo si calcola una volta
    sola e viene riusato da tutte (vedi FlowFieldCache)."""
    dist = {goal: 0}
    frontier = deque([goal])
    while frontier:
        cx, cy = frontier.popleft()
        d = dist[(cx, cy)]
        for ddx, ddy in DIRECTIONS.values():
            nxt = (cx + ddx, cy + ddy)
            if nxt in dist or is_wall(maze, w, h, nxt[0], nxt[1]):
                continue
            dist[nxt] = d + 1
            frontier.append(nxt)
    return dist


def next_step_from_field(dist_field, cur):
    """Dato un distance field gia' calcolato verso un goal (vedi
    build_distance_field), ritorna la cella adiacente libera che riduce
    la distanza dal goal, oppure None se cur non e' nel campo
    (irraggiungibile) o e' gia' il goal stesso. O(1) invece di O(V+E)."""
    cur_d = dist_field.get(cur)
    if cur_d is None or cur_d == 0:
        return None
    cx, cy = cur
    best_cell, best_d = None, cur_d
    for ddx, ddy in DIRECTIONS.values():
        nxt = (cx + ddx, cy + ddy)
        nd = dist_field.get(nxt)
        if nd is not None and nd < best_d:
            best_d, best_cell = nd, nxt
    return best_cell


class FlowFieldCache:
    """Cache condivisa (per Room) dei distance-field verso i goal
    correntemente inseguiti da missili/pet/robot/golem. E' la vera cassa
    di risonanza dell'ottimizzazione: se in uno stesso tick 3 golem
    inseguono lo stesso gadget nemico, il campo si calcola UNA sola
    volta invece di 3, e nei tick successivi (finche' il goal resta lo
    stesso) non si ricalcola affatto. Va istanziata una volta per mappa
    (i muri sono statici per round, vedi Room.pick_new_map) e interrogata
    passando ogni volta il tick corrente: cambiare tick invalida
    automaticamente le entry stantie senza doverle svuotare a mano."""

    def __init__(self, maze, w, h):
        self.maze, self.w, self.h = maze, w, h
        self._fields = {}       # goal -> dist field
        self._built_at = {}     # goal -> ultimo tick in cui e' stato ricalcolato

    def get_field(self, goal, tick):
        if self._built_at.get(goal) != tick:
            self._fields[goal] = build_distance_field(self.maze, self.w, self.h, goal)
            self._built_at[goal] = tick
        return self._fields[goal]

    def next_step(self, cur, goal, tick):
        """Prossima cella verso 'goal' partendo da 'cur', O(1) ammortizzato."""
        if cur == goal:
            return None
        return next_step_from_field(self.get_field(goal, tick), cur)


class SpatialGrid:
    """Griglia spaziale (spatial hashing) per accelerare le query "chi si
    trova esattamente/vicino a questa cella?" che altrimenti richiedono
    un ciclo su tutti i giocatori della stanza. Va ricostruita una volta
    per tick (self.rebuild, O(N) con N = numero di entita': trascurabile)
    e poi risponde in O(1) alle query per-cella, che invece nel vecchio
    codice venivano ripetute decine di volte per tick (un laser per ogni
    cella percorsa, ogni volta che si controlla una collisione)."""

    def __init__(self):
        self.buckets = {}

    def rebuild(self, entities):
        """entities: iterabile di oggetti/dict con attributi/chiavi x, y
        (celle intere)."""
        self.buckets = {}
        for e in entities:
            x, y = (e["x"], e["y"]) if isinstance(e, dict) else (e.x, e.y)
            self.buckets.setdefault((x, y), []).append(e)

    def at_cell(self, x, y):
        """Entita' presenti esattamente sulla cella (x, y). O(1)."""
        return self.buckets.get((x, y), ())


def choose_power_pellet_cells(maze, w, h, count=POWER_PELLET_COUNT):
    """Sceglie 'count' celle libere ben distribuite tra loro (algoritmo
    "farthest point sampling"): si parte dalla cella libera piu' vicina
    all'angolo in alto a sinistra, poi ad ogni passo si aggiunge la cella
    libera piu' lontana (in distanza minima) da quelle gia' scelte. Il
    risultato tende naturalmente a "sparpagliarsi" verso gli estremi/angoli
    della mappa, esattamente come richiesto."""
    floor_cells = [(x, y) for y in range(h) for x in range(w) if maze[y][x] == "."]
    if not floor_cells:
        return []
    count = min(count, len(floor_cells))
    start = min(floor_cells, key=lambda c: c[0] + c[1])
    chosen = [start]
    remaining = set(floor_cells)
    remaining.discard(start)
    while len(chosen) < count and remaining:
        best_cell, best_dist = None, -1
        for c in remaining:
            d = min((c[0] - s[0]) ** 2 + (c[1] - s[1]) ** 2 for s in chosen)
            if d > best_dist:
                best_dist, best_cell = d, c
        chosen.append(best_cell)
        remaining.discard(best_cell)
    return chosen


def encode(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
