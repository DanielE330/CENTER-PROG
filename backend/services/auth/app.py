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
MAX_SIGNATURES = 5
MAX_TOKENS = 10
TOKEN_PREFIX = "ckt_"
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
            # способы входа: email+пароль (has_email) и любое число подписей ЭЦП (identities)
            if "has_email" not in [r[1] for r in conn.execute("PRAGMA table_info(users)")]:
                conn.execute("ALTER TABLE users ADD COLUMN has_email INTEGER NOT NULL DEFAULT 1")
                conn.execute("UPDATE users SET has_email=0 WHERE external_id IS NOT NULL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS identities(external_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                " label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS identities_user ON identities(user_id)")
            conn.execute(
                "INSERT OR IGNORE INTO identities(external_id,user_id,label,created_at)"
                " SELECT external_id,id,'',created_at FROM users WHERE external_id IS NOT NULL"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS api_tokens(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,"
                " token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, created_at REAL NOT NULL, last_used REAL)"
            )
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


class EmailIn(BaseModel):
    email: str
    password: str


class SignatureIn(BaseModel):
    external_id: str
    label: str = ""


class TokenCreateIn(BaseModel):
    name: str


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
        row = conn.execute("SELECT id, salt, pwd_hash FROM users WHERE email=? AND has_email=1", (email,)).fetchone()
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
    """Идемпотентно по подписи (identities): новому пользователю даётся только вход по ЭЦП, пароля у него нет."""
    ext = u.external_id.strip()
    if not (1 <= len(ext) <= 256):
        raise HTTPException(422, {"code": "invalid_external_id"})
    with db() as conn:
        row = conn.execute(
            "SELECT u.id, u.email FROM identities i JOIN users u ON u.id=i.user_id WHERE i.external_id=?", (ext,)
        ).fetchone()
        if row:
            return {"user_id": row[0], "email": row[1], "created": False}
        uid = secrets.token_hex(16)
        email = f"ecp-{hashlib.sha256(ext.encode()).hexdigest()[:16]}@ecp.invalid"
        salt = secrets.token_bytes(16)
        # случайный пароль нигде не раскрывается, поэтому войти по паролю нельзя
        conn.execute(
            "INSERT INTO users(id,email,salt,pwd_hash,created_at,external_id,has_email) VALUES(?,?,?,?,?,?,0)",
            (uid, email, salt, hash_password(secrets.token_urlsafe(32), salt), time.time(), ext),
        )
        conn.execute(
            "INSERT INTO identities VALUES(?,?,?,?)", (ext, uid, u.display_name.strip()[:200], time.time())
        )
    return {"user_id": uid, "email": email, "created": True}


def _methods(conn, uid: str) -> dict:
    row = conn.execute("SELECT email, has_email FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, {"code": "user_not_found"})
    sigs = conn.execute(
        "SELECT external_id, label, created_at FROM identities WHERE user_id=? ORDER BY created_at", (uid,)
    ).fetchall()
    return {
        "email": row[0] if row[1] else None,
        "signatures": [{"id": e, "label": l, "created_at": c} for e, l, c in sigs],
    }


@app.get("/internal/v1/users/{uid}/methods")
def user_methods(uid: str):
    with db() as conn:
        return _methods(conn, uid)


@app.post("/internal/v1/users/{uid}/email")
def user_add_email(uid: str, b: EmailIn):
    email = b.email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(422, {"code": "invalid_email"})
    if not (8 <= len(b.password) <= 128):
        raise HTTPException(422, {"code": "invalid_password"})
    with db() as conn:
        cur = _methods(conn, uid)
        if cur["email"] is not None:
            raise HTTPException(409, {"code": "email_exists"})
        salt = secrets.token_bytes(16)
        try:
            conn.execute(
                "UPDATE users SET email=?, salt=?, pwd_hash=?, has_email=1 WHERE id=?",
                (email, salt, hash_password(b.password, salt), uid),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "email_taken"})
        return _methods(conn, uid)


@app.delete("/internal/v1/users/{uid}/email")
def user_remove_email(uid: str):
    with db() as conn:
        cur = _methods(conn, uid)
        if cur["email"] is None:
            raise HTTPException(404, {"code": "no_email"})
        if not cur["signatures"]:
            raise HTTPException(409, {"code": "last_method"})
        salt = secrets.token_bytes(16)
        conn.execute(
            "UPDATE users SET email=?, salt=?, pwd_hash=?, has_email=0 WHERE id=?",
            (f"removed-{uid[:16]}@ecp.invalid", salt, hash_password(secrets.token_urlsafe(32), salt), uid),
        )
        return _methods(conn, uid)


@app.post("/internal/v1/users/{uid}/signatures")
def user_add_signature(uid: str, b: SignatureIn):
    ext = b.external_id.strip()
    if not (1 <= len(ext) <= 256):
        raise HTTPException(422, {"code": "invalid_external_id"})
    with db() as conn:
        cur = _methods(conn, uid)
        owner = conn.execute("SELECT user_id FROM identities WHERE external_id=?", (ext,)).fetchone()
        if owner and owner[0] != uid:
            raise HTTPException(409, {"code": "signature_taken"})
        if not owner:
            if len(cur["signatures"]) >= MAX_SIGNATURES:
                raise HTTPException(409, {"code": "too_many_signatures"})
            conn.execute("INSERT INTO identities VALUES(?,?,?,?)", (ext, uid, b.label.strip()[:200], time.time()))
        return _methods(conn, uid)


@app.delete("/internal/v1/users/{uid}/signatures/{ext}")
def user_remove_signature(uid: str, ext: str):
    with db() as conn:
        cur = _methods(conn, uid)
        if ext not in [s["id"] for s in cur["signatures"]]:
            raise HTTPException(404, {"code": "signature_not_found"})
        if cur["email"] is None and len(cur["signatures"]) <= 1:
            raise HTTPException(409, {"code": "last_method"})
        conn.execute("DELETE FROM identities WHERE external_id=? AND user_id=?", (ext, uid))
        return _methods(conn, uid)


@app.post("/internal/v1/users/{uid}/tokens", status_code=201)
def token_create(uid: str, b: TokenCreateIn):
    name = b.name.strip()
    if not (1 <= len(name) <= 80):
        raise HTTPException(422, {"code": "invalid_token_name"})
    with db() as conn:
        _methods(conn, uid)
        if conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (uid,)).fetchone()[0] >= MAX_TOKENS:
            raise HTTPException(409, {"code": "too_many_tokens"})
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tid, now = secrets.token_hex(8), time.time()
        conn.execute(
            "INSERT INTO api_tokens(id,user_id,name,token_hash,prefix,created_at) VALUES(?,?,?,?,?,?)",
            (tid, uid, name, token_hash(raw), raw[:8], now),
        )
    # токен показывается один раз; в базе остаётся только sha256
    return {"id": tid, "name": name, "token": raw, "prefix": raw[:8], "created_at": now}


@app.get("/internal/v1/users/{uid}/tokens")
def token_list(uid: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,name,prefix,created_at,last_used FROM api_tokens WHERE user_id=? ORDER BY created_at", (uid,)
        ).fetchall()
    return {"items": [{"id": r[0], "name": r[1], "prefix": r[2], "created_at": r[3], "last_used": r[4]} for r in rows]}


@app.delete("/internal/v1/users/{uid}/tokens/{tid}", status_code=204)
def token_delete(uid: str, tid: str):
    with db() as conn:
        if conn.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (tid, uid)).rowcount != 1:
            raise HTTPException(404, {"code": "token_not_found"})


