"""Хранилище уведомлений (SQLite): чаты, настройки, цели недели, ретро, журнал отправок."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, company_id TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', linked_by TEXT NOT NULL DEFAULT '', linked_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS chats_company ON chats(company_id);
CREATE TABLE IF NOT EXISTS link_codes(code_hash TEXT PRIMARY KEY, company_id TEXT NOT NULL, user_id TEXT NOT NULL,
  expires_at REAL NOT NULL, web_role TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS prefs(company_id TEXT PRIMARY KEY, tz TEXT NOT NULL, risk_enabled INTEGER NOT NULL,
  risk_dow INTEGER NOT NULL, risk_time TEXT NOT NULL, week_enabled INTEGER NOT NULL, week_dow INTEGER NOT NULL,
  week_time TEXT NOT NULL, month_enabled INTEGER NOT NULL, month_day INTEGER NOT NULL, month_time TEXT NOT NULL,
  gap_enabled INTEGER NOT NULL, gap_days INTEGER NOT NULL, gap_threshold REAL NOT NULL, ai_consent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sent(company_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, sent_at REAL NOT NULL,
  PRIMARY KEY(company_id, kind, key));
CREATE TABLE IF NOT EXISTS goals(id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, week_start TEXT NOT NULL,
  kind TEXT NOT NULL, title TEXT NOT NULL, target_kop INTEGER, due TEXT, status TEXT NOT NULL DEFAULT 'open',
  actual_kop INTEGER, source TEXT NOT NULL DEFAULT 'auto', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS goals_week ON goals(company_id, week_start);
CREATE TABLE IF NOT EXISTS retros(company_id TEXT NOT NULL, week_start TEXT NOT NULL, mood INTEGER, blockers TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY(company_id, week_start));
CREATE TABLE IF NOT EXISTS kv(company_id TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY(company_id, k));
CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, name TEXT NOT NULL,
  kinds TEXT NOT NULL DEFAULT 'risk,week,month,gap', allow_excel INTEGER NOT NULL DEFAULT 1,
  default_role TEXT NOT NULL DEFAULT 'viewer', created_at REAL NOT NULL, UNIQUE(company_id, name));
CREATE TABLE IF NOT EXISTS members(company_id TEXT NOT NULL, tg_user_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL, department_id INTEGER, created_at REAL NOT NULL, PRIMARY KEY(company_id, tg_user_id));
CREATE TABLE IF NOT EXISTS bot_state(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending(chat_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
"""

