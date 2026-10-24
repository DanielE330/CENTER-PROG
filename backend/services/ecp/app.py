"""Сервис входа по ЭЦП: одноразовый challenge, проверка подписи CAdES, привязка сертификата к пользователю.

Пользователей и сессии ведёт сервис auth (AUTH_URL); здесь хранятся только challenge и сведения о сертификатах.
"""
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import cades

DB_PATH = os.environ.get("ECP_DB", "data/run/ecp.db")
AUTH_URL = os.environ.get("AUTH_URL", "http://127.0.0.1:8001")
_lock = threading.Lock()

ECP_STATUS = {
    "bad_signature": 422, "no_certificate": 422, "certificate_expired": 401, "certificate_not_yet_valid": 401, "signature_time_invalid": 401,
    "signature_invalid": 401, "invalid_challenge": 401, "verifier_unavailable": 503,
}


@contextmanager
def db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS ecp_challenges(nonce_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ecp_identities(fingerprint TEXT PRIMARY KEY, user_id TEXT NOT NULL,"
                " subject TEXT NOT NULL, issuer TEXT NOT NULL, serial TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            yield conn
            conn.commit()
        finally:
            conn.close()


def nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def auth_client() -> httpx.Client:
    return httpx.Client(base_url=AUTH_URL, timeout=10)


def call_auth(path: str, payload: dict) -> dict:
    try:
        with auth_client() as c:
            r = c.post(path, json=payload)
    except httpx.HTTPError:
        raise HTTPException(503, {"code": "auth_unavailable"})
    if r.status_code != 200:
        raise HTTPException(503, {"code": "auth_unavailable"})
    return r.json()


class EcpLoginIn(BaseModel):
    challenge: str
    signature: str  # base64 откреплённой подписи CAdES (DER)


app = FastAPI(title="ecp-service", docs_url=None, redoc_url=None)


@app.get("/live")
def live():
    return {"status": "ok"}


@app.post("/internal/v1/ecp/challenge")
def ecp_challenge():
    nonce = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM ecp_challenges WHERE expires_at < ?", (time.time(),))
        if conn.execute("SELECT COUNT(*) FROM ecp_challenges").fetchone()[0] > 10000:
            raise HTTPException(429, {"code": "too_many_attempts"})
        conn.execute("INSERT INTO ecp_challenges VALUES(?,?)", (nonce_hash(nonce), time.time() + cades.CHALLENGE_TTL))
    return {"challenge": cades.challenge_text(nonce), "ttl": cades.CHALLENGE_TTL}


def _check_signature(c: EcpLoginIn) -> "cades.Signer":
    """Гасит challenge, разбирает сертификат и проверяет подпись. Бросает HTTPException с кодом ЭЦП."""
    try:
        if not c.challenge.startswith(cades.PREFIX):
            raise cades.EcpError("invalid_challenge")
        nonce = c.challenge[len(cades.PREFIX):]
        with db() as conn:  # одноразовость: challenge гасится до проверки подписи
            cur = conn.execute(
                "DELETE FROM ecp_challenges WHERE nonce_hash=? AND expires_at >= ?", (nonce_hash(nonce), time.time())
            )
            if cur.rowcount != 1:
                raise cades.EcpError("invalid_challenge")
        sig = cades.decode_signature(c.signature)
        signer = cades.parse_signer(sig)
        cades.verify_detached(c.challenge.encode(), sig)
        return signer
    except cades.EcpError as e:
        raise HTTPException(ECP_STATUS.get(e.code, 401), {"code": e.code})


def _company(signer: "cades.Signer") -> dict:
    return {"name": signer.display_name, "inn": signer.inn, "ogrn": signer.ogrn}


@app.post("/internal/v1/ecp/verify")
def ecp_verify(c: EcpLoginIn):
    """Проверяет подпись без входа: для привязки сертификата к уже существующему аккаунту."""
    signer = _check_signature(c)
    return {"external_id": signer.fingerprint, "display_name": signer.display_name, "company": _company(signer)}


@app.post("/internal/v1/ecp/login")
def ecp_login(c: EcpLoginIn):
    signer = _check_signature(c)
    u = call_auth("/internal/v1/users/external",
                  {"external_id": signer.fingerprint, "display_name": signer.display_name})
    if u["created"]:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ecp_identities VALUES(?,?,?,?,?,?)",
                (signer.fingerprint, u["user_id"], signer.subject, signer.issuer, signer.serial, time.time()),
            )
    s = call_auth("/internal/v1/sessions/issue", {"user_id": u["user_id"]})
    return {"token": s["token"], "user_id": s["user_id"], "email": s["email"], "has_email": s.get("has_email", False), "ttl": s["ttl"],
            "display_name": signer.display_name, "new": u["created"], "company": _company(signer)}
