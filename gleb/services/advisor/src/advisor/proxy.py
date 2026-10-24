"""Loopback proxy for the agent runtime. The agent framework speaks plain HTTP
to 127.0.0.1; the proxy adds the client certificate, the user's token (MCP) and
the tenant header (LLM) and forwards over mTLS. Nothing else can reach it:
it listens on loopback inside the advisor container only."""

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass

import uvicorn
from ci_common.tls import MtlsClient
from fastapi import FastAPI, Request, Response

log = logging.getLogger("advisor.proxy")
PORT = 9100


@dataclass
class Context:
    tenant_id: uuid.UUID
    user_token: str


# Hermes runs tool calls in its own threads, so a contextvar would not reach the
# proxy. The runtime serializes conversations; this holds the active one.
_active: Context | None = None
_lock = threading.Lock()


def activate(ctx: Context | None) -> None:
    global _active
    with _lock:
        _active = ctx


def _ctx() -> Context | None:
    with _lock:
        return _active


def build(client: MtlsClient, llm_upstream: str, mcp_upstream: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/llm/v1/chat/completions")
    async def llm(request: Request) -> Response:
        ctx = _ctx()
        if ctx is None:
            return Response(status_code=409)
        resp = await client.get().post(
            f"{llm_upstream}/v1/chat/completions",
            content=await request.body(),
            headers={"content-type": "application/json", "x-tenant-id": str(ctx.tenant_id)},
            timeout=180,
        )
        return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))

    @app.get("/llm/v1/route")
    async def route() -> Response:
        ctx = _ctx()
        if ctx is None:
            return Response(status_code=409)
        resp = await client.get().get(f"{llm_upstream}/v1/route", headers={"x-tenant-id": str(ctx.tenant_id)})
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

    @app.get("/llm/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": "ci1", "object": "model"}]}

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        # Without an active conversation (tool discovery at startup) the request goes out without
        # a user token: advisor-tools then answers initialize and tools/list only.
        ctx = _ctx()
        headers = {"content-type": "application/json"}
        if ctx is not None:
            headers["authorization"] = f"Bearer {ctx.user_token}"
        if sid := request.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = sid
        resp = await client.get().post(f"{mcp_upstream}/mcp", content=await request.body(), headers=headers, timeout=60)
        out = Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))
        if sid := resp.headers.get("mcp-session-id"):
            out.headers["mcp-session-id"] = sid
        return out

    @app.get("/mcp")
    async def mcp_get() -> Response:
        return Response(status_code=405)

    return app


def start(client: MtlsClient, llm_upstream: str, mcp_upstream: str) -> None:
    config = uvicorn.Config(
        build(client, llm_upstream, mcp_upstream), host="127.0.0.1", port=PORT, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)

    def run() -> None:
        asyncio.run(server.serve())

    threading.Thread(target=run, name="agent-proxy", daemon=True).start()
    log.info("agent proxy listening on loopback")
