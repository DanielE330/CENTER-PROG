"""SQLite llm_gateway.db: суточный расход токенов по пользователям и аудит обращений (без содержимого)."""
import datetime as dt
import os
import sqlite3
import threading

_lock = threading.Lock()


def db_path() -> str:
    return os.environ.get("LLM_GATEWAY_DB", "llm_gateway.db")


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS usage(user TEXT NOT NULL, day TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user, day))")
    c.execute(
        "CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL, "
        "provider TEXT NOT NULL, model TEXT NOT NULL, tokens INTEGER NOT NULL, status TEXT NOT NULL)"
    )
    return c


def used_today(user: str) -> int:
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT tokens FROM usage WHERE user=? AND day=?", (user, today())).fetchone()
            return row[0] if row else 0
        finally:
            c.close()


def add_usage(user: str, tokens: int) -> None:
    if tokens <= 0:
        return
    with _lock:
        c = _conn()
        try:
            with c:
                c.execute(
                    "INSERT INTO usage(user, day, tokens) VALUES(?,?,?) ON CONFLICT(user, day) DO UPDATE SET tokens=tokens+excluded.tokens",
                    (user, today(), tokens),
                )
        finally:
            c.close()


def record_audit(user: str, provider: str, model: str, tokens: int, status: str) -> None:
    """Только метаданные: время, пользователь, провайдер, число токенов, статус. Текст запроса и ответа не хранится."""
    with _lock:
        c = _conn()
        try:
            with c:
                c.execute(
                    "INSERT INTO audit(ts, user, provider, model, tokens, status) VALUES(?,?,?,?,?,?)",
                    (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), user, provider, model, tokens, status),
                )
        finally:
            c.close()


def audit_rows(limit: int = 50) -> list[dict]:
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT ts, user, provider, model, tokens, status FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            c.close()
    return [dict(zip(("ts", "user", "provider", "model", "tokens", "status"), r)) for r in rows]
