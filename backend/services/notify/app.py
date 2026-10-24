"""notify (порт 8014): единственный почтовый шлюз. Очередь outbox с повторами, служебные письма и почтовый канал
алертов и отчётов notifier. Доступен только из служебной сети; вызывают api, backoffice и notifier.
uvicorn notify.app:app --host 127.0.0.1 --port 8014

  POST /internal/v1/send {kind, to, data, dedup_key?} -> 202 {id, status}: служебные письма (подтверждение почты, сброс
       пароля, приглашения, блокировка, тест). Ссылку с токеном notify собирает сам из data.token.
  POST /internal/v1/companies/{cid}/broadcast {kind, key, subject, html, ...} -> {queued}: алерт или отчёт notifier
       всем участникам компании с подтверждённой почтой и включённой подпиской (alerts — gap*, reports — risk/week/month)
  GET  /internal/v1/companies/{cid}/subscribers -> {alerts, reports}; GET /internal/v1/companies-with-subscribers
  GET|PUT /internal/v1/prefs/{user_id}?company_id=&role= -> {alerts, reports}
  POST /internal/v1/unsubscribe {token} -> {ok}; DELETE /internal/v1/users/{user_id}
Ошибки: {"error":{"code","message"}}.
"""

import asyncio
import base64
import binascii
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import mailer, render, store

FLUSH_EVERY = float(os.environ.get("NOTIFY_FLUSH_INTERVAL_SECONDS", "5"))
SCHEDULER = os.environ.get("NOTIFY_SCHEDULER", "1") == "1"
TRANSACTIONAL = {"verify_email", "reset_password", "invite", "team_invite", "member_reset", "company_banned", "test"}
# куда ведёт ссылка с токеном
TOKEN_PATHS = {
    "verify_email": "/verify-email?token={}",
    "reset_password": "/reset-password?token={}",
    "invite": "/reset-password?token={}&invite=1",
    "team_invite": "/?invite={}",
    "member_reset": "/reset-password?token={}",
}
# приглашения в команду: не больше 50 писем в час на компанию, чтобы через них нельзя было рассылать спам
INVITES_PER_HOUR = 50
# рассылки notifier -> подписка, которая их включает
BROADCAST = {"gap": "alerts", "gap_resolved": "alerts", "risk": "reports", "week": "reports", "month": "reports"}
TONES = {"gap": "danger", "gap_resolved": "success"}
MAX_ATTACHMENT = 5 * 1024 * 1024
SUBSCRIBERS_TTL = 300  # список компаний с подписчиками для планировщика notifier
_subscribed: dict[str, Any] = {}  # {"at": время, "items": [company_id]}
UID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def log(msg: str) -> None:
    print(f"notify: {msg}", flush=True)  # без адресов, токенов и содержимого писем


class ApiErr(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


async def _loop(name: str, every: float, job) -> None:
    while True:
        try:
            await job()
        except Exception as e:  # noqa: BLE001 фоновая задача не должна умирать
            log(f"{name} failed: {type(e).__name__}")
        await asyncio.sleep(every)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    if SCHEDULER:
        tasks = [asyncio.create_task(_loop("outbox", FLUSH_EVERY, mailer.flush))]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="notify", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.transport = None  # подмена транспорта в тестах


@app.exception_handler(ApiErr)
async def _api_err(_r: Request, e: ApiErr):
    return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _invalid(_r: Request, _e: RequestValidationError):
    return JSONResponse({"error": {"code": "invalid_request", "message": "Некорректный запрос"}}, status_code=422)


@app.get("/live")
def live():
    return {"status": "ok"}


# ---------- внутренние сервисы ----------
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=float(os.environ.get("NOTIFY_TIMEOUT_SECONDS", "60")), transport=app.state.transport
    )


async def call(http: httpx.AsyncClient, env: str, default: str, path: str, ok404: bool = False) -> Any:
    base = os.environ.get(env, default).rstrip("/")
    r = await http.get(f"{base}{path}", headers={"x-actor": "service:notify"})
    if r.status_code == 404 and ok404:
        return None
    r.raise_for_status()
    return r.json()


async def companies(http: httpx.AsyncClient) -> list[dict]:
    """Незаблокированные компании платформы."""
    out, offset = [], 0
    while True:
        page = await call(
            http,
            "LEDGER_URL",
            "http://127.0.0.1:3002",
            f"/internal/v1/admin/companies?banned=false&limit=100&offset={offset}",
        )
        out += page.get("items", [])
        offset += 100
        if offset >= page.get("total", 0):
            return out


