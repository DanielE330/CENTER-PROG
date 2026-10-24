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
            # роли и сотрудники: принудительная смена пароля и признак сгенерированного пароля
            ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            for col in ("must_change_password", "password_generated"):
                if col not in ucols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id)")
            yield conn
            conn.commit()
        finally:
            conn.close()


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


GEN_LEN = 14
_GEN_LOWER = "abcdefghijkmnopqrstuvwxyz"  # без l
_GEN_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # без I, O
_GEN_DIGIT = "23456789"  # без 0, 1
_GEN_ALL = _GEN_LOWER + _GEN_UPPER + _GEN_DIGIT


def generate_password() -> str:
    """Пароль для сотрудника: 14 символов из криптостойкого источника (secrets), минимум по одному из каждого класса.
    Без визуально похожих символов (0/O, 1/l/I), чтобы его можно было продиктовать."""
    chars = [secrets.choice(_GEN_LOWER), secrets.choice(_GEN_UPPER), secrets.choice(_GEN_DIGIT)]
    chars += [secrets.choice(_GEN_ALL) for _ in range(GEN_LEN - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email)) and len(email) <= 254


def valid_password(password: str) -> bool:
    return 8 <= len(password) <= 128


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


class UserCreateIn(BaseModel):
    email: str
    password: str | None = None
    generate: bool = False


class PasswordIn(BaseModel):
    current: str | None = None
    new: str
    # токен текущей сессии: остальные сессии пользователя после смены пароля аннулируются
    keep_token: str | None = None


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
        row = conn.execute(
            "SELECT id, salt, pwd_hash, must_change_password, password_generated FROM users WHERE email=? AND has_email=1",
            (email,),
        ).fetchone()
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
    return {
        "token": token, "user_id": row[0], "email": email, "ttl": SESSION_TTL,
        "must_change_password": bool(row[3]), "password_generated": bool(row[4]),
    }


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


@app.get("/internal/v1/tokens")
def tokens_of_users(user_ids: str):
    """Токены нескольких пользователей (для директора компании): `?user_ids=a,b,c`. Сами токены не отдаются."""
    ids = [i for i in (x.strip() for x in user_ids.split(",")) if i][:200]
    if not ids:
        return {"items": []}
    with db() as conn:
        rows = conn.execute(
            "SELECT id,user_id,name,prefix,created_at,last_used FROM api_tokens WHERE user_id IN (%s) ORDER BY created_at"
            % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    return {"items": [
        {"id": r[0], "user_id": r[1], "name": r[2], "prefix": r[3], "created_at": r[4], "last_used": r[5]} for r in rows
    ]}


# ---------- сотрудники и пароли ----------
@app.post("/internal/v1/users/create", status_code=201)
def user_create(b: UserCreateIn):
    """Заводит пользователя с паролем: заданным вызывающим сервисом или сгенерированным здесь (generate=true).
    Пароль возвращается ТОЛЬКО для generate и только в этом ответе; нигде не логируется и не хранится открытым.
    Новый пользователь обязан сменить сгенерированный пароль (must_change_password=1) и может сменить заданный."""
    email = b.email.strip().lower()
    if not valid_email(email):
        raise HTTPException(422, {"code": "invalid_email"})
    if b.generate and b.password is not None:
        raise HTTPException(422, {"code": "invalid_password"})
    password = generate_password() if b.generate else b.password
    if password is None or not valid_password(password):
        raise HTTPException(422, {"code": "invalid_password"})
    salt = secrets.token_bytes(16)
    uid = secrets.token_hex(16)
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO users(id,email,salt,pwd_hash,created_at,must_change_password,password_generated)"
                " VALUES(?,?,?,?,?,1,?)",
                (uid, email, salt, hash_password(password, salt), time.time(), 1 if b.generate else 0),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "email_taken"})
    out = {"user_id": uid, "email": email, "must_change_password": True, "password_generated": b.generate}
    if b.generate:
        out["password"] = password
    return out


def _pw_user(conn, uid: str):
    row = conn.execute(
        "SELECT salt, pwd_hash, has_email, must_change_password FROM users WHERE id=?", (uid,)
    ).fetchone()
    if not row:
        raise HTTPException(404, {"code": "user_not_found"})
    if not row[2]:
        raise HTTPException(409, {"code": "no_email"})  # у пользователя только ЭЦП: пароля нет
    return row


