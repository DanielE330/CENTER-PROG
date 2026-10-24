"""forecast: keeps projections of categorized flows, obligations and settings,
recomputes the forecast when any of them changes and serves it."""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from ci_common import db, events
from ci_common.auth import User, Verifier
from ci_common.config import Config
from ci_common.tls import MtlsClient
from fastapi import Depends, FastAPI, HTTPException, Request
from forecast import backtest, engine
from pydantic import BaseModel, Field

log = logging.getLogger("forecast")
HORIZON = 90
PATHS = int(os.environ.get("FORECAST_PATHS", "1000"))
MODEL = os.environ.get("FORECAST_MODEL", "weekday_bootstrap")
DEBOUNCE = 1.5


class State:
    pool = None
    js = None
    verifier: Verifier
    dirty: dict[uuid.UUID, float]
    fitted: dict[uuid.UUID, tuple[str, engine.Fitted]]
    backtests: dict[uuid.UUID, tuple[str, list]]


S = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await startup(Config.from_env())
    yield


app = FastAPI(title="forecast", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def current_user(request: Request) -> User:
    return await S.verifier.user(request)


@app.get("/healthz")
async def healthz() -> str:
    return "ok"


# ---------------------------------------------------------------- projections


async def first_delivery(conn, event_id: str) -> bool:
    cur = await conn.execute("insert into processed_event (id) values (%s) on conflict do nothing", (event_id,))
    return cur.rowcount == 1


def mark_dirty(tenant: uuid.UUID) -> None:
    S.dirty[tenant] = time.monotonic()


async def on_categorized(env: dict) -> None:
    tenant = uuid.UUID(env["tenant_id"])
    async with db.tenant_tx(S.pool, tenant) as conn:
        if not await first_delivery(conn, env["id"]):
            return
        async with conn.cursor() as cur:
            await cur.executemany(
                """insert into flow (transaction_id, tenant_id, op_date, amount_kop, balance_eod_kop, counterparty_ref,
                     subcategory, grp, is_rescue, doc_no, doc_date)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (transaction_id) do update set subcategory = excluded.subcategory, grp = excluded.grp""",
                [
                    (
                        i["id"],
                        tenant,
                        i["op_date"],
                        i["amount_kop"],
                        i["balance_eod_kop"],
                        i.get("counterparty_ref"),
                        i["subcategory"],
                        i["group"],
                        i["is_rescue"],
                        i.get("doc_no"),
                        i.get("doc_date"),
                    )
                    for i in env["data"]["items"]
                ],
            )
    mark_dirty(tenant)


async def on_obligations(env: dict) -> None:
    tenant = uuid.UUID(env["tenant_id"])
    async with db.tenant_tx(S.pool, tenant) as conn:
        if not await first_delivery(conn, env["id"]):
            return
        await conn.execute("delete from obligation")
        for o in env["data"]["items"]:
            await conn.execute(
                "insert into obligation (tenant_id, id, payload) values (%s, %s, %s)", (tenant, o["id"], json.dumps(o))
            )
    mark_dirty(tenant)


async def on_settings(env: dict) -> None:
    tenant = uuid.UUID(env["tenant_id"])
    d = env["data"]
    async with db.tenant_tx(S.pool, tenant) as conn:
        if not await first_delivery(conn, env["id"]):
            return
        await conn.execute(
            """insert into settings (tenant_id, as_of, min_balance_kop, overdraft_limit_kop) values (%s, %s, %s, %s)
               on conflict (tenant_id) do update set as_of = excluded.as_of, min_balance_kop = excluded.min_balance_kop,
                 overdraft_limit_kop = excluded.overdraft_limit_kop""",
            (tenant, d["as_of"], d.get("min_balance_kop", 0), d.get("overdraft_limit_kop", 0)),
        )
    mark_dirty(tenant)


async def on_registry(env: dict) -> None:
    tenant = uuid.UUID(env["tenant_id"])
    async with db.tenant_tx(S.pool, tenant) as conn:
        if not await first_delivery(conn, env["id"]):
            return
        for item in env["data"]["items"]:
            await conn.execute(
                "insert into registry (tenant_id, counterparty_ref, status) values (%s, %s, %s) "
                "on conflict (tenant_id, counterparty_ref) do update set status = excluded.status",
                (tenant, item["counterparty_ref"], item["status"]),
            )
    mark_dirty(tenant)


# ---------------------------------------------------------------- computing


def to_obligation(o: dict) -> engine.Obligation:
    return engine.Obligation(
        id=o["id"],
        kind=o["kind"],
        direction=o["direction"],
        amount_kop=int(o["amount_kop"]),
        due_date=date.fromisoformat(o["due_date"]),
        certainty=float(o.get("certainty", 1.0)),
        counterparty_ref=o.get("counterparty_ref"),
        doc_date=date.fromisoformat(o["doc_date"]) if o.get("doc_date") else None,
        status=o["status"],
        label=o.get("note")
        or {"tax": "Налог", "invoice_out": "Оплата по счёту", "invoice_in": "Счёт поставщика"}.get(
            o["kind"], "Плановая операция"
        ),
    )


async def load_inputs(conn) -> tuple:
    settings = await (await conn.execute("select as_of, min_balance_kop, overdraft_limit_kop from settings")).fetchone()
    rows = await (
        await conn.execute(
            "select op_date, amount_kop, balance_eod_kop, counterparty_ref, subcategory, grp, is_rescue, doc_no, doc_date "
            "from flow order by op_date"
        )
    ).fetchall()
    obligations = [to_obligation(r[0]) for r in await (await conn.execute("select payload from obligation")).fetchall()]
    registry = dict(await (await conn.execute("select counterparty_ref, status from registry")).fetchall())
    hist = engine.history_frame(
        [
            {
                "op_date": r[0],
                "amount_kop": r[1],
                "balance_eod_kop": r[2],
                "counterparty_ref": r[3],
                "subcategory": r[4],
                "group": r[5],
                "is_rescue": r[6],
                "doc_no": r[7],
                "doc_date": r[8],
            }
            for r in rows
        ]
    )
    return settings, hist, obligations, registry


def fit_key(hist, as_of: date, obligations) -> str:
    invoices = sorted(o.id for o in obligations if o.kind == "invoice_out")
    raw = f"{as_of}|{len(hist)}|{hist['op_date'].max() if len(hist) else ''}|{int(hist['amount_kop'].sum()) if len(hist) else 0}|{invoices}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def compute(
    tenant: uuid.UUID, scenario_id: uuid.UUID | None = None, transforms: list | None = None, save: bool = True
) -> dict:
    async with db.tenant_tx(S.pool, tenant) as conn:
        settings, hist, obligations, registry = await load_inputs(conn)
    if settings is None or hist.empty:
        raise HTTPException(404, "no data yet")
    as_of, min_balance, overdraft = settings
    key = fit_key(hist, as_of, obligations)
    cached = S.fitted.get(tenant)
    if cached and cached[0] == key:
        fitted, refit = cached[1], False
    else:
        # Fitting only happens when the history changed; new obligations or settings reuse it.
        fitted = await asyncio.to_thread(engine.fit, hist, as_of, obligations, MODEL)
        S.fitted[tenant] = (key, fitted)
        refit = True
    scenario = engine.Scenario.parse(transforms) if transforms else None
    started = time.monotonic()
    result = await asyncio.to_thread(
        engine.simulate,
        fitted,
        obligations,
        engine.Settings(min_balance, overdraft),
        HORIZON,
        PATHS,
        7,
        scenario,
        registry,
    )
    result["timing_ms"] = int((time.monotonic() - started) * 1000)
    result["refit"] = refit
    result["sources"] = {
        "transactions": int(len(hist)),
        "obligations": {k: sum(1 for o in obligations if o.kind == k) for k in {o.kind for o in obligations}},
        "registry_flags": sum(1 for v in registry.values() if v != "active"),
    }
    if save:
        run_id = uuid.uuid4()
        result["run_id"] = str(run_id)
        async with db.tenant_tx(S.pool, tenant) as conn:
            await conn.execute(
                "insert into run (id, tenant_id, as_of, scenario_id, horizon, model, inputs_hash, payload) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, tenant, as_of, scenario_id, HORIZON, fitted.model_name, key, json.dumps(result)),
            )
        await events.publish(
            S.js,
            events.envelope(
                "forecast.updated",
                tenant,
                {
                    "run_id": str(run_id),
                    "as_of": str(as_of),
                    "scenario_id": str(scenario_id) if scenario_id else None,
                    "gaps": result["gaps"],
                    "refit": refit,
                    "timing_ms": result["timing_ms"],
                },
            ),
        )
    return result


