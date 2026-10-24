"""backoffice: операторы, сессии, лимит входа, прокси к ledger/forecast/auth на моках, бан с отзывом сессий, аудит."""
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

import backoffice.app as bo
from backoffice import create_operator as cli
from backoffice import store

P = "/internal/v1"
EMAIL = "ops@example.com"
PWD = "operator-pass-1"


def company(cid, name, **kw):
    base = {
        "id": cid, "name": name, "inn": "7700000000", "ogrn": "1027700000000", "created_at": "2026-10-20T10:00:00Z",
        "banned": False, "banned_at": None, "ban_reason": None, "members_count": 2, "transactions_count": 120,
        "last_transaction_date": "2026-10-01", "last_import_at": "2026-10-02T09:00:00Z", "events_count": 5,
        "director_email": f"boss@{cid}.ru",
        # лишнее поле, которого оператор видеть не должен
        "transactions": [{"purpose": "секрет"}],
    }
    base.update(kw)
    return base


class Mesh:
    """Одна httpx.MockTransport на ledger (:3002), forecast (:3003) и auth (:8001)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.companies = {
            "c1": company("c1", "Ромашка"),
            "c2": company("c2", "Лютик", transactions_count=0, banned=True, banned_at="2026-10-01T00:00:00Z", ban_reason="спам",
                          created_at="2026-01-01T00:00:00Z", members_count=1),
            "c3": company("c3", "Василёк", created_at="2026-09-30T00:00:00Z"),
        }
        self.members = {
            "c1": [{"user_id": "u1", "email": "boss@romashka.ru", "role": "director", "created_at": "2026-10-20"},
                   {"user_id": "u2", "email": "ed@romashka.ru", "role": "editor", "created_at": "2026-10-21"}],
            "c2": [{"user_id": "u3", "email": "x@lyutik.ru", "role": "director", "created_at": "2026-01-01"}],
            "c3": [],
        }
        self.fail_revoke: set[str] = set()
        self.forecast_down = False
        self.ledger_stats = False  # True: ledger отвечает на /admin/stats, иначе 404 и счётчики считаются постраничным обходом

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path + ("?" + req.url.query.decode() if req.url.query else ""), body or {}))
        assert req.headers.get("x-actor", "").startswith("operator:"), "актор должен пробрасываться"
        j = httpx.Response
        err = lambda s, c: j(s, json={"error": {"code": c, "message": c}})  # noqa: E731
        if req.url.port == 3003:
            if self.forecast_down:
                raise httpx.ConnectError("down")
            cid = path.split("/")[4]
            if cid == "c3":
                return err(404, "no_forecast")
            return j(200, json={"run_id": "r", "stale": False, "risk": {"level": "high", "shortfall_probability": 0.42}})
        if req.url.port == 8001:
            uid = path.split("/")[4]
            if uid in self.fail_revoke:
                return err(500, "boom")
            return j(204)
        # ledger
        if path == "/internal/v1/admin/stats":
            if not self.ledger_stats:
                return err(404, "not_found")
            return j(200, json={"companies_total": 7, "companies_active": 6, "companies_banned": 1, "new_7d": 2, "new_30d": 3,
                                "members_total": 11, "extra": "x"})
        if path == "/internal/v1/admin/companies":
            q = req.url.params
            items = list(self.companies.values())
            if q.get("banned") in ("true", "false"):
                items = [c for c in items if c["banned"] == (q["banned"] == "true")]
            if q.get("query"):
                items = [c for c in items if q["query"].lower() in c["name"].lower()]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
            return j(200, json={"total": len(items), "items": items[off:off + lim]})
        parts = path.split("/")
        if parts[3] == "admin" and len(parts) >= 5:
            cid = parts[5]
            if cid not in self.companies:
                return err(404, "not_found")
            c = self.companies[cid]
            if len(parts) == 6:
                return j(200, json={**c, "members": [{k: v for k, v in m.items() if k != "user_id"} for m in self.members[cid]]})
            if parts[6] == "ban":
                if c["banned"]:
                    return err(409, "already_banned")
                c.update(banned=True, ban_reason=body["reason"], banned_at="2026-10-24T00:00:00Z")
                return j(200, json={"ok": True})
            if parts[6] == "unban":
                c.update(banned=False, ban_reason=None, banned_at=None)
                return j(200, json={"ok": True})
        if parts[3] == "companies" and parts[5] == "members":
            if parts[4] not in self.members:
                return err(404, "not_found")
            return j(200, json={"items": self.members[parts[4]]})
        return err(404, "not_found")


@pytest.fixture
def mesh(monkeypatch):
    m = Mesh()
    monkeypatch.setattr(bo.app.state, "transport", httpx.MockTransport(m.handler), raising=False)
    return m


@pytest.fixture
def client(tmp_path, monkeypatch, mesh):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "sub" / "backoffice.db"))
    for k in ("OPERATOR_EMAIL", "OPERATOR_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    bo._fails.clear()
    bo._forecast_cache.clear()
    yield TestClient(bo.app)
    bo._fails.clear()
    bo._forecast_cache.clear()


def login(c, email=EMAIL, pwd=PWD):
    return c.post(f"{P}/operators/login", json={"email": email, "password": pwd})


@pytest.fixture
def op(client):
    created = store.create_operator(EMAIL, PWD)
    return {"x-actor": created["operator_id"], "x-request-id": "rid-1"}


# ---------- вход, сессии ----------
def test_login_session_logout(client):
    store.create_operator(" Ops@Example.com ", PWD)
    r = login(client, "OPS@example.com")
    assert r.status_code == 200
    d = r.json()
    assert d["email"] == EMAIL and d["token"] and d["ttl"] > 0 and d["operator_id"]
    s = client.post(f"{P}/operators/session", json={"token": d["token"]})
    assert s.status_code == 200 and s.json() == {"operator_id": d["operator_id"], "email": EMAIL}
    assert client.post(f"{P}/operators/logout", json={"token": d["token"]}).status_code == 204
    assert client.post(f"{P}/operators/session", json={"token": d["token"]}).status_code == 401


def test_session_unknown_and_expired(client):
    op_ = store.create_operator(EMAIL, PWD)
    assert client.post(f"{P}/operators/session", json={"token": "nope"}).json()["error"]["code"] == "invalid_session"
    token = store.issue_session(op_["operator_id"])
    with store.db() as conn:
        conn.execute("UPDATE operator_sessions SET expires_at=1")
    assert client.post(f"{P}/operators/session", json={"token": token}).status_code == 401


def test_token_stored_only_as_hash(client, tmp_path):
    store.create_operator(EMAIL, PWD)
    token = login(client).json()["token"]
    raw = sqlite3.connect(store.DB_PATH)
    dump = "\n".join(str(r) for t in ("operators", "operator_sessions", "operator_audit") for r in raw.execute(f"SELECT * FROM {t}"))
    assert token not in dump and PWD not in dump
    pwd_hash = raw.execute("SELECT pwd_hash FROM operators").fetchone()[0]
    assert len(pwd_hash) == 32  # scrypt dklen


def test_wrong_password_and_unknown_operator_look_the_same(client):
    store.create_operator(EMAIL, PWD)
    a = login(client, pwd="wrong-password-1")
    b = login(client, email="nobody@example.com")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()
    assert a.json()["error"]["code"] == "invalid_credentials"


def test_login_attempt_limit_blocks_even_right_password(client):
    store.create_operator(EMAIL, PWD)
    for _ in range(bo.MAX_FAILS):
        assert login(client, pwd="wrong-password-1").status_code == 401
    r = login(client)
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_attempts"
    # блокировка истекла: вход снова возможен, счётчик начинается заново
    bo._fails[EMAIL] = (bo.MAX_FAILS, 1.0)
    assert login(client).status_code == 200
    assert EMAIL not in bo._fails


def test_successful_login_resets_counter(client):
    store.create_operator(EMAIL, PWD)
    for _ in range(bo.MAX_FAILS - 1):
        login(client, pwd="wrong-password-1")
    assert login(client).status_code == 200
    assert login(client, pwd="wrong-password-1").status_code == 401


def test_session_count_limited(client):
    created = store.create_operator(EMAIL, PWD)
    tokens = [store.issue_session(created["operator_id"]) for _ in range(store.MAX_SESSIONS_PER_OPERATOR + 3)]
    with store.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operator_sessions").fetchone()[0] == store.MAX_SESSIONS_PER_OPERATOR
    assert store.session_operator(tokens[-1]) is not None


# ---------- создание оператора, bootstrap, CLI ----------
def test_create_operator_validation(client):
    with pytest.raises(store.StoreError) as e:
        store.create_operator("not-an-email", PWD)
    assert e.value.code == "invalid_email"
    with pytest.raises(store.StoreError) as e:
        store.create_operator(EMAIL, "short")
    assert e.value.code == "invalid_password"
    store.create_operator(EMAIL, PWD)
    with pytest.raises(store.StoreError) as e:
        store.create_operator(EMAIL.upper(), PWD)
    assert e.value.code == "email_taken"


def test_bootstrap_from_env(client, monkeypatch):
    assert store.bootstrap_from_env() == "skipped"
    monkeypatch.setenv("OPERATOR_EMAIL", EMAIL)
    monkeypatch.setenv("OPERATOR_PASSWORD", PWD)
    assert store.bootstrap_from_env() == "created"
    assert store.bootstrap_from_env() == "exists"
    assert login(client).status_code == 200
    # второй запуск не меняет пароль существующего оператора
    monkeypatch.setenv("OPERATOR_PASSWORD", "another-password-2")
    assert store.bootstrap_from_env() == "exists"
    assert login(client).status_code == 200
    monkeypatch.setenv("OPERATOR_EMAIL", "weak@example.com")
    monkeypatch.setenv("OPERATOR_PASSWORD", "short")
    assert store.bootstrap_from_env() == "rejected:invalid_password"


def test_bootstrap_runs_on_startup(tmp_path, monkeypatch, mesh):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("OPERATOR_EMAIL", EMAIL)
    monkeypatch.setenv("OPERATOR_PASSWORD", PWD)
    with TestClient(bo.app) as c:
        assert login(c).status_code == 200
        assert c.get("/live").json() == {"status": "ok"}


def test_cli_prompts_for_password(client, monkeypatch, capsys):
    answers = iter([PWD, PWD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["create_operator", "New@Example.com"]) == 0
    assert login(client, "new@example.com").status_code == 200
    # пароль нельзя передать аргументом
    assert cli.main(["create_operator", "a@b.ru", PWD]) == 2
    assert cli.main(["create_operator"]) == 2
    # несовпадение и слабый пароль
    answers = iter([PWD, "other-password-9"])
    assert cli.main(["create_operator", "x@example.com"]) == 1
    answers = iter(["short", "short"])
    assert cli.main(["create_operator", "y@example.com"]) == 1
    assert PWD not in capsys.readouterr().out


# ---------- доступ к admin ----------
def test_admin_requires_known_operator(client, op):
    for h in ({}, {"x-actor": "ghost"}):
        r = client.get(f"{P}/admin/companies", headers=h)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_session"
    assert client.get(f"{P}/admin/companies", headers=op).status_code == 200


# ---------- список, статистика, карточка ----------
def test_companies_list_with_risk_and_no_company_data(client, op, mesh):
    r = client.get(f"{P}/admin/companies", headers=op)
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3
    by = {c["id"]: c for c in d["items"]}
    assert by["c1"]["risk_level"] == "high" and by["c1"]["shortfall_probability"] == 0.42
    assert by["c2"]["risk_level"] is None and by["c2"]["shortfall_probability"] is None  # нет операций: прогноз не запрашивался
    assert by["c3"]["risk_level"] is None  # forecast ответил 404 no_forecast
    assert all("transactions" not in c for c in d["items"]), "содержимое данных компании не отдаётся"
    assert "секрет" not in r.text
    assert not any("/c2/summary" in p for _, p, _ in mesh.calls)


def test_companies_filters_passed_to_ledger(client, op, mesh):
    r = client.get(f"{P}/admin/companies", params={"query": "ром", "banned": "false", "limit": 10, "offset": 0}, headers=op)
    assert [c["id"] for c in r.json()["items"]] == ["c1"]
    sent = next(p for m, p, _ in mesh.calls if p.startswith("/internal/v1/admin/companies?"))
    assert "banned=false" in sent and "limit=10" in sent
    assert [c["id"] for c in client.get(f"{P}/admin/companies?banned=true", headers=op).json()["items"]] == ["c2"]


@pytest.mark.parametrize("qs", ["limit=0", "limit=1000", "offset=-1", "banned=maybe", "query=" + "x" * 101, "limit=abc"])
def test_companies_bad_params(client, op, qs):
    r = client.get(f"{P}/admin/companies?{qs}", headers=op)
    assert r.status_code == 422 and r.json()["error"]["code"] in ("invalid_request",)


def test_forecast_down_gives_null_not_error(client, op, mesh):
    mesh.forecast_down = True
    r = client.get(f"{P}/admin/companies", headers=op)
    assert r.status_code == 200
    assert {c["id"]: c["risk_level"] for c in r.json()["items"]} == {"c1": None, "c2": None, "c3": None}


def test_forecast_summary_is_cached(client, op, mesh):
    client.get(f"{P}/admin/companies", headers=op)
    client.get(f"{P}/admin/companies", headers=op)
    assert sum(1 for _, p, _ in mesh.calls if "/c1/summary" in p) == 1


def test_forecast_concurrency_is_limited(client, op, mesh, monkeypatch):
    import asyncio
    mesh.companies = {f"k{i}": company(f"k{i}", f"K{i}") for i in range(30)}
    state = {"now": 0, "max": 0}

    async def slow(self, svc, base_env, default, method, path, **kw):
        if svc != "forecast":
            return await orig(self, svc, base_env, default, method, path, **kw)
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return {"risk": {"level": "low", "shortfall_probability": 0.01}}

    orig = bo.Hop.call
    monkeypatch.setattr(bo.Hop, "call", slow)
    r = client.get(f"{P}/admin/companies?limit=30", headers=op)
    assert len(r.json()["items"]) == 30 and all(c["risk_level"] == "low" for c in r.json()["items"])
    assert 1 < state["max"] <= bo.FORECAST_CONCURRENCY


def test_stats(client, op, mesh, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    mesh.companies["c1"]["created_at"] = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mesh.companies["c3"]["created_at"] = (now - timedelta(days=20)).strftime("%Y-%m-%d")
    r = client.get(f"{P}/admin/stats", headers=op)
    assert r.status_code == 200
    assert r.json() == {"total": 3, "active": 2, "banned": 1, "new_7d": 1, "new_30d": 2, "users_total": 5}


def test_stats_from_ledger_endpoint(client, op, mesh):
    mesh.ledger_stats = True
    r = client.get(f"{P}/admin/stats", headers=op)
    assert r.json() == {"total": 7, "active": 6, "banned": 1, "new_7d": 2, "new_30d": 3, "users_total": 11}
    assert not any(p.startswith("/internal/v1/admin/companies") for _, p, _ in mesh.calls)


def test_stats_pages_through_ledger(client, op, mesh):
    mesh.companies = {f"k{i}": company(f"k{i}", f"K{i}", members_count=1) for i in range(450)}
    r = client.get(f"{P}/admin/stats", headers=op)
    assert r.json()["total"] == 450 and r.json()["users_total"] == 450


def test_company_card_has_members_without_user_ids(client, op):
    r = client.get(f"{P}/admin/companies/c1", headers=op)
    d = r.json()
    assert r.status_code == 200 and d["name"] == "Ромашка" and d["risk_level"] == "high"
    assert d["director_email"] == "boss@c1.ru"
    assert d["members"] == [{"email": "boss@romashka.ru", "role": "director", "created_at": "2026-10-20"},
                            {"email": "ed@romashka.ru", "role": "editor", "created_at": "2026-10-21"}]
    assert "transactions" not in d
    assert client.get(f"{P}/admin/companies/nope", headers=op).json()["error"]["code"] == "not_found"
    assert client.get(f"{P}/admin/companies/bad..id", headers=op).status_code == 404


def test_ledger_down_is_503(client, op, mesh, monkeypatch):
    def down(_r):
        raise httpx.ConnectError("x")
    monkeypatch.setattr(bo.app.state, "transport", httpx.MockTransport(down))
    r = client.get(f"{P}/admin/companies", headers=op)
    assert r.status_code == 503 and r.json()["error"]["code"] == "ledger_unavailable"


# ---------- бан / разбан ----------
def test_ban_revokes_all_member_sessions_and_audits(client, op, mesh):
    r = client.post(f"{P}/admin/companies/c1/ban", json={"reason": "  Нарушение\nусловий  "}, headers=op)
    assert r.status_code == 200
    assert r.json() == {"company_id": "c1", "banned": True, "ban_reason": "Нарушение условий", "sessions_revoked": 2, "revoke_failed": 0}
    assert mesh.companies["c1"]["banned"] and mesh.companies["c1"]["ban_reason"] == "Нарушение условий"
    revoked = [p for m, p, _ in mesh.calls if p.endswith("/revoke-sessions")]
    assert sorted(revoked) == ["/internal/v1/users/u1/revoke-sessions", "/internal/v1/users/u2/revoke-sessions"]
    log = store.audit_list()
    assert log[0]["action"] == "ban" and log[0]["company_id"] == "c1" and log[0]["operator"] == EMAIL
    assert "Нарушение условий" in log[0]["detail"] and "sessions_revoked=2" in log[0]["detail"]
    listed = client.get(f"{P}/admin/audit", headers=op).json()["items"]
    assert listed[0]["action"] == "ban"


def test_ban_counts_failed_revokes(client, op, mesh):
    mesh.fail_revoke = {"u2"}
    r = client.post(f"{P}/admin/companies/c1/ban", json={"reason": "причина"}, headers=op)
    assert r.status_code == 200 and (r.json()["sessions_revoked"], r.json()["revoke_failed"]) == (1, 1)
    assert mesh.companies["c1"]["banned"]
    assert "revoke_failed=1" in store.audit_list()[0]["detail"]


@pytest.mark.parametrize("reason", ["", "  ", "ab", "x" * 501, "bad\x00reason"])
def test_ban_requires_reason(client, op, mesh, reason):
    r = client.post(f"{P}/admin/companies/c1/ban", json={"reason": reason}, headers=op)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_reason"
    assert not mesh.companies["c1"]["banned"]
    assert not any(m == "POST" for m, _, _ in mesh.calls)
    assert client.post(f"{P}/admin/companies/c1/ban", json={}, headers=op).status_code == 422


def test_ban_errors_pass_through_and_are_audited(client, op, mesh):
    assert client.post(f"{P}/admin/companies/ghost/ban", json={"reason": "причина"}, headers=op).status_code == 404
    r = client.post(f"{P}/admin/companies/c2/ban", json={"reason": "причина"}, headers=op)
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_banned"
    assert not any(p.endswith("/revoke-sessions") for _, p, _ in mesh.calls), "при отказе ledger сессии не трогаем"
    assert store.audit_list()[0]["outcome"] == "already_banned"


def test_unban_and_audit(client, op, mesh):
    r = client.post(f"{P}/admin/companies/c2/unban", headers=op)
    assert r.status_code == 200 and r.json() == {"company_id": "c2", "banned": False}
    assert not mesh.companies["c2"]["banned"]
    assert store.audit_list()[0]["action"] == "unban"
    assert client.post(f"{P}/admin/companies/ghost/unban", headers=op).status_code == 404


def test_audit_has_logins_and_no_company_data(client, op):
    login(client)
    login(client, pwd="wrong-password-1")
    client.get(f"{P}/admin/companies", headers=op)
    actions = [(a["action"], a["outcome"]) for a in store.audit_list()]
    assert ("login", "ok") in actions and ("login", "invalid_credentials") in actions
    cols = {r[1] for r in sqlite3.connect(store.DB_PATH).execute("PRAGMA table_info(operator_audit)")}
    assert cols == {"id", "at", "operator_id", "operator_email", "action", "company_id", "detail", "outcome", "request_id"}
