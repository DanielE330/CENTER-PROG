"""Интегратор: единый список ожидаемых платежей из 1С и ЭДО (+ остаток банка)."""
import datetime as dt
import os
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import merge
from .clients import bank, edo, onec
from .clients.base import SourceError

app = FastAPI(title="integrator", docs_url=None, redoc_url=None)
# Подмена транспорта в тестах: {"onec": transport, "edo": ..., "bank": ...}
app.state.transports = {}
COMPANY = Query("C001", pattern=r"^C\d{3}$")


def today() -> str:
    return os.environ.get("INTEGRATOR_TODAY") or os.environ.get("MOCK_TODAY") or dt.date.today().isoformat()


@app.get("/live")
def live():
    return {"status": "ok"}


def _pull(name: str, fn):
    t0 = time.monotonic()
    try:
        return fn(), {"status": "ok", "error": None, "ms": int((time.monotonic() - t0) * 1000)}
    except (SourceError, httpx.HTTPError) as e:
        return [], {"status": "error", "error": str(e), "ms": int((time.monotonic() - t0) * 1000)}


@app.get("/internal/v1/obligations")
def obligations(company: str = COMPANY, as_of: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    as_of = as_of or today()
    tr = app.state.transports
    with onec.client(tr.get("onec")) as h1, edo.client(tr.get("edo")) as h2:
        r1, s1 = _pull("onec", lambda: onec.fetch(h1))
        r2, s2 = _pull("edo", lambda: edo.fetch(h2, company))
    items, stats = merge.merge(r1 + r2, as_of)
    sources = {}
    for name, s in (("onec", s1), ("edo", s2)):
        sources[name] = {**s, **stats.get(name, {"count": 0, "duplicates": 0, "rejected": 0, "skipped": 0})}
    return {"company": company, "as_of": as_of, "sources": sources, "items": items}


@app.get("/internal/v1/bank-balance")
def bank_balance(company: str = COMPANY):
    try:
        with bank.client(app.state.transports.get("bank")) as h:
            b = bank.balance(h, company)
    except (SourceError, httpx.HTTPError) as e:
        return JSONResponse({"company": company, "status": "error", "error": str(e)}, status_code=502)
    return {"company": company, "status": "ok", **b}