async def subscribers(http: httpx.AsyncClient, cid: str) -> list[dict]:
    """Участники компании с подтверждённой почтой и их подписки: [{user_id, role, email, alerts, reports, unsub_token}]."""
    members = (
        await call(http, "LEDGER_URL", "http://127.0.0.1:3002", f"/internal/v1/companies/{cid}/members", ok404=True)
        or {}
    )
    people = [m for m in members.get("items", []) if m.get("user_id")]
    if not people:
        return []
    ids = ",".join(m["user_id"] for m in people)
    status = await call(http, "AUTH_URL", "http://127.0.0.1:8001", f"/internal/v1/users/email-status?user_ids={ids}")
    verified = {x["user_id"]: x["email"] for x in status.get("items", []) if x.get("verified")}
    known = store.known_prefs([m["user_id"] for m in people if m["user_id"] in verified])
    out = []
    for m in people:
        if m["user_id"] not in verified:
            continue
        pr = known.get(m["user_id"]) or store.get_prefs(m["user_id"], cid, m.get("role", "viewer"))
        out.append({"user_id": m["user_id"], "role": m.get("role", "viewer"), "email": verified[m["user_id"]], **pr})
    return out


def queue(
    kind: str,
    to: str,
    data: dict,
    dedup_key: str | None,
    unsub: str | None = None,
    company_id: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    subject, html, text, headers = render.render(kind, data, unsub)
    return store.enqueue(kind, to, subject, html, text, headers, dedup_key, company_id, attachments)


# ---------- служебные письма ----------
class SendIn(BaseModel):
    kind: str
    to: str = Field(max_length=254)
    data: dict = {}
    dedup_key: str | None = Field(default=None, max_length=200)


@app.post("/internal/v1/send", status_code=202)
async def send(b: SendIn):
    if b.kind not in TRANSACTIONAL:
        raise ApiErr(422, "invalid_kind", "Неизвестный тип письма")
    to = b.to.strip().lower()
    if not EMAIL.match(to) or to.endswith("@ecp.invalid"):
        raise ApiErr(422, "invalid_email", "Некорректный email")
    data = {k: v for k, v in b.data.items() if isinstance(v, (str, int, float, bool)) or v is None}
    data.setdefault("email", to)
    if b.kind in TOKEN_PATHS:
        token = str(data.pop("token", ""))
        if not token:
            raise ApiErr(422, "invalid_request", "Нет токена ссылки")
        data["url"] = render.link(TOKEN_PATHS[b.kind].format(token))
    if "role" in data and "role_text" not in data:
        data["role_text"] = render.ROLES.get(str(data["role"]), str(data["role"]))
    cid = str(data.get("company_id") or "")
    if b.kind == "team_invite" and cid and store.sent_recently(cid, "team_invite", 3600) >= INVITES_PER_HOUR:
        raise ApiErr(429, "mail_limited", "Слишком много приглашений за час, попробуйте позже")
    try:
        out = queue(b.kind, to, data, b.dedup_key, company_id=cid)
    except Exception as e:  # noqa: BLE001 ошибка шаблона (нет переменной) — ошибка вызывающего
        log(f"render {b.kind} failed: {type(e).__name__}")
        raise ApiErr(422, "invalid_request", "Не хватает данных для письма")
    asyncio.get_running_loop().create_task(mailer.flush())  # не ждём минутного цикла
    return {"id": out["id"], "status": out["status"]}


# ---------- настройки ----------
class PrefsIn(BaseModel):
    alerts: bool | None = None
    reports: bool | None = None


def check_uid(uid: str) -> None:
    if not UID.match(uid):
        raise ApiErr(404, "not_found", "Не найдено")


def prefs_out(p: dict) -> dict:
    return {"alerts": p["alerts"], "reports": p["reports"]}


@app.get("/internal/v1/prefs/{uid}")
def prefs_get(uid: str, company_id: str = "", role: str = "viewer"):
    check_uid(uid)
    return prefs_out(store.get_prefs(uid, company_id, role))


@app.put("/internal/v1/prefs/{uid}")
def prefs_put(uid: str, b: PrefsIn, company_id: str = "", role: str = "viewer"):
    check_uid(uid)
    _subscribed.clear()  # список компаний для планировщика notifier пересчитается
    return prefs_out(store.put_prefs(uid, company_id, role, **b.model_dump()))


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


@app.post("/internal/v1/unsubscribe")
def unsubscribe(b: TokenIn):
    if not b.token or not store.unsubscribe(b.token):
        raise ApiErr(404, "token_invalid", "Ссылка недействительна или устарела")
    return {"ok": True}


@app.delete("/internal/v1/users/{uid}", status_code=204)
def user_delete(uid: str):
    check_uid(uid)
    store.delete_user(uid)


# ---------- почтовый канал notifier ----------
CID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_cid(cid: str) -> None:
    if not CID.match(cid):
        raise ApiErr(404, "not_found", "Не найдено")


class BroadcastIn(BaseModel):
    kind: str
    key: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    html: str = Field(max_length=20000)  # Telegram-HTML: <b>, <i>, <a>, <code>, переводы строк
    path: str = Field(default="/", max_length=100)
    button: str = Field(default="Открыть в сервисе", max_length=60)
    chart_png_b64: str | None = Field(default=None, max_length=MAX_ATTACHMENT * 2)
    chart_alt: str = Field(default="График", max_length=200)
    xlsx_b64: str | None = Field(default=None, max_length=MAX_ATTACHMENT * 2)
    xlsx_name: str = Field(default="otchet.xlsx", max_length=80)


def _b64(v: str | None) -> bytes | None:
    if not v:
        return None
    try:
        data = base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError):
        raise ApiErr(422, "invalid_request", "Вложение повреждено")
    if len(data) > MAX_ATTACHMENT:
        raise ApiErr(422, "invalid_request", "Вложение больше 5 МБ")
    return data


