"""SQLite notify: очередь писем (outbox) с вложениями и почтовые подписки пользователей."""

import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("NOTIFY_DB", "../data/notify.db")
# повторная отправка после сбоя SMTP: через 1, 5, 30 и 120 минут, затем failed
RETRY_DELAYS = (60, 300, 1800, 7200)
# у кого рассылки включены, пока пользователь сам ничего не менял
DEFAULT_ON = ("director", "admin")

_lock = threading.Lock()
_ready = ""  # путь базы, для которой уже выполнены миграции


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
        " to_addr TEXT NOT NULL, subject TEXT NOT NULL, html TEXT NOT NULL, text TEXT NOT NULL,"
        " headers TEXT NOT NULL DEFAULT '', dedup_key TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'queued',"
        " attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL, last_error TEXT,"
        " created_at REAL NOT NULL, sent_at REAL, company_id TEXT NOT NULL DEFAULT '')"
    )
    if "company_id" not in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}:
        conn.execute("ALTER TABLE outbox ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_try)")
    conn.execute("CREATE INDEX IF NOT EXISTS outbox_company ON outbox(company_id, kind, created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,"
        " filename TEXT NOT NULL, mime TEXT NOT NULL, content_id TEXT, data BLOB NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS attachments_msg ON attachments(msg_id)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prefs)")}
    if "digest" in cols:
        # первая версия notify (свой порог и день сводки): сводка становится «отчётами»
        conn.execute("ALTER TABLE prefs RENAME TO prefs_v1")
        cols = set()
    if not cols:
        conn.execute(
            "CREATE TABLE prefs(user_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, alerts INTEGER NOT NULL,"
            " reports INTEGER NOT NULL, unsub_token TEXT UNIQUE NOT NULL, updated_at REAL NOT NULL)"
        )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='prefs_v1'").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO prefs SELECT user_id, company_id, alerts, digest, unsub_token, updated_at FROM prefs_v1"
        )
        conn.execute("DROP TABLE prefs_v1")
    conn.execute("DROP TABLE IF EXISTS alert_state")


@contextmanager
def db():
    global _ready
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            if _ready != DB_PATH:
                _migrate(conn)
                _ready = DB_PATH
            yield conn
            conn.commit()
        finally:
            conn.close()


# ---------- outbox ----------
def enqueue(
    kind: str,
    to: str,
    subject: str,
    html: str,
    text: str,
    headers: str = "",
    dedup_key: str | None = None,
    company_id: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    """Ставит письмо в очередь. Повтор с тем же dedup_key второго письма не создаёт.
    attachments: [{filename, mime, data: bytes, content_id?}]; с content_id — картинка внутри письма."""
    now = time.time()
    with db() as conn:
        if dedup_key:
            row = conn.execute("SELECT id, status FROM outbox WHERE dedup_key=?", (dedup_key,)).fetchone()
            if row:
                return {"id": row["id"], "status": row["status"], "duplicate": True}
        cur = conn.execute(
            "INSERT INTO outbox(kind,to_addr,subject,html,text,headers,dedup_key,next_try,created_at,company_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, to, subject, html, text, headers, dedup_key, now, now, company_id),
        )
        for a in attachments or []:
            conn.execute(
                "INSERT INTO attachments(msg_id, filename, mime, content_id, data) VALUES(?,?,?,?,?)",
                (cur.lastrowid, a["filename"], a["mime"], a.get("content_id"), a["data"]),
            )
        return {"id": cur.lastrowid, "status": "queued", "duplicate": False}


def sent_recently(company_id: str, kind: str, seconds: int) -> int:
    """Сколько писем вида kind компания поставила в очередь за последние seconds секунд."""
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE company_id=? AND kind=? AND created_at>?",
            (company_id, kind, time.time() - seconds),
        ).fetchone()[0]


def due(limit: int = 20) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM outbox WHERE status='queued' AND next_try<=? ORDER BY id LIMIT ?", (time.time(), limit)
        ).fetchall()