DEFAULT_PREFS = {
    "tz": config.DEFAULT_TZ, "risk_enabled": 1, "risk_dow": 0, "risk_time": "09:00", "week_enabled": 1, "week_dow": 0,
    "week_time": "09:30", "month_enabled": 1, "month_day": 1, "month_time": "10:00", "gap_enabled": 1, "gap_days": 7,
    "gap_threshold": 0.2, "ai_consent": 0,
}
ROLES = ("viewer", "rater", "admin")  # по возрастанию прав: читает; оценивает цели; управляет настройками и командой
KINDS = ("risk", "week", "month", "gap")
STATUSES = ("open", "done", "partial", "failed", "skipped")


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or config.db_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            if "web_role" not in [r[1] for r in self.conn.execute("PRAGMA table_info(link_codes)")]:
                self.conn.execute("ALTER TABLE link_codes ADD COLUMN web_role TEXT NOT NULL DEFAULT ''")
            if "web_user_id" not in [r[1] for r in self.conn.execute("PRAGMA table_info(members)")]:
                self.conn.execute("ALTER TABLE members ADD COLUMN web_user_id TEXT")
            if "ai_consent" not in [r[1] for r in self.conn.execute("PRAGMA table_info(prefs)")]:
                self.conn.execute("ALTER TABLE prefs ADD COLUMN ai_consent INTEGER NOT NULL DEFAULT 0")
            if "department_id" not in [r[1] for r in self.conn.execute("PRAGMA table_info(chats)")]:
                self.conn.execute("ALTER TABLE chats ADD COLUMN department_id INTEGER")
            self.conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def insert(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ---- привязка чатов ----
    def new_link_code(self, company_id: str, user_id: str, web_role: str = "") -> str:
        code = secrets.token_urlsafe(9)
        with self.lock:
            self.conn.execute("DELETE FROM link_codes WHERE expires_at < ?", (time.time(),))
            self.conn.execute("INSERT INTO link_codes VALUES(?,?,?,?,?)", (code_hash(code), company_id, user_id, time.time() + config.LINK_TTL, web_role))
            self.conn.commit()
        return code

    def use_link_code(self, code: str) -> dict | None:
        """Код одноразовый: после успешной проверки удаляется."""
        with self.lock:
            row = self.one("SELECT * FROM link_codes WHERE code_hash=?", (code_hash(code),))
            if not row:
                return None
            self.conn.execute("DELETE FROM link_codes WHERE code_hash=?", (row["code_hash"],))
            self.conn.commit()
            return row if row["expires_at"] >= time.time() else None

    def link_chat(self, chat_id: int, company_id: str, kind: str, title: str, user_id: str) -> None:
        self.x(
            "INSERT INTO chats(chat_id,company_id,kind,title,linked_by,linked_at,active) VALUES(?,?,?,?,?,?,1)"
            " ON CONFLICT(chat_id) DO UPDATE SET company_id=excluded.company_id, kind=excluded.kind, title=excluded.title,"
            " linked_by=excluded.linked_by, linked_at=excluded.linked_at, active=1, department_id=NULL",
            (chat_id, company_id, kind, title, user_id, time.time()),
        )
        self.prefs(company_id)  # создаёт настройки по умолчанию

    def unlink_chat(self, chat_id: int, company_id: str | None = None) -> bool:
        sql, args = "UPDATE chats SET active=0 WHERE chat_id=? AND active=1", [chat_id]
        if company_id:
            sql += " AND company_id=?"
            args.append(company_id)
        return self.x(sql, tuple(args)) > 0

    def chat(self, chat_id: int) -> dict | None:
        return self.one("SELECT * FROM chats WHERE chat_id=? AND active=1", (chat_id,))

    def chats(self, company_id: str) -> list[dict]:
        return self.q("SELECT * FROM chats WHERE company_id=? AND active=1 ORDER BY linked_at", (company_id,))

    def companies(self) -> list[str]:
        return [r["company_id"] for r in self.q("SELECT DISTINCT company_id FROM chats WHERE active=1")]

    # ---- настройки ----
    def prefs(self, company_id: str) -> dict:
        row = self.one("SELECT * FROM prefs WHERE company_id=?", (company_id,))
        if row:
            return row
        p = {"company_id": company_id, **DEFAULT_PREFS}
        cols = ",".join(p)
        self.x(f"INSERT OR IGNORE INTO prefs({cols}) VALUES({','.join('?' * len(p))})", tuple(p.values()))
        return p

    def set_prefs(self, company_id: str, changes: dict) -> dict:
        self.prefs(company_id)
        allowed = [k for k in changes if k in DEFAULT_PREFS]
        if allowed:
            sets = ",".join(f"{k}=?" for k in allowed)
            self.x(f"UPDATE prefs SET {sets} WHERE company_id=?", (*[changes[k] for k in allowed], company_id))
        return self.prefs(company_id)

    # ---- журнал отправок (защита от дублей) ----
    def claim(self, company_id: str, kind: str, key: str) -> bool:
        """True, если отправка с таким ключом ещё не делалась (и теперь за нами)."""
        with self.lock:
            cur = self.conn.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?,?)", (company_id, kind, key, time.time()))
            self.conn.commit()
            return cur.rowcount == 1

    def release(self, company_id: str, kind: str, key: str) -> None:
        self.x("DELETE FROM sent WHERE company_id=? AND kind=? AND key=?", (company_id, kind, key))

    def was_sent(self, company_id: str, kind: str, key: str) -> bool:
        return self.one("SELECT 1 FROM sent WHERE company_id=? AND kind=? AND key=?", (company_id, kind, key)) is not None

    # ---- kv ----
    def kv_get(self, company_id: str, k: str) -> str | None:
        r = self.one("SELECT v FROM kv WHERE company_id=? AND k=?", (company_id, k))
        return r["v"] if r else None

    def kv_set(self, company_id: str, k: str, v: str | None) -> None:
        if v is None:
            self.x("DELETE FROM kv WHERE company_id=? AND k=?", (company_id, k))
        else:
            self.x("INSERT INTO kv VALUES(?,?,?) ON CONFLICT(company_id,k) DO UPDATE SET v=excluded.v", (company_id, k, v))

    def state_get(self, k: str, default: str = "") -> str:
        r = self.one("SELECT v FROM bot_state WHERE k=?", (k,))
        return r["v"] if r else default

    def state_set(self, k: str, v: str) -> None:
        self.x("INSERT INTO bot_state VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- цели недели ----
    def goals(self, company_id: str, week_start: str) -> list[dict]:
        return self.q("SELECT * FROM goals WHERE company_id=? AND week_start=? ORDER BY id", (company_id, week_start))

    def prev_goals(self, company_id: str, before: str) -> tuple[str | None, list[dict]]:
        """Цели последнего спринта, начавшегося раньше `before` (ISO-дата)."""
        row = self.one("SELECT MAX(week_start) AS w FROM goals WHERE company_id=? AND week_start<?", (company_id, before))
        if not row or not row["w"]:
            return None, []
        return row["w"], self.goals(company_id, row["w"])

    def goal(self, company_id: str, gid: int) -> dict | None:
        return self.one("SELECT * FROM goals WHERE company_id=? AND id=?", (company_id, gid))

    def add_goal(self, company_id: str, week_start: str, kind: str, title: str, target_kop: int | None = None,
                 due: str | None = None, source: str = "auto") -> int:
        return self.insert(
            "INSERT INTO goals(company_id,week_start,kind,title,target_kop,due,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (company_id, week_start, kind, title, target_kop, due, source, time.time()),
        )

    def update_goal(self, company_id: str, gid: int, changes: dict) -> dict | None:
        allowed = {k: v for k, v in changes.items() if k in ("title", "target_kop", "due", "status", "actual_kop")}
        if "status" in allowed and allowed["status"] not in STATUSES:
            raise ValueError("bad_status")
        if allowed:
            sets = ",".join(f"{k}=?" for k in allowed)
            self.x(f"UPDATE goals SET {sets} WHERE company_id=? AND id=?", (*allowed.values(), company_id, gid))
        return self.goal(company_id, gid)

    def delete_goal(self, company_id: str, gid: int) -> bool:
        return self.x("DELETE FROM goals WHERE company_id=? AND id=?", (company_id, gid)) > 0

    # ---- ретро ----
    def retro(self, company_id: str, week_start: str) -> dict | None:
        return self.one("SELECT * FROM retros WHERE company_id=? AND week_start=?", (company_id, week_start))

    def save_retro(self, company_id: str, week_start: str, mood: int | None = None, blockers: str | None = None,
                   note: str | None = None) -> dict:
        cur = self.retro(company_id, week_start) or {"mood": None, "blockers": "", "note": ""}
        new = {"mood": cur["mood"] if mood is None else mood, "blockers": cur["blockers"] if blockers is None else blockers,
               "note": cur["note"] if note is None else note}
        self.x(
            "INSERT INTO retros VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,week_start) DO UPDATE SET mood=excluded.mood,"
            " blockers=excluded.blockers, note=excluded.note",
            (company_id, week_start, new["mood"], new["blockers"], new["note"], time.time()),
        )
        return self.retro(company_id, week_start)

    # ---- ожидание текстового ответа в чате ----
    def set_pending(self, chat_id: int, kind: str, payload: dict) -> None:
        self.x("INSERT INTO pending VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET kind=excluded.kind, payload=excluded.payload,"
               " created_at=excluded.created_at", (chat_id, kind, json.dumps(payload), time.time()))

    def pop_pending(self, chat_id: int, max_age: float = 3600) -> tuple[str, dict] | None:
        row = self.one("SELECT * FROM pending WHERE chat_id=?", (chat_id,))
        if not row:
            return None
        self.x("DELETE FROM pending WHERE chat_id=?", (chat_id,))
        if time.time() - row["created_at"] > max_age:
            return None
        return row["kind"], json.loads(row["payload"])

    # ---------- отделы, роли, политики ----------
    def departments(self, company_id: str) -> list[dict]:
        rows = self.q("SELECT * FROM departments WHERE company_id=? ORDER BY name", (company_id,))
        for r in rows:
            r["kinds"] = [k for k in r["kinds"].split(",") if k]
        return rows

    def department(self, company_id: str, did: int | None) -> dict | None:
        if did is None:
            return None
        return next((d for d in self.departments(company_id) if d["id"] == did), None)

    def add_department(self, company_id: str, name: str, kinds: list[str], allow_excel: bool, default_role: str) -> int:
        self._check_policy(kinds, default_role)
        return self.insert("INSERT INTO departments(company_id,name,kinds,allow_excel,default_role,created_at) VALUES(?,?,?,?,?,?)",
                           (company_id, name, ",".join(kinds), int(allow_excel), default_role, time.time()))

    def update_department(self, company_id: str, did: int, changes: dict) -> dict | None:
        cur = self.department(company_id, did)
        if not cur:
            return None
        new = {**cur, **{k: v for k, v in changes.items() if v is not None}}
        self._check_policy(new["kinds"], new["default_role"])
        self.x("UPDATE departments SET name=?, kinds=?, allow_excel=?, default_role=? WHERE company_id=? AND id=?",
               (new["name"], ",".join(new["kinds"]), int(bool(new["allow_excel"])), new["default_role"], company_id, did))
        return self.department(company_id, did)

    def delete_department(self, company_id: str, did: int) -> bool:
        self.x("UPDATE chats SET department_id=NULL WHERE company_id=? AND department_id=?", (company_id, did))
        self.x("UPDATE members SET department_id=NULL WHERE company_id=? AND department_id=?", (company_id, did))
        return self.x("DELETE FROM departments WHERE company_id=? AND id=?", (company_id, did)) > 0

    @staticmethod
    def _check_policy(kinds: list[str], role: str) -> None:
        if role not in ROLES or any(k not in KINDS for k in kinds):
            raise ValueError("bad_policy")

    def set_chat_department(self, company_id: str, chat_id: int, did: int | None) -> bool:
        if did is not None and not self.department(company_id, did):
            return False
        return self.x("UPDATE chats SET department_id=? WHERE chat_id=? AND company_id=? AND active=1", (did, chat_id, company_id)) > 0

    def policy(self, chat: dict) -> dict:
        """Политика чата: отдел задаёт виды рассылок, Excel и роль по умолчанию. Без отдела — всё разрешено, личный чат — админ."""
        d = self.department(chat["company_id"], chat.get("department_id"))
        if d:
            return {"kinds": d["kinds"], "allow_excel": bool(d["allow_excel"]), "default_role": d["default_role"], "department": d["name"]}
        return {"kinds": list(KINDS), "allow_excel": True, "default_role": "admin" if chat["kind"] == "private" else "viewer", "department": None}

    def members(self, company_id: str) -> list[dict]:
        return self.q("SELECT * FROM members WHERE company_id=? ORDER BY created_at", (company_id,))

    def touch_member(self, company_id: str, uid: int, name: str, role: str | None = None) -> None:
        """Запоминает участника при первом обращении. Пустая роль = «как в политике отдела»; назначенная не перезаписывается."""
        self.x("INSERT INTO members(company_id,tg_user_id,name,role,created_at) VALUES(?,?,?,?,?)"
               " ON CONFLICT(company_id,tg_user_id) DO UPDATE SET name=excluded.name", (company_id, uid, name[:80], role or "", time.time()))

    def set_member(self, company_id: str, uid: int, role: str | None, did: int | None, set_did: bool) -> dict | None:
        if role is not None and role not in ROLES and role != "":  # "" — снять назначение: роль берётся из политики отдела
            raise ValueError("bad_role")
        if not self.one("SELECT 1 FROM members WHERE company_id=? AND tg_user_id=?", (company_id, uid)):
            return None
        if role is not None:
            self.x("UPDATE members SET role=? WHERE company_id=? AND tg_user_id=?", (role, company_id, uid))
        if set_did:
            self.x("UPDATE members SET department_id=? WHERE company_id=? AND tg_user_id=?", (did, company_id, uid))
        return self.one("SELECT * FROM members WHERE company_id=? AND tg_user_id=?", (company_id, uid))

    def delete_member(self, company_id: str, uid: int) -> bool:
        return self.x("DELETE FROM members WHERE company_id=? AND tg_user_id=?", (company_id, uid)) > 0

    def role_of(self, chat: dict, uid: int) -> str:
        """Роль участника в этом чате: назначенная администратором, иначе роль по умолчанию политики чата."""
        if chat["kind"] == "private":
            m = self.one("SELECT role FROM members WHERE company_id=? AND tg_user_id=?", (chat["company_id"], uid))
            return m["role"] if m and m["role"] in ROLES else "admin"  # владелец личного чата — админ, если веб-роль не ограничила
        m = self.one("SELECT role FROM members WHERE company_id=? AND tg_user_id=?", (chat["company_id"], uid))
        return m["role"] if m and m["role"] in ROLES else self.policy(chat)["default_role"]

    def can(self, chat: dict, uid: int, need: str) -> bool:
        return ROLES.index(self.role_of(chat, uid)) >= ROLES.index(need)

    # ---------- история спринтов ----------
    def sprints(self, company_id: str, limit: int = 8) -> list[dict]:
        weeks = self.q("SELECT DISTINCT week_start FROM goals WHERE company_id=? ORDER BY week_start DESC LIMIT ?", (company_id, limit))
        return [{"week_start": w["week_start"], "goals": self.goals(company_id, w["week_start"]), "retro": self.retro(company_id, w["week_start"])}
                for w in reversed(weeks)]

    def hit(self, company_id: str, key: str, limit: int) -> bool:
        """Счётчик обращений за период (ключ включает период). False, если лимит исчерпан."""
        n = int(self.kv_get(company_id, key) or 0)
        if n >= limit:
            return False
        self.kv_set(company_id, key, str(n + 1))
        return True

    def pop_pending_peek(self, chat_id: int, max_age: float = 3600) -> bool:
        row = self.one("SELECT created_at FROM pending WHERE chat_id=?", (chat_id,))
        return bool(row) and time.time() - row["created_at"] <= max_age

    # ---------- связь с веб-ролями ----------
    # веб-роли: director/admin/editor/viewer (прежние названия owner/analyst/observer понимаем для старых записей)
    WEB_TO_TG = {"director": "admin", "owner": "admin", "admin": "admin", "editor": "rater", "analyst": "rater", "viewer": "viewer", "observer": "viewer"}

    def bind_web_member(self, company_id: str, tg_uid: int, name: str, web_user_id: str, web_role: str) -> None:
        """Telegram-роль выводится из веб-роли: владелец (director) и администратор — admin, аналитик (editor) — rater, наблюдатель (viewer) — viewer."""
        role = self.WEB_TO_TG.get(web_role, "viewer")
        self.touch_member(company_id, tg_uid, name, role)
        self.x("UPDATE members SET role=?, web_user_id=? WHERE company_id=? AND tg_user_id=?", (role, web_user_id, company_id, tg_uid))

    def sync_web_role(self, company_id: str, web_user_id: str, web_role: str | None) -> int:
        """Веб-роль изменилась (или человека убрали из команды): обновляем его Telegram-роль; убранному отключаем личные чаты."""
        if web_role is None:
            self.x("UPDATE chats SET active=0 WHERE company_id=? AND kind='private' AND linked_by=?", (company_id, web_user_id))
            return self.x("DELETE FROM members WHERE company_id=? AND web_user_id=?", (company_id, web_user_id))
        return self.x("UPDATE members SET role=? WHERE company_id=? AND web_user_id=?", (self.WEB_TO_TG.get(web_role, "viewer"), company_id, web_user_id))
