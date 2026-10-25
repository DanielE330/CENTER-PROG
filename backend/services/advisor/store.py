"""Чаты консультанта и стратегии (SQLite, ADVISOR_DB). Чат принадлежит пользователю внутри компании: чужие чаты не видны
и не удаляются (владелец — x-actor от api). У чата свой контекст (история сообщений), режим (chat | plan), модель и
уровень размышления; уровень задаётся при создании. Стратегия из режима «План» — одна активная на компанию."""
import json
import os
import sqlite3
import threading
import time
import uuid

MODES = ("chat", "plan")
EFFORTS = ("low", "medium", "high", "xhigh")
MAX_CHATS = 200  # на пользователя в компании; старые удаляются
MAX_MESSAGES = 400  # на чат

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT NOT NULL,
  mode TEXT NOT NULL, model TEXT NOT NULL, reasoning TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS chats_owner ON chats(company_id, user_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL, chat_id TEXT, user_id TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS plans_company ON plans(company_id, status, created_at);
"""


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ---------- чаты ----------
    def _chat(self, r: sqlite3.Row, count: int | None = None) -> dict:
        d = {"id": r["id"], "title": r["title"], "mode": r["mode"], "model": r["model"], "reasoning": r["reasoning"],
             "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])}
        if count is not None:
            d["messages"] = count
        return d

    def chats(self, cid: str, uid: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS n FROM chats c "
                "WHERE company_id=? AND user_id=? ORDER BY updated_at DESC LIMIT ?", (cid, uid, MAX_CHATS)).fetchall()
        return [self._chat(r, r["n"]) for r in rows]

    def create(self, cid: str, uid: str, title: str, mode: str, model: str, reasoning: str) -> dict:
        now = time.time()
        cid_ = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute("INSERT INTO chats VALUES (?,?,?,?,?,?,?,?,?)", (cid_, cid, uid, title, mode, model, reasoning, now, now))
            # не больше MAX_CHATS у пользователя: самые старые уходят
            self.db.execute("DELETE FROM chats WHERE id IN (SELECT id FROM chats WHERE company_id=? AND user_id=? "
                            "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (cid, uid, MAX_CHATS))
        return self.get(cid, uid, cid_)

    def get(self, cid: str, uid: str, chat_id: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM chats WHERE id=? AND company_id=? AND user_id=?", (chat_id, cid, uid)).fetchone()
        return self._chat(r) if r else None

    def update(self, cid: str, uid: str, chat_id: str, **fields) -> dict | None:
        sets = {k: v for k, v in fields.items() if k in ("title", "model") and v is not None}
        if sets:
            with self.lock, self.db:
                self.db.execute(f"UPDATE chats SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=? AND company_id=? AND user_id=?",
                                (*sets.values(), time.time(), chat_id, cid, uid))
        return self.get(cid, uid, chat_id)

    def delete(self, cid: str, uid: str, chat_id: str) -> bool:
        with self.lock, self.db:
            n = self.db.execute("DELETE FROM chats WHERE id=? AND company_id=? AND user_id=?", (chat_id, cid, uid)).rowcount
            self.db.execute("UPDATE plans SET chat_id=NULL WHERE chat_id=?", (chat_id,))
        return n > 0

    # ---------- сообщения ----------
    def messages(self, chat_id: str, limit: int = MAX_MESSAGES) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM (SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                                   (chat_id, limit)).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"], "meta": json.loads(r["meta"] or "{}"),
                 "created_at": iso(r["created_at"])} for r in rows]

    def add(self, chat_id: str, role: str, content: str, meta: dict | None = None, title: str | None = None) -> dict:
        now = time.time()
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO messages(chat_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                                  (chat_id, role, content, json.dumps(meta or {}, ensure_ascii=False), now))
            self.db.execute("DELETE FROM messages WHERE chat_id=? AND id NOT IN (SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
                            (chat_id, chat_id, MAX_MESSAGES))
            if title:
                self.db.execute("UPDATE chats SET title=?, updated_at=? WHERE id=?", (title, now, chat_id))
            else:
                self.db.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
        return {"id": cur.lastrowid, "role": role, "content": content, "meta": meta or {}, "created_at": iso(now)}

    # ---------- стратегии ----------
    def save_plan(self, cid: str, uid: str, chat_id: str | None, body: dict) -> dict:
        pid, now = uuid.uuid4().hex, time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE plans SET status='archived' WHERE company_id=? AND status='active'", (cid,))
            self.db.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?)", (pid, cid, chat_id, uid, json.dumps(body, ensure_ascii=False), "active", now))
        return self.active_plan(cid)

    def active_plan(self, cid: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM plans WHERE company_id=? AND status='active' ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "chat_id": r["chat_id"], "created_at": iso(r["created_at"]), **json.loads(r["body"])}

    def delete_plan(self, cid: str, pid: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("UPDATE plans SET status='archived' WHERE id=? AND company_id=? AND status='active'", (pid, cid)).rowcount > 0


_store: Store | None = None


def get() -> Store:
    global _store
    path = os.environ.get("ADVISOR_DB", "/data/advisor.db")
    if _store is None or _store.path != path:
        _store = Store(path)
    return _store
