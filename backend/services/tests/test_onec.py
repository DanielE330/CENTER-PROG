"""Тесты имитатора 1С (без сети)."""
import base64

import pytest
from fastapi.testclient import TestClient

import mockdata
from onec.app import app

pytestmark = pytest.mark.skipif(not mockdata.company_exists("C001"), reason="нет датасета")
c = TestClient(app)
B = "/odata/standard.odata/"
H = {"Authorization": "Basic " + base64.b64encode(b"demo:demo").decode()}


def get(entity, **p):
    return c.get(B + entity, headers=H, params={"$format": "json", **p})


def test_live_open_and_auth_401():
    assert c.get("/live").json() == {"status": "ok"}
    r = c.get(B + "Catalog_Контрагенты")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    bad = {"Authorization": "Basic " + base64.b64encode(b"demo:x").decode()}
    assert c.get(B + "Catalog_Контрагенты", headers=bad).status_code == 401


def test_invoice_shape():
    j = get("Document_СчетНаОплатуПокупателю").json()
    assert j["odata.metadata"].endswith("#Document_СчетНаОплатуПокупателю")
    v = j["value"][0]
    for k in ("Ref_Key", "Номер", "Дата", "Контрагент_Key", "СуммаДокумента", "ДатаОплаты", "Проведен", "ПометкаУдаления"):
        assert k in v


def test_paging_and_filter():
    allv = get("Document_СчетНаОплатуПоставщика").json()["value"]
    p = get("Document_СчетНаОплатуПоставщика", **{"$top": 2, "$skip": 1}).json()["value"]
    assert p == allv[1:3]
    f = get("Document_СчетНаОплатуПоставщика", **{"$filter": "ДатаОплаты ge datetime'2026-10-15T00:00:00' and Проведен eq true"}).json()["value"]
    assert f and all(r["ДатаОплаты"] >= "2026-10-15" and r["Проведен"] for r in f)
    assert get("Document_СчетНаОплатуПоставщика", **{"$filter": "foo"}).status_code == 400
    j = get("Document_СчетНаОплатуПоставщика", **{"$inlinecount": "allpages", "$top": 1}).json()
    assert int(j["odata.count"]) == len(allv)


def test_scenarios_and_references():
    inv = get("Document_СчетНаОплатуПокупателю").json()["value"]
    cps = {r["Ref_Key"] for r in get("Catalog_Контрагенты").json()["value"]}
    ctr = get("Catalog_ДоговорыКонтрагентов").json()["value"]
    assert all(r["Контрагент_Key"] in cps for r in inv)
    assert {r["ДоговорКонтрагента_Key"] for r in inv} <= {r["Ref_Key"] for r in ctr}
    assert any(r["Комментарий"] == "Новый договор" for r in ctr)
    assert any(r["ДатаОплаты"] < "2026-09-30" for r in inv)          # просрочен
    assert any(not r["Проведен"] for r in inv)
    one = c.get(B + f"Document_СчетНаОплатуПокупателю(guid'{inv[0]['Ref_Key']}')", headers=H)
    assert one.json()["Номер"] == inv[0]["Номер"]


def test_deterministic_and_company():
    assert get("Catalog_Контрагенты").json() == get("Catalog_Контрагенты").json()
    assert get("Catalog_Контрагенты", company="C002").json() != get("Catalog_Контрагенты").json()
    assert get("Catalog_Контрагенты", company="C999").status_code == 404
    assert get("Nope").status_code == 404


def test_fail_rate(monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_RATE", "1")
    assert get("Catalog_Контрагенты").status_code == 503
