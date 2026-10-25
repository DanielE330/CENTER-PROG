"""Тесты модуля gov на сохранённых образцах ответов (без сети)."""
import json
import time
import urllib.error
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gov.app as gov_app
import gov.service as service
from gov.client import GovClient
from gov.errors import FetchError, ParseError
from gov.parsers import calendar as cal
from gov.parsers import cbr, nalog

FX = Path(__file__).parent / "fixtures" / "gov"


def fx(name):
    return (FX / name).read_text("utf-8")


class FakeNet:
    """Подмена сети: url-подстрока -> тело."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return 200, "User-agent: *\nDisallow: /search/\n"
        for k, v in self.routes.items():
            if k in url:
                return 200, v
        raise urllib.error.HTTPError(url, 404, "nf", None, None)


def client(tmp_path, routes, **kw):
    return GovClient(cache_dir=tmp_path, ttl=kw.pop("ttl", 86400), min_interval=0,
                     opener=FakeNet(routes), **kw)


ROUTES = {"cbr.ru/hd_base": fx("cbr_keyrate.html"), "xmlcalendar.ru": fx("xmlcalendar_2026.json"),
          "nalog.gov.ru/rn77/calendar": fx("nalog_calendar.html"),
          "nalog.gov.ru/rn77/taxation/insprem": fx("nalog_insprem.html")}


# --- парсеры ---
def test_key_rate():
    r = cbr.parse_key_rate(fx("cbr_keyrate.html"))
    assert r.rate == 14.0 and r.as_of == "2026-10-23"
    assert r.history[0]["rate"] == 14.0
    assert r.previous_rate is None or r.previous_rate != r.rate
    assert [h["date"] for h in r.history] == sorted((h["date"] for h in r.history), reverse=True)


def test_key_rate_markup_change():
    with pytest.raises(ParseError):
        cbr.parse_key_rate("<html><body>новая вёрстка</body></html>")
    with pytest.raises(ParseError):
        cbr.parse_key_rate('<table class="data"><tr><th>Дата</th><th>Ставка</th></tr></table>')


def test_production_calendar():
    c = cal.parse_calendar(fx("xmlcalendar_2026.json"), 2026)
    assert "2026-01-01" in c.non_working and "2026-01-09" in c.transferred_off
    assert "2026-04-30" in c.short_days and "2026-04-30" not in c.non_working
    assert c.is_working(date(2026, 1, 12)) and not c.is_working(date(2026, 1, 1))
    assert not c.is_working(date(2026, 5, 11))
    assert c.statistic["workdays"] == 247
    assert 365 - len(c.non_working) == 247


def test_calendar_bad_input():
    with pytest.raises(ParseError):
        cal.parse_calendar("<html>", 2026)
    with pytest.raises(ParseError):
        cal.parse_calendar(fx("xmlcalendar_2026.json"), 2025)
    with pytest.raises(ParseError):
        cal.parse_calendar('{"year":2026,"months":[{"month":1,"days":"x"}]}', 2026)


def test_tax_calendar():
    ev = nalog.parse_tax_calendar(fx("nalog_calendar.html"))
    assert len(ev) == 113 and min(e.date for e in ev) == "2026-09-03"
    assert max(e.date for e in ev) == "2026-12-28"
    enp = [e for e in ev if e.date == "2026-09-25" and "enp" in e.categories]
    assert enp and "уведомление" in enp[0].text
    assert any("usn" in e.categories for e in ev) and any("ausn" in e.categories for e in ev) and any("nds" in e.categories for e in ev)
    assert any("insurance" in e.categories for e in ev)
    assert all("<" not in e.text for e in ev)


def test_tax_calendar_markup_change():
    with pytest.raises(ParseError):
        nalog.parse_tax_calendar("<html>календарь переехал</html>")
    with pytest.raises(ParseError):
        nalog.parse_tax_calendar("NalogCalendar([]);</script>")


def test_insurance():
    i = nalog.parse_insurance(fx("nalog_insprem.html"))
    assert i.rate_main_pct == 30.0 and i.rate_above_limit_pct == 15.1
    assert i.base_limits[2026] == 2979000 and i.base_limits[2025] == 2759000
    with pytest.raises(ParseError):
        nalog.parse_insurance("<p>пусто</p>")


# --- клиент ---
def test_cache_ttl_and_stale(tmp_path):
    c = client(tmp_path, ROUTES, ttl=100)
    u = "https://xmlcalendar.ru/data/ru/2026/calendar.json"
    a = c.get(u); b = c.get(u)
    assert not a.from_cache and b.from_cache
    assert [x for x in c._open.calls if x == u] == [u]  # одна загрузка
    # просрочка: кэш устарел, сеть упала -> отдаём stale
    p = c._path(u); d = json.loads(p.read_text()); d["fetched_at"] = time.time() - 1000
    p.write_text(json.dumps(d))
    c2 = client(tmp_path, {}, ttl=100)
    s = c2.get(u)
    assert s.stale and s.body == a.body


def test_fetch_error_without_cache(tmp_path):
    with pytest.raises(FetchError):
        client(tmp_path, {}).get("https://xmlcalendar.ru/data/ru/2026/calendar.json")


def test_robots_respected(tmp_path):
    c = client(tmp_path, ROUTES)
    with pytest.raises(FetchError, match="robots"):
        c.get("https://www.cbr.ru/search/?q=1")


# --- сервис и API ---
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_client", client(tmp_path, ROUTES))
    return TestClient(gov_app.app)


def test_api(api):
    assert api.get("/live").json() == {"status": "ok"}
    r = api.get("/internal/v1/gov/key-rate").json()
    assert r["rate"] == 14.0 and r["source"] == "cbr.ru"
    c = api.get("/internal/v1/gov/calendar?year=2026").json()
    assert c["statistic"]["workdays"] == 247
    t = api.get("/internal/v1/gov/tax-calendar?year=2026").json()
    assert t["coverage"]["from"] == "2026-09-03" and len(t["events"]) == 113
    t27 = api.get("/internal/v1/gov/tax-calendar?year=2027").json()
    assert t27["events"] == [] and "warning" in t27
    assert api.get("/internal/v1/gov/insurance").json()["rate_main_pct"] == 30.0


def test_api_errors(tmp_path, monkeypatch):
    routes = dict(ROUTES, **{"cbr.ru/hd_base": "<html>другая вёрстка</html>"})
    monkeypatch.setattr(service, "_client", client(tmp_path, routes))
    cl = TestClient(gov_app.app)
    r = cl.get("/internal/v1/gov/key-rate")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "gov_parse_error"
    monkeypatch.setattr(service, "_client", client(tmp_path / "x", {}))
    assert cl.get("/internal/v1/gov/calendar?year=2026").status_code == 503
    assert cl.get("/internal/v1/gov/calendar?year=1900").status_code == 422


# --- курсы валют ЦБ (XML_daily.asp) ---
def test_rates_parser_comma_and_nominal():
    r = cbr.parse_rates(fx("cbr_rates.xml"))
    assert r.date == "2026-10-24" and r.source == "cbr.ru" and not r.approx
    assert list(r.rates) == ["USD", "EUR", "CNY", "GBP", "KZT", "BYN", "TRY", "AED"]  # лишние (AUD, JPY, DZD) отброшены
    assert r.rates["USD"] == {"nominal": 1, "value": 80.5381}  # «80,5381»: запятая как десятичный разделитель
    assert r.rates["KZT"] == {"nominal": 100, "value": 17.9797}
    assert r.rates["TRY"]["nominal"] == 10


def test_rates_parser_rejects_bad_input():
    with pytest.raises(ParseError):
        cbr.parse_rates("<html>не xml")
    with pytest.raises(ParseError):
        cbr.parse_rates('<Other Date="24.10.2026"/>')
    with pytest.raises(ParseError):
        cbr.parse_rates('<ValCurs Date="24.10.2026"></ValCurs>')  # нет USD/EUR
    with pytest.raises(ParseError):
        cbr.parse_rates('<!DOCTYPE x [<!ENTITY a "b">]><ValCurs Date="24.10.2026"/>')
    bad = fx("cbr_rates.xml").replace("<Value>80,5381</Value>", "<Value>abc</Value>")
    with pytest.raises(ParseError):
        cbr.parse_rates(bad)
    zero = fx("cbr_rates.xml").replace("<Nominal>10</Nominal>", "<Nominal>0</Nominal>")
    with pytest.raises(ParseError):
        cbr.parse_rates(zero)


def test_rates_service_fresh_then_cached(tmp_path):
    c = client(tmp_path, {"XML_daily.asp": fx("cbr_rates.xml")})
    r = service.rates(c)
    assert r["date"] == "2026-10-24" and r["source"] == "cbr.ru"
    assert r["stale"] is False and r["approx"] is False and r["from_cache"] is False
    assert r["rates"]["EUR"]["value"] == 90.2429
    again = service.rates(c)
    assert again["from_cache"] is True and again["stale"] is False
    assert sum("XML_daily" in u for u in c._open.calls) == 1  # сутки обслуживаются из кэша


def test_rates_service_stale_cache_when_cbr_down(tmp_path):
    c = client(tmp_path, {"XML_daily.asp": fx("cbr_rates.xml")}, ttl=0)  # кэш сразу просрочен
    assert service.rates(c)["stale"] is False
    c._open.routes.clear()  # ЦБ отвечает 404
    r = service.rates(c)
    assert r["stale"] is True and r["approx"] is False and r["date"] == "2026-10-24"
    assert r["rates"]["USD"]["value"] == 80.5381


def test_rates_service_fallback_when_no_cache(tmp_path):
    c = client(tmp_path, {})
    r = service.rates(c)
    assert r["approx"] is True and r["date"] is None and r["source"] == "cbr.ru"
    assert set(r["rates"]) == set(cbr.SUPPORTED)
    assert all(v["nominal"] > 0 and v["value"] > 0 for v in r["rates"].values())
    assert r["rates"]["KZT"]["nominal"] == 100


def test_rates_service_fallback_on_unparseable_body(tmp_path):
    r = service.rates(client(tmp_path, {"XML_daily.asp": "<html>заглушка DDoS-защиты</html>"}))
    assert r["approx"] is True


def test_rates_endpoint(tmp_path, monkeypatch):
    c = client(tmp_path, {"XML_daily.asp": fx("cbr_rates.xml")})
    monkeypatch.setattr(service, "_client", c)
    monkeypatch.setattr(service, "get_client", lambda: c)
    res = TestClient(gov_app.app).get("/internal/v1/gov/rates")
    assert res.status_code == 200
    body = res.json()
    assert body["date"] == "2026-10-24" and body["rates"]["CNY"] == {"nominal": 1, "value": 12.0557}
    assert body["approx"] is False and body["stale"] is False
