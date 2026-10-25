"""Внутренний сервис уведомлений (порт 8009): Telegram-бот, расписание, цели недели.
uvicorn notifier.app:app --host 127.0.0.1 --port 8009
Без TELEGRAM_BOT_TOKEN бот выключен (configured=false), а расписание работает: алерты и отчёты уходят письмом через notify
тем участникам, у которых включена почта."""
import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .bot import BotApp
from .jobs import Jobs
from .store import KINDS, ROLES, STATUSES, Store
from .telegram import Bot, TelegramError
from .upstream import Upstream, UpstreamError

log = logging.getLogger("notifier")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CO = "/internal/v1/companies/{cid}/notifications"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = app.state.store or Store()
    token = config.bot_token()
    app.state.bot = app.state.bot or (Bot(token) if token else None)
    app.state.up = app.state.up or Upstream()
    # расписание нужно и без бота: почтовый канал (notify) работает сам по себе
    app.state.jobs = Jobs(app.state.store, app.state.bot, app.state.up)
    app.state.username = config.env("TELEGRAM_BOT_USERNAME")
    tasks = []
    if app.state.bot and not app.state.disable_tasks:
        try:
            app.state.username = app.state.username or (await app.state.bot.get_me()).get("username", "")
        except TelegramError as e:
            log.error("getMe: %s (проверьте TELEGRAM_BOT_TOKEN)", e.code)
        app.state.botapp = BotApp(app.state.store, app.state.bot, app.state.jobs, app.state.username)
        tasks = [asyncio.create_task(app.state.botapp.poll_forever())]
    if not app.state.disable_tasks:
        tasks.append(asyncio.create_task(app.state.jobs.run_scheduler()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="notifier-service", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.store = app.state.bot = app.state.up = None
app.state.disable_tasks = False


@app.exception_handler(HTTPException)
async def http_error(_: Request, e: HTTPException):
    """Тело ошибки как у остальных сервисов: {"error": {"code", "message"}} — его читает SPA."""
    d = e.detail if isinstance(e.detail, dict) else {"code": "error", "message": str(e.detail)}
    return JSONResponse({"error": {"code": d.get("code", "error"), "message": d.get("message", "")}}, status_code=e.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, e: RequestValidationError):
    return JSONResponse({"error": {"code": "invalid_request", "message": "Некорректные параметры запроса"}}, status_code=422)


def jobs(request: Request) -> Jobs:
    """Рассылки для Telegram: без токена бота — 503."""
    if not request.app.state.bot:
        raise HTTPException(503, {"code": "bot_not_configured", "message": "Telegram-бот не настроен: не задан TELEGRAM_BOT_TOKEN"})
    return request.app.state.jobs


def any_jobs(request: Request) -> Jobs:
    """Рассылки, которые доходят хотя бы одним каналом: Telegram или почта."""
    return request.app.state.jobs


@app.get("/live")
def live():
    return {"status": "ok"}


def chat_json(c: dict) -> dict:
    return {"chat_id": c["chat_id"], "kind": c["kind"], "title": c["title"], "linked_at": c["linked_at"], "department_id": c.get("department_id")}


def prefs_json(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "company_id"}


@app.get(CO)
def status(cid: str, request: Request):
    s: Store = request.app.state.store
    return {"configured": request.app.state.bot is not None, "advisor": bool(config.hermes_url()), "bot_username": request.app.state.username,
            "chats": [chat_json(c) for c in s.chats(cid)], "prefs": prefs_json(s.prefs(cid))}


class LinkBody(BaseModel):
    web_user_id: str | None = None
    web_role: str | None = None


class WebRoleBody(BaseModel):
    web_user_id: str
    web_role: str | None = None


@app.post(CO + "/link")
def link(cid: str, request: Request, body: LinkBody | None = None, x_actor: str = Header(default="")):
    """Одноразовая ссылка привязки. Код действует 15 минут и сгорает при первом использовании.
    Роль в боте выводится из веб-роли того, кто выпустил ссылку (её передаёт BFF)."""
    jobs(request)
    if body and body.web_role and body.web_role not in request.app.state.store.WEB_TO_TG:
        raise HTTPException(422, {"code": "invalid_role", "message": "Неизвестная роль"})
    code = request.app.state.store.new_link_code(cid, (body and body.web_user_id) or x_actor, (body and body.web_role) or "")
    u = request.app.state.username
    return {"code": code, "expires_in": config.LINK_TTL, "link": f"https://t.me/{u}?start={code}", "group_link": f"https://t.me/{u}?startgroup={code}"}


class PrefsBody(BaseModel):
    tz: str | None = None
    risk_enabled: bool | None = None
    risk_dow: int | None = Field(None, ge=0, le=6)
    risk_time: str | None = None
    week_enabled: bool | None = None
    week_dow: int | None = Field(None, ge=0, le=6)
    week_time: str | None = None
    month_enabled: bool | None = None
    month_day: int | None = Field(None, ge=1, le=31)
    month_time: str | None = None
    gap_enabled: bool | None = None
    gap_days: int | None = Field(None, ge=1, le=30)
    gap_threshold: float | None = Field(None, ge=0.05, le=0.95)
    ai_consent: bool | None = None
    missed_enabled: bool | None = None
    missed_min_kop: int | None = Field(None, ge=0, le=10**13)


@app.put(CO + "/prefs")
def set_prefs(cid: str, body: PrefsBody, request: Request):
    ch = {k: v for k, v in body.model_dump().items() if v is not None}
    for k in ("risk_time", "week_time", "month_time"):
        if k in ch and not TIME_RE.match(ch[k]):
            raise HTTPException(422, {"code": "invalid_time", "message": "Время в формате ЧЧ:ММ"})
    if "tz" in ch:
        try:
            config.ZoneInfo(ch["tz"])
        except Exception:
            raise HTTPException(422, {"code": "invalid_tz", "message": "Неизвестный часовой пояс"})
    for k, v in list(ch.items()):
        if isinstance(v, bool):
            ch[k] = int(v)
    return prefs_json(request.app.state.store.set_prefs(cid, ch))


@app.post(CO + "/web-role")
def web_role(cid: str, body: WebRoleBody, request: Request):
    """BFF сообщает, что веб-роль человека изменилась или его убрали из команды (role=null)."""
    if body.web_role is not None and body.web_role not in request.app.state.store.WEB_TO_TG:
        raise HTTPException(422, {"code": "invalid_role", "message": "Неизвестная роль"})
    return {"updated": request.app.state.store.sync_web_role(cid, body.web_user_id, body.web_role)}


@app.delete(CO + "/chats/{chat_id}", status_code=204)
def unlink(cid: str, chat_id: int, request: Request):
    if not request.app.state.store.unlink_chat(chat_id, cid):
        raise HTTPException(404, {"code": "not_found", "message": "Чат не найден"})


@app.post(CO + "/test")
async def test_message(cid: str, request: Request):
    j = jobs(request)
    n = await j.broadcast(cid, lambda c: j.bot.send(c, "✅ Тестовое уведомление: бот подключён и может писать в этот чат."))
    return {"delivered": n}


@app.post(CO + "/send/{kind}")
async def send_now(cid: str, kind: str, request: Request):
    """Ручной запуск рассылки (для проверки и кнопки «Отправить сейчас» в настройках): в Telegram и на почту."""
    j = any_jobs(request)
    has_chats = request.app.state.bot is not None and request.app.state.store.chats(cid)
    if not has_chats and not await j.up.mail_recipients(cid, kind):
        raise HTTPException(409, {"code": "no_chats", "message": "Некому отправить: нет подключённых чатов и подписчиков на почту"})
    try:
        if kind == "risk":
            await j.send_risk(cid)
        elif kind == "week":
            await j.send_week(cid, manual=True)
        elif kind == "month":
            await j.send_month(cid)
        elif kind == "gap":
            return {"result": await j.check_gap(cid)}
        elif kind == "missed":
            return {"result": await j.check_missed(cid)}
        else:
            raise HTTPException(404, {"code": "unknown_kind", "message": "Неизвестный тип отчёта"})
    except UpstreamError as e:
        raise HTTPException(502 if e.status >= 500 else 409, {"code": "no_data", "message": "Нет данных для отчёта: загрузите выписку"})
    return {"result": "sent"}


# ---------- цели недели и ретро (веб-ввод; то же, что кнопки в боте) ----------
class GoalBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    target_kop: int | None = Field(None, ge=0, le=10**15)
    due: str | None = None


class GoalPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    target_kop: int | None = Field(None, ge=0, le=10**15)
    due: str | None = None
    status: str | None = None
    actual_kop: int | None = Field(None, ge=-(10**15), le=10**15)


class RetroBody(BaseModel):
    week_start: str
    mood: int | None = Field(None, ge=1, le=5)
    blockers: str | None = Field(None, max_length=500)
    note: str | None = Field(None, max_length=1000)


def _sprint(request: Request, cid: str):
    s: Store = request.app.state.store
    p = s.prefs(cid)
    today = config.now_in(config.tz_of(p["tz"])).date()
    row = s.one("SELECT MAX(week_start) AS w FROM goals WHERE company_id=? AND week_start<=?", (cid, today.isoformat()))
    cur = row["w"] if row else None
    return s, today, cur


@app.get(CO + "/sprint")
def sprint(cid: str, request: Request):
    s, today, cur = _sprint(request, cid)
    from .analytics import velocity
    prev_week, prev = s.prev_goals(cid, cur) if cur else (None, [])
    return {"today": today.isoformat(), "sprint": cur, "goals": s.goals(cid, cur) if cur else [], "prev_sprint": prev_week, "prev_goals": prev,
            "velocity": velocity(prev), "retro": s.retro(cid, prev_week) if prev_week else None}


@app.post(CO + "/goals", status_code=201)
def add_goal(cid: str, body: GoalBody, request: Request):
    s, today, cur = _sprint(request, cid)
    gid = s.add_goal(cid, cur or today.isoformat(), "custom", body.title.strip(), body.target_kop, body.due, "user")
    return s.goal(cid, gid)


@app.patch(CO + "/goals/{gid}")
async def patch_goal(cid: str, gid: int, body: GoalPatch, request: Request):
    s: Store = request.app.state.store
    if body.status is not None and body.status not in STATUSES:
        raise HTTPException(422, {"code": "bad_status", "message": "Статус: " + ", ".join(STATUSES)})
    g = s.update_goal(cid, gid, {k: v for k, v in body.model_dump().items() if v is not None}) if s.goal(cid, gid) else None
    if not g:
        raise HTTPException(404, {"code": "not_found", "message": "Цель не найдена"})
    # все цели прошлого спринта оценены — план текущего пересобирается (так же, как при оценке кнопками)
    j: Jobs | None = request.app.state.jobs
    if body.status and j and not [x for x in s.goals(cid, g["week_start"]) if x["status"] == "open"]:
        nxt = s.one("SELECT MIN(week_start) AS w FROM goals WHERE company_id=? AND week_start>?", (cid, g["week_start"]))
        if nxt and nxt["w"]:
            try:
                await j.replan(cid, nxt["w"])
            except UpstreamError:
                pass
    return g


@app.delete(CO + "/goals/{gid}", status_code=204)
def delete_goal(cid: str, gid: int, request: Request):
    if not request.app.state.store.delete_goal(cid, gid):
        raise HTTPException(404, {"code": "not_found", "message": "Цель не найдена"})


@app.post(CO + "/retro")
def retro(cid: str, body: RetroBody, request: Request):
    return request.app.state.store.save_retro(cid, body.week_start, body.mood, body.blockers, body.note)


@app.get(CO + "/history")
def history(cid: str, request: Request):
    """Последние спринты: выполнение целей по неделям (для графика на сайте)."""
    from .analytics import velocity
    out = []
    for sp in request.app.state.store.sprints(cid):
        out.append({"week_start": sp["week_start"], "velocity": velocity(sp["goals"]), "retro": sp["retro"],
                    "done": sum(g["status"] == "done" for g in sp["goals"]), "total": sum(g["status"] != "skipped" for g in sp["goals"])})
    return {"items": out}


# ---------- команда: отделы, роли, групповые политики (управляет администратор компании) ----------
class DeptBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kinds: list[str] = list(KINDS)
    allow_excel: bool = True
    default_role: str = "viewer"


class DeptPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    kinds: list[str] | None = None
    allow_excel: bool | None = None
    default_role: str | None = None


class ChatPatch(BaseModel):
    department_id: int | None = None


class MemberPatch(BaseModel):
    role: str | None = None
    department_id: int | None = None


def _bad_policy():
    return HTTPException(422, {"code": "invalid_policy", "message": f"Роли: {', '.join(ROLES)}; рассылки: {', '.join(KINDS)}"})


@app.get(CO + "/team")
def team(cid: str, request: Request):
    s: Store = request.app.state.store
    return {"roles": list(ROLES), "kinds": list(KINDS), "departments": s.departments(cid),
            "chats": [chat_json(c) for c in s.chats(cid)],
            "members": [{"tg_user_id": m["tg_user_id"], "name": m["name"], "role": m["role"] or None, "department_id": m["department_id"]} for m in s.members(cid)]}


@app.post(CO + "/departments", status_code=201)
def add_department(cid: str, body: DeptBody, request: Request):
    s: Store = request.app.state.store
    try:
        did = s.add_department(cid, body.name.strip(), body.kinds, body.allow_excel, body.default_role)
    except ValueError:
        raise _bad_policy()
    except Exception:
        raise HTTPException(409, {"code": "department_exists", "message": "Отдел с таким названием уже есть"})
    return s.department(cid, did)


@app.patch(CO + "/departments/{did}")
def patch_department(cid: str, did: int, body: DeptPatch, request: Request):
    try:
        d = request.app.state.store.update_department(cid, did, body.model_dump())
    except ValueError:
        raise _bad_policy()
    if not d:
        raise HTTPException(404, {"code": "not_found", "message": "Отдел не найден"})
    return d


@app.delete(CO + "/departments/{did}", status_code=204)
def delete_department(cid: str, did: int, request: Request):
    if not request.app.state.store.delete_department(cid, did):
        raise HTTPException(404, {"code": "not_found", "message": "Отдел не найден"})


@app.patch(CO + "/chats/{chat_id}")
def patch_chat(cid: str, chat_id: int, body: ChatPatch, request: Request):
    if not request.app.state.store.set_chat_department(cid, chat_id, body.department_id):
        raise HTTPException(404, {"code": "not_found", "message": "Чат или отдел не найден"})
    return {"chat_id": chat_id, "department_id": body.department_id}


@app.patch(CO + "/members/{uid}")
def patch_member(cid: str, uid: int, body: MemberPatch, request: Request):
    s: Store = request.app.state.store
    if body.department_id is not None and not s.department(cid, body.department_id):
        raise HTTPException(404, {"code": "not_found", "message": "Отдел не найден"})
    try:
        m = s.set_member(cid, uid, body.role, body.department_id, "department_id" in body.model_fields_set)
    except ValueError:
        raise _bad_policy()
    if not m:
        raise HTTPException(404, {"code": "not_found", "message": "Участник не найден"})
    return m


@app.delete(CO + "/members/{uid}", status_code=204)
def delete_member(cid: str, uid: int, request: Request):
    if not request.app.state.store.delete_member(cid, uid):
        raise HTTPException(404, {"code": "not_found", "message": "Участник не найден"})
