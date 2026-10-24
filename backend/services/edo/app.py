"""Имитатор ЭДО: uvicorn edo.app:app --port 8005"""
import base64
import os
import secrets
from datetime import date, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, Request

import mockdata as md

app = FastAPI(title="edo-mock", docs_url=None, redoc_url=None)
TYPES = {"УПД": "UniversalTransferDocument", "Счёт": "Invoice", "Акт": "Act"}


def chaos():
    if md.delay_and_should_fail():
        raise HTTPException(503, {"error": "service_unavailable", "message": "simulated failure"})


def auth(request: Request):
    tok = os.environ.get("MOCK_EDO_TOKEN", "edo-demo-token")
    h = request.headers.get("authorization", "")
    given = h[7:] if h.lower().startswith("bearer ") else request.headers.get("x-api-token", "")
    if not given or not secrets.compare_digest(given, tok):
        raise HTTPException(401, {"error": "unauthorized", "message": "Invalid or missing token"},
                            headers={"WWW-Authenticate": "Bearer"})


def company_param(company: str = Query("C001", pattern=r"^C\d{3}$")) -> str:
    if not md.company_exists(company):
        raise HTTPException(404, {"error": "not_found", "message": f"Unknown company {company}"})
    return company


@app.get("/live")
def live():
    return {"status": "ok"}


def build_docs(cid: str, debug: bool) -> list[dict]:
    m = md.model(cid)
    me = md.company_info(cid)
    docs = []
    for o in m.obligations:
        if not o.edo_type:
            continue
        outgoing = o.kind == "receivable"   # мы выставили клиенту -> исходящий
        d = {
            "id": md.guid(cid, "edo", o.key),
            "direction": "outgoing" if outgoing else "incoming",
            "type": o.edo_type, "type_code": TYPES[o.edo_type],
            "number": o.edo_number, "date": o.issue.isoformat(),
            "status": o.edo_status,
            "status_updated_at": f"{o.issue + timedelta(days=1):%Y-%m-%d}T10:00:00+03:00",
            "counterparty": {"inn": o.inn, "name": o.name},
            "own_inn": me["company_inn"],
            "amount": o.amount, "vat_amount": round(o.amount * 20 / 120, 2), "currency": "RUB",
            "payment_due_date": o.due.isoformat(),
            "rejection_reason": "Расхождение в цене/количестве" if o.edo_status == "Отклонён" else None,
        }
        if debug:
            d["x_scenario"] = o.scenario
        docs.append(d)
    docs.sort(key=lambda d: (d["date"], d["id"]))
    return docs


def _cursor_dec(c: str | None) -> int:
    if not c:
        return 0
    try:
        return int(base64.urlsafe_b64decode(c.encode()).decode().removeprefix("o:"))
    except Exception:
        raise HTTPException(400, {"error": "bad_cursor", "message": "Invalid cursor"})


def _cursor_enc(n: int) -> str:
    return base64.urlsafe_b64encode(f"o:{n}".encode()).decode()


@app.get("/edo/v1/documents", dependencies=[Depends(chaos), Depends(auth)])
def documents(company: str = Depends(company_param),
              direction: str | None = Query(None, pattern="^(incoming|outgoing)$"),
              from_: date | None = Query(None, alias="from"), to: date | None = None,
              status: str | None = None, type: str | None = None,
              cursor: str | None = None, limit: int = Query(50, ge=1, le=100),
              debug: bool = False):
    docs = build_docs(company, debug)
    if direction:
        docs = [d for d in docs if d["direction"] == direction]
    if from_:
        docs = [d for d in docs if d["date"] >= from_.isoformat()]
    if to:
        docs = [d for d in docs if d["date"] <= to.isoformat()]
    if status:
        docs = [d for d in docs if d["status"] == status]
    if type:
        docs = [d for d in docs if d["type"] == type]
    off = _cursor_dec(cursor)
    page = docs[off:off + limit]
    nxt = _cursor_enc(off + limit) if off + limit < len(docs) else None
    return {"documents": page, "next_cursor": nxt, "total": len(docs)}


@app.get("/edo/v1/documents/{doc_id}", dependencies=[Depends(chaos), Depends(auth)])
def document(doc_id: str, company: str = Depends(company_param)):
    for d in build_docs(company, False):
        if d["id"] == doc_id:
            return d
    raise HTTPException(404, {"error": "not_found", "message": "Document not found"})