@app.post("/internal/v1/users/{uid}/password")
def user_change_password(uid: str, b: PasswordIn):
    """Смена пароля. `current` обязателен, пока пароль не помечен must_change_password (после входа с выданным
    паролем сессия уже подтверждена этим паролем). После смены флаги сбрасываются, остальные сессии закрываются."""
    if not valid_password(b.new):
        raise HTTPException(422, {"code": "invalid_password"})
    key = f"pw:{uid}"
    cnt, until = _fails.get(key, (0, 0.0))
    if cnt >= MAX_FAILS and time.time() < until:
        raise HTTPException(429, {"code": "too_many_attempts"})
    with db() as conn:
        salt, pwd_hash, _, must = _pw_user(conn, uid)
        if not must:
            if b.current is None or not hmac.compare_digest(hash_password(b.current, salt), pwd_hash):
                _fails[key] = (cnt + 1, time.time() + LOCK_SECONDS)
                raise HTTPException(403, {"code": "invalid_current_password"})
        _fails.pop(key, None)
        nsalt = secrets.token_bytes(16)
        conn.execute(
            "UPDATE users SET salt=?, pwd_hash=?, must_change_password=0, password_generated=0 WHERE id=?",
            (nsalt, hash_password(b.new, nsalt), uid),
        )
        if b.keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, token_hash(b.keep_token)))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True, "must_change_password": False}


@app.post("/internal/v1/users/{uid}/reset-password")
def user_reset_password(uid: str):
    """Сброс пароля директором: новый сгенерированный пароль (виден один раз), смена обязательна, все сессии закрыты."""
    password = generate_password()
    with db() as conn:
        _pw_user(conn, uid)
        salt = secrets.token_bytes(16)
        conn.execute(
            "UPDATE users SET salt=?, pwd_hash=?, must_change_password=1, password_generated=1 WHERE id=?",
            (salt, hash_password(password, salt), uid),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    _fails.pop(f"pw:{uid}", None)
    return {"password": password, "must_change_password": True, "password_generated": True}


@app.post("/internal/v1/users/{uid}/revoke-sessions")
def user_revoke_sessions(uid: str):
    """Закрывает все сессии пользователя (бан компании, подозрение на компрометацию). API-токены не затрагиваются."""
    with db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            raise HTTPException(404, {"code": "user_not_found"})
        n = conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,)).rowcount
    return {"revoked": n}


@app.delete("/internal/v1/users/{uid}", status_code=204)
def user_delete(uid: str):
    """Удаляет пользователя вместе с сессиями, API-токенами и привязанными подписями."""
    with db() as conn:
        if conn.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount != 1:
            raise HTTPException(404, {"code": "user_not_found"})
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM api_tokens WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM identities WHERE user_id=?", (uid,))
    _fails.pop(f"pw:{uid}", None)


@app.post("/internal/v1/users/{uid}/email-password")
def user_set_email_password(uid: str, b: EmailIn):
    """Установка email и пароля пользователю, созданному через ЭЦП (директор без пароля). Подписи остаются."""
    email = b.email.strip().lower()
    if not valid_email(email):
        raise HTTPException(422, {"code": "invalid_email"})
    if not valid_password(b.password):
        raise HTTPException(422, {"code": "invalid_password"})
    with db() as conn:
        row = conn.execute("SELECT has_email FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        if row[0]:
            raise HTTPException(409, {"code": "email_exists"})
        salt = secrets.token_bytes(16)
        try:
            conn.execute(
                "UPDATE users SET email=?, salt=?, pwd_hash=?, has_email=1, must_change_password=0, password_generated=0"
                " WHERE id=?",
                (email, salt, hash_password(b.password, salt), uid),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "email_taken"})
        return _methods(conn, uid)


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
        row = conn.execute(
            "SELECT id, email, has_email, must_change_password, password_generated FROM users WHERE id=?", (u.user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), row[0], time.time() + SESSION_TTL))
    return {
        "token": token, "ttl": SESSION_TTL, "user_id": row[0], "email": row[1], "has_email": bool(row[2]),
        "must_change_password": bool(row[3]), "password_generated": bool(row[4]),
    }


@app.post("/internal/v1/session")
def session(t: TokenIn):
    with db() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email, s.expires_at, u.has_email, u.must_change_password, u.password_generated"
            " FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?",
            (token_hash(t.token),),
        ).fetchone()
    if not row or row[2] < time.time():
        raise HTTPException(401, {"code": "invalid_session"})
    return {
        "user_id": row[0], "email": row[1], "has_email": bool(row[3]),
        "must_change_password": bool(row[4]), "password_generated": bool(row[5]),
    }


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