@app.post("/internal/v1/tokens/verify")
def token_verify(t: TokenIn):
    if not t.token.startswith(TOKEN_PREFIX):
        raise HTTPException(401, {"code": "invalid_token"})
    with db() as conn:
        row = conn.execute(
            "SELECT t.id, u.id, u.email FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
            (token_hash(t.token),),
        ).fetchone()
        if not row:
            raise HTTPException(401, {"code": "invalid_token"})
        conn.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (time.time(), row[0]))
    return {"user_id": row[1], "email": row[2]}


@app.post("/internal/v1/sessions/issue")
def sessions_issue(u: IssueIn):
    """Выдаёт сессию уже аутентифицированному вызывающим сервисом пользователю."""
    with db() as conn:
        row = conn.execute("SELECT id, email, has_email FROM users WHERE id=?", (u.user_id,)).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), row[0], time.time() + SESSION_TTL))
    return {"token": token, "ttl": SESSION_TTL, "user_id": row[0], "email": row[1], "has_email": bool(row[2])}


@app.post("/internal/v1/session")
def session(t: TokenIn):
    with db() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email, s.expires_at, u.has_email FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?",
            (token_hash(t.token),),
        ).fetchone()
    if not row or row[2] < time.time():
        raise HTTPException(401, {"code": "invalid_session"})
    return {"user_id": row[0], "email": row[1], "has_email": bool(row[3])}


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
