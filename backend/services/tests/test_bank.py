"""Тесты имитатора банка (без сети)."""
import base64

import pytest
from fastapi.testclient import TestClient

import mockdata
from bank.app import app

pytestmark = pytest.mark.skipif(not mockdata.company_exists("C001"), reason="нет датасета")
c = TestClient(app)


def tok():
    r = c.post("/bank/oauth/token", data={"grant_type": "client_credentials", "client_id": "demo-client",
                                           "client_secret": "demo-secret"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_oauth():
    assert c.get("/live").status_code == 200
    r = c.post("/bank/oauth/token", data={"grant_type": "client_credentials", "client_id": "a", "client_secret": "b"})
    assert r.status_code == 401
    assert c.post("/bank/oauth/token", data={"grant_type": "password"}).status_code == 400
    b = base64.b64encode(b"demo-client:demo-secret").decode()
    r = c.post("/bank/oauth/token", data={"grant_type": "client_credentials"}, headers={"Authorization": "Basic " + b})
    assert r.json()["token_type"] == "Bearer" and r.json()["expires_in"] > 0
    assert c.get("/bank/v1/accounts").status_code == 401
    assert c.get("/bank/v1/accounts", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_accounts_balance_transactions():
    h = tok()
    a = c.get("/bank/v1/accounts", headers=h).json()["accounts"][0]
    assert a["currency"] == "RUB" and a["balance"]["amount"] is not None
    bal = c.get(f"/bank/v1/accounts/{a['id']}/balance", headers=h).json()
    assert bal["amount"] == a["balance"]["amount"]
    t = c.get(f"/bank/v1/accounts/{a['id']}/transactions", headers=h,
              params={"from": "2026-09-01", "to": "2026-09-30", "per_page": 5}).json()
    assert t["total"] > 5 and t["pages"] > 1 and len(t["items"]) == 5
    it = t["items"][0]
    assert it["direction"] in ("CREDIT", "DEBIT") and it["amount"] > 0
    p2 = c.get(f"/bank/v1/accounts/{a['id']}/transactions", headers=h,
               params={"from": "2026-09-01", "to": "2026-09-30", "per_page": 5, "page": 2}).json()
    assert p2["items"][0]["id"] != it["id"]
    ops = [o for o in mockdata.operations("C001") if "2026-09-01" <= o["date"].isoformat() <= "2026-09-30"]
    assert len(ops) == t["total"]
    assert c.get("/bank/v1/accounts/acc-X/balance", headers=h).status_code == 404


def test_fail_rate(monkeypatch):
    h = tok()
    monkeypatch.setenv("MOCK_FAIL_RATE", "1")
    assert c.get("/bank/v1/accounts", headers=h).status_code == 503
