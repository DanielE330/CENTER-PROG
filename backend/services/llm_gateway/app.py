"""Шлюз к облачным LLM (порт 8010). Единственный сервис с выходом к провайдерам.
uvicorn llm_gateway.app:app --host 127.0.0.1 --port 8010

POST /internal/v1/chat {user, messages, tools?, protect?, max_tokens?} -> {message, usage, provider, model, budget}
Ошибки: {"error":{"code","message"}}: provider_not_configured (503), budget_exceeded (429), provider_unavailable (503/502)."""
import json
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import providers, store
from .mask import Masker
from .providers import GatewayError

app = FastAPI(title="llm-gateway", docs_url=None, redoc_url=None)
# Подмена транспорта в тестах (httpx.MockTransport); в проде None.
app.state.transport = None

MAX_MESSAGES = 60
MAX_TEXT = 200_000


class ChatRequest(BaseModel):
    user: str = Field(min_length=1, max_length=128)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_MESSAGES)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    protect: list[str] = Field(default_factory=list, max_length=200)
    max_tokens: int = Field(default=4096, ge=16, le=16384)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@app.exception_handler(GatewayError)
async def _gateway_error(_req: Request, e: GatewayError):
    return _err(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
async def _invalid(_req: Request, _e: RequestValidationError):
    return _err(422, "invalid_request", "Некорректный запрос к шлюзу")


@app.get("/live")
def live():
    return {"status": "ok"}


def daily_budget() -> int:
    """0 — без ограничения."""
    try:
        return max(0, int(os.environ.get("LLM_DAILY_TOKEN_BUDGET", "200000")))
    except ValueError:
        return 200000


@app.get("/internal/v1/status")
def get_status(user: str | None = None):
    out = {**providers.status(), "daily_token_budget": daily_budget()}
    if user:
        out["used_today"] = store.used_today(user)
    return out


@app.get("/internal/v1/audit")
def get_audit(limit: int = 50):
    return {"items": store.audit_rows(max(1, min(limit, 500)))}


def _estimate(messages: list[dict], tools: list[dict]) -> int:
    n = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) + len(json.dumps(tools, ensure_ascii=False))
    return n // 3 + 1


def _mask_messages(masker: Masker, messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), str):
            m["content"] = masker.mask(m["content"])
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = dict(c.get("function") or {})
                try:
                    fn["arguments"] = json.dumps(masker.mask_json(json.loads(fn.get("arguments") or "{}")), ensure_ascii=False)
                except json.JSONDecodeError:
                    fn["arguments"] = "{}"
                calls.append({**c, "function": fn})
            m["tool_calls"] = calls
        out.append(m)
    return out


@app.post("/internal/v1/chat")
async def chat(req: ChatRequest):
    user = req.user
    try:
        cfg = providers.current_config()
    except GatewayError as e:
        store.record_audit(user, os.environ.get("LLM_PROVIDER", "") or "none", "", 0, e.code)
        raise
    if sum(len(json.dumps(m, ensure_ascii=False)) for m in req.messages) > MAX_TEXT:
        raise GatewayError(422, "invalid_request", "Слишком большой запрос")

    limit, used = daily_budget(), store.used_today(user)
    if limit and used + _estimate(req.messages, req.tools) > limit:
        store.record_audit(user, cfg.provider, cfg.model, 0, "budget_exceeded")
        raise GatewayError(429, "budget_exceeded", "Суточный лимит токенов исчерпан")

    masker = Masker(req.protect)
    messages = _mask_messages(masker, req.messages)
    tools = req.tools  # определения инструментов статичны и не содержат данных пользователя

    async with httpx.AsyncClient(timeout=providers.timeout(), transport=app.state.transport) as client:
        try:
            done = await providers.complete(client, cfg, messages, tools, req.max_tokens)
        except GatewayError as e:
            store.record_audit(user, cfg.provider, cfg.model, 0, e.code)
            raise

    spent = done.prompt_tokens + done.completion_tokens
    if spent == 0:  # провайдер не прислал usage
        spent = _estimate(messages, tools)
    store.add_usage(user, spent)
    store.record_audit(user, cfg.provider, cfg.model, spent, "ok")

    msg = dict(done.message)
    msg["content"] = masker.unmask(msg.get("content") or "")
    if msg.get("tool_calls"):
        calls = []
        for c in msg["tool_calls"]:
            fn = dict(c["function"])
            try:
                fn["arguments"] = json.dumps(masker.unmask_json(json.loads(fn.get("arguments") or "{}")), ensure_ascii=False)
            except json.JSONDecodeError:
                fn["arguments"] = "{}"
            calls.append({**c, "function": fn})
        msg["tool_calls"] = calls
    return {
        "message": msg,
        "usage": {"prompt_tokens": done.prompt_tokens, "completion_tokens": done.completion_tokens, "total_tokens": spent},
        "provider": cfg.provider,
        "model": cfg.model,
        "masked_items": masker.replaced,
        "budget": {"limit": limit, "used": used + spent},
    }
