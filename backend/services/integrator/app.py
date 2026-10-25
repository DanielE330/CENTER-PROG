"""Интегратор: единый список ожидаемых платежей из 1С и ЭДО (+ остаток банка)."""
import datetime as dt
import os
import time

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import connections, guard, merge
from .clients import bank, edo, onec
from .clients.base import SourceError

app = FastAPI(title="integrator", docs_url=None, redoc_url=None)
# Подмена транспорта в тестах: {"onec": transport, "edo": ..., "bank": ..., "ledger": ...}
app.state.transports = {}
# Подмена DNS в тестах (по умолчанию socket.getaddrinfo) и лимит проверок подключения (по компании)
app.state.resolver = None
app.state.limiter = connections.RateLimiter(connections.check_limit(), 60)
COMPANY = Query("C001", pattern=r"^C\d{3}$")
TENANT = r"^[A-Za-z0-9_-]{1,64}$"


@app.exception_handler(RequestValidationError)
async def invalid_request(_req: Request, _exc: RequestValidationError):
    """Стандартный ответ FastAPI повторяет присланные значения (там мог быть пароль): отдаём только код."""
    return JSONResponse({"error": {"code": "invalid_request", "message": "Некорректный запрос"}}, status_code=422)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def today() -> str:
    return os.environ.get("INTEGRATOR_TODAY") or os.environ.get("MOCK_TODAY") or dt.date.today().isoformat()


@app.get("/live")
def live():
    return {"status": "ok"}


def _pull(name: str, fn, live: bool = False):
    t0 = time.monotonic()
    try:
        return fn(), {"status": "ok", "error": None, "ms": int((time.monotonic() - t0) * 1000)}
    except (SourceError, httpx.HTTPError) as e:
        stat = {"status": "error", "error": str(e), "ms": int((time.monotonic() - t0) * 1000)}
        if live:  # настоящая 1С: понятная причина вместо текста исключения
            stat["code"], stat["error"] = connections.explain(e)
        return [], stat


def _fetch_live(conn: connections.Connection):
    url = guard.validate_url(conn.url, app.state.resolver)
    with onec.live_client(connections.Connection(url, conn.username, conn.password), app.state.transports.get("onec"), app.state.resolver) as h:
        return onec.fetch(h)


@app.get("/internal/v1/obligations")
def obligations(
    company: str = COMPANY,
    as_of: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    tenant: str | None = Query(None, pattern=TENANT),
):
    """`tenant` — компания платформы: если она подключила свою 1С (Настройки → Интеграции), данные берутся оттуда
    (`onec: "live"`); иначе, как раньше, из 1С по env (`ONEC_URL`) и ЭДО по `company`. Имитатор ЭДО чужие демо-данные
    в базу с настоящей 1С не подмешивает (`sources.edo.status = "off"`)."""
    tr = app.state.transports
    conn, conn_err = None, None
    if tenant:
        try:
            conn = connections.load(tenant, tr.get("ledger"))
        except SourceError as e:
            conn_err = str(e)
    if conn or conn_err:
        as_of = as_of or dt.date.today().isoformat()  # настоящая 1С: реальная дата, а не «сегодня» демо
        if conn:
            r1, s1 = _pull("onec", lambda: _fetch_live(conn), live=True)
        else:
            r1, s1 = [], {"status": "error", "error": conn_err, "code": "ledger_unavailable", "ms": 0}
        r2, s2 = [], {"status": "off", "error": None, "ms": 0}
        mode = "live" if conn else None
    else:
        as_of = as_of or today()
        with onec.client(tr.get("onec")) as h1, edo.client(tr.get("edo")) as h2:
            r1, s1 = _pull("onec", lambda: onec.fetch(h1))
            r2, s2 = _pull("edo", lambda: edo.fetch(h2, company))
        mode = connections.env_source()
    items, stats = merge.merge(r1 + r2, as_of)
    sources = {}
    for name, s in (("onec", s1), ("edo", s2)):
        sources[name] = {**s, **stats.get(name, {"count": 0, "duplicates": 0, "rejected": 0, "skipped": 0})}
    return {"company": company, "as_of": as_of, "onec": mode, "sources": sources, "items": items}


class ConnCheckBody(BaseModel):
    tenant: str = Field(pattern=TENANT)
    url: str | None = Field(None, max_length=600)
    username: str | None = Field(None, max_length=200)
    password: str | None = Field(None, max_length=400, repr=False)


class UrlBody(BaseModel):
    url: str = Field(max_length=600)


@app.post("/internal/v1/onec/check-url")
def onec_check_url(body: UrlBody):
    """Адрес из формы подключения: форма, https, порт, DNS (все адреса публичные). Ответ: канонический адрес."""
    try:
        return {"ok": True, "url": guard.validate_url(body.url, app.state.resolver)}
    except guard.GuardError as e:
        return error(422, e.code, str(e))


@app.post("/internal/v1/onec/test")
def onec_test(body: ConnCheckBody):
    """Проверка подключения 1С. Поля, которых нет в запросе, берутся из сохранённого подключения компании; сохранённый
    пароль подставляется только если адрес остался тем же. Не больше 10 проверок в минуту на компанию (429).
    Ответ 200: `{ok, code, message, documents?, more?, optional_missing?}`; пароль в ответ не попадает."""
    if not app.state.limiter.allow(body.tenant):
        return error(429, "rate_limited", "Слишком много проверок подключения. Подождите минуту и повторите")
    tr = app.state.transports
    saved = None
    if not (body.url and body.username and body.password):
        try:
            saved = connections.load(body.tenant, tr.get("ledger"))
        except SourceError as e:
            return error(503, "ledger_unavailable", str(e))
    url = (body.url or (saved.url if saved else "")).strip()
    user = (body.username or (saved.username if saved else "")).strip()
    if not url or not user:
        return error(422, "not_configured", "Сначала введите адрес и логин")
    password = body.password
    if not password:
        try:
            same = saved is not None and guard.origin(saved.url) == guard.origin(url)
        except guard.GuardError as e:
            return {"ok": False, "code": e.code, "message": str(e), "source": "live"}
        if not same:
            return error(422, "onec_password_required", "Введите пароль служебного пользователя 1С")
        password = saved.password
    return connections.check(connections.Connection(url, user, password), tr.get("onec"), app.state.resolver)


@app.get("/internal/v1/bank-balance")
def bank_balance(company: str = COMPANY):
    try:
        with bank.client(app.state.transports.get("bank")) as h:
            b = bank.balance(h, company)
    except (SourceError, httpx.HTTPError) as e:
        return JSONResponse({"company": company, "status": "error", "error": str(e)}, status_code=502)
    return {"company": company, "status": "ok", **b}
