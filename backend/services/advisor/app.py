"""Консультант (порт 8011): вопрос + контекст компании -> ответ, цифры из инструментов и предложения действий.
uvicorn advisor.app:app --host 127.0.0.1 --port 8011

POST /internal/v1/companies/{cid}/ask {question, history?, lang?, context?:{company_name}}  (пользователь — заголовок x-actor)
  -> {answer, suggestions:[{id,type,params,title}], provider, model, notice, usage, tools_used}
provider "none" — ответ по шаблону из прогноза, notice объясняет почему: provider_not_configured | provider_unavailable | budget_exceeded.
Консультант ничего не записывает: предложения применяет пользователь через api."""
import asyncio
import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import agent, template, tools as tl

app = FastAPI(title="advisor", docs_url=None, redoc_url=None)
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


@app.post("/internal/v1/companies/{cid}/ask")
async def ask(cid: str, req: AskRequest, x_actor: str = Header(default=""), x_request_id: str = Header(default="")):
    if not CID.match(cid):
        return JSONResponse({"error": {"code": "not_found", "message": "Не найдено"}}, status_code=404)
    user = x_actor or f"company:{cid}"
    async with httpx.AsyncClient(timeout=30, transport=app.state.transport) as http:
        box = tl.Toolbox(tl.Services(http, cid, x_request_id, x_actor), req.lang)
        notice: str | None = None
        try:
            async with asyncio.timeout(deadline()):
                out = await agent.run(http, box, user, x_request_id, req.question, [h.model_dump() for h in req.history],
                                      req.context.company_name, req.lang)
            return {"answer": out.answer, "suggestions": out.suggestions, "provider": out.provider, "model": out.model,
                    "notice": None, "usage": out.usage, "tools_used": out.tools_used}
        except agent.LlmError as e:
            notice = e.code
        except TimeoutError:
            notice = "provider_unavailable"
        # шаблонный ответ из прогноза: без модели, предложений нет
        try:
            run = await box.svc.latest_forecast()
            answer = template.answer_from_forecast(run, req.lang)
        except tl.ToolError as e:
            answer = template.no_data_answer(e.code, req.lang)
        return {"answer": answer, "suggestions": [], "provider": "none", "model": None, "notice": notice,
                "usage": usage_zero(), "tools_used": ["get_forecast_risk"]}
