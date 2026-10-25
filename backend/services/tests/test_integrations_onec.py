"""Подключение 1С пользователем: реквизиты компании из ledger, проверка подключения на записанных ответах 1С:Фреш,
защита от SSRF (guard.py), ограничения ответа и частоты проверок. Сеть не используется: транспорты и DNS подменены."""
import base64
import json
import socket
import ssl
from pathlib import Path
from urllib.parse import unquote

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from integrator import connections, guard
from integrator.app import app
from integrator.clients import onec

FIX = Path(__file__).parent / "fixtures" / "onec_fresh"
URL = "https://msk1.1cfresh.com/a/ea/4269703"
SECRET = "S3cret-пароль-1С!"
PUBLIC = "93.184.216.34"


def resolver_to(*ips):
    def resolve(host, port, **kw):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return resolve


def basic(user="odata", password=SECRET) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def fresh(user="odata", password=SECRET, hits=None, publish=True, entities=None, status=None) -> httpx.MockTransport:
    """Запись 1С:Фреш с проверкой Basic-логина. `entities` — какие сущности опубликованы (None — все из фикстур)."""
    def handler(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append((req.url.query.decode(), req.headers.get("authorization")))
        if status:
            return httpx.Response(status)
        if not publish:
            return httpx.Response(404, text="<html>not found</html>")
        if req.headers.get("authorization") != basic(user, password):
            return httpx.Response(401, headers={"WWW-Authenticate": "Basic"})
        entity = unquote(req.url.path).rsplit("/", 1)[1]
        if entity == "standard.odata":
            return httpx.Response(200, json={"value": [{"name": "Catalog_Контрагенты"}]})
        f = FIX / f"{entity}.json"
        if not f.exists() or (entities is not None and entity not in entities):
            return httpx.Response(404, json={"odata.error": {"code": "8", "message": {"lang": "ru", "value": f"Сущность '{entity}' не найдена"}}})
        if int(req.url.params.get("$skip", "0")) > 0:
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json=json.loads(f.read_text(encoding="utf-8")))
    return httpx.MockTransport(handler)


def conn(**kw) -> connections.Connection:
    return connections.Connection(**{"url": URL, "username": "odata", "password": SECRET, **kw})


def check(transport, **kw):
    return connections.check(conn(**kw), transport, resolver_to(PUBLIC))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("INTEGRATIONS_ALLOW_HTTP", "INTEGRATIONS_ALLOW_LOOPBACK", "INTEGRATIONS_CHECK_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INTEGRATOR_RETRY_PAUSE", "0")
    app.state.transports = {}
    app.state.resolver = resolver_to(PUBLIC)
    app.state.limiter = connections.RateLimiter(10, 60)
    yield
    app.state.transports = {}
    app.state.resolver = None


# ---------- адрес: форма ----------
def test_normalize_accepts_https_and_canonicalizes():
    assert guard.normalize_url("  HTTPS://Msk1.1CFresh.com/a/ea/4269703/ ") == URL
    assert guard.normalize_url("https://1c.example.ru:8443/base/") == "https://1c.example.ru:8443/base"
    assert guard.normalize_url("https://1c.example.ru:443") == "https://1c.example.ru"
    assert guard.normalize_url("https://бухгалтерия.рф/x") == "https://xn--80abgjpq0bofu1j.xn--p1ai/x"


