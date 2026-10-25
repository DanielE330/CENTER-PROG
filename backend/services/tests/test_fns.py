"""Сервис fns: календарь правилами, ЕНС из загруженной справки, проверка контрагентов по открытым реестрам, контракты HTTP.
Без сети: ЕГРЮЛ, открытые данные ФНС (debtam) и ЕФРСБ подменены транспортом в памяти, gov недоступен (только выходные)."""
import base64
import gzip
import io
import json
import zipfile
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

import fns.app as fns_app
from fns import counterparty, ens_import
from fns.deps import Deps
from fns.store import Store


def make_inn(prefix: str, n: int = 10) -> str:
    """ИНН с верным контрольным числом."""
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


OK_INN, DEBT_INN, SMALL_DEBT_INN, LIQ_INN, BANKRUPT_INN, MISSING_INN = (make_inn(p) for p in ("7701", "7702", "7705", "7704", "7703", "7790"))
DEBT_URL = "https://file.nalog.ru/opendata/7707329152-debtam/data-20260925-structure-20181201.zip"
DEBTS = {DEBT_INN: "4969978.38", SMALL_DEBT_INN: "49835.55"}


def debt_zip() -> bytes:
    docs = "".join(
        f'<Документ ИдДок="{i}" ДатаДок="25.09.2026" ДатаСост="01.09.2026"><СведНП НаимОрг="ООО" ИННЮЛ="{inn}"/>'
        f'<СведНедоим НаимНалог="НДС" СумНедНалог="{amt}" СумПени="0" СумШтраф="0" ОбщСумНедоим="{amt}"/></Документ>'
        for i, (inn, amt) in enumerate(DEBTS.items()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("VO_debt_0001.xml", f"<Файл>{docs}</Файл>")
    return buf.getvalue()


class Registries:
    """Открытые реестры в памяти: robots.txt нет, ЕГРЮЛ (ответы gzip), набор debtam, ЕФРСБ с авторизацией."""

    def __init__(self):
        self.calls: list[str] = []
        self.tokens: dict[str, str] = {}
        self.down = False
        self.zip = debt_zip()

    def handler(self, req: httpx.Request):
        self.calls.append(f"{req.method} {req.url.host}{req.url.path}")
        if self.down:
            return httpx.Response(503)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.host == "egrul.nalog.ru" and req.method == "POST":
            q = dict(httpx.QueryParams(req.content.decode()))
            t = f"res{len(self.tokens):08d}"  # токен результата непрозрачный, как у ЕГРЮЛ: ИНН в адресе нет
            self.tokens[t] = q.get("query", "")
            body = gzip.compress(json.dumps({"t": t, "captchaRequired": False}).encode())
            return httpx.Response(200, content=body, headers={"content-encoding": "gzip", "content-type": "application/json"})
        if req.url.host == "egrul.nalog.ru":
            inn = self.tokens.get(req.url.path.rsplit("/", 1)[-1], "")
            if inn == MISSING_INN:
                return httpx.Response(200, json={"rows": []})
            return httpx.Response(200, json={"rows": [{"i": inn, "k": "ul", "r": "07.11.2003", "e": "01.08.2026" if inn == LIQ_INN else ""}]})
        if req.url.path.endswith(".zip"):
            return httpx.Response(200, content=self.zip)
        if "fedresurs" in req.url.host and req.url.path == "/v1/auth":
            return httpx.Response(200, json={"jwt": "jwt"})
        if "fedresurs" in req.url.host:
            return httpx.Response(200, json={"total": 1 if req.url.params.get("inn") == BANKRUPT_INN else 0})
        return httpx.Response(404)


@pytest.fixture
def reg():
    return Registries()


@pytest.fixture
def deps(tmp_path, monkeypatch, reg):
    monkeypatch.setenv("FNS_MIN_INTERVAL", "0")
    monkeypatch.setenv("FNS_DEBT_URL", DEBT_URL)
    monkeypatch.delenv("FEDRESURS_LOGIN", raising=False)
    monkeypatch.delenv("FEDRESURS_PASSWORD", raising=False)
    counterparty.fedresurs._tok.update(jwt=None, until=0.0)
    gov_down = httpx.MockTransport(lambda req: httpx.Response(503, json={}))
    d = Deps(store=Store(tmp_path / "fns.db"), public_transport=httpx.MockTransport(reg.handler), gov_transport=gov_down)
    d.debt.ensure(block=True)
    return d


@pytest.fixture
def client(deps):
    fns_app.app.state.deps = deps
    yield TestClient(fns_app.app)
    fns_app.app.state.deps = None


def one(client, inn, **settings):
    body = {"inns": [inn]} | ({"settings": settings} if settings else {})
    return client.post("/internal/v1/fns/counterparties/batch", json=body).json()["items"][0]


# --- календарь ---

def test_obligations_usn_year_has_advances_and_enp_dates_are_workdays(client):
    r = client.get("/internal/v1/fns/obligations", params={"inn": OK_INN, "regime": "usn_income", "year": 2026, "check": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "rules"
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


# --- контрагенты: только открытые реестры, сведения помечены как выгруженные из ФНС ---

def test_debt_index_built_from_open_data(deps):
    st = deps.debt.state()
    assert st["state"] == "ready" and st["as_of"] == "2026-09-01" and st["records"] == 2


def test_counterparty_from_registries_is_marked_as_fns_export(client, reg):
    ok = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    assert (ok["status"], ok["risk_level"], ok["source"], ok["exported_from"]) == ("active", "low", "egrul", "fns")
    assert ok["registered_at"] == "2003-11-07" and ok["tax_debt"] == {"has": False, "amount_kop": 0, "as_of": "2026-09-01"}
    assert "mode" not in ok and not any("sim" in c for c in reg.calls)
    debt = one(client, DEBT_INN)
    assert debt["tax_debt"] == {"has": True, "amount_kop": 496997838, "as_of": "2026-09-01"}
    assert (debt["risk_level"], debt["risk_reasons"]) == ("medium", ["tax_debt_large"])
    assert one(client, SMALL_DEBT_INN)["risk_reasons"] == ["tax_debt"]
    liq = one(client, LIQ_INN)
    assert liq["status"] == "liquidated" and liq["risk_level"] == "high"


def test_not_in_registry_counts_as_risk_only_when_enabled(client):
    off = one(client, MISSING_INN)
    assert off["status"] == "not_found" and off["risk_level"] == "low" and off["risk_reasons"] == []
    on = one(client, MISSING_INN, risk={"not_in_registry": True})
    assert on["risk_level"] == "medium" and on["risk_reasons"] == ["not_in_registry"]


def test_settings_switch_off_reasons_and_threshold(client):
    assert one(client, DEBT_INN, risk={"tax_debt": False})["risk_level"] == "low"
    assert one(client, LIQ_INN, risk={"liquidation": False})["risk_level"] == "low"
    # порог крупного долга выше суммы: долг обычный, вес 1
    assert one(client, DEBT_INN, risk={"large_debt_kop": 10**12})["risk_reasons"] == ["tax_debt"]


def test_disabled_source_is_not_requested(client, reg):
    rec = one(client, OK_INN, sources={"egrul": False})
    assert not any(c.startswith("POST egrul") for c in reg.calls)
    assert rec["sources"]["egrul"] == "off" and rec["status"] == "unknown" and rec["source"] == "data_nalog"
    assert "reason" not in rec


def test_fedresurs_needs_operator_credentials_then_flags_bankruptcy(client, monkeypatch):
    rec = one(client, BANKRUPT_INN)
    assert rec["sources"]["fedresurs"] == "unavailable:credentials_required" and rec["bankruptcy"]["flag"] is None
    monkeypatch.setenv("FEDRESURS_LOGIN", "op")
    monkeypatch.setenv("FEDRESURS_PASSWORD", "pw")
    rec = client.post("/internal/v1/fns/counterparties/batch", json={"inns": [BANKRUPT_INN], "force": True}).json()["items"][0]
    assert rec["bankruptcy"] == {"flag": True, "source": "fedresurs"} and rec["risk_level"] == "high"
    assert one(client, BANKRUPT_INN, risk={"bankruptcy": False})["risk_level"] == "low"


def test_counterparty_cache_and_stale_when_source_down(client, reg, monkeypatch):
    first = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    assert first["cached"] is False
    assert client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()["sources"]["egrul"] == "ok"
    reg.down = True
    monkeypatch.setenv("FNS_CACHE_TTL", "0")
    out = client.get("/internal/v1/fns/counterparty", params={"inn": OK_INN}).json()
    assert out["stale"] is True and out["status"] == "active"


def test_counterparty_unavailable_without_cache_is_reported_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setenv("FNS_MIN_INTERVAL", "0")
    down = httpx.MockTransport(lambda req: httpx.Response(503))
    d = Deps(store=Store(tmp_path / "x.db"), public_transport=down, gov_transport=down)
    out = counterparty.check(d, make_inn("7791"))
    assert out["source"] == "unavailable" and out["risk_level"] == "unknown" and out["reason"]


def test_batch_dedup_limit_invalid_and_settings_echo(client):
    body = client.post("/internal/v1/fns/counterparties/batch", json={"inns": [OK_INN, OK_INN, DEBT_INN], "settings": {"risk": {"tax_debt": False, "nope": 1}}}).json()
    assert body["requested"] == 2 and len(body["items"]) == 2 and not body["partial"]
    assert body["settings"]["risk"]["tax_debt"] is False and "nope" not in body["settings"]["risk"]
    assert client.post("/internal/v1/fns/counterparties/batch", json={"inns": ["abc"]}).status_code == 422
    assert client.post("/internal/v1/fns/counterparties/batch", json={"inns": [OK_INN] * 51}).status_code == 422


def test_bad_checksum_rejected_without_network(client, reg):
    reg.calls.clear()
    out = client.get("/internal/v1/fns/counterparty", params={"inn": "7701234567"}).json()
    assert out["status"] == "invalid_inn" and out["reason"] == "inn_checksum_invalid" and reg.calls == []


# --- ЕНС: только из загруженной справки ---

def test_ens_without_import_is_404_with_hint(client):
    r = client.get("/internal/v1/fns/ens", params={"inn": OK_INN})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "fns_ens_not_imported"


def b64(s: str | bytes) -> str:
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


CSV = "as_of;balance;debt;penalty;due_date;due_amount\n2026-10-20;-12 500,50;12500,50;300;2026-10-28;85000\n;;;;2026-11-28;90000,00\n"


def test_import_csv_then_ens_uses_snapshot(client):
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
    info = client.get("/internal/v1/fns/info").json()
    assert info["ens"] == "import" and info["debt_index"]["state"] == "ready" and info["fedresurs_configured"] is False


def test_polite_http_streams_and_decodes_gzip_once():
    from fns.http import PoliteHttp
    t = httpx.MockTransport(lambda req: httpx.Response(200, content=gzip.compress(b'{"ok": true}'), headers={"content-encoding": "gzip"})
                            if req.url.path != "/robots.txt" else httpx.Response(404))
    r = PoliteHttp(transport=t, min_interval=0).request("GET", "https://example.ru/x")
    assert r.status_code == 200 and r.json() == {"ok": True}
