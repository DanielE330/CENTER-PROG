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

from . import mfa


DB_PATH = os.environ.get("AUTH_DB", "../data/auth.db")
SESSION_TTL = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
MAX_SIGNATURES = 5
MAX_TOKENS = 10
TOKEN_PREFIX = "ckt_"
INVITE_TTL = 7 * 24 * 3600
MAX_MEMBERS = 20
MFA_TTL = 300
MFA_MAX_ATTEMPTS = 5
# срок жизни ссылок из писем; invite — приглашение сотрудника и сброс пароля директором
LINK_TTL = {"verify": 24 * 3600, "reset": 3600, "invite": 72 * 3600}
RESEND_SECONDS = 60
FORGOT_PER_HOUR = 3
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.Lock()
_fails: dict[str, tuple[int, float]] = {}
_mfa_fails: dict[str, tuple[int, float]] = {}


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
            # приглашения в команду: code_hash -> компания (id компании в ledger), роль и необязательный email.
            # Членство и роли живут в ledger (company_members); таблица memberships — наследие версии с tenant_id, читается только миграцией.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS invites(code_hash TEXT PRIMARY KEY, company_id TEXT NOT NULL, role TEXT NOT NULL,"
                " created_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, email TEXT NOT NULL DEFAULT '')"
            )
            icols = [r[1] for r in conn.execute("PRAGMA table_info(invites)")]
            if "tenant_id" in icols:
                conn.execute("ALTER TABLE invites RENAME COLUMN tenant_id TO company_id")
                conn.execute("DELETE FROM invites")  # старые ссылки указывали на владельца, а не на компанию: выпускаются заново
            if "email" not in icols:
                conn.execute("ALTER TABLE invites ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            # 2FA: секрет TOTP только в зашифрованном виде; mfa_required — обязательность (оператор), last_step — защита от повтора кода
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            for name, ddl in (("mfa_secret", "TEXT"), ("mfa_enabled", "INTEGER NOT NULL DEFAULT 0"),
                              ("mfa_required", "INTEGER NOT NULL DEFAULT 0"), ("mfa_last_step", "INTEGER NOT NULL DEFAULT 0")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mfa_pending(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, purpose TEXT NOT NULL,"
                " secret_enc TEXT, attempts INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mfa_backup(code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, used_at REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS mfa_backup_user ON mfa_backup(user_id)")
            # роли и сотрудники: принудительная смена пароля и признак сгенерированного пароля
            ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            for col in ("must_change_password", "password_generated"):
                if col not in ucols:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            # подтверждение почты: пользователи, заведённые до появления писем, считаются подтверждёнными
            if "email_verified" not in ucols:
                conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE users SET email_verified=1 WHERE has_email=1")
            # одноразовые ссылки из писем: подтверждение почты и смена пароля; хранится только sha256
            conn.execute(
                "CREATE TABLE IF NOT EXISTS email_tokens(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,"
                " email TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS email_tokens_user ON email_tokens(user_id, kind)")
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


def _new_session(conn, uid: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), uid, time.time() + SESSION_TTL))
    return token


def _mfa_state(conn, uid: str, password_login: bool) -> str | None:
    """None — второй фактор не нужен; 'setup' — обязателен, но ещё не настроен; 'verify' — нужен код."""
    enabled, required, has_email = conn.execute(
        "SELECT mfa_enabled, mfa_required, has_email FROM users WHERE id=?", (uid,)).fetchone()
    if enabled:
        return "verify"
    if required or (password_login and has_email and os.environ.get("AUTH_REQUIRE_2FA") == "1"):
        return "setup"
    return None


def _mfa_pending(conn, uid: str, purpose: str) -> str:
    conn.execute("DELETE FROM mfa_pending WHERE expires_at < ? OR user_id=?", (time.time(), uid))
    tok = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO mfa_pending(token_hash,user_id,purpose,expires_at) VALUES(?,?,?,?)",
                 (token_hash(tok), uid, purpose, time.time() + MFA_TTL))
    return tok