async def recompute_loop() -> None:
    while True:
        await asyncio.sleep(0.5)
        now = time.monotonic()
        ready = [t for t, at in list(S.dirty.items()) if now - at >= DEBOUNCE]
        for tenant in ready:
            S.dirty.pop(tenant, None)
            try:
                await compute(tenant)
                async with db.tenant_tx(S.pool, tenant) as conn:
                    scenarios = await (await conn.execute("select id, transforms from scenario")).fetchall()
                for sid, transforms in scenarios:
                    await compute(tenant, sid, transforms)
            except HTTPException:
                pass
            except Exception:  # noqa: BLE001 - one tenant's failure must not stop the loop
                log.exception("recompute failed")


async def latest(tenant: uuid.UUID, scenario_id: uuid.UUID | None = None) -> dict:
    async with db.tenant_tx(S.pool, tenant) as conn:
        row = await (
            await conn.execute(
                "select payload from run where scenario_id is not distinct from %s order by created_at desc limit 1",
                (scenario_id,),
            )
        ).fetchone()
    if row:
        return row[0]
    return await compute(tenant, scenario_id)


def trim(result: dict, horizon: int) -> dict:
    out = dict(result)
    out["points"] = result["points"][:horizon]
    out["gaps"] = [g for g in result["gaps"] if g["start_date"] <= result["points"][horizon - 1]["date"]]
    out["horizon"] = horizon
    return out


