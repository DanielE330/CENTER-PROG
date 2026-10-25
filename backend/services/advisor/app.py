"""Консультант (порт 8011): вопрос + контекст компании -> ответ, цифры из инструментов и предложения действий.
uvicorn advisor.app:app --host 127.0.0.1 --port 8011

POST /internal/v1/companies/{cid}/ask {question, history?, lang?, context?:{company_name}, permissions?}  (пользователь — x-actor)
  -> {answer, suggestions:[{id,type,params,title}], actions:[...], followups:[...], provider, model, notice, usage, tools_used}
GET  /internal/v1/companies/{cid}/prompts?lang=&permissions=a,b  -> {items:[{text, kind}]} — подсказки «что написать»
GET  /internal/v1/engine -> {engine: hermes|llm_gateway, hermes:{configured, available, models}}
POST /mcp — инструменты для Hermes (mcp.py)

Движок: Hermes Agent, если задан HERMES_AGENT_URL (подписка Codex через OAuth в контейнере hermes); иначе агент через
llm_gateway; при недоступности — ответ по шаблону из прогноза (provider "none", notice: provider_not_configured |
provider_unavailable | budget_exceeded). Права (`permissions`) выдаёт владелец; api пересекает их с ролью пользователя.
Записывать консультант может только с правом add_payments/recalc_forecast; остальное — предложения, их применяет пользователь."""
import asyncio
import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import agent, chats, hermes, mcp, permissions, prompts, template, tickets, tools as tl

app = FastAPI(title="advisor", docs_url=None, redoc_url=None)
app.include_router(mcp.router)
app.include_router(chats.router)
# Подмена транспорта в тестах: один httpx.MockTransport на все внутренние сервисы и llm_gateway.
app.state.transport = None

CID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class HistoryItem(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=4000)


class Context(BaseModel):
    company_name: str = Field(default="", max_length=200)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)
    lang: str = Field(default="ru", pattern="^(ru|en)$")
    context: Context = Field(default_factory=Context)
    # права консультанта для этого пользователя; None — старый вызов: только чтение, сценарии и предложения
    permissions: list[str] | None = Field(default=None, max_length=20)


@app.exception_handler(RequestValidationError)
async def _invalid(_req: Request, _e: RequestValidationError):
    return JSONResponse({"error": {"code": "invalid_request", "message": "Некорректный запрос"}}, status_code=422)


@app.get("/live")
def live():
    return {"status": "ok"}


def deadline() -> float:
    return float(os.environ.get("ADVISOR_TIMEOUT_SECONDS", "75"))


def usage_zero() -> dict[str, Any]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "steps": 0}


@app.get("/internal/v1/engine")
async def engine():
    async with httpx.AsyncClient(timeout=5, transport=app.state.transport) as http:
        st = await hermes.status(http)
    return {"engine": "hermes" if st["configured"] else "llm_gateway", "hermes": st,
            "permissions": [{"code": k, "needs_edit": v["needs_edit"]} for k, v in permissions.PERMISSIONS.items()],
            "default_permissions": permissions.DEFAULT}


@app.get("/internal/v1/companies/{cid}/prompts")
async def company_prompts(cid: str, lang: str = "ru", permissions_: str = Query(default="", alias="permissions"), mode: str = "chat",
                          x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    if not CID.match(cid):
        return JSONResponse({"error": {"code": "not_found", "message": "Не найдено"}}, status_code=404)
    lang = lang if lang in ("ru", "en") else "ru"
    perms = permissions.clean([p for p in permissions_.split(",") if p])
    async with httpx.AsyncClient(timeout=20, transport=app.state.transport) as http:
        box = tl.Toolbox(tl.Services(http, cid, x_request_id, x_actor), lang, permissions.allowed_tools(perms))
        return {"items": await prompts.build(box, perms, lang, "plan" if mode == "plan" else "chat")}


@app.post("/internal/v1/companies/{cid}/ask")
async def ask(cid: str, req: AskRequest, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    if not CID.match(cid):
        return JSONResponse({"error": {"code": "not_found", "message": "Не найдено"}}, status_code=404)
    res, _plan = await answer(cid, x_actor, x_request_id, req.question, [h.model_dump() for h in req.history], req.lang,
                              req.context.company_name, req.permissions)
    return res


def deadline_for(reasoning: str | None) -> float:
    """Срок ответа Hermes: чем глубже размышление, тем дольше (xhigh с несколькими инструментами идёт минутами).
    api ждёт advisor дольше самого длинного срока (Upstream advisor, 320 с)."""
    return {"high": 200.0, "xhigh": 300.0}.get(reasoning or "", max(deadline(), 120.0))


async def answer(cid: str, actor: str, rid: str, question: str, history: list[dict], lang: str, company: str,
                 perms_in: list[str] | None, model: str | None = None, reasoning: str | None = None,
                 mode: str = "chat") -> tuple[dict, dict]:
    """Ответ консультанта: Hermes -> агент через llm_gateway -> шаблон из прогноза. Возвращает (ответ, стратегия из режима «План»)."""
    user = actor or f"company:{cid}"
    perms = permissions.clean(perms_in) if perms_in is not None else None
    allowed = permissions.allowed_tools(perms) if perms is not None else None
    async with httpx.AsyncClient(timeout=30, transport=app.state.transport) as http:
        notice: str | None = None
        if hermes.configured() and perms is not None:
            t = tickets.issue(cid, actor, rid, lang, perms)
            try:
                async with asyncio.timeout(deadline_for(reasoning)):
                    out = await hermes.run(http, t, question, history, company, model, reasoning, mode)
                return reply(out, t.actions, t.followups), t.plan
            except agent.LlmError as e:
                notice = e.code
            except TimeoutError:
                notice = "provider_unavailable"
            finally:
                tickets.close(t.id)
            if t.actions or t.plan:  # Hermes уже что-то записал: не отвечаем повторно другим движком, а сообщаем о сделанном
                text = ("Ответ не успел сформироваться, но действия ниже уже выполнены." if lang == "ru"
                        else "The answer did not finish, but the actions below are done.")
                return reply(agent.Outcome(text, t.proposals, "hermes", model, usage_zero(), t.used), t.actions, [], notice), t.plan
        box = tl.Toolbox(tl.Services(http, cid, rid, actor), lang, allowed)
        try:
            async with asyncio.timeout(deadline()):
                out = await agent.run(http, box, user, rid, question, history, company, lang)
            return reply(out, box.actions, box.followups, notice), {}
        except agent.LlmError as e:
            notice = notice or e.code
        except TimeoutError:
            notice = notice or "provider_unavailable"
        # шаблонный ответ из прогноза: без модели, предложений нет
        try:
            run = await box.svc.latest_forecast()
            text = template.answer_from_forecast(run, lang)
        except tl.ToolError as e:
            text = template.no_data_answer(e.code, lang)
        return {"answer": text, "suggestions": [], "actions": [], "followups": [], "provider": "none", "model": None,
                "notice": notice, "usage": usage_zero(), "tools_used": ["get_forecast_risk"]}, {}


def reply(out: agent.Outcome, actions: list[dict], followups: list[str], notice: str | None = None) -> dict:
    return {"answer": out.answer, "suggestions": out.suggestions, "actions": actions, "followups": followups,
            "provider": out.provider, "model": out.model, "notice": notice, "usage": out.usage, "tools_used": out.tools_used}
