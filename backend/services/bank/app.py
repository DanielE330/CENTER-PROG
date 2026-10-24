"""Имитатор банковского API: uvicorn bank.app:app --port 8006"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request

import mockdata as md

app = FastAPI(title="bank-mock", docs_url=None, redoc_url=None)
TTL = 3600


def _secret() -> bytes:
    return os.environ.get("MOCK_BANK_SIGN_KEY", "mock-bank-key").encode()


def make_token(client_id: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"c": client_id, "exp": int(time.time()) + TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def check_token(tok: str) -> bool:
    try:
        body, sig = tok.rsplit(".", 1)
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, good) and json.loads(base64.urlsafe_b64decode(body))["exp"] > time.time()
    except Exception:
        return False


def chaos():
    if md.delay_and_should_fail():
        raise HTTPException(503, {"error": "temporarily_unavailable", "error_description": "simulated failure"})


def auth(request: Request):
    h = request.headers.get("authorization", "")
    if not (h.lower().startswith("bearer ") and check_token(h[7:])):
        raise HTTPException(401, {"error": "invalid_token", "error_description": "Bearer token missing or expired"},
                            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def company_param(company: str = Query("C001", pattern=r"^C\d{3}$")) -> str:
    if not md.company_exists(company):
        raise HTTPException(404, {"error": "not_found", "message": f"Unknown company {company}"})
    return company


def account_id(cid: str) -> str:
    return f"acc-{cid}-1"


def _acc(cid: str) -> dict:
    ops = md.operations(cid)
    info = md.company_info(cid)
    num = "40702810" + "".join(c for c in md.guid(cid, "acc") if c.isdigit())[:12].ljust(12, "0")
    return {"id": account_id(cid), "number": num, "currency": "RUB", "type": "CURRENT", "status": "ACTIVE",
            "owner": {"inn": info["company_inn"], "name": info["company_name"]},
            "bank": {"bic": "044525000", "name": "Банк «Центр»"}, "opened_at": ops[0]["date"].isoformat()}


def _balance_on(cid: str, d: date) -> float:
    ops = md.operations(cid)
    last = None
    for o in ops:
        if o["date"] <= d:
            last = o
        else:
            break
    return last["balance_eod"] if last else md.opening_balance(cid)


@app.get("/live")
def live():
    return {"status": "ok"}


@app.post("/bank/oauth/token", dependencies=[Depends(chaos)])
async def token(request: Request, grant_type: str = Form(...), client_id: str | None = Form(None),
                client_secret: str | None = Form(None), scope: str | None = Form(None)):
    h = request.headers.get("authorization", "")
    if h.lower().startswith("basic "):
        try:
            client_id, _, client_secret = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            pass
    if grant_type != "client_credentials":
        raise HTTPException(400, {"error": "unsupported_grant_type"})
    ok = (client_id and client_secret
          and secrets.compare_digest(client_id, os.environ.get("MOCK_BANK_CLIENT_ID", "demo-client"))
          and secrets.compare_digest(client_secret, os.environ.get("MOCK_BANK_CLIENT_SECRET", "demo-secret")))
    if not ok:
        raise HTTPException(401, {"error": "invalid_client"}, headers={"WWW-Authenticate": "Basic"})
    return {"access_token": make_token(client_id), "token_type": "Bearer", "expires_in": TTL,
            "scope": scope or "accounts transactions"}


@app.get("/bank/v1/accounts", dependencies=[Depends(chaos), Depends(auth)])
def accounts(company: str = Depends(company_param)):
    t = md.today(company)
    a = _acc(company)
    a["balance"] = {"amount": _balance_on(company, t), "currency": "RUB", "as_of": t.isoformat()}
    return {"accounts": [a]}


def _own(acc_id: str, company: str):
    if acc_id != account_id(company):
        raise HTTPException(404, {"error": "not_found", "message": "Account not found"})


@app.get("/bank/v1/accounts/{acc_id}/balance", dependencies=[Depends(chaos), Depends(auth)])
def balance(acc_id: str, company: str = Depends(company_param), on: date | None = Query(None, alias="date")):
    _own(acc_id, company)
    d = min(on, md.today(company)) if on else md.today(company)
    return {"account_id": acc_id, "amount": _balance_on(company, d), "currency": "RUB", "as_of": d.isoformat()}


@app.get("/bank/v1/accounts/{acc_id}/transactions", dependencies=[Depends(chaos), Depends(auth)])
def transactions(acc_id: str, company: str = Depends(company_param),
                 from_: date | None = Query(None, alias="from"), to: date | None = None,
                 page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=500)):
    _own(acc_id, company)
    t = md.today(company)
    to = min(to, t) if to else t
    from_ = from_ or (to - timedelta(days=30))
    if from_ > to:
        raise HTTPException(400, {"error": "invalid_request", "message": "from > to"})
    rows = [o for o in md.operations(company) if from_ <= o["date"] <= to]
    pages = max(1, -(-len(rows) // per_page))
    items = [{
        "id": o["id"], "booking_date": o["date"].isoformat(),
        "amount": o["amount"], "currency": "RUB", "direction": "CREDIT" if o["credit"] else "DEBIT",
        "counterparty": {"inn": o["inn"] or None, "name": o["name"] or None},
        "purpose": o["purpose"] or None, "balance_after": o["balance_eod"],
    } for o in rows[(page - 1) * per_page: page * per_page]]
    return {"account_id": acc_id, "from": from_.isoformat(), "to": to.isoformat(), "page": page,
            "per_page": per_page, "total": len(rows), "pages": pages, "items": items}
