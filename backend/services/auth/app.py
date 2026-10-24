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

from . import ecp

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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ecp_challenges(nonce_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ecp_identities(fingerprint TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                " subject TEXT NOT NULL, issuer TEXT NOT NULL, serial TEXT NOT NULL, created_at REAL NOT NULL)"
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


class EcpLoginIn(BaseModel):
    challenge: str
    signature: str  # base64 откреплённой подписи CAdES (DER)


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
                "INSERT INTO users VALUES(?,?,?,?,?)",
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


ECP_STATUS = {
    "bad_signature": 422, "no_certificate": 422, "certificate_expired": 401,
    "signature_invalid": 401, "invalid_challenge": 401, "verifier_unavailable": 503,
}


@app.post("/internal/v1/ecp/challenge")
def ecp_challenge():
    nonce = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM ecp_challenges WHERE expires_at < ?", (time.time(),))
        if conn.execute("SELECT COUNT(*) FROM ecp_challenges").fetchone()[0] > 10000:
            raise HTTPException(429, {"code": "too_many_attempts"})
        conn.execute("INSERT INTO ecp_challenges VALUES(?,?)", (token_hash(nonce), time.time() + ecp.CHALLENGE_TTL))
    return {"challenge": ecp.challenge_text(nonce), "ttl": ecp.CHALLENGE_TTL}


@app.post("/internal/v1/ecp/login")
def ecp_login(c: EcpLoginIn):
    try:
        if not c.challenge.startswith(ecp.PREFIX):
            raise ecp.EcpError("invalid_challenge")
        nonce = c.challenge[len(ecp.PREFIX):]
        with db() as conn:  # одноразовость: challenge гасится до проверки подписи
            cur = conn.execute(
                "DELETE FROM ecp_challenges WHERE nonce_hash=? AND expires_at >= ?", (token_hash(nonce), time.time())
            )
            if cur.rowcount != 1:
                raise ecp.EcpError("invalid_challenge")
        sig = ecp.decode_signature(c.signature)
        signer = ecp.parse_signer(sig)
        ecp.verify_detached(c.challenge.encode(), sig)
    except ecp.EcpError as e:
        raise HTTPException(ECP_STATUS.get(e.code, 401), {"code": e.code})
    with db() as conn:
        row = conn.execute("SELECT user_id FROM ecp_identities WHERE fingerprint=?", (signer.fingerprint,)).fetchone()
        new = row is None
        if new:  # первый вход этим сертификатом: создаём учётную запись без пароля
            uid = secrets.token_hex(16)
            salt = secrets.token_bytes(16)
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?)",
                (uid, f"ecp-{signer.fingerprint[:16]}@ecp.invalid", salt,
                 hash_password(secrets.token_urlsafe(32), salt), time.time()),
            )
            conn.execute(
                "INSERT INTO ecp_identities VALUES(?,?,?,?,?,?)",
                (signer.fingerprint, uid, signer.subject, signer.issuer, signer.serial, time.time()),
            )
        else:
            uid = row[0]
        email = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()[0]
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), uid, time.time() + SESSION_TTL))
    return {"token": token, "user_id": uid, "email": email, "ttl": SESSION_TTL,
            "display_name": signer.display_name, "new": new}


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
