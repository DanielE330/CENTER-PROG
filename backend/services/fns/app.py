"""Сервис сведений ФНС (порт 8015).
uvicorn fns.app:app --host 127.0.0.1 --port 8015

Режим FNS_MODE=sim (по умолчанию) — имитатор fns_sim; live — реальные открытые источники, выключен, пока владелец
не включит его флагом (docs/fns.md); auto — имитатор для ИНН датасета, реестры для настоящих ИНН (контрагенты из 1С). ЕНС в режиме live берётся только из загруженной справки: в личный кабинет не входим.
"""
import time

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import config, counterparty, ens_import, logsafe, rules
from .deps import Deps
from .errors import SourceUnavailable
from .rules_data import MAX_YEAR, MIN_YEAR

logsafe.install()
app = FastAPI(title="fns-service", docs_url=None, redoc_url=None)
app.state.deps = None


def deps(request: Request) -> Deps:
    if request.app.state.deps is None:
        request.app.state.deps = Deps()
    return request.app.state.deps


@app.on_event("startup")
def preload_debt_index():
    """live/auto: индекс задолженности (открытые данные ФНС, ~70 МБ) строится в фоне сразу, а не при первой проверке —
    иначе первая проверка контрагента вернёт сведения без долга. Включается FNS_DEBT_PRELOAD=1 (в compose)."""
    if config.mode() in ("live", "auto") and config.env("FNS_DEBT_PRELOAD", "0") == "1":
        if app.state.deps is None:
            app.state.deps = Deps()
        app.state.deps.debt.ensure()


def fail(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def need_inn(inn: str) -> str:
    if not counterparty.INN_RE.match(inn or ""):
        fail(422, "invalid_inn", "ИНН: 10 или 12 цифр")
    return inn


@app.get("/live")
def live():
    return {"status": "ok", "mode": config.mode()}


@app.get("/internal/v1/fns/info")
def info(request: Request):
    d = deps(request)
    return {"mode": config.mode(), "years": [MIN_YEAR, MAX_YEAR], "cache_ttl": config.cache_ttl(),
            "cache_entries": d.store.cache_count(), "ens": "sim" if config.mode() == "sim" else "import"}


@app.get("/internal/v1/fns/obligations")
def obligations(request: Request, inn: str | None = None, regime: str = "usn_income",
                year: int = Query(default_factory=lambda: time.gmtime().tm_year), entity: str | None = None,
                employees: bool | None = None, check: bool = True):
    if inn is not None:
        need_inn(inn)
    try:
        out = rules.obligations(deps(request), inn, regime, year, entity, employees, check)
    except rules.BadParams as e:
        fail(422, "invalid_params", str(e))
    out["mode"] = config.mode()
    return out


def _ens_from_snapshot(inn: str, snap: dict) -> dict:
    return {"inn": inn, "as_of": snap["as_of"], "balance_kop": snap["balance_kop"], "debt_kop": snap["debt_kop"],
            "overpayment_kop": snap["overpayment_kop"], "penalty_kop": snap["penalty_kop"], "upcoming": snap["upcoming"],
            "checked_at": ens_import.iso_utc(snap["imported_at"]), "source": "import", "mode": config.mode(),
            "label": snap.get("label")}


@app.get("/internal/v1/fns/ens")
def ens(request: Request, inn: str):
    d = deps(request)
    need_inn(inn)
    snap = d.store.ens_latest(inn)
    if snap:  # справка, загруженная владельцем, надёжнее имитатора
        return _ens_from_snapshot(inn, snap)
    if config.mode() != "sim":
        fail(404, "fns_ens_not_imported",
             "ЕНС в режиме live берётся из справки о состоянии расчётов: выгрузите её из личного кабинета и загрузите")
    from .sources import sim
    try:
        with d.sim() as c:
            data = sim.ens(c, inn)
    except SourceUnavailable as e:
        fail(503, "fns_unavailable", e.detail)
    if data is None:
        if config.mode() == "auto":  # настоящая компания: ЕНС только из справки, как в live
            fail(404, "fns_ens_not_imported",
                 "ЕНС настоящей компании берётся из справки о состоянии расчётов: выгрузите её из личного кабинета и загрузите")
        fail(404, "fns_unknown_taxpayer", "Налогоплательщик не найден в источнике")
    return {**data, "checked_at": ens_import.iso_utc(time.time()), "source": "fns_sim", "mode": "sim"}


class ImportBody(BaseModel):
    filename: str = Field(default="", max_length=200)
    content_b64: str = Field(max_length=4_000_000)


@app.post("/internal/v1/fns/ens/import")
def ens_import_route(request: Request, body: ImportBody, inn: str):
    d = deps(request)
    need_inn(inn)
    try:
        snap = ens_import.parse(ens_import.decode_b64(body.content_b64), inn)
    except ens_import.ImportError_ as e:
        fail(e.status, e.code, e.message)
    d.store.ens_put(inn, snap, label=(body.filename or None) and body.filename[:200])
    return _ens_from_snapshot(inn, {**d.store.ens_latest(inn)}) | {"format": snap["format"], "warnings": snap["warnings"]}


@app.get("/internal/v1/fns/counterparty")
def counterparty_one(request: Request, inn: str, force: bool = False):
    need_inn(inn)
    return counterparty.check(deps(request), inn, force)


class BatchBody(BaseModel):
    inns: list[str] = Field(max_length=counterparty.BATCH_MAX)
    force: bool = False


@app.post("/internal/v1/fns/counterparties/batch")
def counterparties_batch(request: Request, body: BatchBody):
    bad = [i for i in body.inns if not counterparty.INN_RE.match(i)]
    if bad:
        fail(422, "invalid_inn", "ИНН: 10 или 12 цифр")
    return counterparty.batch(deps(request), body.inns, body.force)
