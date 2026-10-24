"""advisor: the consulting agent. Explains the forecast and proposes actions;
it cannot change data — proposals are applied by the user."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from advisor import proxy, runtime, template
from ci_common.auth import User, Verifier
from ci_common.config import Config
from ci_common.tls import MtlsClient
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("advisor")


class State:
    verifier: Verifier
    runtime: runtime.AgentRuntime
    fallback: runtime.AgentRuntime
    template: runtime.AgentRuntime
    lock: asyncio.Lock


S = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config = Config.from_env()
    client = MtlsClient(config.cert_dir)
    S.verifier = Verifier(config, client)
    # The proxy runs its own event loop in a thread; httpx clients cannot be shared across loops.
    proxy.start(
        MtlsClient(config.cert_dir),
        os.environ.get("LLM_GATEWAY_URL", "https://llm-gateway:8443"),
        os.environ.get("ADVISOR_TOOLS_URL", "https://advisor-tools:8443"),
    )
    S.runtime = runtime.make_runtime()
    S.fallback = runtime.BuiltinRuntime()
    S.template = template.TemplateRuntime()
    S.lock = asyncio.Lock()
    log.info("agent runtime: %s", S.runtime.name)
    yield


app = FastAPI(title="advisor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def current_user(request: Request) -> User:
    return await S.verifier.user(request)


@app.get("/healthz")
async def healthz() -> str:
    return "ok"


class HistoryItem(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)


async def uses_cloud_model(llm_url: str) -> bool:
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            resp = await http.get(f"{llm_url}/route")
            return resp.status_code == 200 and bool(resp.json().get("external"))
        except httpx.HTTPError:
            return False


@app.get("/api/v1/advisor/runtime")
async def which_runtime(user: User = Depends(current_user)) -> dict:
    return {"runtime": S.runtime.name}


@app.post("/api/v1/advisor/chat")
async def chat(body: ChatIn, user: User = Depends(current_user)) -> dict:
    llm = f"http://127.0.0.1:{proxy.PORT}/llm/v1"
    mcp = f"http://127.0.0.1:{proxy.PORT}/mcp"
    history = [h.model_dump() for h in body.history]
    # One conversation at a time: the loopback proxy holds the active user's context.
    async with S.lock:
        proxy.activate(proxy.Context(tenant_id=user.tenant_id, user_token=user.token))
        try:
            notice = ""
            turn = None
            if not await uses_cloud_model(llm):
                # Consulting runs only on cloud models; without one the facts come as a template.
                notice = "not_connected"
            else:
                for agent in dict.fromkeys([S.runtime, S.fallback]):
                    try:
                        turn = await agent.run(body.message, history, llm, mcp)
                        if turn.answer.strip():
                            break
                        turn = None
                    except Exception as exc:  # noqa: BLE001 - the user still gets an answer
                        log.warning("%s runtime failed: %s", agent.name, type(exc).__name__)
                if turn is None:
                    notice = "unavailable"
            if turn is None:
                try:
                    turn = await S.template.run(body.message, history, llm, mcp)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(502, "advisor unavailable") from exc
        finally:
            proxy.activate(None)
    return {
        "answer": turn.answer,
        "tools_used": turn.tools_used,
        "proposals": turn.proposals,
        "runtime": turn.runtime,
        "notice": notice,
    }
