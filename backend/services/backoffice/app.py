"""backoffice (порт 8013): панель оператора платформы. Операторы, их сессии, журнал действий и прокси к ledger/forecast/auth.
uvicorn backoffice.app:app --host 127.0.0.1 --port 8013

Оператор видит только метаданные и агрегаты компаний (название, реквизиты, даты, числа), но не операции и платежи.
Внутренние эндпоинты (вызывает только api; сессию оператора api проверяет через /operators/session и передаёт id в x-actor):
  POST /internal/v1/operators/login {email,password} -> {token, operator_id, email, ttl}; 401 invalid_credentials, 429 too_many_attempts
  POST /internal/v1/operators/session {token} -> {operator_id, email}; POST /internal/v1/operators/logout {token} -> 204
  GET  /internal/v1/admin/companies?query=&banned=&limit=&offset= -> {total, items:[... , risk_level, shortfall_probability]}
  GET  /internal/v1/admin/companies/{cid}; POST .../ban {reason}; POST .../unban; GET /internal/v1/admin/stats; GET /internal/v1/admin/audit
Ошибки: {"error":{"code","message"}}.
"""
import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import store

CID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FAILS = 5
LOCK_SECONDS = 300
REASON_MIN, REASON_MAX = 3, 500
FORECAST_CONCURRENCY = 6
FORECAST_TIMEOUT = float(os.environ.get("BACKOFFICE_FORECAST_TIMEOUT_SECONDS", "6"))
FORECAST_TTL = 60.0
LIST_LIMIT_MAX = 100
# поля компании, которые вообще могут уйти оператору: только метаданные и агрегаты
COMPANY_FIELDS = (
    "id", "name", "inn", "ogrn", "created_at", "banned", "banned_at", "ban_reason", "members_count",
    "transactions_count", "last_transaction_date", "last_import_at", "events_count", "director_email",
)
MEMBER_FIELDS = ("email", "role", "created_at")

_fails: dict[str, tuple[int, float]] = {}
_forecast_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def log(msg: str) -> None:
    print(f"backoffice: {msg}", flush=True)  # без паролей, токенов и данных компаний


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log(f"operator bootstrap: {store.bootstrap_from_env()}")
    yield


app = FastAPI(title="backoffice", docs_url=None, redoc_url=None, lifespan=lifespan)
# Подмена транспорта в тестах: один httpx.MockTransport на ledger, forecast и auth.
app.state.transport = None


