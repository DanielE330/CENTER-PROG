"""Тонкий клиент Telegram Bot API. Токен нигде не попадает в сообщения об ошибках и логи."""
import asyncio
import logging
import re

import httpx

from . import config

API = "https://api.telegram.org"  # адрес фиксирован; TELEGRAM_API_URL (оператор, не пользователь) нужен для локального Bot API сервера и живых проверок
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def scrub(text: str) -> str:
    """Вырезает из текста всё, что похоже на токен бота (в адресе запроса к Telegram он есть): сообщения об ошибках и журналы его не повторяют."""
    return TOKEN_RE.sub("[token]", text)


def install_log_scrub() -> None:
    """Все записи журнала (в том числе httpx/httpcore, которые пишут адрес запроса) проходят через scrub. Безопасно вызывать повторно."""
    old = logging.getLogRecordFactory()
    if getattr(old, "_tg_scrub", False):
        return

    def factory(*a, **kw):
        r = old(*a, **kw)
        if isinstance(r.msg, str):
            r.msg = scrub(r.msg)
        if isinstance(r.args, tuple):
            r.args = tuple(scrub(x) if isinstance(x, str) else (scrub(str(x)) if isinstance(x, BaseException) else x) for x in r.args)
        elif isinstance(r.args, dict):
            r.args = {k: scrub(v) if isinstance(v, str) else v for k, v in r.args.items()}
        return r

    factory._tg_scrub = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)  # INFO-запись httpx содержит адрес с токеном


class TelegramError(Exception):
    def __init__(self, code: int, description: str = "", retry_after: int = 0, migrate_to: int | None = None):
        description = scrub(description)
        super().__init__(f"telegram {code}: {description}")
        self.code, self.description, self.retry_after, self.migrate_to = code, description, retry_after, migrate_to

    @property
    def chat_gone(self) -> bool:
        """Бота удалили из чата или заблокировали: рассылать туда больше нельзя."""
        d = self.description.lower()
        return self.code == 403 or "chat not found" in d or "kicked" in d or "deactivated" in d


class Bot:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None, proxy: str | None = None):
        self._token, self.transport = token, transport
        self.proxy = config.telegram_proxy() if proxy is None else proxy  # TELEGRAM_PROXY: http://… или socks5h://…

    def client(self, timeout: float) -> httpx.AsyncClient:
        # при подменённом транспорте (тесты) прокси не используется: в httpx он имел бы приоритет над транспортом
        if self.transport is None and self.proxy:
            return httpx.AsyncClient(timeout=timeout, proxy=self.proxy)
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    async def call(self, method: str, data: dict | None = None, files: dict | None = None, timeout: float = 30) -> dict | list | bool:
        url = f"{config.telegram_api() or API}/bot{self._token}/{method}"
        for attempt in (0, 1):
            try:
                async with self.client(timeout) as c:
                    if files:
                        r = await c.post(url, data={k: v for k, v in (data or {}).items() if v is not None}, files=files)
                    else:
                        r = await c.post(url, json=data or {})
            except ImportError:
                raise TelegramError(503, "ImportError: для socks5h:// нужен пакет httpx[socks]") from None
            except Exception as e:
                raise TelegramError(503, type(e).__name__) from None  # без текста исходной ошибки: в нём может быть URL с токеном
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if j.get("ok"):
                return j["result"]
            params = j.get("parameters") or {}
            err = TelegramError(j.get("error_code", r.status_code), j.get("description", ""), params.get("retry_after", 0), params.get("migrate_to_chat_id"))
            if err.code == 429 and attempt == 0 and 0 < err.retry_after <= 30:
                await asyncio.sleep(err.retry_after)
                continue
            raise err

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return await self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query", "my_chat_member"]}, timeout=timeout + 10)

    @staticmethod
    def chunks(text: str, limit: int = 4000) -> list[str]:
        """Режет по строкам: теги в отчётах не пересекают строки, поэтому HTML остаётся валидным."""
        out, cur = [], ""
        for line in text.split("\n"):
            line = line[:limit]
            if len(cur) + len(line) + 1 > limit and cur:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line
        return out + [cur] if cur else out or [""]

    async def send(self, chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> dict:
        parts = self.chunks(text)
        res: dict = {}
        for i, part in enumerate(parts):
            data = {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
            if keyboard and i == len(parts) - 1:
                data["reply_markup"] = {"inline_keyboard": keyboard}
            res = await self.call("sendMessage", data)
        return res

    async def send_photo(self, chat_id: int, png: bytes, caption: str = "") -> dict:
        return await self.call("sendPhoto", {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}, files={"photo": ("chart.png", png, "image/png")})

    async def send_document(self, chat_id: int, content: bytes, filename: str, caption: str = "") -> dict:
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return await self.call("sendDocument", {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}, files={"document": (filename, content, mime)})

    async def answer_callback(self, cb_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:200]})
        except TelegramError:
            pass

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: list[list[dict]]) -> None:
        try:
            await self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": keyboard}})
        except TelegramError:
            pass

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        try:
            m = await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        except TelegramError:
            return False
        return m.get("status") in ("creator", "administrator")
