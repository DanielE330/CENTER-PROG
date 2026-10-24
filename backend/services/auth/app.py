"""Внутренний сервис аутентификации: пользователи и серверные сессии.

Доступен только из служебного контура (api вызывает его по localhost).
Пароли и токены в логи не пишутся.
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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


DB_PATH = os.environ.get("AUTH_DB", "../data/auth.db")
SESSION_TTL = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.Lock()
_fails: dict[str, tuple[int, float]] = {}


@contextmanager
def db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,"
                " salt BLOB NOT NULL, pwd_hash BLOB NOT NULL, created_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                " expires_at REAL NOT NULL)"
            )
            # миграция существующей БД: внешний идентификатор (например, отпечаток сертификата ЭЦП)
            if "external_id" not in [r[1] for r in conn.execute("PRAGMA table_info(users)")]:
                conn.execute("ALTER TABLE users ADD COLUMN external_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_external_id ON users(external_id)")
            yield conn
            conn.commit()
        finally:
            conn.close()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Credentials(BaseModel):
    email: str
    password: str


class TokenIn(BaseModel):
    token: str


class ExternalUserIn(BaseModel):
    external_id: str
    display_name: str = ""


class IssueIn(BaseModel):
    user_id: str


app = FastAPI(title="auth-service", docs_url=None, redoc_url=None)


@app.get("/live")
def live():
    return {"status": "ok"}


@app.post("/internal/v1/register", status_code=201)
def register(c: Credentials):
    email = c.email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(422, {"code": "invalid_email"})
    if not (8 <= len(c.password) <= 128):
        raise HTTPException(422, {"code": "invalid_password"})
    salt = secrets.token_bytes(16)
    uid = secrets.token_hex(16)
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO users(id,email,salt,pwd_hash,created_at) VALUES(?,?,?,?,?)",
                (uid, email, salt, hash_password(c.password, salt), time.time()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "email_taken"})
    return {"user_id": uid, "email": email}


@app.post("/internal/v1/login")
def login(c: Credentials):
    email = c.email.strip().lower()
    cnt, until = _fails.get(email, (0, 0.0))
    if cnt >= MAX_FAILS and time.time() < until:
        raise HTTPException(429, {"code": "too_many_attempts"})
    with db() as conn:
        row = conn.execute("SELECT id, salt, pwd_hash FROM users WHERE email=?", (email,)).fetchone()
        # одинаковая работа для существующего и несуществующего пользователя
        salt = row[1] if row else b"\0" * 16
        h = hash_password(c.password, salt)
        if not row or not hmac.compare_digest(h, row[2]):
            _fails[email] = (cnt + 1, time.time() + LOCK_SECONDS)
            raise HTTPException(401, {"code": "invalid_credentials"})
        _fails.pop(email, None)
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), row[0], time.time() + SESSION_TTL)
        )
    return {"token": token, "user_id": row[0], "email": email, "ttl": SESSION_TTL}


@app.post("/internal/v1/users/external")
def users_external(u: ExternalUserIn):
    """Идемпотентно по external_id: создаёт пользователя без возможности входа по паролю."""
    ext = u.external_id.strip()
    if not (1 <= len(ext) <= 256):
        raise HTTPException(422, {"code": "invalid_external_id"})
    with db() as conn:
        row = conn.execute("SELECT id, email FROM users WHERE external_id=?", (ext,)).fetchone()
        if row:
            return {"user_id": row[0], "email": row[1], "created": False}
        uid = secrets.token_hex(16)
        email = f"ecp-{hashlib.sha256(ext.encode()).hexdigest()[:16]}@ecp.invalid"
        salt = secrets.token_bytes(16)
        # случайный пароль нигде не раскрывается, поэтому войти по паролю нельзя
        conn.execute(
            "INSERT INTO users(id,email,salt,pwd_hash,created_at,external_id) VALUES(?,?,?,?,?,?)",
            (uid, email, salt, hash_password(secrets.token_urlsafe(32), salt), time.time(), ext),
        )
    return {"user_id": uid, "email": email, "created": True}


@app.post("/internal/v1/sessions/issue")
def sessions_issue(u: IssueIn):
    """Выдаёт сессию уже аутентифицированному вызывающим сервисом пользователю."""
    with db() as conn:
        row = conn.execute("SELECT id, email FROM users WHERE id=?", (u.user_id,)).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), row[0], time.time() + SESSION_TTL))
    return {"token": token, "ttl": SESSION_TTL, "user_id": row[0], "email": row[1]}


@app.post("/internal/v1/session")
def session(t: TokenIn):
    with db() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?",
            (token_hash(t.token),),
        ).fetchone()
    if not row or row[2] < time.time():
        raise HTTPException(401, {"code": "invalid_session"})
    return {"user_id": row[0], "email": row[1]}


@app.post("/internal/v1/logout", status_code=204)
def logout(t: TokenIn):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(t.token),))


@app.on_event("startup")
def seed_demo():
    if os.environ.get("DEMO_SEED") == "1":
        try:
            register(Credentials(email="demo@example.com", password="demo12345"))
        except HTTPException:
            pass