class ApiErr(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


@app.exception_handler(ApiErr)
async def _api_err(_r: Request, e: ApiErr):
    return JSONResponse(body(e.code, e.message), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _invalid(_r: Request, _e: RequestValidationError):
    return JSONResponse(body("invalid_request", "Некорректный запрос"), status_code=422)


@app.get("/live")
def live():
    return {"status": "ok"}


# ---------- операторы: вход, сессия ----------
class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=store.MAX_PASSWORD)


class TokenIn(BaseModel):
    token: str = Field(max_length=512)


class BanIn(BaseModel):
    reason: str = Field(max_length=2000)


def _locked(email: str) -> bool:
    cnt, until = _fails.get(email, (0, 0.0))
    if until and time.time() >= until:
        _fails.pop(email, None)  # блокировка истекла: счёт начинается заново
        return False
    return cnt >= MAX_FAILS


def _note_failure(email: str) -> None:
    now = time.time()
    if len(_fails) > 10_000:  # неизвестные адреса тоже считаются, но память ограничена
        for k in [k for k, (_, u) in _fails.items() if u < now]:
            _fails.pop(k, None)
    cnt, _ = _fails.get(email, (0, 0.0))
    _fails[email] = (cnt + 1, now + LOCK_SECONDS)


@app.post("/internal/v1/operators/login")
def operator_login(c: LoginIn, x_request_id: str = Header(default="")):
    email = c.email.strip().lower()
    if _locked(email):
        store.audit(None, "login", outcome="locked", email=email[:254], request_id=x_request_id)
        raise ApiErr(429, "too_many_attempts", "Слишком много попыток входа, подождите несколько минут")
    op = store.verify_login(email, c.password)
    if not op:
        _note_failure(email)
        store.audit(None, "login", outcome="invalid_credentials", email=email[:254], request_id=x_request_id)
        raise ApiErr(401, "invalid_credentials", "Неверный email или пароль")
    _fails.pop(email, None)
    state = store.mfa_state(op["operator_id"])
    if state:
        # пароль верен, но сессии ещё нет: короткоживущий билет для второго шага (настройка при первом входе, затем код)
        store.audit(op, "login", outcome=f"mfa_{state}", request_id=x_request_id)
        return {"mfa": state, "mfa_token": store.mfa_ticket(op["operator_id"], state), "email": op["email"]}
    return _session_for(op, x_request_id)


def _session_for(op: dict, rid: str, extra: dict | None = None) -> dict:
    token = store.issue_session(op["operator_id"])
    store.audit(op, "login", request_id=rid)
    return {"token": token, "operator_id": op["operator_id"], "email": op["email"], "ttl": store.SESSION_TTL, **(extra or {})}


class MfaTicketIn(BaseModel):
    mfa_token: str = Field(max_length=512)


class MfaCodeIn(MfaTicketIn):
    code: str = Field(max_length=64)


_mfa_fails: dict[str, tuple[int, float]] = {}


def _mfa_ticket(tok: str, purpose: str) -> tuple[str, str | None]:
    row = store.mfa_ticket_row(tok, purpose)
    if not row:
        raise ApiErr(401, "mfa_session_expired", "Время ввода кода истекло, войдите заново")
    cnt, until = _mfa_fails.get(row[0], (0, 0.0))
    if cnt >= MAX_FAILS and time.time() < until:
        raise ApiErr(429, "too_many_attempts", "Слишком много попыток, подождите несколько минут")
    return row[0], row[1]


def _mfa_reject(tok: str, operator_id: str, rid: str) -> None:
    cnt, _ = _mfa_fails.get(operator_id, (0, 0.0))
    _mfa_fails[operator_id] = (cnt + 1, time.time() + LOCK_SECONDS)
    store.mfa_ticket_fail(tok)
    op = store.operator_by_id(operator_id)
    store.audit(op, "mfa", outcome="invalid_code", request_id=rid)
    raise ApiErr(401, "invalid_mfa_code", "Неверный код. Проверьте время на телефоне или введите резервный код")


def _operator_of(operator_id: str) -> dict:
    op = store.operator_by_id(operator_id)
    if not op:
        raise ApiErr(401, "mfa_session_expired", "Время ввода кода истекло, войдите заново")
    return op


@app.post("/internal/v1/operators/mfa/setup")
def operator_mfa_setup(b: MfaTicketIn):
    """Первый вход: секрет, otpauth-ссылка и QR (рисуется на сервере: секрет не уходит наружу)."""
    operator_id, _ = _mfa_ticket(b.mfa_token, "setup")
    return store.mfa_begin_setup(b.mfa_token, operator_id, _operator_of(operator_id)["email"])


@app.post("/internal/v1/operators/mfa/enable")
def operator_mfa_enable(b: MfaCodeIn, x_request_id: str = Header(default="")):
    operator_id, enc = _mfa_ticket(b.mfa_token, "setup")
    if not enc:
        raise ApiErr(409, "mfa_setup_not_started", "Сначала откройте настройку двухфакторной защиты")
    codes = store.mfa_enable(b.mfa_token, operator_id, enc, b.code)
    if codes is None:
        _mfa_reject(b.mfa_token, operator_id, x_request_id)
    _mfa_fails.pop(operator_id, None)
    store.mfa_ticket_drop(b.mfa_token)
    store.audit(_operator_of(operator_id), "mfa_enable", request_id=x_request_id)
    return _session_for(_operator_of(operator_id), x_request_id, {"backup_codes": codes})


@app.post("/internal/v1/operators/mfa/verify")
def operator_mfa_verify(b: MfaCodeIn, x_request_id: str = Header(default="")):
    operator_id, _ = _mfa_ticket(b.mfa_token, "verify")
    ok, left = store.mfa_check(operator_id, b.code)
    if not ok:
        _mfa_reject(b.mfa_token, operator_id, x_request_id)
    _mfa_fails.pop(operator_id, None)
    store.mfa_ticket_drop(b.mfa_token)
    return _session_for(_operator_of(operator_id), x_request_id, {} if left is None else {"backup_codes_left": left})


@app.post("/internal/v1/operators/session")
def operator_session(t: TokenIn):
    op = store.session_operator(t.token)
    if not op:
        raise ApiErr(401, "invalid_session", "Сессия оператора недействительна")
    return op


@app.post("/internal/v1/operators/logout", status_code=204)
def operator_logout(t: TokenIn, x_request_id: str = Header(default="")):
    op = store.session_operator(t.token)
    store.end_session(t.token)
    if op:
        store.audit(op, "logout", request_id=x_request_id)
    return Response(status_code=204)


def current_operator(x_actor: str) -> dict:
    """api передаёт id уже проверенного оператора в x-actor; неизвестный id отклоняется."""
    op = store.operator_by_id(x_actor.removeprefix("operator:")) if x_actor else None
    if not op:
        raise ApiErr(401, "invalid_session", "Сессия оператора недействительна")
    return op


# ---------- вызовы внутренних сервисов ----------
class Hop:
    """Один вызов внутреннего сервиса: заголовки x-request-id/x-actor, 503 при недоступности, ошибки сервиса как есть."""

    def __init__(self, http: httpx.AsyncClient, rid: str, actor: str):
        self.http, self.rid, self.actor = http, rid, actor

    async def call(self, svc: str, base_env: str, default: str, method: str, path: str, *, ok404: bool = False, **kw) -> Any:
        base = os.environ.get(base_env, default).rstrip("/")
        headers = {"x-actor": f"operator:{self.actor}"}
        if self.rid:
            headers["x-request-id"] = self.rid
        try:
            r = await self.http.request(method, f"{base}{path}", headers=headers, **kw)
        except httpx.HTTPError:
            raise ApiErr(503, f"{svc}_unavailable", f"Сервис «{svc}» недоступен")
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            try:
                e = r.json()["error"]
                code, msg = str(e["code"]), str(e["message"])
            except (ValueError, KeyError, TypeError):
                code, msg = f"{svc}_error", f"Ошибка сервиса «{svc}»"
            status = r.status_code if r.status_code < 500 else 502
            raise ApiErr(status, code, msg)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise ApiErr(502, f"{svc}_error", f"Ошибка сервиса «{svc}»")

    def ledger(self, method: str, path: str, **kw):
        return self.call("ledger", "LEDGER_URL", "http://127.0.0.1:3002", method, path, **kw)

    def auth(self, method: str, path: str, **kw):
        return self.call("auth", "AUTH_URL", "http://127.0.0.1:8001", method, path, **kw)

    def forecast(self, method: str, path: str, **kw):
        return self.call("forecast", "FORECAST_URL", "http://127.0.0.1:3003", method, path, **kw)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=float(os.environ.get("BACKOFFICE_TIMEOUT_SECONDS", "15")), transport=app.state.transport)


