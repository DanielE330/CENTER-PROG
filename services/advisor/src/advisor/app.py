"""advisor: the consulting agent. Explains the forecast and proposes actions;
it cannot change data — proposals are applied by the user."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from advisor import proxy, runtime
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
            primary = S.runtime
            if primary is not S.fallback and not await uses_cloud_model(llm):
                # Hermes needs a cloud model (64K+ context); the local model runs in the built-in loop.
                primary = S.fallback
            try:
                turn = await primary.run(body.message, history, llm, mcp)
                if not turn.answer.strip():
                    raise RuntimeError("empty answer")
            except Exception as exc:  # noqa: BLE001 - the user still gets an answer from the built-in loop
                if primary is S.fallback:
                    raise HTTPException(502, "advisor unavailable") from exc
                log.warning("primary runtime failed, falling back: %s", type(exc).__name__)
                turn = await S.fallback.run(body.message, history, llm, mcp)
        finally:
            proxy.activate(None)
    return {"answer": turn.answer, "tools_used": turn.tools_used, "proposals": turn.proposals, "runtime": turn.runtime}