@app.post("/internal/v1/login")
def login(c: Credentials):
    email = c.email.strip().lower()
    cnt, until = _fails.get(email, (0, 0.0))
    if cnt >= MAX_FAILS and time.time() < until:
        raise HTTPException(429, {"code": "too_many_attempts"})
    with db() as conn:
        row = conn.execute(
            "SELECT id, salt, pwd_hash, must_change_password, password_generated, email_verified FROM users"
            " WHERE email=? AND has_email=1",
            (email,),
        ).fetchone()
        # одинаковая работа для существующего и несуществующего пользователя
        salt = row[1] if row else b"\0" * 16
        h = hash_password(c.password, salt)
        if not row or not hmac.compare_digest(h, row[2]):
            _fails[email] = (cnt + 1, time.time() + LOCK_SECONDS)
            raise HTTPException(401, {"code": "invalid_credentials"})
        _fails.pop(email, None)
        state = _mfa_state(conn, row[0], True)
        if state:
            # пароль верен, но сессии ещё нет: выдаём короткоживущий билет для второго шага
            return {"mfa": state, "mfa_token": _mfa_pending(conn, row[0], state), "user_id": row[0], "email": email}
        token = _new_session(conn, row[0])
    return {
        "token": token, "user_id": row[0], "email": email, "ttl": SESSION_TTL,
        "must_change_password": bool(row[3]), "password_generated": bool(row[4]), "email_verified": bool(row[5]),
    }


class MfaTokenIn(BaseModel):
    mfa_token: str


class MfaCodeIn(BaseModel):
    mfa_token: str
    code: str


def _pending(conn, tok: str, purpose: str):
    row = conn.execute(
        "SELECT user_id, secret_enc, attempts FROM mfa_pending WHERE token_hash=? AND purpose=? AND expires_at>=?",
        (token_hash(tok), purpose, time.time())).fetchone()
    if not row:
        raise HTTPException(401, {"code": "mfa_session_expired"})
    cnt, until = _mfa_fails.get(row[0], (0, 0.0))
    if cnt >= MAX_FAILS and time.time() < until:
        raise HTTPException(429, {"code": "too_many_attempts"})
    return row


def _mfa_fail(conn, tok: str, uid: str):
    cnt, _ = _mfa_fails.get(uid, (0, 0.0))
    _mfa_fails[uid] = (cnt + 1, time.time() + LOCK_SECONDS)
    conn.execute("UPDATE mfa_pending SET attempts=attempts+1 WHERE token_hash=?", (token_hash(tok),))
    if conn.execute("SELECT attempts FROM mfa_pending WHERE token_hash=?", (token_hash(tok),)).fetchone()[0] >= MFA_MAX_ATTEMPTS:
        conn.execute("DELETE FROM mfa_pending WHERE token_hash=?", (token_hash(tok),))  # после 5 ошибок нужен новый вход по паролю
    conn.commit()
    raise HTTPException(401, {"code": "invalid_mfa_code"})


def _mfa_done(conn, tok: str, uid: str, extra: dict | None = None) -> dict:
    _mfa_fails.pop(uid, None)
    conn.execute("DELETE FROM mfa_pending WHERE token_hash=?", (token_hash(tok),))
    email, must, gen = conn.execute("SELECT email, must_change_password, password_generated FROM users WHERE id=?", (uid,)).fetchone()
    return {
        "token": _new_session(conn, uid), "user_id": uid, "email": email, "ttl": SESSION_TTL,
        "must_change_password": bool(must), "password_generated": bool(gen), **(extra or {}),
    }


def _take_step(conn, uid: str, step: int) -> bool:
    """Код каждого шага принимается один раз: шаг должен быть строго больше последнего принятого."""
    return conn.execute("UPDATE users SET mfa_last_step=? WHERE id=? AND mfa_last_step<?", (step, uid, step)).rowcount == 1


@app.post("/internal/v1/mfa/setup")
def mfa_setup(b: MfaTokenIn):
    """Первый вход: выдаёт секрет и otpauth-ссылку. Секрет включается только после подтверждения кодом."""
    with db() as conn:
        uid, enc, _ = _pending(conn, b.mfa_token, "setup")
        secret = mfa.unseal(DB_PATH, enc) if enc else mfa.new_secret()
        if not enc:
            conn.execute("UPDATE mfa_pending SET secret_enc=? WHERE token_hash=?",
                         (mfa.seal(DB_PATH, secret), token_hash(b.mfa_token)))
        email = conn.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()[0]
        uri = mfa.otpauth_uri(secret, email)
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": mfa.qr_svg(uri)}


@app.post("/internal/v1/mfa/enable")
def mfa_enable(b: MfaCodeIn):
    with db() as conn:
        uid, enc, _ = _pending(conn, b.mfa_token, "setup")
        if not enc:
            raise HTTPException(409, {"code": "mfa_setup_not_started"})
        secret = mfa.unseal(DB_PATH, enc)
        step = mfa.match_step(secret, b.code)
        if step is None:
            _mfa_fail(conn, b.mfa_token, uid)
        conn.execute("UPDATE users SET mfa_secret=?, mfa_enabled=1, mfa_last_step=? WHERE id=?",
                     (enc, step, uid))
        codes = mfa.new_backup_codes()
        conn.execute("DELETE FROM mfa_backup WHERE user_id=?", (uid,))
        conn.executemany("INSERT INTO mfa_backup(code_hash,user_id) VALUES(?,?)", [(mfa.backup_hash(c), uid) for c in codes])
        return _mfa_done(conn, b.mfa_token, uid, {"backup_codes": codes})


