"""MCP-сервер инструментов консультанта для Hermes (streamable HTTP, JSON-RPC 2.0, ответы application/json).
POST /mcp — initialize, tools/list, tools/call, ping; уведомления -> 202. GET (поток событий) не поддерживаем -> 405.

Доступ: заголовок `Authorization: Bearer <ADVISOR_MCP_TOKEN>` (его знает только сервис hermes). У каждого инструмента
обязательный параметр `ticket`: пропуск вопроса (tickets.py) определяет компанию, пользователя и права. Инструмент
без права или с чужим/просроченным пропуском не выполняется."""
import hmac
import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import permissions, tickets
from . import tools as tl

router = APIRouter()
log = logging.getLogger("advisor.mcp")
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "ci-advisor-tools", "version": "1.0"}
TICKET_PROP = {"ticket": {"type": "string", "description": "Пропуск из системного сообщения; передавай без изменений в каждом вызове"}}


def token() -> str:
    return os.environ.get("ADVISOR_MCP_TOKEN", "")


def tool_list() -> list[dict]:
    out = []
    for s in tl.TOOL_SPECS:
        f = s["function"]
        params = f.get("parameters") or {"type": "object", "properties": {}}
        schema = {**params, "properties": {**TICKET_PROP, **(params.get("properties") or {})},
                  "required": ["ticket", *(params.get("required") or [])]}
        out.append({"name": f["name"], "description": f["description"], "inputSchema": schema})
    return out


def ok(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def fail(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def text(data: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": tl.dumps(data)}], "isError": error}


async def call_tool(request: Request, name: str, args: dict) -> dict:
    t = tickets.get(str(args.pop("ticket", "") or ""))
    if t is None:
        return text({"error": "invalid_ticket", "message": "Пропуск недействителен или истёк: ответь пользователю без этого инструмента"}, True)
    if name not in permissions.allowed_tools(t.perms):
        return text({"error": "permission_denied", "message": "Это действие консультанту не разрешено в настройках компании"}, True)
    async with httpx.AsyncClient(timeout=30, transport=request.app.state.transport) as http:
        box = tl.Toolbox(tl.Services(http, t.cid, t.rid, t.actor), t.lang, permissions.allowed_tools(t.perms), state=t)
        try:
            res = await box.call(name, args)
        except Exception:  # noqa: BLE001 — сбой инструмента не должен рвать MCP-сессию Hermes (иначе он ждёт таймаут)
            log.exception("MCP tool %s failed", name)
            res = {"error": "tool_failed", "message": "Инструмент не выполнился; если это была запись, проверь результат перед повтором"}
    return text(res, isinstance(res, dict) and "error" in res)


async def handle(request: Request, msg: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return fail(None, -32600, "invalid request")
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # уведомление (initialized, cancelled): ответа нет
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                        "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                        "instructions": "Инструменты финансового консультанта одной компании. В каждом вызове передавай ticket из системного сообщения."})
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return fail(mid, -32602, "invalid params")
        return ok(mid, await call_tool(request, name, dict(args)))
    if method in ("resources/list", "prompts/list"):
        return ok(mid, {method.split("/")[0]: []})
    return fail(mid, -32601, f"method not found: {method}")


@router.post("/mcp")
async def mcp_post(request: Request):
    want = token()
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not want or not hmac.compare_digest(got.encode(), want.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse(fail(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):
        out = [r for r in [await handle(request, m) for m in body] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await handle(request, body)
    return JSONResponse(res) if res is not None else Response(status_code=202)


@router.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete():
    return Response(status_code=200)
