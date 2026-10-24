"""categorizer: assigns categories to ingested operations, keeps tenant
catalogs and corrections, flags rescue inflows and suggests the tax regime."""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from categorizer import model, rules, tax
from ci_common import db, events
from ci_common.auth import User, Verifier
from ci_common.config import Config
from ci_common.tls import MtlsClient
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("categorizer")
RULES_PATH = Path(os.environ.get("RULES_DIR", "/app/rules")) / "categories.yaml"


class State:
    config: Config
    taxonomy: rules.Taxonomy
    pool = None
    js = None
    verifier: Verifier
    models: dict[uuid.UUID, model.TfidfLogReg]


S = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await startup(Config.from_env())
    yield


app = FastAPI(title="categorizer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def current_user(request: Request) -> User:
    return await S.verifier.user(request)


@app.get("/healthz")
async def healthz() -> str:
    return "ok"


async def ensure_catalog(conn, tenant: uuid.UUID) -> dict[str, uuid.UUID]:
    """Seeds one tenant category per system subcategory on first use."""
    rows = await (await conn.execute("select subcategory, category_id from category_map")).fetchall()
    if rows:
        return {r[0]: r[1] for r in rows}
    mapping = {}
    for sub, meta in S.taxonomy.subcategories.items():
        cid = uuid.uuid4()
        await conn.execute(
            "insert into category (id, tenant_id, name, grp, system_subcategory) values (%s, %s, %s, %s, %s)",
            (cid, tenant, meta["name"], meta["group"], sub),
        )
        await conn.execute(
            "insert into category_map (tenant_id, subcategory, category_id) values (%s, %s, %s)", (tenant, sub, cid)
        )
        mapping[sub] = cid
    return mapping


async def tenant_model(conn, tenant: uuid.UUID) -> model.TfidfLogReg:
    if tenant not in S.models:
        rows = await (
            await conn.execute(
                "select purpose, counterparty_name, amount_kop, subcategory from assignment "
                "where method in ('rule', 'manual') order by op_date desc limit 20000"
            )
        ).fetchall()
        m = model.TfidfLogReg()
        await asyncio.to_thread(
            m.fit, [model.features(p or "", n or "", a) for p, n, a, _ in rows], [r[3] for r in rows]
        )
        S.models[tenant] = m
    return S.models[tenant]


async def categorize_items(conn, tenant: uuid.UUID, items: list[dict]) -> list[dict]:
    mapping = await ensure_catalog(conn, tenant)
    overrides = {
        r[0]: r[1]
        for r in await (await conn.execute("select counterparty_ref, category_id from tenant_rule")).fetchall()
    }
    groups = {
        r[0]: (r[1], r[2])
        for r in await (await conn.execute("select id, grp, system_subcategory from category")).fetchall()
    }
    pending = []
    out = []
    for it in items:
        sub = rules.match(S.taxonomy, it.get("purpose", ""), it.get("counterparty_name", ""), it["amount_kop"])
        method, confidence = ("rule", 0.95) if sub else ("fallback", 0.3)
        if not sub:
            pending.append(it)
            sub = S.taxonomy.fallback(it["amount_kop"])
        it["_sub"], it["_method"], it["_conf"] = sub, method, confidence
    if pending:
        mdl = await tenant_model(conn, tenant)
        preds = mdl.predict(
            [model.features(i.get("purpose", ""), i.get("counterparty_name", ""), i["amount_kop"]) for i in pending]
        )
        for it, (label, conf) in zip(pending, preds, strict=True):
            if label and conf >= model.MIN_CONFIDENCE:
                it["_sub"], it["_method"], it["_conf"] = label, "ml", conf
    for it in items:
        sub, method, conf = it["_sub"], it["_method"], it["_conf"]
        category_id = mapping.get(sub) or mapping[S.taxonomy.fallback(it["amount_kop"])]
        if it.get("counterparty_ref") in overrides:
            category_id, method, conf = overrides[it["counterparty_ref"]], "tenant_rule", 1.0
        grp = groups.get(category_id, (S.taxonomy.group(sub), sub))[0]
        doc_no, doc_date = rules.document(it.get("purpose", ""))
        rescue = rules.is_rescue(sub, it["amount_kop"], it["balance_eod_kop"])
        await conn.execute(
            """insert into assignment (transaction_id, tenant_id, op_date, amount_kop, balance_eod_kop, counterparty_ref,
                 counterparty_name, purpose, subcategory, category_id, method, confidence, is_rescue, doc_no, doc_date)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (transaction_id) do nothing""",
            (
                it["id"],
                tenant,
                it["op_date"],
                it["amount_kop"],
                it["balance_eod_kop"],
                it.get("counterparty_ref"),
                it.get("counterparty_name"),
                it.get("purpose"),
                sub,
                category_id,
                method,
                conf,
                rescue,
                doc_no,
                doc_date,
            ),
        )
        out.append(
            event_item(
                it["id"],
                it["op_date"],
                it["amount_kop"],
                it["balance_eod_kop"],
                it.get("counterparty_ref"),
                sub,
                grp,
                category_id,
                method,
                conf,
                rescue,
                doc_no,
                doc_date,
            )
        )
    return out


def event_item(tx_id, op_date, amount, balance, cp_ref, sub, grp, category_id, method, conf, rescue, doc_no, doc_date):
    # No purpose text and no names: downstream services do not need them.
    return {
        "id": str(tx_id),
        "op_date": str(op_date),
        "amount_kop": amount,
        "balance_eod_kop": balance,
        "counterparty_ref": cp_ref,
        "subcategory": sub,
        "group": grp,
        "category_id": str(category_id),
        "method": method,
        "confidence": round(float(conf), 3),
        "is_rescue": rescue,
        "doc_no": doc_no,
        "doc_date": str(doc_date) if doc_date else None,
    }


async def publish_categorized(tenant: uuid.UUID, items: list[dict]) -> None:
    for i in range(0, len(items), 500):
        await events.publish(S.js, events.envelope("transactions.categorized", tenant, {"items": items[i : i + 500]}))


async def on_ingested(env: dict) -> None:
    tenant = uuid.UUID(env["tenant_id"])
    async with db.tenant_tx(S.pool, tenant) as conn:
        cur = await conn.execute("insert into processed_event (id) values (%s) on conflict do nothing", (env["id"],))
        if cur.rowcount == 0:
            return
        items = env["data"]["items"]
        for it in items:
            it["op_date"] = date.fromisoformat(it["op_date"])
        out = await categorize_items(conn, tenant, items)
    await publish_categorized(tenant, out)
    await maybe_suggest_tax(tenant)


async def maybe_suggest_tax(tenant: uuid.UUID) -> None:
    async with db.tenant_tx(S.pool, tenant) as conn:
        rows = await (await conn.execute("select op_date, amount_kop, subcategory, purpose from assignment")).fetchall()
    guess = tax.infer([{"op_date": r[0], "amount_kop": r[1], "subcategory": r[2], "purpose": r[3]} for r in rows])
    if guess:
        await events.publish(S.js, events.envelope("categorizer.tax_regime_suggested", tenant, guess))


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    group: str


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    archived: bool | None = None


class Correction(BaseModel):
    category_id: uuid.UUID
    apply_to_similar: bool = True


@app.get("/api/v1/categories")
async def list_categories(user: User = Depends(current_user)) -> dict:
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        await ensure_catalog(conn, user.tenant_id)
        rows = await (
            await conn.execute("select id, name, grp, system_subcategory, archived from category order by grp, name")
        ).fetchall()
    return {
        "groups": S.taxonomy.groups,
        "items": [
            {"id": str(r[0]), "name": r[1], "group": r[2], "system_subcategory": r[3], "archived": r[4]} for r in rows
        ],
    }


@app.post("/api/v1/categories")
async def create_category(body: CategoryIn, user: User = Depends(current_user)) -> dict:
    user.require_write()
    if body.group not in S.taxonomy.groups:
        raise HTTPException(400, "unknown group")
    cid = uuid.uuid4()
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        await ensure_catalog(conn, user.tenant_id)
        await conn.execute(
            "insert into category (id, tenant_id, name, grp) values (%s, %s, %s, %s)",
            (cid, user.tenant_id, body.name, body.group),
        )
    return {"id": str(cid), "name": body.name, "group": body.group}


@app.patch("/api/v1/categories/{category_id}")
async def patch_category(category_id: uuid.UUID, body: CategoryPatch, user: User = Depends(current_user)) -> dict:
    user.require_write()
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        cur = await conn.execute(
            "update category set name = coalesce(%s, name), archived = coalesce(%s, archived) where id = %s",
            (body.name, body.archived, category_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "not found")
    return {"ok": True}


@app.patch("/api/v1/transactions/category/{transaction_id}")
async def correct(transaction_id: uuid.UUID, body: Correction, user: User = Depends(current_user)) -> dict:
    """Manual correction; with apply_to_similar every operation of the counterparty follows it."""
    user.require_write()
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        cat = await (
            await conn.execute("select grp, system_subcategory from category where id = %s", (body.category_id,))
        ).fetchone()
        tx = await (
            await conn.execute("select counterparty_ref from assignment where transaction_id = %s", (transaction_id,))
        ).fetchone()
        if not cat or not tx:
            raise HTTPException(404, "not found")
        grp, system_sub = cat
        targets = [transaction_id]
        if body.apply_to_similar and tx[0]:
            await conn.execute(
                "insert into tenant_rule (tenant_id, counterparty_ref, category_id) values (%s, %s, %s) "
                "on conflict (tenant_id, counterparty_ref) do update set category_id = excluded.category_id",
                (user.tenant_id, tx[0], body.category_id),
            )
            targets = [
                r[0]
                for r in await (
                    await conn.execute("select transaction_id from assignment where counterparty_ref = %s", (tx[0],))
                ).fetchall()
            ]
        sub_update = system_sub or None
        await conn.execute(
            "update assignment set category_id = %s, method = 'manual', confidence = 1, "
            "subcategory = coalesce(%s, subcategory) where transaction_id = any(%s)",
            (body.category_id, sub_update, targets),
        )
        rows = await (
            await conn.execute(
                "select transaction_id, op_date, amount_kop, balance_eod_kop, counterparty_ref, subcategory, category_id, "
                "method, confidence, is_rescue, doc_no, doc_date from assignment where transaction_id = any(%s)",
                (targets,),
            )
        ).fetchall()
    S.models.pop(user.tenant_id, None)
    items = [event_item(r[0], r[1], r[2], r[3], r[4], r[5], grp, r[6], r[7], r[8], r[9], r[10], r[11]) for r in rows]
    await publish_categorized(user.tenant_id, items)
    return {"updated": len(items)}


@app.get("/api/v1/categories/summary")
async def summary(days: int = 90, user: User = Depends(current_user)) -> dict:
    days = max(7, min(days, 365))
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        last = await (await conn.execute("select max(op_date) from assignment")).fetchone()
        if not last or not last[0]:
            return {"days": days, "groups": []}
        since = last[0] - timedelta(days=days)
        rows = await (
            await conn.execute(
                "select c.grp, sum(greatest(a.amount_kop, 0))::bigint, sum(least(a.amount_kop, 0))::bigint, count(*) "
                "from assignment a join category c on c.id = a.category_id where a.op_date > %s group by c.grp order by 3",
                (since,),
            )
        ).fetchall()
    return {
        "days": days,
        "until": str(last[0]),
        "groups": [
            {
                "group": r[0],
                "name": S.taxonomy.groups.get(r[0], r[0]),
                "inflow_kop": r[1],
                "outflow_kop": r[2],
                "count": r[3],
            }
            for r in rows
        ],
    }


async def startup(config: Config) -> None:
    S.config = config
    S.taxonomy = rules.load(RULES_PATH)
    S.models = {}
    client = MtlsClient(config.cert_dir)
    S.verifier = Verifier(config, client)
    S.pool = await db.connect(config.database_url, config.cert_dir)
    await db.migrate(S.pool, Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "migrations")))
    _, S.js = await events.connect(config.nats_url, config.cert_dir)
    await events.ensure_stream(S.js, "CATEGORIZER", ["ci.v1.categorizer.>"])
    asyncio.create_task(
        events.consume(S.js, "TRANSACTIONS", "categorizer-ingested", "ci.v1.transactions.ingested", on_ingested)
    )