@app.post("/internal/v1/mfa/verify")
def mfa_verify(b: MfaCodeIn):
    """Код из приложения или один из резервных кодов (каждый действует один раз)."""
    with db() as conn:
        uid, _, _ = _pending(conn, b.mfa_token, "verify")
        enc = conn.execute("SELECT mfa_secret FROM users WHERE id=?", (uid,)).fetchone()[0]
        step = mfa.match_step(mfa.unseal(DB_PATH, enc), b.code) if enc else None
        if step is not None:
            if not _take_step(conn, uid, step):
                _mfa_fail(conn, b.mfa_token, uid)  # повтор уже использованного кода
            return _mfa_done(conn, b.mfa_token, uid)
        used = conn.execute("UPDATE mfa_backup SET used_at=? WHERE code_hash=? AND user_id=? AND used_at IS NULL",
                            (time.time(), mfa.backup_hash(b.code), uid)).rowcount
        if used != 1:
            _mfa_fail(conn, b.mfa_token, uid)
        left = conn.execute("SELECT COUNT(*) FROM mfa_backup WHERE user_id=? AND used_at IS NULL", (uid,)).fetchone()[0]
        return _mfa_done(conn, b.mfa_token, uid, {"backup_codes_left": left})


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
                "UPDATE users SET email=?, salt=?, pwd_hash=?, has_email=1, email_verified=0 WHERE id=?",
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
        conn.execute("DELETE FROM email_tokens WHERE user_id=?", (uid,))
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
                "UPDATE users SET email=?, salt=?, pwd_hash=?, has_email=1, must_change_password=0, password_generated=0,"
                " email_verified=0 WHERE id=?",
                (email, salt, hash_password(b.password, salt), uid),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "email_taken"})
        return _methods(conn, uid)


# ---------- ссылки из писем: подтверждение почты и смена пароля ----------
class LinkIn(BaseModel):
    kind: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