# ---------------------------------------------------------------- API


@app.get("/api/v1/forecast")
async def get_forecast(
    horizon: int = 30, scenario_id: uuid.UUID | None = None, user: User = Depends(current_user)
) -> dict:
    horizon = max(7, min(horizon, HORIZON))
    return trim(await latest(user.tenant_id, scenario_id), horizon)


@app.get("/api/v1/gaps")
async def get_gaps(user: User = Depends(current_user)) -> dict:
    r = await latest(user.tenant_id)
    return {"as_of": r["as_of"], "gaps": r["gaps"], "warnings": r["warnings"]}


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transforms: list[dict] = Field(min_length=1, max_length=10)


class PreviewIn(BaseModel):
    transforms: list[dict] = Field(min_length=1, max_length=10)


def check_transforms(transforms: list[dict]) -> None:
    try:
        engine.Scenario.parse(transforms)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"invalid transforms: {exc}") from None


def compare(base: dict, alt: dict) -> dict:
    by_date = {p["date"]: p for p in base["points"]}
    return {
        "as_of": base["as_of"],
        "points": [
            {
                "date": p["date"],
                "base_p50_kop": by_date[p["date"]]["p50_kop"],
                "scenario_p50_kop": p["p50_kop"],
                "scenario_p10_kop": p["p10_kop"],
            }
            for p in alt["points"]
            if p["date"] in by_date
        ],
        "base_gaps": base["gaps"],
        "scenario_gaps": alt["gaps"],
    }


@app.post("/api/v1/scenarios/preview")
async def preview(body: PreviewIn, user: User = Depends(current_user)) -> dict:
    check_transforms(body.transforms)
    base = trim(await latest(user.tenant_id), 60)
    alt = trim(await compute(user.tenant_id, None, body.transforms, save=False), 60)
    return compare(base, alt)


@app.post("/api/v1/scenarios")
async def create_scenario(body: ScenarioIn, user: User = Depends(current_user)) -> dict:
    user.require_write()
    check_transforms(body.transforms)
    sid = uuid.uuid4()
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        await conn.execute(
            "insert into scenario (id, tenant_id, name, transforms) values (%s, %s, %s, %s)",
            (sid, user.tenant_id, body.name, json.dumps(body.transforms)),
        )
    await compute(user.tenant_id, sid, body.transforms)
    return {"id": str(sid), "name": body.name}


@app.get("/api/v1/scenarios")
async def list_scenarios(user: User = Depends(current_user)) -> list:
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        rows = await (
            await conn.execute("select id, name, transforms, created_at from scenario order by created_at desc")
        ).fetchall()
    return [{"id": str(r[0]), "name": r[1], "transforms": r[2], "created_at": r[3].isoformat()} for r in rows]


@app.get("/api/v1/scenarios/{scenario_id}/compare")
async def compare_scenario(scenario_id: uuid.UUID, horizon: int = 60, user: User = Depends(current_user)) -> dict:
    horizon = max(7, min(horizon, HORIZON))
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        row = await (await conn.execute("select transforms from scenario where id = %s", (scenario_id,))).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return compare(
        trim(await latest(user.tenant_id), horizon), trim(await latest(user.tenant_id, scenario_id), horizon)
    )