@pytest.mark.parametrize("url,code", [
    ("http://msk1.1cfresh.com/a", "url_scheme"),
    ("ftp://msk1.1cfresh.com/a", "url_scheme"),
    ("//msk1.1cfresh.com/a", "url_scheme"),
    ("msk1.1cfresh.com/a", "url_scheme"),
    ("javascript:alert(1)", "url_scheme"),
    ("file:///etc/passwd", "url_scheme"),
    ("https://user:pw@msk1.1cfresh.com/a", "url_userinfo"),
    ("https://evil.com@10.0.0.1/", "url_userinfo"),
    ("https://localhost/a", "url_forbidden"),
    ("https://app.localhost/a", "url_forbidden"),
    ("https://ledger/a", "url_forbidden"),
    ("https://ledger:3002/a", "url_forbidden"),
    ("https://onec.internal/a", "url_forbidden"),
    ("https://printer.local/a", "url_forbidden"),
    ("https://127.0.0.1/a", "url_forbidden"),
    ("https://127.1/a", "url_forbidden"),
    ("https://2130706433/a", "url_forbidden"),
    ("https://0x7f.1/a", "url_forbidden"),
    ("https://10.0.0.5/a", "url_forbidden"),
    ("https://172.16.0.1/a", "url_forbidden"),
    ("https://192.168.0.10/a", "url_forbidden"),
    ("https://169.254.169.254/latest/meta-data", "url_forbidden"),
    ("https://[::1]/a", "url_forbidden"),
    ("https://[fc00::1]/a", "url_forbidden"),
    ("https://[::ffff:127.0.0.1]/a", "url_forbidden"),
    ("https://msk1.1cfresh.com:22/a", "url_forbidden"),
    ("https://msk1.1cfresh.com:3002/a", "url_forbidden"),
    ("https://msk1.1cfresh.com/a/../../x", "invalid_url"),
    ("https://msk1.1cfresh.com/a b", "invalid_url"),
    ("https://msk1.1cfresh.com\\@evil.com/", "invalid_url"),
    ("", "invalid_url"),
    ("https://", "invalid_url"),
    ("https://x.ru/" + "a" * 300, "invalid_url"),
])
def test_normalize_rejects_dangerous_or_wrong_urls(url, code):
    with pytest.raises(guard.GuardError) as e:
        guard.normalize_url(url)
    assert e.value.code == code


def test_http_and_ports_only_with_dev_switch(monkeypatch):
    monkeypatch.setenv("INTEGRATIONS_ALLOW_HTTP", "1")
    assert guard.normalize_url("http://msk1.1cfresh.com:8080/a") == "http://msk1.1cfresh.com:8080/a"
    with pytest.raises(guard.GuardError):  # частные адреса по-прежнему запрещены
        guard.normalize_url("http://10.0.0.1/a")
    with pytest.raises(guard.GuardError):
        guard.normalize_url("http://127.0.0.1:8004")
    monkeypatch.setenv("INTEGRATIONS_ALLOW_LOOPBACK", "1")
    assert guard.normalize_url("http://127.0.0.1:8004") == "http://127.0.0.1:8004"
    assert guard.validate_url("http://localhost:8004", resolver_to("127.0.0.1")) == "http://localhost:8004"
    for url in ("http://10.0.0.1/a", "http://169.254.169.254/latest", "http://192.168.1.1:8004", "http://[fc00::1]:8004", "http://ledger:3002", "http://printer.local/a"):
        with pytest.raises(guard.GuardError):  # разрешён только loopback: метаданные облака и частные сети по-прежнему закрыты
            guard.normalize_url(url)
    with pytest.raises(guard.GuardError):  # имя, ведущее в частную сеть, закрыто и в этом режиме
        guard.validate_url("http://app.example.com:8004", resolver_to("10.0.0.7"))
    monkeypatch.delenv("INTEGRATIONS_ALLOW_HTTP")
    with pytest.raises(guard.GuardError):  # LOOPBACK без HTTP ничего не снимает
        guard.normalize_url("https://127.0.0.1/a")


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("93.184.216.34", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("127.255.255.254", False), ("10.1.2.3", False), ("172.16.0.1", False), ("172.31.255.255", False),
    ("192.168.1.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("255.255.255.255", False), ("::1", False), ("::", False), ("fc00::1", False), ("fd12:3456::1", False), ("fe80::1", False),
    ("::ffff:10.0.0.1", False), ("::ffff:127.0.0.1", False), ("::ffff:8.8.8.8", True),
    ("2002:7f00:1::", False), ("2002:0a00:1::", False), ("64:ff9b::a00:1", False), ("64:ff9b::808:808", True),
    ("not-an-ip", False),
])
def test_is_public(ip, public):
    assert guard.is_public(ip) is public