def company_view(c: dict) -> dict:
    out = {k: c.get(k) for k in COMPANY_FIELDS}
    out["banned"] = bool(out["banned"])
    members = c.get("members")
    if isinstance(members, list):
        out["members"] = [{k: m.get(k) for k in MEMBER_FIELDS} for m in members if isinstance(m, dict)]
    return out


async def risk_of(http: httpx.AsyncClient, hop: Hop, sem: asyncio.Semaphore, company: dict) -> dict[str, Any]:
    """Уровень риска и вероятность разрыва из forecast summary; нет прогноза или сервис не ответил -> null."""
    none: dict[str, Any] = {"risk_level": None, "shortfall_probability": None}
    cid = company.get("id")
    if not cid or not CID.match(str(cid)) or not company.get("transactions_count"):
        return none
    hit = _forecast_cache.get(cid)
    if hit and time.time() - hit[0] < FORECAST_TTL:
        return hit[1]
    async with sem:
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT):
                s = await hop.forecast("GET", f"/internal/v1/companies/{cid}/summary", ok404=True)
        except (ApiErr, TimeoutError):
            return none  # ошибку не кэшируем
    risk = (s or {}).get("risk") or {}
    out: dict[str, Any] = {"risk_level": risk.get("level"), "shortfall_probability": risk.get("shortfall_probability")}
    if len(_forecast_cache) > 2000:
        _forecast_cache.clear()
    _forecast_cache[cid] = (time.time(), out)
    return out


def check_cid(cid: str) -> None:
    if not CID.match(cid):
        raise ApiErr(404, "not_found", "Не найдено")