def _issue_link(conn, uid: str, kind: str, email: str) -> str:
    """Новая ссылка отменяет прежние того же назначения: действует только последняя."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    family = ("reset", "invite") if kind in ("reset", "invite") else (kind,)
    conn.execute(
        "UPDATE email_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL AND kind IN (%s)" % ",".join("?" * len(family)),
        (now, uid, *family),
    )
    conn.execute("DELETE FROM email_tokens WHERE expires_at < ?", (now - 7 * 24 * 3600,))
    conn.execute(
        "INSERT INTO email_tokens VALUES(?,?,?,?,?,?,NULL)", (token_hash(token), uid, kind, email, now, now + LINK_TTL[kind])
    )
    return token


def _take_link(conn, token: str, kinds: tuple[str, ...]):
    """Ссылка действует один раз и до истечения срока; иначе 404 token_invalid (причину не уточняем)."""
    row = conn.execute(
        "SELECT t.user_id, t.email, t.kind, t.expires_at, t.used_at, u.email FROM email_tokens t"
        " JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
        (token_hash(token),),
    ).fetchone()
    # адрес мог смениться после отправки письма: старая ссылка его не подтверждает
    if not row or row[2] not in kinds or row[4] is not None or row[3] < time.time() or row[1] != row[5]:
        raise HTTPException(404, {"code": "token_invalid"})
    conn.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=?", (time.time(), token_hash(token)))
    return row[0], row[1]


@app.post("/internal/v1/users/{uid}/email-link")
def user_email_link(uid: str, b: LinkIn):
    """Выпускает ссылку для письма (verify | invite). Повторное письмо подтверждения — не чаще раза в минуту."""
    if b.kind not in ("verify", "invite"):
        raise HTTPException(422, {"code": "invalid_kind"})
    with db() as conn:
        row = conn.execute("SELECT email, has_email, email_verified FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        if not row[1]:
            raise HTTPException(409, {"code": "no_email"})
        if b.kind == "verify":
            if row[2]:
                raise HTTPException(409, {"code": "already_verified"})
            last = conn.execute(
                "SELECT MAX(created_at) FROM email_tokens WHERE user_id=? AND kind='verify'", (uid,)
            ).fetchone()[0]
            if last and time.time() - last < RESEND_SECONDS:
                raise HTTPException(429, {"code": "too_many_requests"})
        return {"token": _issue_link(conn, uid, b.kind, row[0]), "email": row[0]}


@app.post("/internal/v1/email/verify")
def email_verify(b: TokenIn):
    with db() as conn:
        uid, email = _take_link(conn, b.token, ("verify",))
        conn.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
    return {"user_id": uid, "email": email, "email_verified": True}


@app.post("/internal/v1/password/forgot")
def password_forgot(b: ForgotIn):
    """{} для неизвестного адреса и при превышении лимита: вызывающий всегда отвечает одинаково и не раскрывает,
    есть ли такой пользователь. Иначе {user_id, email, token} — ссылку отправляет notify."""
    email = b.email.strip().lower()
    if not valid_email(email):
        return {}
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=? AND has_email=1", (email,)).fetchone()
        if not row:
            return {}
        n = conn.execute(
            "SELECT COUNT(*) FROM email_tokens WHERE user_id=? AND kind='reset' AND created_at>?", (row[0], time.time() - 3600)
        ).fetchone()[0]
        if n >= FORGOT_PER_HOUR:
            return {}
        return {"user_id": row[0], "email": email, "token": _issue_link(conn, row[0], "reset", email)}


@app.post("/internal/v1/password/reset")
def password_reset(b: ResetIn):
    """Новый пароль по ссылке из письма (сброс, приглашение). Ссылка подтверждает и почту; все сессии закрываются."""
    if not valid_password(b.password):
        raise HTTPException(422, {"code": "invalid_password"})
    with db() as conn:
        uid, email = _take_link(conn, b.token, ("reset", "invite"))
        salt = secrets.token_bytes(16)
        conn.execute(
            "UPDATE users SET salt=?, pwd_hash=?, must_change_password=0, password_generated=0, email_verified=1 WHERE id=?",
            (salt, hash_password(b.password, salt), uid),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    _fails.pop(f"pw:{uid}", None)
    _fails.pop(email, None)
    return {"user_id": uid, "email": email}


@app.get("/internal/v1/users/email-status")
def users_email_status(user_ids: str):
    """Адреса и признак подтверждения для рассылок notify: `?user_ids=a,b,c`. Пользователи только с ЭЦП не попадают."""
    ids = [i for i in (x.strip() for x in user_ids.split(",")) if i][:2000]
    if not ids:
        return {"items": []}
    with db() as conn:
        rows = conn.execute(
            "SELECT id, email, email_verified FROM users WHERE has_email=1 AND id IN (%s)" % ",".join("?" * len(ids)), ids
        ).fetchall()
    return {"items": [{"user_id": r[0], "email": r[1], "verified": bool(r[2])} for r in rows]}


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
            "SELECT id, email, has_email, must_change_password, password_generated, email_verified FROM users WHERE id=?",
            (u.user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, {"code": "user_not_found"})
        # вход по ЭЦП не обходит 2FA: у учётной записи с включённым или обязательным вторым фактором сессию даёт только вход по паролю и коду
        if _mfa_state(conn, row[0], False):
            raise HTTPException(403, {"code": "mfa_required"})
        token = _new_session(conn, row[0])
    return {
        "token": token, "ttl": SESSION_TTL, "user_id": row[0], "email": row[1], "has_email": bool(row[2]),
        "must_change_password": bool(row[3]), "password_generated": bool(row[4]), "email_verified": bool(row[5]),
    }


@app.post("/internal/v1/session")
def session(t: TokenIn):
    with db() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email, s.expires_at, u.has_email, u.must_change_password, u.password_generated,"
            " u.email_verified"
            " FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?",
            (token_hash(t.token),),
        ).fetchone()
    if not row or row[2] < time.time():
        raise HTTPException(401, {"code": "invalid_session"})
    return {
        "user_id": row[0], "email": row[1], "has_email": bool(row[3]),
        "must_change_password": bool(row[4]), "password_generated": bool(row[5]), "email_verified": bool(row[6]),
    }


# ---------- приглашения в команду ----------
# Роли и состав команды хранит ledger, права проверяет api; здесь только одноразовые ссылки-приглашения (код хранится хешем).
INVITE_ROLES = ("admin", "editor", "viewer")


class InviteIn(BaseModel):
    role: str
    actor: str
    email: str = ""  # если задан, принять приглашение сможет только пользователь с этим email


class AcceptIn(BaseModel):
    code: str
    user_id: str


def _label(conn, uid: str) -> tuple[str | None, str]:
    row = conn.execute("SELECT email, has_email FROM users WHERE id=?", (uid,)).fetchone()
    lab = conn.execute("SELECT label FROM identities WHERE user_id=? ORDER BY created_at LIMIT 1", (uid,)).fetchone()
    return (row[0] if row and row[1] else None), (lab[0] if lab else "")


@app.get("/internal/v1/users/labels")
def users_labels(user_ids: str):
    """Подписи участников для журнала: email или название сертификата ЭЦП."""
    ids = [u for u in user_ids.split(",") if u][:200]
    with db() as conn:
        out = {}
        for uid in ids:
            email, label = _label(conn, uid)
            if email or label:
                out[uid] = {"email": email, "label": label}
    return {"items": out}


@app.post("/internal/v1/companies/{cid}/invites", status_code=201)
def invite_create(cid: str, b: InviteIn):
    if b.role not in INVITE_ROLES:
        raise HTTPException(422, {"code": "invalid_role"})
    email = b.email.strip().lower()
    if email and (not EMAIL_RE.match(email) or len(email) > 254):
        raise HTTPException(422, {"code": "invalid_email"})
    with db() as conn:
        code = secrets.token_urlsafe(24)
        now = time.time()
        conn.execute("DELETE FROM invites WHERE expires_at < ?", (now,))
        if conn.execute("SELECT COUNT(*) FROM invites WHERE company_id=?", (cid,)).fetchone()[0] >= MAX_MEMBERS:
            raise HTTPException(409, {"code": "too_many_members"})
        conn.execute("INSERT INTO invites VALUES(?,?,?,?,?,?,?)", (token_hash(code), cid, b.role, b.actor, now, now + INVITE_TTL, email))
    return {"code": code, "role": b.role, "email": email or None, "expires_at": now + INVITE_TTL}


@app.get("/internal/v1/companies/{cid}/invites")
def invites_list(cid: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT code_hash, role, created_at, expires_at, email FROM invites WHERE company_id=? AND expires_at>=? ORDER BY created_at",
            (cid, time.time()),
        ).fetchall()
    return {"items": [{"id": r[0][:12], "role": r[1], "created_at": r[2], "expires_at": r[3], "email": r[4] or None} for r in rows]}


@app.delete("/internal/v1/companies/{cid}/invites/{iid}", status_code=204)
def invite_delete(cid: str, iid: str):
    with db() as conn:
        n = conn.execute("DELETE FROM invites WHERE company_id=? AND substr(code_hash,1,12)=?", (cid, iid)).rowcount
    if n != 1:
        raise HTTPException(404, {"code": "invite_not_found"})


@app.post("/internal/v1/invites/check")
def invite_check(b: AcceptIn):
    """Проверка приглашения без расходования: компания, роль, совпадение email. Расходует его `invites/consume` после записи в ledger."""
    with db() as conn:
        _methods(conn, b.user_id)
        inv = conn.execute("SELECT company_id, role, expires_at, email FROM invites WHERE code_hash=?", (token_hash(b.code.strip()),)).fetchone()
        if not inv or inv[2] < time.time():
            raise HTTPException(404, {"code": "invite_invalid"})
        if inv[3]:
            email, _ = _label(conn, b.user_id)
            if (email or "").lower() != inv[3]:
                raise HTTPException(403, {"code": "invite_email_mismatch"})  # приглашение выписано на другой email
    return {"company_id": inv[0], "role": inv[1]}


@app.post("/internal/v1/invites/consume", status_code=204)
def invite_consume(b: AcceptIn):
    with db() as conn:
        n = conn.execute("DELETE FROM invites WHERE code_hash=?", (token_hash(b.code.strip()),)).rowcount
    if n != 1:
        raise HTTPException(404, {"code": "invite_invalid"})


@app.post("/internal/v1/memberships/legacy/done", status_code=204)
def memberships_legacy_done():
    """Миграция в ledger прошла: старая таблица откладывается, иначе удалённых из команды вернуло бы повторным запуском."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memberships'").fetchone():
            conn.execute("DROP TABLE IF EXISTS memberships_migrated")
            conn.execute("ALTER TABLE memberships RENAME TO memberships_migrated")


@app.get("/internal/v1/memberships/legacy")
def memberships_legacy():
    """Команды из версии с tenant_id (user -> владелец компании, роль owner|admin|analyst|observer): для разовой миграции в ledger."""
    with db() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memberships'").fetchone():
            return {"items": []}
        rows = conn.execute("SELECT user_id, tenant_id, role FROM memberships").fetchall()
    return {"items": [{"user_id": r[0], "tenant_id": r[1], "role": r[2]} for r in rows]}


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
        with db() as conn:
            conn.execute("UPDATE users SET email_verified=1 WHERE email='demo@example.com'")