@app.get("/api/v1/plan-fact")
async def plan_fact(user: User = Depends(current_user)) -> dict:
    """Compares an earlier forecast (made at least a week ago) with what happened since."""
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        settings = await (await conn.execute("select as_of from settings")).fetchone()
        if not settings:
            raise HTTPException(404, "no data yet")
        as_of = settings[0]
        row = await (
            await conn.execute(
                "select as_of, payload from run where scenario_id is null and as_of <= %s order by as_of desc, created_at desc limit 1",
                (as_of - timedelta(days=7),),
            )
        ).fetchone()
        if not row:
            row = await (
                await conn.execute(
                    "select as_of, payload from run where scenario_id is null and as_of < %s order by as_of, created_at limit 1",
                    (as_of,),
                )
            ).fetchone()
        if not row:
            return {"as_of": str(as_of), "plan_as_of": None, "points": [], "weeks": []}
        plan_as_of, payload = row
        facts = await (
            await conn.execute(
                "select op_date, max(balance_eod_kop), sum(greatest(amount_kop, 0))::bigint, sum(least(amount_kop, 0))::bigint "
                "from flow where op_date > %s and op_date <= %s group by op_date order by op_date",
                (plan_as_of, as_of),
            )
        ).fetchall()
    fact = {str(r[0]): r for r in facts}
    points, weeks = [], {}
    last_balance = None
    for p in payload["points"]:
        if p["date"] > str(as_of):
            break
        f = fact.get(p["date"])
        if f:
            last_balance = f[1]
        week = date.fromisoformat(p["date"]) - timedelta(days=date.fromisoformat(p["date"]).weekday())
        w = weeks.setdefault(
            str(week), {"week": str(week), "plan_in_kop": 0, "plan_out_kop": 0, "fact_in_kop": 0, "fact_out_kop": 0}
        )
        w["plan_in_kop"] += p["inflow_kop"]
        w["plan_out_kop"] += p["outflow_kop"]
        if f:
            w["fact_in_kop"] += f[2]
            w["fact_out_kop"] += f[3]
        points.append(
            {
                "date": p["date"],
                "plan_p10_kop": p["p10_kop"],
                "plan_p50_kop": p["p50_kop"],
                "plan_p90_kop": p["p90_kop"],
                "fact_kop": last_balance,
            }
        )
    for w in weeks.values():
        planned = w["plan_in_kop"] + w["plan_out_kop"]
        actual = w["fact_in_kop"] + w["fact_out_kop"]
        w["deviation_kop"] = actual - planned
        w["flag"] = abs(actual - planned) > 0.2 * max(abs(w["plan_in_kop"]), abs(w["plan_out_kop"]), 1)
    return {"as_of": str(as_of), "plan_as_of": str(plan_as_of), "points": points, "weeks": list(weeks.values())}


@app.get("/api/v1/forecast/backtest")
async def tenant_backtest(user: User = Depends(current_user)) -> dict:
    """Rolling-origin backtest on this tenant's own history (cached per data version)."""
    async with db.tenant_tx(S.pool, user.tenant_id) as conn:
        _, hist, _, _ = await load_inputs(conn)
    if len(hist) < 200:
        raise HTTPException(409, "not enough history for a backtest")
    key = fit_key(hist, hist["op_date"].max(), [])
    cached = S.backtests.get(user.tenant_id)
    if not cached or cached[0] != key:

        def run() -> list:
            results = []
            for c in backtest.cutoffs_for(hist, 30, count=6):
                results += backtest.run_cutoff(hist, c, 30, n_paths=200)
            return backtest.summarize(results)

        S.backtests[user.tenant_id] = (key, await asyncio.to_thread(run))
    return {"horizon": 30, "results": S.backtests[user.tenant_id][1]}


async def startup(config: Config) -> None:
    S.dirty, S.fitted, S.backtests = {}, {}, {}
    S.verifier = Verifier(config, MtlsClient(config.cert_dir))
    S.pool = await db.connect(config.database_url, config.cert_dir)
    await db.migrate(S.pool, Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "migrations")))
    _, S.js = await events.connect(config.nats_url, config.cert_dir)
    await events.ensure_stream(S.js, "FORECAST", ["ci.v1.forecast.>"])
    for stream, durable, subject, handler in [
        ("TRANSACTIONS", "forecast-categorized", "ci.v1.transactions.categorized", on_categorized),
        ("OBLIGATIONS", "forecast-obligations", "ci.v1.obligations.changed", on_obligations),
        ("TENANCY", "forecast-settings", "ci.v1.tenancy.settings_changed", on_settings),
        ("CONNECTORS", "forecast-registry", "ci.v1.connectors.registry_status", on_registry),
    ]:
        asyncio.create_task(events.consume(S.js, stream, durable, subject, handler))
    asyncio.create_task(recompute_loop())
