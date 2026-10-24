"""Тесты интегратора: имитаторы через httpx-транспорт (без сети и портов)."""
import httpx
import pytest
from fastapi.testclient import TestClient

import mockdata
from bank.app import app as bank_app
from edo.app import app as edo_app
from integrator import merge
from integrator.app import app
from integrator.clients import base, edo as edo_client, onec as onec_client
from onec.app import app as onec_app

pytestmark = pytest.mark.skipif(not mockdata.company_exists("C001"), reason="нет датасета")


def asgi(target, hits=None, fail_first=0):
    """Синхронный httpx-транспорт поверх TestClient имитатора."""
    tc = TestClient(target)
    state = {"left": fail_first}

    def handler(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(req)
        if state["left"] > 0:
            state["left"] -= 1
            return httpx.Response(503, json={"error": "busy"})
        r = tc.request(req.method, req.url.raw_path.decode(), headers=dict(req.headers), content=req.content)
        return httpx.Response(r.status_code, headers={"content-type": r.headers["content-type"]}, content=r.content)

    return httpx.MockTransport(handler)


def down(_req):
    return httpx.Response(503)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setenv("INTEGRATOR_RETRY_PAUSE", "0")
    monkeypatch.setenv("INTEGRATOR_TODAY", "2026-09-29")
    yield
    app.state.transports = {}


def setup(**over):
    app.state.transports = {"onec": asgi(onec_app), "edo": asgi(edo_app), "bank": asgi(bank_app), **over}
    return TestClient(app)


def test_live():
    assert TestClient(app).get("/live").json() == {"status": "ok"}


def test_obligations_contract_and_dedup():
    j = setup().get("/internal/v1/obligations", params={"company": "C001"}).json()
    assert j["company"] == "C001" and j["as_of"] == "2026-09-29"
    assert j["sources"]["onec"]["status"] == "ok" and j["sources"]["edo"]["status"] == "ok"
    items = j["items"]
    assert items and all(i["duplicate_of"] is None for i in items)
    for k in ("external_id", "source", "kind", "direction", "date", "amount_kop", "title", "counterparty",
              "inn", "number", "confidence", "status", "overdue"):
        assert k in items[0]
    assert isinstance(items[0]["amount_kop"], str) and items[0]["amount_kop"].isdigit()
    keys = [(i["number"], i["inn"], i["amount_kop"]) for i in items]
    assert len(keys) == len(set(keys))
    assert j["sources"]["edo"]["duplicates"] >= 1  # дубль 1С/ЭДО
    assert all(i["overdue"] == (i["date"] < j["as_of"]) for i in items)
    assert any(i["overdue"] for i in items)
    assert all(0 <= i["confidence"] <= 1 for i in items)


def test_rejected_and_unposted_dropped():
    j = setup().get("/internal/v1/obligations").json()
    assert j["sources"]["edo"]["rejected"] >= 1
    assert all(i["status"] != "Отклонён" for i in j["items"])
    assert all(i["status"] != "Не проведён" for i in j["items"])
    # без фильтра 1С отдаёт черновики/удалённые; интегратор их отбрасывает
    raw = onec_client.fetch_all(onec_client.client(asgi(onec_app)), "Document_СчетНаОплатуПоставщика")
    assert any(not d["Проведен"] or d["ПометкаУдаления"] for d in raw)
    assert j["sources"]["onec"]["count"] < len(raw) + len(onec_client.fetch_all(onec_client.client(asgi(onec_app)), "Document_СчетНаОплатуПокупателю"))


def test_one_source_down():
    j = setup(edo=httpx.MockTransport(down)).get("/internal/v1/obligations")
    assert j.status_code == 200
    j = j.json()
    assert j["sources"]["edo"]["status"] == "error" and j["sources"]["edo"]["error"]
    assert j["sources"]["onec"]["status"] == "ok" and j["items"]
    assert {i["source"] for i in j["items"]} == {"onec"}
    j = setup(onec=httpx.MockTransport(down)).get("/internal/v1/obligations").json()
    assert j["sources"]["onec"]["status"] == "error" and {i["source"] for i in j["items"]} == {"edo"}


def test_retry_on_503():
    hits = []
    j = setup(edo=asgi(edo_app, hits, fail_first=2)).get("/internal/v1/obligations").json()
    assert j["sources"]["edo"]["status"] == "ok" and len(hits) >= 3
    hits.clear()
    j = setup(edo=asgi(edo_app, hits, fail_first=3)).get("/internal/v1/obligations").json()
    assert j["sources"]["edo"]["status"] == "error" and len(hits) == 3  # 1 + 2 ретрая


def test_auth_failure_reported_not_500(monkeypatch):
    monkeypatch.setenv("ONEC_PASSWORD", "wrong")
    j = setup().get("/internal/v1/obligations")
    assert j.status_code == 200 and j.json()["sources"]["onec"]["status"] == "error"


def test_onec_pagination_and_filter():
    hits = []
    http = onec_client.client(asgi(onec_app, hits))
    all_ = onec_client.fetch_all(http, "Document_СчетНаОплатуПоставщика", onec_client.FILTER, page=1)
    one = onec_client.fetch_all(http, "Document_СчетНаОплатуПоставщика", onec_client.FILTER, page=1000)
    assert len(all_) == len(one) > 1
    assert [d["Ref_Key"] for d in all_] == [d["Ref_Key"] for d in one]
    assert all(d["Проведен"] and not d["ПометкаУдаления"] for d in all_)
    assert len(hits) > 3 and all("skip" in r.url.params or "$skip" in r.url.params for r in hits)


def test_edo_cursor_pagination():
    hits = []
    http = edo_client.client(asgi(edo_app, hits))
    small = edo_client.fetch_all(http, "C001", limit=2)
    big = edo_client.fetch_all(http, "C001", limit=100)
    assert [d["id"] for d in small] == [d["id"] for d in big] and len(small) > 2
    assert any("cursor" in str(r.url) for r in hits)


def test_bank_balance():
    j = setup().get("/internal/v1/bank-balance", params={"company": "C001"}).json()
    assert j["status"] == "ok" and j["balance_kop"].lstrip("-").isdigit() and j["last_date"]
    j = setup(bank=httpx.MockTransport(down)).get("/internal/v1/bank-balance")
    assert j.status_code == 502 and j.json()["status"] == "error"


# --- чистая склейка ---
def rec(src, ext, **kw):
    r = {"external_id": ext, "source": src, "kind": "invoice", "direction": "out", "date": "2026-10-10",
         "amount": 100.5, "title": "t", "counterparty": "c", "inn": "1", "number": "N1", "doc_status": "Подписан"}
    return {**r, **kw}


def test_merge_pure():
    items, st = merge.merge([
        rec("edo", "e1"), rec("onec", "o1", posted=True, doc_status="Проведён"),
        rec("onec", "o2", number="N2", posted=False),
        rec("onec", "o3", number="N3", deleted=True),
        rec("edo", "e4", number="N4", doc_status="Отклонён"),
        rec("edo", "e5", number="N5", date="2026-09-01", doc_status="Отправлен"),
        rec("edo", "e6", number="N6", doc_status="Подписан", amount=100.5),
    ], "2026-09-29")
    ids = {i["external_id"] for i in items}
    assert ids == {"o1", "e5", "e6"}
    o1 = next(i for i in items if i["external_id"] == "o1")
    assert o1["amount_kop"] == "10050"
    assert st["edo"]["duplicates"] == 1 and st["edo"]["rejected"] == 1
    assert st["onec"]["skipped"] == 2
    e5, e6 = (next(i for i in items if i["external_id"] == x) for x in ("e5", "e6"))
    assert e5["overdue"] and e5["confidence"] < e6["confidence"]
    assert not e6["overdue"] and e6["confidence"] > 0.9
