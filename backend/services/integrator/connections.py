"""Подключения компаний к 1С: реквизиты из ledger, проверка подключения и понятные причины отказа.

Реквизиты (адрес, логин, пароль) вводит директор в «Настройки → Интеграции», хранит их ledger (пароль зашифрован).
Integrator запрашивает их у ledger по номеру компании только на время запроса и нигде не сохраняет и не логирует.
"""
import os
import ssl
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import guard
from .clients import base, onec
from .clients.base import SourceError

ACTOR = "svc:integrator"


@dataclass
class Connection:
    url: str
    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:  # пароль не должен попасть ни в лог, ни в трейс
        return f"Connection(url={self.url!r}, username={self.username!r}, password=***)"


# ---------- ledger ----------
def ledger_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base.env("LEDGER_URL", "http://127.0.0.1:3002"), timeout=5, transport=transport,
        headers={"x-actor": ACTOR}, trust_env=False,
    )


def load(tenant: str, transport: httpx.BaseTransport | None = None) -> Connection | None:
    """Подключение компании или None, если его нет. Ошибка ledger — SourceError (на имитатор в этом случае не переключаемся)."""
    try:
        with ledger_client(transport) as h:
            r = h.get(f"/internal/v1/companies/{tenant}/integrations/onec/credentials")
    except httpx.HTTPError:
        raise SourceError("Сервис данных недоступен: не удалось получить подключение 1С") from None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = "не удалось получить подключение 1С"
        raise SourceError(str(msg)[:300])
    j = r.json()
    return Connection(url=j["url"], username=j["username"], password=j["password"])


# ---------- источник по умолчанию (env) ----------
def env_source() -> str:
    """`sim`, пока ONEC_URL указывает на имитатор (localhost, onec); иначе `live` (настоящая 1С, заданная администратором в env)."""
    host = (urlsplit(base.env("ONEC_URL", "http://localhost:8004")).hostname or "").lower()
    return "sim" if host in ("localhost", "127.0.0.1", "::1", "onec", "") else "live"


# ---------- ограничение числа проверок ----------
class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10000:  # не копим ключи бесконечно
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                self.hits.pop(k, None)
        return True


def check_limit() -> int:
    return int(os.environ.get("INTEGRATIONS_CHECK_LIMIT", "10"))


# ---------- проверка подключения ----------
REQUIRED = ("Catalog_Контрагенты", "Document_СчетНаОплатуПокупателю", "Document_СчетНаОплатуПоставщика")
OPTIONAL = ("Catalog_ДоговорыКонтрагентов", "Document_ПоступлениеНаРасчетныйСчет", "Document_СписаниеСРасчетногоСчета", "Document_ПлатежноеПоручение")
MESSAGES = {
    "ok": "Подключение работает",
    "bad_credentials": "1С не приняла логин или пароль. Проверьте служебного пользователя OData",
    "no_access": "У пользователя нет прав на чтение данных через OData",
    "odata_not_published": "По этому адресу нет OData. Включите «Настройки стандартного интерфейса OData» в 1С и проверьте адрес приложения",
    "no_catalog_access": "Не все нужные объекты добавлены в состав OData: {names}",
    "unreachable": "Сервер 1С недоступен по этому адресу",
    "timeout": "1С не ответила вовремя",
    "tls_error": "Сертификат сервера 1С не прошёл проверку: нужен действующий сертификат, выданный публичным центром",
    "redirect": "1С отвечает перенаправлением на другой адрес. Укажите конечный адрес приложения",
    "too_large": "Ответ 1С слишком большой",
    "bad_response": "1С ответила в неожиданном формате. Проверьте адрес приложения (без кода языка и без /odata)",
    "url_forbidden": "Адрес не подходит: нужен публичный адрес базы 1С",
    "url_scheme": "Адрес должен начинаться с https://",
    "url_userinfo": "Логин и пароль в адресе не указываются",
    "invalid_url": "Адрес базы 1С записан неверно",
}


def message(code: str, **kw) -> str:
    return MESSAGES.get(code, MESSAGES["bad_response"]).format(**kw)