@app.post("/internal/v1/companies/{cid}/broadcast")
async def broadcast(cid: str, b: BroadcastIn):
    """Алерт или отчёт notifier — письмом каждому подписчику компании. Повтор с тем же key второго письма не создаёт."""
    check_cid(cid)
    if b.kind not in BROADCAST:
        raise ApiErr(422, "invalid_kind", "Неизвестный тип рассылки")
    if not b.path.startswith("/") or "//" in b.path:
        raise ApiErr(422, "invalid_request", "Некорректная ссылка")
    png, xlsx = _b64(b.chart_png_b64), _b64(b.xlsx_b64)
    body_html, body_text = render.tg_to_mail(b.html)
    xlsx_name = re.sub(r"[^A-Za-z0-9_.-]", "_", b.xlsx_name)[:80] or "otchet.xlsx"
    try:
        async with client() as http:
            people = await subscribers(http, cid)
    except httpx.HTTPError:
        raise ApiErr(503, "upstream_unavailable", "Состав компании сейчас недоступен")
    want = BROADCAST[b.kind]
    queued = 0
    for p in people:
        if not p[want]:
            continue
        attachments = []
        if png:
            attachments.append({"filename": "chart.png", "mime": "image/png", "data": png, "content_id": "chart"})
        if xlsx:
            attachments.append(
                {
                    "filename": xlsx_name,
                    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": xlsx,
                }
            )
        data = {
            "title": render.one_line(b.subject),
            "body_html": body_html,
            "body_text": body_text.strip(),
            "tone": TONES.get(b.kind, ""),
            "chart_cid": "chart" if png else None,
            "chart_alt": b.chart_alt,
            "attachment_name": xlsx_name if xlsx else None,
            "url": render.link(b.path),
            "button_label": b.button,
        }
        out = queue("report", p["email"], data, f"{b.kind}:{p['user_id']}:{b.key}", p["unsub_token"], cid, attachments)
        queued += not out["duplicate"]
    if queued:
        asyncio.get_running_loop().create_task(mailer.flush())
    return {"queued": queued, "recipients": sum(1 for p in people if p[want])}


@app.get("/internal/v1/companies/{cid}/subscribers")
async def subscribers_count(cid: str):
    check_cid(cid)
    try:
        async with client() as http:
            people = await subscribers(http, cid)
    except httpx.HTTPError:
        raise ApiErr(503, "upstream_unavailable", "Состав компании сейчас недоступен")
    return {"alerts": sum(p["alerts"] for p in people), "reports": sum(p["reports"] for p in people)}


@app.get("/internal/v1/companies-with-subscribers")
async def companies_with_subscribers():
    """Компании, где хоть кто-то получает алерты или отчёты на почту: notifier обходит их, даже если Telegram не подключён.
    Кэш на 5 минут: планировщик notifier спрашивает каждую минуту."""
    if _subscribed.get("at", 0) > time.time() - SUBSCRIBERS_TTL:
        return {"items": _subscribed["items"]}
    items = []
    try:
        async with client() as http:
            for c in await companies(http):
                people = await subscribers(http, c["id"])
                if any(p["alerts"] or p["reports"] for p in people):
                    items.append(c["id"])
    except httpx.HTTPError:
        raise ApiErr(503, "upstream_unavailable", "Состав компаний сейчас недоступен")
    _subscribed.update(at=time.time(), items=items)
    return {"items": items}
