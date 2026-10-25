"""Пропуск на один вопрос к Hermes. Hermes вызывает наши инструменты по MCP и не сообщает, чей это запрос, поэтому
каждый вопрос получает случайный пропуск: компания, пользователь, права, язык. Модель передаёт его в каждом вызове
инструмента; без живого пропуска инструмент не выполняется. Пропуск живёт, пока идёт ответ (TTL), и даёт не больше прав,
чем у самого пользователя, — чужой компании по нему не достать. Хранится в памяти процесса advisor (один процесс)."""
import secrets
import threading
import time
from dataclasses import dataclass, field

TTL = 300.0
MAX_LIVE = 500


@dataclass
class Ticket:
    id: str
    cid: str
    actor: str
    rid: str
    lang: str
    perms: list[str]
    created: float = field(default_factory=time.time)
    proposals: list[dict] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)  # что консультант записал сам (по праву на запись)
    used: list[str] = field(default_factory=list)
    counterparties: set[str] = field(default_factory=set)
    plan: dict = field(default_factory=dict)  # стратегия из режима «План» (save_strategy_plan)


_lock = threading.Lock()
_live: dict[str, Ticket] = {}


def issue(cid: str, actor: str, rid: str, lang: str, perms: list[str]) -> Ticket:
    t = Ticket("tkt_" + secrets.token_urlsafe(18), cid, actor, rid, lang, list(perms))
    with _lock:
        _gc()
        _live[t.id] = t
    return t


def get(tid: str) -> Ticket | None:
    with _lock:
        t = _live.get(tid or "")
        if t and time.time() - t.created > TTL:
            _live.pop(t.id, None)
            return None
        return t


def close(tid: str) -> None:
    with _lock:
        _live.pop(tid, None)


def _gc() -> None:
    now = time.time()
    for k in [k for k, t in _live.items() if now - t.created > TTL]:
        _live.pop(k, None)
    while len(_live) >= MAX_LIVE:
        _live.pop(next(iter(_live)))