# ---------- админ-эндпоинты ----------
@app.get("/internal/v1/admin/companies")
async def companies(query: str = "", banned: str = "", limit: int = 50, offset: int = 0,
                    x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    op = current_operator(x_actor)
    query = query.strip()
    if len(query) > 100 or banned not in ("", "true", "false") or not (1 <= limit <= LIST_LIMIT_MAX) or offset < 0:
        raise ApiErr(422, "invalid_request", "Некорректные параметры списка")
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if banned:
        params["banned"] = banned
    async with client() as http:
        hop = Hop(http, x_request_id, op["operator_id"])
        data = await hop.ledger("GET", "/internal/v1/admin/companies", params=params)
        items = [company_view(c) for c in data.get("items", []) if isinstance(c, dict)]
        sem = asyncio.Semaphore(FORECAST_CONCURRENCY)
        risks = await asyncio.gather(*(risk_of(http, hop, sem, c) for c in items))
    for c, r in zip(items, risks):
        c.update(r)
    return {"total": data.get("total", len(items)), "limit": limit, "offset": offset, "items": items}


@app.get("/internal/v1/admin/companies/{cid}")
async def company(cid: str, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    op = current_operator(x_actor)
    check_cid(cid)
    async with client() as http:
        hop = Hop(http, x_request_id, op["operator_id"])
        c = company_view(await hop.ledger("GET", f"/internal/v1/admin/companies/{cid}"))
        c.update(await risk_of(http, hop, asyncio.Semaphore(1), c))
    return c


def clean_reason(reason: str) -> str:
    r = " ".join(reason.split())  # переводы строк и повторные пробелы в одну строку
    if not (REASON_MIN <= len(r) <= REASON_MAX) or any(ord(ch) < 32 for ch in r):
        raise ApiErr(422, "invalid_reason", f"Укажите причину блокировки ({REASON_MIN}–{REASON_MAX} символов)")
    return r


@app.post("/internal/v1/admin/companies/{cid}/ban")
async def ban(cid: str, b: BanIn, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    op = current_operator(x_actor)
    check_cid(cid)
    reason = clean_reason(b.reason)
    async with client() as http:
        hop = Hop(http, x_request_id, op["operator_id"])
        members = await hop.ledger("GET", f"/internal/v1/companies/{cid}/members")  # 404, если компании нет
        user_ids = [m["user_id"] for m in members.get("items", []) if isinstance(m, dict) and m.get("user_id")]
        try:
            await hop.ledger("POST", f"/internal/v1/admin/companies/{cid}/ban", json={"reason": reason})
        except ApiErr as e:
            store.audit(op, "ban", cid, reason, outcome=e.code, request_id=x_request_id)
            raise
        # блокировка уже действует в ledger; здесь дополнительно обрываем действующие сессии всех участников
        results = await asyncio.gather(
            *(hop.auth("POST", f"/internal/v1/users/{uid}/revoke-sessions") for uid in user_ids), return_exceptions=True
        )
    failed = sum(1 for r in results if isinstance(r, BaseException))
    store.audit(op, "ban", cid, f"{reason} | sessions_revoked={len(user_ids) - failed} revoke_failed={failed}", request_id=x_request_id)
    return {"company_id": cid, "banned": True, "ban_reason": reason, "sessions_revoked": len(user_ids) - failed, "revoke_failed": failed}


@app.post("/internal/v1/admin/companies/{cid}/unban")
async def unban(cid: str, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    op = current_operator(x_actor)
    check_cid(cid)
    async with client() as http:
        try:
            await Hop(http, x_request_id, op["operator_id"]).ledger("POST", f"/internal/v1/admin/companies/{cid}/unban")
        except ApiErr as e:
            store.audit(op, "unban", cid, outcome=e.code, request_id=x_request_id)
            raise
    store.audit(op, "unban", cid, request_id=x_request_id)
    return {"company_id": cid, "banned": False}


def _created(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(value[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


STATS_FROM_LEDGER = {
    "total": "companies_total", "active": "companies_active", "banned": "companies_banned",
    "new_7d": "new_7d", "new_30d": "new_30d", "users_total": "members_total",
}


async def stats_by_paging(hop: Hop) -> dict[str, int]:
    """Запасной путь, если у ledger нет /admin/stats: проходим постранично по списку компаний (только метаданные)."""
    now = datetime.now(timezone.utc)
    out = {"total": 0, "active": 0, "banned": 0, "new_7d": 0, "new_30d": 0, "users_total": 0}
    offset = 0
    for _ in range(1000):
        page = await hop.ledger("GET", "/internal/v1/admin/companies", params={"limit": 200, "offset": offset})
        items = [c for c in page.get("items", []) if isinstance(c, dict)]
        for c in items:
            out["total"] += 1
            out["banned" if c.get("banned") else "active"] += 1
            out["users_total"] += int(c.get("members_count") or 0)
            d = _created(c.get("created_at"))
            if d and d >= now - timedelta(days=7):
                out["new_7d"] += 1
            if d and d >= now - timedelta(days=30):
                out["new_30d"] += 1
        offset += len(items)
        if not items or offset >= int(page.get("total", 0) or 0):
            break
    return out


@app.get("/internal/v1/admin/stats")
async def stats(x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    """Счётчики платформы: всего, активных, заблокированных, новых за 7/30 дней, пользователей."""
    op = current_operator(x_actor)
    async with client() as http:
        hop = Hop(http, x_request_id, op["operator_id"])
        v = await hop.ledger("GET", "/internal/v1/admin/stats", ok404=True)
        if isinstance(v, dict) and all(isinstance(v.get(src), int) for src in STATS_FROM_LEDGER.values()):
            return {k: v[src] for k, src in STATS_FROM_LEDGER.items()}
        return await stats_by_paging(hop)


@app.get("/internal/v1/admin/audit")
def audit_log(limit: int = 100, company_id: str = "", x_actor: str = Header(default="")):
    current_operator(x_actor)
    if not (1 <= limit <= 500) or (company_id and not CID.match(company_id)):
        raise ApiErr(422, "invalid_request", "Некорректные параметры")
    return {"items": store.audit_list(limit, company_id or None)}