def attachments_of(mid: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM attachments WHERE msg_id=? ORDER BY id", (mid,)).fetchall()


def mark_sent(mid: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
            (time.time(), mid),
        )
        conn.execute("DELETE FROM attachments WHERE msg_id=?", (mid,))  # отправленное вложение хранить незачем


def mark_failed(mid: int, error: str, permanent: bool = False) -> str:
    """Ошибка отправки: следующая попытка по расписанию RETRY_DELAYS или окончательный failed.
    permanent: повтор бесполезен (адреса не существует, письмо отклонено) — сразу failed."""
    with db() as conn:
        attempts = conn.execute("SELECT attempts FROM outbox WHERE id=?", (mid,)).fetchone()["attempts"] + 1
        if permanent or attempts > len(RETRY_DELAYS):
            conn.execute(
                "UPDATE outbox SET status='failed', attempts=?, last_error=? WHERE id=?", (attempts, error[:500], mid)
            )
            conn.execute("DELETE FROM attachments WHERE msg_id=?", (mid,))
            return "failed"
        conn.execute(
            "UPDATE outbox SET attempts=?, last_error=?, next_try=? WHERE id=?",
            (attempts, error[:500], time.time() + RETRY_DELAYS[attempts - 1], mid),
        )
        return "queued"


def stats() -> dict:
    """Состояние очереди для диагностики: письма по статусам, возраст самого старого ожидающего, род последних ошибок.
    Адресов и текстов писем здесь нет."""
    with db() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
        oldest = conn.execute("SELECT MIN(created_at) FROM outbox WHERE status='queued'").fetchone()[0]
        errors: dict[str, int] = {}
        for r in conn.execute(
            "SELECT last_error FROM outbox WHERE status IN ('queued','failed') AND last_error IS NOT NULL"
        ):
            cls = r["last_error"].split(":")[0].split(" ")[0]
            errors[cls] = errors.get(cls, 0) + 1
        last = conn.execute(
            "SELECT status, attempts, last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "queued": by_status.get("queued", 0),
        "sent": by_status.get("sent", 0),
        "failed": by_status.get("failed", 0),
        "oldest_queued_seconds": int(time.time() - oldest) if oldest else 0,
        "errors": errors,
        "last_error": last["last_error"] if last else None,
    }


def requeue_failed(max_age_seconds: int = 86400) -> int:
    """Вернуть в очередь письма со статусом failed за последние сутки (после исправления SMTP_*).
    Вложения у failed удалены, поэтому подходят только письма без них."""
    now = time.time()
    with db() as conn:
        return conn.execute(
            "UPDATE outbox SET status='queued', attempts=0, next_try=?, last_error=NULL"
            " WHERE status='failed' AND kind NOT IN ('report') AND created_at>?",
            (now, now - max_age_seconds),
        ).rowcount


def message(mid: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, kind, to_addr, status, attempts, last_error FROM outbox WHERE id=?", (mid,)
        ).fetchone()
    return dict(row) if row else None


# ---------- подписки ----------
def default_prefs(role: str) -> dict:
    """Без явных настроек алерты и отчёты на почту включены у владельца и администраторов."""
    on = role in DEFAULT_ON
    return {"alerts": on, "reports": on}


def _prefs_out(row) -> dict:
    return {"alerts": bool(row["alerts"]), "reports": bool(row["reports"]), "unsub_token": row["unsub_token"]}


def get_prefs(uid: str, company_id: str, role: str) -> dict:
    """Настройки пользователя; при первом обращении создаются из значений по умолчанию для его роли."""
    with db() as conn:
        row = conn.execute("SELECT * FROM prefs WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            d = default_prefs(role)
            conn.execute(
                "INSERT INTO prefs VALUES(?,?,?,?,?,?)",
                (uid, company_id, int(d["alerts"]), int(d["reports"]), secrets.token_urlsafe(24), time.time()),
            )
            row = conn.execute("SELECT * FROM prefs WHERE user_id=?", (uid,)).fetchone()
        elif company_id and row["company_id"] != company_id:
            conn.execute("UPDATE prefs SET company_id=? WHERE user_id=?", (company_id, uid))
    return _prefs_out(row)


def put_prefs(uid: str, company_id: str, role: str, **changes) -> dict:
    get_prefs(uid, company_id, role)
    cols = {k: int(v) for k, v in changes.items() if k in ("alerts", "reports") and v is not None}
    if cols:
        with db() as conn:
            sets = ", ".join(f"{k}=?" for k in cols)
            conn.execute(f"UPDATE prefs SET {sets}, updated_at=? WHERE user_id=?", (*cols.values(), time.time(), uid))
    return get_prefs(uid, company_id, role)


def unsubscribe(token: str) -> bool:
    """Отписка по ссылке из письма: выключает и алерты, и отчёты."""
    with db() as conn:
        return (
            conn.execute(
                "UPDATE prefs SET alerts=0, reports=0, updated_at=? WHERE unsub_token=?", (time.time(), token)
            ).rowcount
            == 1
        )


def known_prefs(user_ids: list[str]) -> dict[str, dict]:
    if not user_ids:
        return {}
    with db() as conn:
        marks = ",".join("?" * len(user_ids))
        rows = conn.execute(f"SELECT * FROM prefs WHERE user_id IN ({marks})", user_ids)
        return {r["user_id"]: _prefs_out(r) for r in rows}


def delete_user(uid: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM prefs WHERE user_id=?", (uid,))