# ---------- адрес: DNS ----------
def test_validate_url_checks_every_resolved_address():
    assert guard.validate_url(URL, resolver_to(PUBLIC, "2606:4700:4700::1111")) == URL
    for bad in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], [PUBLIC, "192.168.0.7"], ["::ffff:10.0.0.1"]):
        with pytest.raises(guard.GuardError) as e:
            guard.validate_url(URL, resolver_to(*bad))
        assert e.value.code == "url_forbidden", bad


def test_validate_url_dns_failure_is_unreachable():
    def nxdomain(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")
    with pytest.raises(guard.GuardError) as e:
        guard.validate_url(URL, nxdomain)
    assert e.value.code == "unreachable"
    with pytest.raises(guard.GuardError) as e:
        guard.validate_url(URL, resolver_to())
    assert e.value.code == "unreachable"


def test_guarded_transport_blocks_private_addresses_at_connect_time():
    """Защита работает на самом соединении: имя, разрешившееся в закрытый адрес после проверки (DNS rebinding), не откроется."""
    t = guard.guarded_transport(resolver_to("127.0.0.1"))
    with httpx.Client(transport=t, trust_env=False) as h, pytest.raises(guard.GuardError) as e:
        h.get("https://rebind.example.com/odata")
    assert e.value.code == "url_forbidden"
    # IP-литерал в запросе проверяется так же, без обращения к DNS
    with httpx.Client(transport=guard.guarded_transport(), trust_env=False) as h:
        for u in ("https://169.254.169.254/latest", "https://[::1]/", "https://10.0.0.1:8443/"):
            with pytest.raises(guard.GuardError):
                h.get(u)


def test_guarded_backend_connects_to_checked_ip_not_to_name():
    calls = []

    class Spy(httpcore.NetworkBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            calls.append((host, port))
            raise httpcore.ConnectError("stop")

        def connect_unix_socket(self, *a, **k):
            raise AssertionError

        def sleep(self, s):
            pass

    b = guard.GuardedBackend(resolver_to(PUBLIC), Spy())
    with pytest.raises(httpcore.ConnectError):
        b.connect_tcp("msk1.1cfresh.com", 443)
    assert calls == [(PUBLIC, 443)]
    with pytest.raises(guard.GuardError):
        guard.GuardedBackend(resolver_to("10.0.0.1"), Spy()).connect_tcp("x.example.com", 443)
    assert calls == [(PUBLIC, 443)]


# ---------- ограничения ответа ----------
def limited(handler, **kw):
    return httpx.Client(base_url="https://x.example.com", transport=guard.LimitedTransport(httpx.MockTransport(handler), **kw))


def test_redirects_are_not_followed_and_credentials_not_resent():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "https://10.0.0.1/steal"})
    with limited(handler) as h, pytest.raises(guard.GuardError) as e:
        h.get("/a", auth=("u", "p"))
    assert e.value.code == "redirect" and seen == ["https://x.example.com/a"]


def test_response_size_is_limited():
    with limited(lambda r: httpx.Response(200, content=b"x" * 5000), max_bytes=1000) as h, pytest.raises(guard.GuardError) as e:
        h.get("/a")
    assert e.value.code == "too_large"
    big = httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")
    with limited(lambda r: big, max_bytes=1000) as h, pytest.raises(guard.GuardError):
        h.get("/a")
    with limited(lambda r: httpx.Response(200, content=b"x" * 900), max_bytes=1000) as h:
        assert len(h.get("/a").content) == 900


