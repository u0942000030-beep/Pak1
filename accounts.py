# -*- coding: utf-8 -*-
"""
accounts.py - Gestione account utente per Robot Arena.

Persistenza su SQLite (file singolo, zero dipendenze esterne).
Password mai salvate in chiaro: hash PBKDF2-HMAC-SHA256 con salt
casuale per utente (200k iterazioni, stdlib 'hashlib', niente
librerie esterne come bcrypt/argon2 - coerente col resto del progetto).

Uso tipico:

    conn = get_connection("robot_arena.db")
    init_db(conn)

    try:
        account = create_account(conn, "savvynick", "nick@example.com", "password123")
    except AccountError as e:
        print("Errore registrazione:", e)

    account = authenticate(conn, "savvynick", "password123")
    if account is None:
        print("Credenziali errate")
"""

import hashlib
import re
import secrets
import sqlite3
import time


# ---------------------------------------------------------------------------
# Eccezioni
# ---------------------------------------------------------------------------

class AccountError(Exception):
    """Errore applicativo (username duplicato, password troppo corta, ecc.).
    Il messaggio e' pensato per essere mostrato direttamente all'utente."""
    pass


# ---------------------------------------------------------------------------
# Parametri hashing password
# ---------------------------------------------------------------------------

_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )


def _make_password_hash(password: str) -> str:
    """Ritorna una stringa 'salt_hex$hash_hex' da salvare nel DB."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _hash_password(password, salt)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    actual = _hash_password(password, salt)
    return secrets.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Connessione e schema
# ---------------------------------------------------------------------------

def get_connection(db_path: str = "robot_arena.db") -> sqlite3.Connection:
    """Apre (o crea) il file DB. check_same_thread=False perche' il server
    websockets e' asyncio single-thread ma potremmo voler usare la stessa
    connessione da callback diversi."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_login    REAL
);

-- Indici espliciti (UNIQUE sopra li crea gia', ma restano qui per chiarezza
-- se in futuro si passa a confronti case-insensitive con colonne derivate)
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);

-- Sessioni "ricordami": un token opaco e casuale (mai la password) salvato
-- lato client (localStorage) e qui. Alla riapertura della pagina il client
-- manda {type:"login_token", token:...} invece di richiedere username/
-- password: se il token esiste ed e' entro SESSION_MAX_AGE_SECONDS viene
-- fatto il login automatico. Un token e' revocabile subito (logout) o scade
-- da solo dopo tanto tempo, cosi' un dispositivo perso/rubato non resta
-- valido per sempre.
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
"""

# Dopo tanto tempo senza riaprire l'app il token smette di funzionare da
# solo (l'utente rivedra' semplicemente la schermata di login). 60 giorni:
# comodo per non doversi riloggare spesso, ma non "per sempre".
SESSION_MAX_AGE_SECONDS = 60 * 24 * 3600


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Validazione input
# ---------------------------------------------------------------------------

def _validate_username(username: str) -> None:
    if not USERNAME_RE.match(username or ""):
        raise AccountError(
            "Username non valido: 3-20 caratteri, solo lettere/numeri/underscore."
        )


def _validate_email(email: str) -> None:
    if not EMAIL_RE.match(email or ""):
        raise AccountError("Email non valida.")


def _validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AccountError(f"La password deve avere almeno {MIN_PASSWORD_LEN} caratteri.")


# ---------------------------------------------------------------------------
# API pubbliche
# ---------------------------------------------------------------------------

def create_account(conn: sqlite3.Connection, username: str, email: str, password: str) -> dict:
    """Crea un nuovo account. Solleva AccountError se i dati non sono validi
    o se username/email sono gia' in uso. Ritorna il record account (senza
    password_hash) in caso di successo."""
    username = (username or "").strip()
    email = (email or "").strip().lower()

    _validate_username(username)
    _validate_email(email)
    _validate_password(password)

    password_hash = _make_password_hash(password)
    now = time.time()

    try:
        cur = conn.execute(
            "INSERT INTO accounts (username, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, email, password_hash, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        # Distinguiamo quale vincolo UNIQUE e' scattato per un messaggio utile
        msg = str(e).lower()
        if "username" in msg:
            raise AccountError("Username gia' in uso.")
        if "email" in msg:
            raise AccountError("Email gia' registrata.")
        raise AccountError("Username o email gia' in uso.")

    return {
        "id": cur.lastrowid,
        "username": username,
        "email": email,
        "created_at": now,
        "last_login": None,
    }


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """Verifica le credenziali. Ritorna il record account (senza
    password_hash) se corrette, altrimenti None. Aggiorna last_login."""
    username = (username or "").strip()
    row = conn.execute(
        "SELECT id, username, email, password_hash, created_at, last_login "
        "FROM accounts WHERE username = ?",
        (username,),
    ).fetchone()

    if row is None:
        # Nessun account con questo username: eseguiamo comunque un hash
        # "a vuoto" per non rivelare via timing se lo username esiste.
        _hash_password(password, secrets.token_bytes(_SALT_BYTES))
        return None

    if not _verify_password(password, row["password_hash"]):
        return None

    now = time.time()
    conn.execute("UPDATE accounts SET last_login = ? WHERE id = ?", (now, row["id"]))
    conn.commit()

    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "created_at": row["created_at"],
        "last_login": now,
    }


# ---------------------------------------------------------------------------
# Sessioni "ricordami" (login automatico via token, senza ripassare la
# password)
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, account_id: int) -> str:
    """Crea un nuovo token di sessione per l'account e lo ritorna. Da
    chiamare dopo un login/register riuscito e da salvare lato client
    (localStorage)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (token, account_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
        (token, account_id, now, now),
    )
    conn.commit()
    return token


