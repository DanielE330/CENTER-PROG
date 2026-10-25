"""Боты компаний. Каждый директор вводит токен СВОЕГО Telegram-бота («Настройки → Интеграции»), ledger хранит его зашифрованным.
BotManager при запуске и каждые 60 секунд (и по `POST /internal/v1/telegram/sync` после сохранения токена) сверяет токены из ledger
с запущенными циклами long polling: поднимает цикл для нового токена, останавливает при отключении и пересоздаёт при смене токена —
без перезапуска сервиса. Компания без своего токена остаётся на общем боте (TELEGRAM_BOT_TOKEN)."""
import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from . import config
from .bot import BotApp
from .jobs import Jobs
from .store import Store
from .telegram import Bot, TelegramError
from .upstream import Upstream

log = logging.getLogger("notifier")
VERIFY_LIMIT, VERIFY_WINDOW = 10, 60.0  # проверок токена в минуту на компанию


def fingerprint(token: str) -> str:
    """Отпечаток токена: по нему видно, что токен сменился, а сам токен второй раз не хранится."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


@dataclass
class Runner:
    cid: str
    fp: str
    bot: Bot
    app: BotApp
    username: str
    task: asyncio.Task = field(default=None)  # type: ignore[assignment]


class RateLimit:
    def __init__(self, limit: int = VERIFY_LIMIT, window: float = VERIFY_WINDOW):
        self.limit, self.window, self.hits = limit, window, {}

    def allow(self, key: str) -> bool:
        q = self.hits.setdefault(key, deque())
        now = time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class BotManager:
    def __init__(self, store: Store, jobs: Jobs, up: Upstream, transport: httpx.AsyncBaseTransport | None = None):
        self.store, self.jobs, self.up, self.transport = store, jobs, up, transport
        self.runners: dict[str, Runner] = {}
        self._lock = asyncio.Lock()
        self.limiter = RateLimit()

    def username(self, cid: str) -> str:
        r = self.runners.get(cid)
        return r.username if r else ""

    def _start(self, cid: str, token: str, username: str) -> Runner:
        bot = Bot(token, self.transport)
        app = BotApp(self.store, bot, self.jobs, username, scope=cid)
        r = Runner(cid, fingerprint(token), bot, app, username)
        r.task = asyncio.create_task(app.poll_forever(), name=f"bot:{cid}")
        self.runners[cid] = r
        self.jobs.own[cid] = bot
        log.info("бот компании %s запущен (@%s)", cid, username)
        return r

    async def _stop(self, cid: str) -> None:
        r = self.runners.pop(cid, None)
        self.jobs.own.pop(cid, None)
        if r:
            r.task.cancel()
            try:
                await r.task
            except (asyncio.CancelledError, Exception):
                pass
            log.info("бот компании %s остановлен", cid)

    async def sync(self) -> dict:
        """Сверяет запущенные боты с токенами в ledger. Если ledger недоступен, ничего не меняет."""
        async with self._lock:
            items = await self.up.bot_tokens()
            if items is None:
                return {"started": 0, "stopped": 0, "unavailable": True}
            want = {i["company_id"]: i for i in items}
            started = stopped = 0
            for cid in list(self.runners):
                r = self.runners[cid]
                if cid not in want or r.fp != fingerprint(want[cid]["token"]):
                    await self._stop(cid)
                    stopped += 1
            for cid, i in want.items():
                if cid not in self.runners:
                    self._start(cid, i["token"], i.get("bot_username") or "")
                    started += 1
            return {"started": started, "stopped": stopped, "running": len(self.runners)}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception as e:
                log.warning("синхронизация ботов компаний: %s", type(e).__name__)
            await asyncio.sleep(config.bots_sync_every())

    async def stop_all(self) -> None:
        for cid in list(self.runners):
            await self._stop(cid)

    # ---------- проверка токена (getMe) ----------
    async def verify(self, cid: str, token: str) -> dict:
        """getMe для токена. Возвращает {bot_username, bot_id}; TelegramError — как есть (токен в нём не повторяется)."""
        me = await Bot(token, self.transport).get_me()
        if not me.get("is_bot", True) or not me.get("username"):
            raise TelegramError(401, "not a bot")
        return {"bot_username": me["username"], "bot_id": me.get("id")}