def explain(e: Exception) -> tuple[str, str]:
    """Причина сбоя запроса к 1С: (код, понятное сообщение). Текст исключения наружу не отдаётся (там могут быть детали сети)."""
    if isinstance(e, guard.GuardError):
        return e.code, str(e)
    if isinstance(e, httpx.TimeoutException):
        return "timeout", message("timeout")
    if isinstance(e, httpx.TransportError):
        cause = e
        while cause is not None:
            if isinstance(cause, ssl.SSLError):
                return "tls_error", message("tls_error")
            cause = cause.__cause__ or cause.__context__
        return "unreachable", message("unreachable")
    text = str(e)
    for status, code in (("HTTP 401", "bad_credentials"), ("HTTP 403", "no_access"), ("HTTP 404", "odata_not_published")):
        if status in text:
            return code, message(code)
    return "bad_response", message("bad_response")


def _get(http: httpx.Client, path: str) -> httpx.Response:
    return http.get(path, headers={"Accept": "application/json"})


def _is_odata(r: httpx.Response) -> bool:
    try:
        return isinstance(r.json().get("value"), list)
    except (ValueError, AttributeError):
        return False


def probe(http: httpx.Client) -> dict:
    """Проверка по шагам без повторов: доступ, права, состав OData, число проведённых счетов. Результат всегда словарь."""
    t0 = time.monotonic()
    missing: list[str] = []
    for entity in REQUIRED:
        r = _get(http, f"{onec.B}{entity}?{onec.query({'$format': 'json', '$top': 1})}")
        if r.status_code == 401:
            return fail("bad_credentials")
        if r.status_code == 403:
            return fail("no_access")
        if r.status_code in (404, 400):
            missing.append(entity)
        elif r.status_code != 200 or not _is_odata(r):
            return fail("odata_not_published" if r.status_code == 200 else "bad_response")
    if missing:
        if len(missing) == len(REQUIRED):
            root = _get(http, f"{onec.B}?{onec.query({'$format': 'json'})}")
            if root.status_code in (401, 403, 404):
                return fail("bad_credentials" if root.status_code == 401 else "no_access" if root.status_code == 403 else "odata_not_published")
        return fail("no_catalog_access", names=", ".join(m.split("_", 1)[1] for m in missing), missing=missing)
    docs, more = 0, False
    for entity in ("Document_СчетНаОплатуПокупателю", "Document_СчетНаОплатуПоставщика"):
        r = _get(http, f"{onec.B}{entity}?{onec.query({'$format': 'json', '$top': 100, '$filter': onec.FILTER})}")
        if r.status_code != 200 or not _is_odata(r):
            return fail("bad_response")
        n = len(r.json()["value"])
        docs += n
        more = more or n >= 100
    optional = []
    for entity in OPTIONAL:
        r = _get(http, f"{onec.B}{entity}?{onec.query({'$format': 'json', '$top': 1})}")
        if r.status_code != 200:
            optional.append(entity.split("_", 1)[1])
    return {"ok": True, "code": "ok", "message": message("ok"), "documents": docs, "more": more, "optional_missing": optional, "ms": int((time.monotonic() - t0) * 1000), "source": "live"}


def fail(code: str, **kw) -> dict:
    missing = kw.pop("missing", None)
    out = {"ok": False, "code": code, "message": message(code, **kw), "source": "live"}
    if missing:
        out["missing"] = [m.split("_", 1)[1] for m in missing]
    return out


def check(conn: Connection, transport: httpx.BaseTransport | None = None, resolver=None) -> dict:
    """Проверка подключения: адрес (SSRF), вход, права, состав OData. Не бросает исключений; пароль в результат не попадает."""
    try:
        url = guard.validate_url(conn.url, resolver)
        with onec.live_client(Connection(url, conn.username, conn.password), transport, resolver) as http:
            return probe(http)
    except (SourceError, httpx.HTTPError) as e:
        code, msg = explain(e)
        return {"ok": False, "code": code, "message": msg, "source": "live"}