def test_response_time_is_limited(monkeypatch):
    clock = iter([0.0, 100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(guard, "time", type("T", (), {"monotonic": staticmethod(lambda: next(clock))}))
    streamed = httpx.Response(200, stream=httpx.ByteStream(b"x" * 10))
    with limited(lambda r: streamed, max_seconds=5) as h, pytest.raises(guard.GuardError) as e:
        h.get("/a")
    assert e.value.code == "timeout"


def test_response_size_is_limited_without_content_length():
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                yield b"x" * 100

    with limited(lambda r: httpx.Response(200, stream=Chunks()), max_bytes=1000) as h, pytest.raises(guard.GuardError) as e:
        h.get("/a")
    assert e.value.code == "too_large"


# ---------- проверка подключения ----------
def test_check_ok_against_recorded_fresh_answers():
    hits = []
    r = check(fresh(hits=hits))
    assert r["ok"] and r["code"] == "ok" and r["source"] == "live"
    total = sum(len(json.loads((FIX / f"Document_СчетНаОплату{k}.json").read_text(encoding="utf-8"))["value"]) for k in ("Покупателю", "Поставщика"))
    assert r["documents"] == total and r["more"] is False
    assert "СписаниеСРасчетногоСчета" in r["optional_missing"]            # этого объекта нет в записи Фреша
    assert all("+" not in q for q, _ in hits)                              # пробел в запросе только %20
    assert all(a == basic() for _, a in hits)
    assert SECRET not in json.dumps(r, ensure_ascii=False)


@pytest.mark.parametrize("kw,code", [
    ({"password": "wrong"}, "bad_credentials"),
    ({"username": "other"}, "bad_credentials"),
])
def test_check_bad_credentials(kw, code):
    r = check(fresh(), **kw)
    assert (r["ok"], r["code"]) == (False, code)
    assert "логин" in r["message"].lower() and "wrong" not in json.dumps(r)


def test_check_forbidden_odata_not_published_and_missing_catalogs():
    assert check(fresh(status=403))["code"] == "no_access"
    r = check(fresh(publish=False))
    assert r["code"] == "odata_not_published" and "OData" in r["message"]
    r = check(fresh(entities={"Catalog_Контрагенты"}))
    assert r["code"] == "no_catalog_access" and set(r["missing"]) == {"СчетНаОплатуПокупателю", "СчетНаОплатуПоставщика"}
    assert "СчетНаОплатуПокупателю" in r["message"]
    assert check(fresh(status=500))["code"] == "bad_response"
    html = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}))
    assert check(html)["code"] == "odata_not_published"


def test_check_network_errors_are_explained_without_details():
    def boom(exc):
        def h(req):
            raise exc
        return httpx.MockTransport(h)

    assert check(boom(httpx.ConnectTimeout("t")))["code"] == "timeout"
    assert check(boom(httpx.ReadTimeout("t")))["code"] == "timeout"
    assert check(boom(httpx.ConnectError("refused 10.1.1.1:443")))["code"] == "unreachable"
    err = httpx.ConnectError("tls")
    err.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    r = check(boom(err))
    assert r["code"] == "tls_error" and "сертификат" in r["message"].lower()
    r = check(boom(httpx.ConnectError("refused 10.1.1.1:443")))
    assert "10.1.1.1" not in json.dumps(r)
    assert check(httpx.MockTransport(lambda req: httpx.Response(302, headers={"Location": "https://10.0.0.1/"})))["code"] == "redirect"


def test_check_refuses_forbidden_addresses_before_any_request():
    hits = []
    t = fresh(hits=hits)
    for url in ("http://msk1.1cfresh.com/a", "https://10.0.0.5/a", "https://localhost/a", "https://u:p@x.example.com/"):
        assert connections.check(conn(url=url), t, resolver_to(PUBLIC))["ok"] is False
    # имя, которое разрешается во внутренний адрес
    r = connections.check(conn(), t, resolver_to("10.0.0.5"))
    assert (r["ok"], r["code"]) == (False, "url_forbidden")
    assert hits == []


def test_connection_repr_hides_password():
    c = conn()
    assert SECRET not in repr(c) and SECRET not in str(c) and "***" in repr(c)


def test_explain_maps_http_codes_of_sync():
    assert connections.explain(onec.SourceError("GET /x: HTTP 401"))[0] == "bad_credentials"
    assert connections.explain(onec.SourceError("GET /x: HTTP 404"))[0] == "odata_not_published"
    assert connections.explain(onec.SourceError("GET /x: HTTP 500 (после 3 попыток)"))[0] == "bad_response"


