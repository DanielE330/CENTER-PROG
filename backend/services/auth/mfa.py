"""Двухфакторная защита: TOTP (RFC 6238) на стандартной библиотеке, шифрование секрета, резервные коды.

Секрет TOTP хранится в БД только в зашифрованном виде (ключ — AUTH_MFA_KEY или файл рядом с БД с правами 600).
Ничего из этого модуля в логи не пишется.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # допускаем сдвиг часов на один шаг в обе стороны
ISSUER = "Центр Прогнозирования"
BACKUP_COUNT = 8


def new_secret() -> str:
    """160 бит в base32 без паддинга: формат, который понимают Google/Microsoft Authenticator и аналоги."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF) % 10**DIGITS
    return str(num).zfill(DIGITS)


def totp(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((time.time() if at is None else at) // STEP))


def match_step(secret: str, code: str, at: float | None = None) -> int | None:
    """Номер 30-секундного шага, под который подошёл код (окно ±1), иначе None. Сравнение за постоянное время."""
    code = code.strip().replace(" ", "")
    if not (len(code) == DIGITS and code.isdigit()):
        return None
    now = int((time.time() if at is None else at) // STEP)
    found = None
    for step in range(now - WINDOW, now + WINDOW + 1):
        if hmac.compare_digest(_hotp(secret, step), code):
            found = step
    return found


def otpauth_uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER, safe='')}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg(data: str) -> str:
    """QR рисуется на сервере: секрет не уходит во внешние сервисы. Если segno не установлен — только текстовый секрет."""
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(data, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")


# ---- шифрование секрета: HMAC-SHA256 в режиме потока + имитовставка (без внешних библиотек) ----

def _key(db_path: str, env_name: str = "AUTH_MFA_KEY") -> bytes:
    env = os.environ.get(env_name, "")
    if env:
        return hashlib.sha256(env.encode()).digest()
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "mfa.key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).digest()


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hmac.new(key, b"enc" + nonce + struct.pack(">I", i), hashlib.sha256).digest()
        i += 1
    return out[:n]


def seal(db_path: str, plain: str, env_name: str = "AUTH_MFA_KEY") -> str:
    key = _key(db_path, env_name)
    nonce = secrets.token_bytes(16)
    data = plain.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode()


def unseal(db_path: str, blob: str, env_name: str = "AUTH_MFA_KEY") -> str:
    key = _key(db_path, env_name)
    raw = base64.urlsafe_b64decode(blob.encode())
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("mfa secret tampered")
    return bytes(a ^ b for a, b in zip(ct, _stream(key, nonce, len(ct)))).decode()


# ---- резервные коды ----

def new_backup_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
            for _ in range(BACKUP_COUNT)]


def backup_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
