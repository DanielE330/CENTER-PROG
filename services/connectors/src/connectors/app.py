"""connectors: external data sources behind one contract. In the MVP:
replay of the organizers' dataset (statement + issued invoices) and the
counterparty registry (synthetic for fictitious INNs, real one pluggable)."""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

from ci_common import events
from ci_common.auth import ServiceTokens, User, Verifier
from ci_common.config import Config, secret
from ci_common.tls import MtlsClient
from connectors import dataset
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("connectors")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CHUNK = 400


class State:
    config: Config
    client: MtlsClient
    tokens: ServiceTokens
    verifier: Verifier
    js = None
    key: bytes
    locks: dict[uuid.UUID, asyncio.Lock]


S = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await startup(Config.from_env())
    yield


app = FastAPI(title="connectors", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def current_user(request: Request) -> User:
    return await S.verifier.user(request)


@app.get("/healthz")
async def healthz() -> str:
    return "ok"


async def internal(method: str, service: str, path: str, body: dict | list | None = None) -> dict:
    token = await S.tokens.token()
    resp = await S.client.get().request(
        method, f"https://{service}:8443{path}", json=body, headers={"authorization": f"Bearer {token}"}
    )
    if resp.status_code >= 400:
        raise HTTPException(502, f"{service} returned {resp.status_code}")
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return {"text": resp.text}
    return resp.json()


async def push_period(tenant: uuid.UUID, company: str, start: date | None, end: date) -> dict:
    """Statement rows for (start, end], invoices open at `end`, registry, then the new "today"."""
    ops = dataset.load_company(DATA_DIR, company)
    rows = [o.row() for o in dataset.between(ops, start, end)]
    inserted = 0
    for i in range(0, len(rows), CHUNK):
        res = await internal(
            "POST",
            "ingest",
            f"/internal/v1/tenants/{tenant}/transactions",
            {"batch_id": f"{company}:{start}:{end}:{i}", "rows": rows[i : i + CHUNK]},
        )
        inserted += res.get("inserted", 0)
    invoices = dataset.open_invoices(ops, end, str(tenant), S.key)
    if invoices:
        await internal("POST", "obligations", f"/internal/v1/tenants/{tenant}/obligations", invoices)
    statuses = dataset.registry_statuses(ops, end, str(tenant), S.key)
    if statuses:
        await events.publish(S.js, events.envelope("connectors.registry_status", tenant, {"items": statuses}))
    await internal("PUT", "tenancy", f"/internal/v1/tenants/{tenant}/as-of", {"as_of": str(end)})
    return {
        "rows": len(rows),
        "inserted": inserted,
        "open_invoices": len(invoices),
        "as_of": str(end),
        "registry_flags": sum(1 for s in statuses if s["status"] != "active"),
    }


class Advance(BaseModel):
    days: int = Field(default=7, ge=1, le=60)


@app.get("/api/v1/demo/status")
async def demo_status(user: User = Depends(current_user)) -> dict:
    settings = await internal("GET", "tenancy", f"/internal/v1/tenants/{user.tenant_id}/settings")
    company = settings.get("demo_company")
    if not company:
        return {"replay": False}
    ops = dataset.load_company(DATA_DIR, company)
    return {"replay": True, "company": company, "as_of": settings["as_of"], "data_until": str(ops[-1].op_date)}


@app.post("/api/v1/demo/advance")
async def demo_advance(body: Advance, user: User = Depends(current_user)) -> dict:
    """Replay: "a week passed" feeds the next operations through the normal ingest path."""
    user.require_write()
    settings = await internal("GET", "tenancy", f"/internal/v1/tenants/{user.tenant_id}/settings")
    company = settings.get("demo_company")
    if not company:
        raise HTTPException(409, "replay is available only for demo tenants")
    lock = S.locks.setdefault(user.tenant_id, asyncio.Lock())
    async with lock:
        as_of = date.fromisoformat(settings["as_of"])
        end = as_of + timedelta(days=body.days)
        ops = dataset.load_company(DATA_DIR, company)
        if as_of >= ops[-1].op_date:
            raise HTTPException(409, "end of dataset reached")
        return await push_period(user.tenant_id, company, as_of, min(end, ops[-1].op_date))


@app.get("/api/v1/counterparties/{ref}/status")
async def counterparty_status(ref: str, user: User = Depends(current_user)) -> dict:
    settings = await internal("GET", "tenancy", f"/internal/v1/tenants/{user.tenant_id}/settings")
    company = settings.get("demo_company")
    if company:
        ops = dataset.load_company(DATA_DIR, company)
        for s in dataset.registry_statuses(ops, date.fromisoformat(settings["as_of"]), str(user.tenant_id), S.key):
            if s["counterparty_ref"] == ref:
                return {"counterparty_ref": ref, "status": s["status"], "source": "synthetic"}
    return {"counterparty_ref": ref, "status": "unknown", "source": "none"}


async def startup(config: Config) -> None:
    S.config = config
    S.client = MtlsClient(config.cert_dir)
    S.tokens = ServiceTokens(config, S.client)
    S.verifier = Verifier(config, S.client)
    S.key = secret("counterparty_hmac_key").encode()
    S.locks = {}
    _, S.js = await events.connect(config.nats_url, config.cert_dir)
    await events.ensure_stream(S.js, "CONNECTORS", ["ci.v1.connectors.>"])
