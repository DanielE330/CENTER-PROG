"""Тесты имитатора ЭДО (без сети)."""
import pytest
from fastapi.testclient import TestClient

import mockdata
from edo.app import app

pytestmark = pytest.mark.skipif(not mockdata.company_exists("C001"), reason="нет датасета")
c = TestClient(app)
H = {"Authorization": "Bearer edo-demo-token"}


def test_auth():
    assert c.get("/live").status_code == 200
    assert c.get("/edo/v1/documents").status_code == 401
    assert c.get("/edo/v1/documents", headers={"X-Api-Token": "edo-demo-token"}).status_code == 200
    assert c.get("/edo/v1/documents", headers={"Authorization": "Bearer x"}).status_code == 401


def test_direction_shape_statuses():
    r = c.get("/edo/v1/documents", headers=H, params={"debug": True}).json()
    docs = r["documents"]
    d = docs[0]
    for k in ("id", "direction", "type", "number", "date", "status", "counterparty", "amount", "payment_due_date"):
        assert k in d
    assert {x["type"] for x in docs} <= {"УПД", "Счёт", "Акт"}
    assert {"Отклонён", "Отправлен", "Подписан"} <= {x["status"] for x in docs}
    inc = c.get("/edo/v1/documents", headers=H, params={"direction": "incoming"}).json()["documents"]
    out = c.get("/edo/v1/documents", headers=H, params={"direction": "outgoing"}).json()["documents"]
    assert len(inc) + len(out) == r["total"]
    assert c.get("/edo/v1/documents", headers=H, params={"direction": "x"}).status_code == 422


def test_cursor_pagination():
    seen, cur = [], None
    while True:
        p = {"limit": 3, **({"cursor": cur} if cur else {})}
        j = c.get("/edo/v1/documents", headers=H, params=p).json()
        seen += [x["id"] for x in j["documents"]]
        cur = j["next_cursor"]
        if not cur:
            break
    full = c.get("/edo/v1/documents", headers=H).json()
    assert seen == [x["id"] for x in full["documents"]] and len(set(seen)) == full["total"] > 3
    assert c.get("/edo/v1/documents", headers=H, params={"cursor": "!!"}).status_code == 400


def test_date_filter_and_get():
    full = c.get("/edo/v1/documents", headers=H).json()["documents"]
    cut = full[-1]["date"]
    f = c.get("/edo/v1/documents", headers=H, params={"from": cut}).json()["documents"]
    assert f and all(x["date"] >= cut for x in f)
    assert c.get(f"/edo/v1/documents/{full[0]['id']}", headers=H).json()["id"] == full[0]["id"]
    assert c.get("/edo/v1/documents/nope", headers=H).status_code == 404


def test_duplicate_with_onec():
    """Один и тот же счёт есть и в 1С, и в ЭДО (номер, ИНН, сумма совпадают)."""
    import base64
    from onec.app import app as oapp
    oc = TestClient(oapp)
    oh = {"Authorization": "Basic " + base64.b64encode(b"demo:demo").decode()}
    one = oc.get("/odata/standard.odata/Document_СчетНаОплатуПоставщика", headers=oh).json()["value"]
    cps = {r["Ref_Key"]: r["ИНН"] for r in oc.get("/odata/standard.odata/Catalog_Контрагенты", headers=oh).json()["value"]}
    k1 = {(r["Номер"], cps[r["Контрагент_Key"]], r["СуммаДокумента"]) for r in one}
    edo = c.get("/edo/v1/documents", headers=H, params={"direction": "incoming"}).json()["documents"]
    k2 = {(d["number"], d["counterparty"]["inn"], d["amount"]) for d in edo}
    assert len(k1 & k2) >= 1


def test_fail_rate(monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_RATE", "1")
    assert c.get("/edo/v1/documents", headers=H).status_code == 503
