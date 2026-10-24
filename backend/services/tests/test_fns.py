"""Сервис fns: календарь правилами, ЕНС (имитатор и загруженная справка), проверка контрагентов, контракты HTTP.
Без сети: имитатор fns_sim подключён через транспорт в памяти, gov недоступен (учитываются только выходные)."""
import base64
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

import fns.app as fns_app
from fns import config, ens_import
from fns.deps import Deps
from fns.store import Store
import fns_sim.app as sim_app
import fns_sim.universe as universe


def make_inn(prefix: str, n: int = 10) -> str:
    """ИНН с верным контрольным числом (для режима live)."""
    d = [int(c) for c in prefix]
    if n == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        d += [0] * (9 - len(d))
        return "".join(map(str, d)) + str(sum(a * b for a, b in zip(d, w)) % 11 % 10)
    w1, w2 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8], [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d += [0] * (10 - len(d))
    d.append(sum(a * b for a, b in zip(d, w1)) % 11 % 10)
    d.append(sum(a * b for a, b in zip(d, w2)) % 11 % 10)
    return "".join(map(str, d))


OK_INN, DEBT_INN, BANKRUPT_INN, LIQ_INN = (make_inn(p) for p in ("7701", "7702", "7703", "7704"))


@pytest.fixture
def deps(tmp_path, monkeypatch):
    monkeypatch.setenv("FNS_MODE", "sim")
    monkeypatch.setenv("FNS_MIN_INTERVAL", "0")
    monkeypatch.setenv("FNS_SIM_PROBLEM_INNS", f"{OK_INN}=ok,{DEBT_INN}=tax_debt,{BANKRUPT_INN}=bankrupt,{LIQ_INN}=liquidating")
    universe.reset_cache()
    sim = TestClient(sim_app.app)

    def to_sim(req: httpx.Request):
        r = sim.request(req.method, req.url.path, params=dict(req.url.params), headers=dict(req.headers), content=req.content)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    gov_down = httpx.MockTransport(lambda req: httpx.Response(503, json={}))
    return Deps(store=Store(tmp_path / "fns.db"), sim_transport=httpx.MockTransport(to_sim), gov_transport=gov_down)


@pytest.fixture
def client(deps):
    fns_app.app.state.deps = deps
    yield TestClient(fns_app.app)
    fns_app.app.state.deps = None


# --- календарь ---

def test_obligations_usn_year_has_advances_and_enp_dates_are_workdays(client):
    r = client.get("/internal/v1/fns/obligations", params={"inn": OK_INN, "regime": "usn_income", "year": 2026, "check": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "sim" and body["source"] == "rules"
    kinds = [e["kind"] for e in body["events"]]
    assert kinds.count("enp") == 12 and kinds.count("usn_advance") == 3 and "usn_annual" in kinds
    assert all(date.fromisoformat(e["date"]).weekday() < 5 for e in body["events"])
    assert any(w["code"] == "calendar_weekends_only" for w in body["warnings"])  # gov недоступен: честная пометка


def test_obligations_nds_three_installments_per_quarter(client):
    body = client.get("/internal/v1/fns/obligations", params={"regime": "osno", "year": 2026, "check": "false"}).json()
    q1 = [e for e in body["events"] if e["kind"] == "nds" and e["period"] == "2026-Q1"]
    assert [e["installment"] for e in q1] == [1, 2, 3]


def test_obligations_patent_has_only_contributions_for_ip(client):
    body = client.get("/internal/v1/fns/obligations", params={"inn": make_inn("501", 12), "regime": "patent", "year": 2026, "check": "false"}).json()
    kinds = {e["kind"] for e in body["events"]}
    assert body["entity"] == "ip" and {"ip_fixed_contributions", "ip_contributions_1pct"} <= kinds
    assert not kinds & {"usn_advance", "nds", "usn_annual"}


@pytest.mark.parametrize("params", [{"regime": "nope"}, {"regime": "osno", "year": 1999}, {"regime": "osno", "inn": "123"}])
def test_obligations_bad_params(client, params):
    r = client.get("/internal/v1/fns/obligations", params={"year": 2026, **params})
    assert r.status_code == 422 and r.json()["detail"]["code"] in ("invalid_params", "invalid_inn")


# --- контрагенты ---

def test_counterparty_scenarios_from_sim(client):
    ok = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    assert ok["status"] == "active" and ok["risk_level"] == "low" and ok["source"] == "fns_sim" and ok["mode"] == "sim"
    debt = client.get("/internal/v1/fns/counterparty", params={"inn": DEBT_INN}).json()
    assert debt["tax_debt"]["has"] and debt["tax_debt"]["amount_kop"] > 0 and "tax_debt" in debt["risk_reasons"][0]
    bk = client.get("/internal/v1/fns/counterparty", params={"inn": BANKRUPT_INN}).json()
    assert bk["bankruptcy"]["flag"] and bk["risk_level"] == "high"
    liq = client.get("/internal/v1/fns/counterparty", params={"inn": LIQ_INN}).json()
    assert liq["status"] == "liquidating" and liq["risk_level"] == "high"


def test_counterparty_cache_and_stale_when_source_down(deps, client):
    first = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    assert first["cached"] is False
    assert client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()["cached"] is True
    # источник упал, кэш протух: отдаём просроченный кэш с пометкой, а не ошибку
    deps.sim_transport = httpx.MockTransport(lambda req: httpx.Response(503, json={}))
    monkey_ttl = pytest.MonkeyPatch()
    monkey_ttl.setenv("FNS_CACHE_TTL", "0")
    try:
        out = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    finally:
        monkey_ttl.undo()
    assert out["stale"] is True and out["status"] == "active"


def test_counterparty_unavailable_without_cache_is_reported_not_guessed(deps, client):
    deps.sim_transport = httpx.MockTransport(lambda req: httpx.Response(503, json={}))
    out = client.get("/internal/v1/fns/counterparty", params={"inn": make_inn("7790")}).json()
    assert out["source"] == "unavailable" and out["risk_level"] == "unknown" and out["reason"].startswith("http_")


def test_batch_dedup_limit_and_invalid(client):
    r = client.post("/internal/v1/fns/counterparties/batch", json={"inns": [OK_INN, OK_INN, DEBT_INN]})
    body = r.json()
    assert body["requested"] == 2 and len(body["items"]) == 2 and not body["partial"]
    assert client.post("/internal/v1/fns/counterparties/batch", json={"inns": ["abc"]}).status_code == 422
    assert client.post("/internal/v1/fns/counterparties/batch", json={"inns": [OK_INN] * 51}).status_code == 422


def test_live_mode_rejects_bad_checksum_without_network(deps, client, monkeypatch):
    monkeypatch.setenv("FNS_MODE", "live")
    out = client.get("/internal/v1/fns/counterparty", params={"inn": "7701234567"}).json()
    assert out["status"] == "invalid_inn" and out["reason"] == "inn_checksum_invalid"


# --- ЕНС ---

def test_ens_sim_requires_known_taxpayer(client):
    r = client.get("/internal/v1/fns/ens", params={"inn": make_inn("9999")})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "fns_unknown_taxpayer"


@pytest.mark.skipif(not universe.world().companies, reason="нужен датасет (MOCK_DATASET_DIR)")
def test_ens_sim_for_dataset_company(client):
    inn = next(iter(universe.world().companies))
    body = client.get("/internal/v1/fns/ens", params={"inn": inn}).json()
    assert body["source"] == "fns_sim" and body["upcoming"] and body["debt_kop"] == max(0, -body["balance_kop"])


def test_ens_live_without_import_is_404_with_hint(client, monkeypatch):
    monkeypatch.setenv("FNS_MODE", "live")
    r = client.get("/internal/v1/fns/ens", params={"inn": OK_INN})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "fns_ens_not_imported"


def b64(s: str | bytes) -> str:
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


CSV = "as_of;balance;debt;penalty;due_date;due_amount\n2026-10-20;-12 500,50;12500,50;300;2026-10-28;85000\n;;;;2026-11-28;90000,00\n"


def test_import_csv_then_ens_uses_snapshot_over_sim(client):
    r = client.post("/internal/v1/fns/ens/import", params={"inn": OK_INN}, json={"filename": "ens.csv", "content_b64": b64(CSV)})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["format"] == "csv" and out["balance_kop"] == -1_250_050 and out["debt_kop"] == 1_250_050
    assert [u["amount_kop"] for u in out["upcoming"]] == [8_500_000, 9_000_000]
    got = client.get("/internal/v1/fns/ens", params={"inn": OK_INN}).json()
    assert got["source"] == "import" and got["as_of"] == "2026-10-20" and got["debt_kop"] == 1_250_050


XML = """<?xml version="1.0" encoding="windows-1251"?>
<Файл><Документ КНД="1160080" ДатаДок="21.10.2026"><СвНП ИННЮЛ="{inn}"/>
<СостРасч СальдоЕНС="-45000.00" Недоимка="45000.00" Пени="1200.40"/></Документ></Файл>"""


def test_import_xml_knd_1160080_windows1251(client):
    raw = XML.format(inn=OK_INN).encode("cp1251")
    out = client.post("/internal/v1/fns/ens/import", params={"inn": OK_INN}, json={"content_b64": b64(raw)}).json()
    assert out["format"] == "knd_1160080" and out["as_of"] == "2026-10-21"
    assert out["balance_kop"] == -4_500_000 and out["debt_kop"] == 4_500_000 and out["penalty_kop"] == 120_040


@pytest.mark.parametrize("payload,code", [
    (XML.format(inn="7799999999").encode("cp1251"), "fns_import_inn_mismatch"),
    (XML.replace("1160080", "1150058").format(inn=OK_INN).encode("cp1251"), "fns_import_wrong_form"),
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', "fns_import_bad_file"),
    ('<Файл><Документ КНД="1160080"/></Файл>'.encode("utf-8"), "fns_import_unrecognized"),
    (b"not,a,known\n1,2,3\n", "fns_import_unrecognized"),
])
def test_import_rejects_bad_files(client, payload, code):
    r = client.post("/internal/v1/fns/ens/import", params={"inn": OK_INN}, json={"content_b64": b64(payload)})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_import_too_large_and_empty(client):
    big = client.post("/internal/v1/fns/ens/import", params={"inn": OK_INN}, json={"content_b64": b64(b"a" * (ens_import.MAX_BYTES + 1))})
    assert big.status_code == 413
    assert client.post("/internal/v1/fns/ens/import", params={"inn": OK_INN}, json={"content_b64": ""}).status_code == 422


def test_to_kop_and_dates():
    assert ens_import.to_kop("1 234,56") == 123_456 and ens_import.to_kop("-0.5") == -50 and ens_import.to_kop("—") is None
    assert ens_import.to_date("05.03.2026") == "2026-03-05" and ens_import.to_date("x") is None


def test_logs_do_not_contain_inn(client, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN})
    assert OK_INN not in caplog.text


def test_info_and_live(client):
    assert client.get("/live").json()["status"] == "ok"
    assert client.get("/internal/v1/fns/info").json()["mode"] == config.mode()
