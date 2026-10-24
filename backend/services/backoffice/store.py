"""Хранилище backoffice: операторы платформы, их сессии и журнал действий (SQLite).

Пароли — scrypt, токены сессий хранятся только как sha256. В журнале действий нет данных компаний:
только кто (оператор), что (действие), над какой компанией (идентификатор) и введённая оператором причина.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("BACKOFFICE_DB", "../data/backoffice.db")
SESSION_TTL = int(os.environ.get("BACKOFFICE_SESSION_TTL_SECONDS", str(8 * 3600)))
MAX_SESSIONS_PER_OPERATOR = 10
MIN_PASSWORD = 12
MAX_PASSWORD = 128
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# параметры scrypt: n=2**15 требует ~32 МБ, поэтому maxmem задан явно
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 128 * 1024 * 1024

_lock = threading.Lock()
_ready: set[str] = set()


class StoreError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@contextmanager
def db():
    path = os.path.abspath(DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(path)
        try:
            if path not in _ready:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS operators(
                        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, pwd_hash BLOB NOT NULL,
                        created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS operator_sessions(
                        token_hash TEXT PRIMARY KEY, operator_id TEXT NOT NULL, expires_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS operator_sessions_op ON operator_sessions(operator_id);
                    CREATE TABLE IF NOT EXISTS operator_audit(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, operator_id TEXT, operator_email TEXT,
                        action TEXT NOT NULL, company_id TEXT, detail TEXT, outcome TEXT NOT NULL, request_id TEXT);
                    """
                )
                _ready.add(path)
            yield conn
            conn.commit()
        finally:
            conn.close()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    e = email.strip().lower()
    if not EMAIL_RE.match(e) or len(e) > 254:
        raise StoreError("invalid_email")
    return e


def create_operator(email: str, password: str) -> dict:
    e = normalize_email(email)
    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise StoreError("invalid_password")
    salt = secrets.token_bytes(16)
    oid = secrets.token_hex(16)
    h = hash_password(password, salt)
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO operators(id,email,salt,pwd_hash,created_at) VALUES(?,?,?,?,?)", (oid, e, salt, h, time.time())
            )
        except sqlite3.IntegrityError:
            raise StoreError("email_taken")
    return {"operator_id": oid, "email": e}


def bootstrap_from_env() -> str:
    """Создаёт оператора из OPERATOR_EMAIL/OPERATOR_PASSWORD, если оператора с таким email ещё нет. Возвращает итог для лога."""
    email, password = os.environ.get("OPERATOR_EMAIL", ""), os.environ.get("OPERATOR_PASSWORD", "")
    if not email or not password:
        return "skipped"
    try:
        create_operator(email, password)
    except StoreError as e:
        return "exists" if e.code == "email_taken" else f"rejected:{e.code}"
    return "created"


def verify_login(email: str, password: str) -> dict | None:
    """Проверка пароля за одинаковое время для существующего и несуществующего оператора."""
    e = email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT id, salt, pwd_hash, disabled FROM operators WHERE email=?", (e,)).fetchone()
    salt = row[1] if row else b"\0" * 16
    h = hash_password(password, salt)
    if not row or row[3] or not hmac.compare_digest(h, row[2]):
        return None
    return {"operator_id": row[0], "email": e}


def issue_session(operator_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM operator_sessions WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO operator_sessions VALUES(?,?,?)", (token_hash(token), operator_id, now + SESSION_TTL))
        # оставляем только последние сессии оператора
        conn.execute(
            "DELETE FROM operator_sessions WHERE operator_id=? AND token_hash NOT IN"
            " (SELECT token_hash FROM operator_sessions WHERE operator_id=? ORDER BY expires_at DESC LIMIT ?)",
            (operator_id, operator_id, MAX_SESSIONS_PER_OPERATOR),
        )
    return token


def session_operator(token: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT o.id, o.email, s.expires_at FROM operator_sessions s JOIN operators o ON o.id=s.operator_id"
            " WHERE s.token_hash=? AND o.disabled=0",
            (token_hash(token),),
        ).fetchone()
    if not row or row[2] < time.time():
        return None
    return {"operator_id": row[0], "email": row[1]}


def end_session(token: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM operator_sessions WHERE token_hash=?", (token_hash(token),))


def operator_by_id(operator_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT id, email FROM operators WHERE id=? AND disabled=0", (operator_id,)).fetchone()
    return {"operator_id": row[0], "email": row[1]} if row else None


def audit(operator: dict | None, action: str, company_id: str | None = None, detail: str | None = None,
          outcome: str = "ok", request_id: str = "", email: str | None = None) -> None:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db() as conn:
        conn.execute(
            "INSERT INTO operator_audit(at,operator_id,operator_email,action,company_id,detail,outcome,request_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (at, operator["operator_id"] if operator else None, operator["email"] if operator else email, action,
             company_id, detail[:1000] if detail else None, outcome, request_id or None),
        )


def audit_list(limit: int = 100, company_id: str | None = None) -> list[dict]:
    q = "SELECT id,at,operator_email,action,company_id,detail,outcome FROM operator_audit"
    args: list = []
    if company_id:
        q += " WHERE company_id=?"
        args.append(company_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
    keys = ("id", "at", "operator", "action", "company_id", "detail", "outcome")
    return [dict(zip(keys, r)) for r in rows]
