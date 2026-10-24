"""SQLite: кэш ответов источников (по хэшу ИНН), снимки ЕНС, индекс открытых данных о недоимке."""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS src_cache(source TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
  fetched_at REAL NOT NULL, PRIMARY KEY(source, key));
CREATE TABLE IF NOT EXISTS ens_snapshots(inn_key TEXT NOT NULL, as_of TEXT NOT NULL, balance_kop INTEGER NOT NULL,
  debt_kop INTEGER NOT NULL, overpayment_kop INTEGER NOT NULL, penalty_kop INTEGER, upcoming TEXT NOT NULL,
  imported_at REAL NOT NULL, label TEXT, PRIMARY KEY(inn_key, as_of));
CREATE TABLE IF NOT EXISTS debt(inn TEXT PRIMARY KEY, total_kop INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def inn_key(inn: str) -> str:
    return hashlib.sha256(("fns:" + inn).encode()).hexdigest()[:32]


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else config.db_path()
        self._init_lock = threading.Lock()
        self._ready = False

    @contextmanager
    def conn(self):
        with self._init_lock:
            if not self._ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=15)) as c:
                    c.executescript(SCHEMA)
                    c.commit()
                self._ready = True
        c = sqlite3.connect(self.path, timeout=15)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    # --- кэш источников ---
    def cache_get(self, source: str, key: str):
        with self.conn() as c:
            r = c.execute("SELECT payload, fetched_at FROM src_cache WHERE source=? AND key=?", (source, key)).fetchone()
        return (json.loads(r[0]), r[1]) if r else None

    def cache_put(self, source: str, key: str, payload: dict, now: float | None = None):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO src_cache VALUES(?,?,?,?)",
                      (source, key, json.dumps(payload, ensure_ascii=False), now if now is not None else time.time()))

    def cache_count(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM src_cache").fetchone()[0]

    # --- meta ---
    def meta_get(self, k: str):
        with self.conn() as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def meta_put(self, k: str, v):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