def get_account_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    """Ritorna l'account associato al token se questo esiste ed e' ancora
    valido (non scaduto), altrimenti None. Aggiorna last_seen/last_login."""
    if not token:
        return None
    row = conn.execute(
        "SELECT account_id, last_seen FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None

    now = time.time()
    if now - row["last_seen"] > SESSION_MAX_AGE_SECONDS:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None

    account = get_account(conn, row["account_id"])
    if account is None:
        return None

    conn.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now, token))
    conn.execute("UPDATE accounts SET last_login = ? WHERE id = ?", (now, account["id"]))
    conn.commit()
    account["last_login"] = now
    return account


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    """Invalida un token (logout esplicito)."""
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, username, email, created_at, last_login FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Self-test rapido (python3 accounts.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        conn = get_connection(tmp.name)
        init_db(conn)

        acc = create_account(conn, "savvynick", "nick@example.com", "supersegreta")
        print("Account creato:", acc)

        # Username duplicato -> deve fallire
        try:
            create_account(conn, "savvynick", "altra@example.com", "supersegreta")
            print("ERRORE: doveva sollevare AccountError")
        except AccountError as e:
            print("OK, rifiutato duplicato:", e)

        # Login corretto
        ok = authenticate(conn, "savvynick", "supersegreta")
        print("Login corretto:", ok)
        assert ok is not None

        # Login sbagliato
        bad = authenticate(conn, "savvynick", "password-sbagliata")
        print("Login sbagliato:", bad)
        assert bad is None

        # Login utente inesistente
        ghost = authenticate(conn, "nonesiste", "qualsiasi")
        assert ghost is None
        print("Login utente inesistente: OK (None)")

        # Validazioni
        for bad_input, kind in [
            (("ab", "a@b.com", "password123"), "username corto"),
            (("validuser", "non-una-email", "password123"), "email invalida"),
            (("validuser2", "a@b.com", "corta"), "password corta"),
        ]:
            try:
                create_account(conn, *bad_input)
                print(f"ERRORE: {kind} doveva fallire")
            except AccountError as e:
                print(f"OK, rifiutato {kind}:", e)

        print("\nTutti i test passati.")
    finally:
        os.unlink(tmp.name)
