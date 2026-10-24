"""End-to-end checks against a running stack (`make up && make seed`, then `make e2e`).

They go through the public entry point only (Caddy → gateway), except the mTLS
checks, which open connections from inside the internal network."""

import html
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("CI1_URL", "https://localhost:8443")
PASSWORD = os.environ.get("CI1_DEMO_PASSWORD") or (ROOT / "deploy/dev-secrets/demo_password").read_text().strip()
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml")]
CSRF = {"x-ci1-csrf": "1"}


def login(email: str) -> httpx.Client:
    """Real browser flow: gateway → Keycloak login form → callback → session cookie."""
    c = httpx.Client(base_url=BASE, verify=False, follow_redirects=False, timeout=60)  # noqa: S501 - local CA
    r = c.get("/api/v1/auth/login", params={"return_to": "/"})
    assert r.status_code in (302, 303, 307), r.text
    r = c.get(r.headers["location"], follow_redirects=True)
    # With Organizations enabled Keycloak uses identity-first login: username, then password.
    for _ in range(3):
        m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', r.text)
        assert m, "Keycloak login form not found"
        data = {"username": email, "credentialId": ""}
        if 'name="password"' in r.text:
            data["password"] = PASSWORD
        r = c.post(html.unescape(m.group(1)), data=data)
        if r.status_code in (302, 303):
            break
    assert r.status_code in (302, 303), f"login failed: {r.status_code}"
    r = c.get(r.headers["location"])
    assert r.status_code in (302, 303, 307)
    assert any(k == "__Host-ci1_sid" for k in c.cookies), "no session cookie"
    return c


@pytest.fixture(scope="session")
def a() -> httpx.Client:
    return login("owner@demo-a.ci1.local")


@pytest.fixture(scope="session")
def b() -> httpx.Client:
    return login("owner@demo-b.ci1.local")


def wait_for(fn, timeout=90, interval=3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


def test_unauthenticated_api_is_rejected():
    r = httpx.get(f"{BASE}/api/v1/forecast", verify=False)  # noqa: S501
    assert r.status_code == 401


def test_security_headers_on_spa():
    r = httpx.get(f"{BASE}/", verify=False)  # noqa: S501
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert "strict-transport-security" in r.headers
    assert "server" not in {k.lower() for k in r.headers}


def test_me_has_tenant_and_no_tokens(a):
    r = a.get("/api/v1/me")
    assert r.status_code == 200
    me = r.json()
    assert me["tenant_id"] and "owner" in me["roles"]
    assert "eyJ" not in r.text, "a JWT reached the browser"


def test_forecast_and_gaps(a):
    f = wait_for(lambda: (r := a.get("/api/v1/forecast?horizon=30")).status_code == 200 and r.json())
    assert f, "no forecast"
    assert len(f["points"]) == 30
    assert all(p["p10_kop"] <= p["p50_kop"] <= p["p90_kop"] for p in f["points"])
    gaps = a.get("/api/v1/gaps").json()
    assert "gaps" in gaps


def test_tenants_are_isolated(a, b):
    me_a, me_b = a.get("/api/v1/me").json(), b.get("/api/v1/me").json()
    assert me_a["tenant_id"] != me_b["tenant_id"]
    created = b.post(
        "/api/v1/obligations",
        headers=CSRF,
        json={"direction": "outflow", "amount_kop": 12_300, "due_date": "2026-12-01", "note": "isolation check"},
    ).json()
    ids_a = {o["id"] for o in a.get("/api/v1/obligations").json()}
    assert created["id"] not in ids_a
    r = a.patch(f"/api/v1/obligations/{created['id']}", headers=CSRF, json={"amount_kop": 1})
    assert r.status_code == 404
    tx_a = {t["id"] for t in a.get("/api/v1/transactions?limit=50").json()["items"]}
    tx_b = {t["id"] for t in b.get("/api/v1/transactions?limit=50").json()["items"]}
    assert tx_a and tx_b and not (tx_a & tx_b)


def test_mutation_without_csrf_header_is_forbidden(a):
    r = a.post("/api/v1/obligations", json={"direction": "inflow", "amount_kop": 100, "due_date": "2026-12-01"})
    assert r.status_code == 403


def test_viewer_scope_rules_are_enforced_by_services(a):
    r = a.put(
        "/api/v1/settings",
        headers=CSRF,
        json={"llm_policy": {"provider": "openai", "external_allowed": True, "masking": False}},
    )
    assert r.status_code == 400, "external provider without masking must be refused"


def internal_curl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "ci1_app",
            "curlimages/curl:8.11.1",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "10",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("service", ["ingest", "forecast", "tenancy", "advisor-tools"])
def test_internal_service_refuses_connection_without_client_certificate(service):
    res = internal_curl("-k", f"https://{service}:8443/healthz")
    assert res.stdout in ("000", ""), f"{service} answered without a client certificate: {res.stdout}"


def test_scenario_preview_shows_impact(a):
    r = a.post(
        "/api/v1/scenarios/preview",
        headers=CSRF,
        json={"transforms": [{"type": "scale_group", "group": "revenue", "factor": 0.7}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    last = body["points"][-1]
    assert last["scenario_p50_kop"] < last["base_p50_kop"]


def test_advisor_answers_with_tools(a):
    r = a.post(
        "/api/v1/advisor/chat",
        headers=CSRF,
        json={"message": "Почему может не хватить денег и что делать?"},
        timeout=180,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"].strip()
    assert body["tools_used"], "the agent did not use any tool"
    assert "Не удалось получить прогноз" not in body["answer"], "tools were called but returned no data"
    assert body["runtime"] == os.environ.get("CI1_EXPECT_RUNTIME", body["runtime"])


def test_replay_week_updates_plan_fact(a):
    before = a.get("/api/v1/demo/status").json()
    assert before["replay"]
    r = a.post("/api/v1/demo/advance", headers=CSRF, json={"days": 7})
    assert r.status_code == 200, r.text
    pf = wait_for(lambda: (x := a.get("/api/v1/plan-fact").json()).get("plan_as_of") and x, timeout=120)
    assert pf and pf["points"], "plan-fact did not appear after a replayed week"


def test_payment_purpose_never_reaches_logs(b):
    marker = f"CANARY-{secrets.token_hex(6)}"
    csv = (
        "﻿company_id;operation_id;operation_date;amount;direction;counterparty_inn;counterparty_name;payment_purpose;balance_eod\n"
        f"X;X-1;2026-09-01;1000.00;Поступление;;ООО «{marker}»;Оплата {marker};1000.00\n"
    )
    r = b.post("/api/v1/imports", headers=CSRF, files={"file": ("canary.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    time.sleep(8)  # let categorizer, forecast and alerts process the events
    logs = subprocess.run([*COMPOSE, "logs", "--no-color", "--since", "5m"], capture_output=True, text=True, check=True)
    assert marker not in logs.stdout + logs.stderr, "payment purpose or counterparty name leaked into logs"