# ---------- ограничение частоты ----------
def test_rate_limiter_window():
    rl = connections.RateLimiter(3, 60)
    assert [rl.allow("c1", t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert rl.allow("c2", 3) is True                 # другая компания считается отдельно
    assert rl.allow("c1", 61) is True                # окно прошло


# ---------- HTTP: проверка и синхронизация ----------
def ledger(creds=None, status=200, hits=None) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append((req.method, req.url.path, req.headers.get("x-actor")))
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "x", "message": "сервис данных: сбой"}})
        if creds is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "Не найдено"}})
        return httpx.Response(200, json=creds)
    return httpx.MockTransport(handler)


SAVED = {"url": URL, "username": "odata", "password": SECRET}


def test_test_endpoint_uses_saved_connection_and_service_actor():
    hits = []
    app.state.transports = {"ledger": ledger(SAVED, hits=hits), "onec": fresh()}
    r = TestClient(app).post("/internal/v1/onec/test", json={"tenant": "c1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert hits == [("GET", "/internal/v1/companies/c1/integrations/onec/credentials", "svc:integrator")]
    assert SECRET not in r.text


def test_test_endpoint_entered_values_and_password_reuse_rules():
    app.state.transports = {"ledger": ledger(SAVED), "onec": fresh(user="boss", password="new-pass")}
    c = TestClient(app)
    # введены все поля: сохранённое не нужно
    r = c.post("/internal/v1/onec/test", json={"tenant": "c1", "url": URL, "username": "boss", "password": "new-pass"})
    assert r.json()["ok"] is True
    # пароль пуст, адрес тот же: берётся сохранённый (в записи 1С он не подходит к «boss» — значит, действительно подставлен)
    r = c.post("/internal/v1/onec/test", json={"tenant": "c1", "username": "boss", "url": URL + "/"})
    assert r.json()["code"] == "bad_credentials"
    # другой адрес без пароля: сохранённый пароль на чужой сервер не уходит
    r = c.post("/internal/v1/onec/test", json={"tenant": "c1", "url": "https://other.example.org/a", "username": "odata"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "onec_password_required"
    # нет сохранённого подключения и нет полей
    app.state.transports = {"ledger": ledger(None), "onec": fresh()}
    r = c.post("/internal/v1/onec/test", json={"tenant": "c1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_configured"


def test_test_endpoint_rejects_ssrf_targets_with_clear_codes():
    app.state.transports = {"ledger": ledger(None), "onec": fresh()}
    c = TestClient(app)
    for url, code in (("http://msk1.1cfresh.com/a", "url_scheme"), ("https://169.254.169.254/", "url_forbidden"), ("https://localhost/", "url_forbidden")):
        j = c.post("/internal/v1/onec/test", json={"tenant": "c1", "url": url, "username": "u", "password": "p"}).json()
        assert (j["ok"], j["code"]) == (False, code), url
    app.state.resolver = resolver_to("10.0.0.9")
    j = c.post("/internal/v1/onec/test", json={"tenant": "c1", "url": URL, "username": "u", "password": "p"}).json()
    assert j["code"] == "url_forbidden"


def test_test_endpoint_is_rate_limited_per_company():
    app.state.transports = {"ledger": ledger(SAVED), "onec": fresh()}
    c = TestClient(app)
    codes = [c.post("/internal/v1/onec/test", json={"tenant": "c1"}).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
    assert c.post("/internal/v1/onec/test", json={"tenant": "c2"}).status_code == 200
    j = c.post("/internal/v1/onec/test", json={"tenant": "c1"}).json()
    assert j["error"]["code"] == "rate_limited"


def test_test_endpoint_does_not_echo_password_on_invalid_request():
    c = TestClient(app)
    r = c.post("/internal/v1/onec/test", json={"tenant": "bad tenant!", "password": SECRET})
    assert r.status_code == 422 and SECRET not in r.text and r.json()["error"]["code"] == "invalid_request"
    r = c.post("/internal/v1/onec/test", json={"tenant": "c1", "password": "x" * 1000})
    assert r.status_code == 422 and "xxxx" not in r.text


def test_ledger_down_is_reported_not_guessed():
    app.state.transports = {"ledger": ledger(status=503), "onec": fresh()}
    r = TestClient(app).post("/internal/v1/onec/test", json={"tenant": "c1"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ledger_unavailable"


def test_check_url_endpoint():
    c = TestClient(app)
    assert c.post("/internal/v1/onec/check-url", json={"url": URL + "/"}).json() == {"ok": True, "url": URL}
    r = c.post("/internal/v1/onec/check-url", json={"url": "https://192.168.1.5/a"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "url_forbidden"
    app.state.resolver = resolver_to("127.0.0.1")
    assert c.post("/internal/v1/onec/check-url", json={"url": URL}).status_code == 422


def test_obligations_of_connected_company_come_from_its_1c():
    hits = []
    app.state.transports = {"ledger": ledger(SAVED), "onec": fresh(hits=hits)}
    j = TestClient(app).get("/internal/v1/obligations", params={"company": "C001", "tenant": "c1", "as_of": "2026-09-29"}).json()
    assert j["onec"] == "live" and j["sources"]["onec"]["status"] == "ok"
    assert j["sources"]["edo"]["status"] == "off"                      # демо-ЭДО в базу настоящей 1С не подмешивается
    numbers = {i["number"] for i in j["items"]}
    assert {"BS-1001", "ПП-101"} <= numbers and all(i["source"] == "onec" for i in j["items"])
    assert all("+" not in q and a == basic() for q, a in hits)
    assert SECRET not in json.dumps(j, ensure_ascii=False)


def test_obligations_live_errors_are_explained():
    app.state.transports = {"ledger": ledger({**SAVED, "password": "wrong"}), "onec": fresh()}
    j = TestClient(app).get("/internal/v1/obligations", params={"tenant": "c1", "as_of": "2026-09-29"}).json()
    assert j["onec"] == "live" and j["sources"]["onec"]["status"] == "error" and j["sources"]["onec"]["code"] == "bad_credentials"
    assert j["items"] == []
    # адрес из ledger стал закрытым (DNS поменялся): не ходим
    app.state.transports = {"ledger": ledger(SAVED), "onec": fresh()}
    app.state.resolver = resolver_to("10.1.1.1")
    j = TestClient(app).get("/internal/v1/obligations", params={"tenant": "c1", "as_of": "2026-09-29"}).json()
    assert j["sources"]["onec"]["code"] == "url_forbidden"


def test_obligations_without_connection_keep_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("ONEC_URL", "http://onec:8004")
    edo = httpx.MockTransport(lambda req: httpx.Response(200, json={"documents": []}))
    # прежний путь: 1С по env (Basic demo/demo) и ЭДО по company; подключения у компании нет (ledger: 404)
    app.state.transports = {"ledger": ledger(None), "onec": fresh(user="demo", password="demo"), "edo": edo}
    monkeypatch.setattr(onec.base, "env", lambda n, d: "demo" if n in ("ONEC_USER", "ONEC_PASSWORD") else {"ONEC_URL": "http://onec:8004"}.get(n, d))
    j = TestClient(app).get("/internal/v1/obligations", params={"tenant": "c1", "as_of": "2026-09-29"}).json()
    assert j["onec"] == "sim" and j["sources"]["edo"]["status"] == "ok" and j["sources"]["onec"]["status"] == "ok"
    monkeypatch.undo()
    monkeypatch.setenv("ONEC_URL", "https://msk1.1cfresh.com/a/ea/1")
    assert connections.env_source() == "live"


def test_obligations_with_ledger_down_do_not_fall_back_to_sim():
    app.state.transports = {"ledger": ledger(status=503), "onec": fresh()}
    j = TestClient(app).get("/internal/v1/obligations", params={"tenant": "c1", "as_of": "2026-09-29"}).json()
    assert j["onec"] is None and j["sources"]["onec"]["code"] == "ledger_unavailable" and j["items"] == []
