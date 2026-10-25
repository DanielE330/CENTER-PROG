"""Чаты консультанта (внутренний API; права и принадлежность компании проверяет api, владелец чата — x-actor).

GET    /internal/v1/companies/{cid}/advisor/chats                     -> {items:[{id,title,mode,model,reasoning,messages,...}]}
POST   /internal/v1/companies/{cid}/advisor/chats {title?,mode?,model?,reasoning?} -> 201 чат
GET    /internal/v1/companies/{cid}/advisor/chats/{id}                -> {chat, messages:[{id,role,content,meta,created_at}]}
PATCH  /internal/v1/companies/{cid}/advisor/chats/{id} {title?,model?} -> чат (уровень размышления задаётся при создании)
DELETE /internal/v1/companies/{cid}/advisor/chats/{id}                -> 204
POST   /internal/v1/companies/{cid}/advisor/chats/{id}/messages {question, lang?, permissions[], context?}
       -> {user_message, message, chat}: ответ с контекстом чата; meta ответа — предложения, действия, подсказки, модель
GET    /internal/v1/companies/{cid}/advisor/models                    -> {models[], efforts[], default_model, default_effort}
GET    /internal/v1/companies/{cid}/advisor/plans/active              -> {plan|null}
DELETE /internal/v1/companies/{cid}/advisor/plans/{id}                -> 204"""
import re

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import hermes, store

router = APIRouter()
CID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_EFFORT = "medium"
co = "/internal/v1/companies/{cid}/advisor"


def err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status)


def nf() -> JSONResponse:
    return err(404, "not_found", "Чат не найден")


class NewChat(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    mode: str = Field(default="chat", pattern="^(chat|plan)$")
    model: str | None = Field(default=None, max_length=64)
    reasoning: str = Field(default=DEFAULT_EFFORT, pattern="^(low|medium|high|xhigh)$")


class PatchChat(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=64)


class Context(BaseModel):
    company_name: str = Field(default="", max_length=200)


class Send(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    lang: str = Field(default="ru", pattern="^(ru|en)$")
    context: Context = Field(default_factory=Context)
    permissions: list[str] = Field(default_factory=list, max_length=20)


async def available(request: Request) -> dict:
    async with httpx.AsyncClient(timeout=5, transport=request.app.state.transport) as http:
        return await hermes.models(http)


async def check_model(request: Request, model: str | None) -> str | None:
    """Модель чата: из списка подписки; пусто — модель по умолчанию. Неизвестная — ошибка."""
    av = await available(request)
    if not model:
        return av["default_model"] or ""
    if model not in av["models"]:
        raise ValueError(model)
    return model


def title_of(question: str) -> str:
    t = " ".join(question.split())
    return t if len(t) <= 60 else t[:57].rstrip() + "…"


@router.get(co + "/models")
async def models(cid: str, request: Request):
    av = await available(request)
    return {**av, "default_effort": DEFAULT_EFFORT}


@router.get(co + "/chats")
async def list_chats(cid: str, x_actor: str = Header(default="")):
    if not CID.match(cid) or not x_actor:
        return nf()
    return {"items": store.get().chats(cid, x_actor)}


@router.post(co + "/chats")
async def create_chat(cid: str, body: NewChat, request: Request, x_actor: str = Header(default="")):
    if not CID.match(cid) or not x_actor:
        return nf()
    try:
        model = await check_model(request, body.model)
    except ValueError:
        return err(422, "invalid_model", "Эта модель недоступна в подписке")
    title = (body.title or "").strip() or ("Новая стратегия" if body.mode == "plan" else "Новый чат")
    return JSONResponse(store.get().create(cid, x_actor, title, body.mode, model or "", body.reasoning), status_code=201)


@router.get(co + "/chats/{chat_id}")
async def get_chat(cid: str, chat_id: str, x_actor: str = Header(default="")):
    st = store.get()
    chat = st.get(cid, x_actor, chat_id) if HEX.match(chat_id) else None
    if not chat:
        return nf()
    return {"chat": chat, "messages": st.messages(chat_id)}


@router.patch(co + "/chats/{chat_id}")
async def patch_chat(cid: str, chat_id: str, body: PatchChat, request: Request, x_actor: str = Header(default="")):
    st = store.get()
    if not HEX.match(chat_id) or not st.get(cid, x_actor, chat_id):
        return nf()
    model = None
    if body.model is not None:
        try:
            model = await check_model(request, body.model)
        except ValueError:
            return err(422, "invalid_model", "Эта модель недоступна в подписке")
    return st.update(cid, x_actor, chat_id, title=body.title.strip() if body.title else None, model=model)


@router.delete(co + "/chats/{chat_id}")
async def delete_chat(cid: str, chat_id: str, x_actor: str = Header(default="")):
    if not HEX.match(chat_id) or not store.get().delete(cid, x_actor, chat_id):
        return nf()
    return Response(status_code=204)


@router.post(co + "/chats/{chat_id}/messages")
async def send(cid: str, chat_id: str, body: Send, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    from .app import answer  # app импортирует этот модуль

    st = store.get()
    chat = st.get(cid, x_actor, chat_id) if HEX.match(chat_id) else None
    if not chat:
        return nf()
    history = [{"role": m["role"], "content": m["content"]} for m in st.messages(chat_id)]
    first = not history
    question = body.question.strip()
    user_msg = st.add(chat_id, "user", question, title=title_of(question) if first and chat["title"] in ("Новый чат", "Новая стратегия") else None)
    res, plan = await answer(cid, x_actor, x_request_id, question, history, body.lang, body.context.company_name, body.permissions,
                             chat["model"] or None, chat["reasoning"], chat["mode"])
    saved = None
    if plan:
        saved = st.save_plan(cid, x_actor, chat_id, plan)
    meta = {k: res.get(k) for k in ("suggestions", "actions", "followups", "provider", "model", "notice", "tools_used")}
    meta["reasoning"] = chat["reasoning"]
    if saved:
        meta["plan_id"] = saved["id"]
    msg = st.add(chat_id, "assistant", res.get("answer") or "", meta)
    return {"user_message": user_msg, "message": msg, "chat": st.get(cid, x_actor, chat_id), "usage": res.get("usage")}


@router.get(co + "/plans/active")
async def active_plan(cid: str):
    return {"plan": store.get().active_plan(cid) if CID.match(cid) else None}


@router.delete(co + "/plans/{plan_id}")
async def delete_plan(cid: str, plan_id: str):
    if not HEX.match(plan_id) or not store.get().delete_plan(cid, plan_id):
        return err(404, "not_found", "Стратегия не найдена")
    return Response(status_code=204)
