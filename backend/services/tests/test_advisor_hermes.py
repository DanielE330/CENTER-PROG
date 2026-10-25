"""Консультант на Hermes Agent: пропуск вопроса, MCP-инструменты с правами, запись по праву, подсказки, откат на шаблон.
Hermes изображает заглушка: как настоящий агент, она берёт пропуск из системного сообщения и вызывает наш MCP-сервер."""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from advisor import tickets
from advisor import tools as tl
from advisor.app import app
from test_advisor import TODAY, TXS, forecast_run

MCP_TOKEN = "mcp-secret"


class Hermes:
    """Заглушка Hermes: план — список вызовов инструментов (имя, аргументы) и итоговый текст."""

    def __init__(self):
        self.plan: list[tuple[str, dict]] = []
        self.answer = "Разрыв 14.10: не хватит около 210 000 ₽."
        self.status = 200
        self.fail_text = ""  # Hermes вернул ошибку модели с HTTP 200
        self.results: list[dict] = []
        self.requests: list[dict] = []
        self.ticket = ""

    async def mcp(self, method: str, params: dict | None = None, token: str = MCP_TOKEN) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://advisor") as c:
            return await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                headers={"Authorization": f"Bearer {token}"})

    async def chat(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.requests.append({"headers": dict(req.headers), "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "down"})
        if self.fail_text:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": self.fail_text}, "finish_reason": "error"}],
                                             "hermes": {"failed": True, "error": self.fail_text}})
        system = body["messages"][0]["content"]
        self.ticket = re.search(r'ticket="(tkt_[^"]+)"', system).group(1)
        await self.mcp("initialize", {"protocolVersion": "2025-06-18"})
        for name, args in self.plan:
            r = (await self.mcp("tools/call", {"name": name, "arguments": {"ticket": self.ticket, **args}})).json()["result"]
            self.results.append({"name": name, "error": r["isError"], "data": json.loads(r["content"][0]["text"])})
        return httpx.Response(200, json={"model": "gpt-5.5", "choices": [{"message": {"role": "assistant", "content": self.answer}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


class World:
    def __init__(self):
        self.hermes = Hermes()
        self.events: list[dict] = []
        self.audit: list[dict] = []
        self.log: list[str] = []
        self.level = "high"

    async def handler(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.log.append(f"{req.method} {host}{path}")
        if host == "hermes" and path == "/v1/chat/completions":
            return await self.hermes.chat(req)
        if host == "hermes" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "hermes-agent"}]})
        if host == "forecast" and path.endswith("/forecasts/latest"):
            run = forecast_run(self.level)
            if self.level == "none":
                run["result"]["risk"]["first_break_date"] = "2026-11-10"  # дата пересечения нуля есть и при level none
            return httpx.Response(200, json=run)
        if host == "forecast" and path.endswith("/forecasts") and req.method == "POST":
            return httpx.Response(201, json=forecast_run("low") | {"run_id": "run2"})
        if host == "ledger" and path.endswith("/transactions"):
            return httpx.Response(200, json={"items": TXS, "total": len(TXS)})
        if host == "ledger" and path.endswith("/counterparties/top"):
            return httpx.Response(200, json={"items": [
                {"inn": "6166065050", "name": "ДОНАГРО ООО", "income_kop": 0, "expense_kop": 0, "expected_in_kop": 90_000_000, "income_share": 0.3,
                 "origin": ["expected", "onec"], "risk": {"risk_level": "medium", "status": "active", "tax_debt_kop": 496_997_838, "bankruptcy_flag": False}},
                {"inn": "2310031475", "name": "ТАНДЕР АО", "income_kop": 0, "income_share": 0.2, "risk": None}]})
        if host == "ledger" and path.endswith("/expected-events") and req.method == "POST":
            self.events.append(json.loads(req.content) | {"actor": req.headers.get("x-actor")})
            return httpx.Response(201, json={"id": f"ev{len(self.events)}"})
        if host == "ledger" and path.endswith("/audit"):
            self.audit.append(json.loads(req.content))
            return httpx.Response(204)  # как настоящий ledger: без тела
        if host == "llm-gateway":
            return httpx.Response(503, json={"error": {"code": "provider_not_configured", "message": "нет ключа"}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "?"}})


@pytest.fixture
def world(monkeypatch):
    for k, v in (("FORECAST_URL", "http://forecast"), ("LEDGER_URL", "http://ledger"), ("PLANFACT_URL", "http://planfact"),
                 ("LLM_GATEWAY_URL", "http://llm-gateway"), ("HERMES_AGENT_URL", "http://hermes"), ("HERMES_AGENT_KEY", "hk"),
                 ("ADVISOR_MCP_TOKEN", MCP_TOKEN)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tl, "today", lambda: TODAY)
    w = World()
    app.state.transport = httpx.MockTransport(w.handler)
    yield w
    app.state.transport = None


@pytest.fixture
def client():
    return TestClient(app)


READ_ONLY = ["read_forecast", "read_counterparties", "run_scenarios", "propose_payments"]
PAY = {"direction": "out", "amount_rub": 120000, "date": "2026-10-05", "title": "Аренда склада", "counterparty": "ООО «Склад»"}


def ask(client, perms, question="Почему будет разрыв?"):
    return client.post("/internal/v1/companies/c1/ask", json={"question": question, "permissions": perms, "context": {"company_name": "ООО «БЕЛОСНЕЖКА»"}},
                       headers={"x-actor": "u1", "x-request-id": "rid-1"})


def test_hermes_answers_with_tools_and_followups_within_permissions(client, world):
    world.hermes.plan = [("get_forecast_risk", {}), ("get_counterparty_risks", {"direction": "in"}),
                         ("add_expected_payment", PAY), ("propose_expected_payment", PAY),
                         ("suggest_followups", {"questions": ["Что если ДОНАГРО задержит оплату?", "Какие платежи перенести?"]})]
    j = ask(client, READ_ONLY).json()
    assert (j["provider"], j["model"], j["notice"]) == ("hermes", "gpt-5.5", None)
    assert j["answer"].startswith("Разрыв 14.10")
    res = {r["name"]: r for r in world.hermes.results}
    assert res["get_forecast_risk"]["data"]["risk"]["first_break_date"] == "2026-10-14"
    cp = res["get_counterparty_risks"]["data"]["counterparties"][0]
    assert cp["fns"]["tax_debt_rub"] == 4_969_978 and "ФНС" in res["get_counterparty_risks"]["data"]["source"]
    # запись не разрешена: инструмент отказал, в ledger ничего не ушло; предложение — можно
    assert res["add_expected_payment"]["error"] and res["add_expected_payment"]["data"]["error"] == "permission_denied"
    assert world.events == [] and j["actions"] == []
    assert len(j["suggestions"]) == 1 and j["suggestions"][0]["params"]["amount_kop"] == "12000000"
    assert j["followups"] == ["Что если ДОНАГРО задержит оплату?", "Какие платежи перенести?"]
    # системное сообщение: пропуск, разрешённое и запрещённое; новая сессия Hermes на вопрос, ключ памяти — компания
    sent = world.hermes.requests[0]
    system = sent["body"]["messages"][0]["content"]
    assert "добавлять платежи в план самому" in system.split("Запрещено:")[1] and "ООО «БЕЛОСНЕЖКА»" in system
    assert sent["headers"]["x-hermes-session-id"] == world.hermes.ticket and sent["headers"]["x-hermes-session-key"] == "company:c1"
    assert sent["headers"]["authorization"] == "Bearer hk"


def test_ticket_dies_after_answer(client, world):
    world.hermes.plan = [("get_forecast_risk", {})]
    ask(client, READ_ONLY)
    import asyncio
    r = asyncio.run(world.hermes.mcp("tools/call", {"name": "get_forecast_risk", "arguments": {"ticket": world.hermes.ticket}})).json()
    assert r["result"]["isError"] and "invalid_ticket" in r["result"]["content"][0]["text"]


def test_add_payment_and_recalc_with_write_permission(client, world):
    world.hermes.plan = [("add_expected_payment", PAY), ("recalc_forecast", {})]
    j = ask(client, [*READ_ONLY, "add_payments", "recalc_forecast"], "Добавь аренду склада 120 000 на 5 октября").json()
    assert len(world.events) == 1
    ev = world.events[0]
    assert (ev["title"], ev["amount_kop"], ev["direction"], ev["source"], ev["actor"]) == ("ИИ-консультант: Аренда склада", "12000000", "out", "manual", "u1")
    assert world.audit == [{"action": "advisor.add_payment", "object_id": "ev1", "outcome": "ok"}]
    assert j["actions"][0]["title"].startswith("Добавлен ожидаемый платёж в план: Аренда склада, −120")
    assert [(a["type"], a["id"]) for a in j["actions"]] == [("add_expected_payment", "ev1"), ("recalc_forecast", "run2")]
    assert all(not r["error"] for r in world.hermes.results), world.hermes.results
    assert "Можно добавлять платежи" not in world.hermes.requests[0]["body"]["messages"][0]["content"]


def test_mcp_requires_token_and_lists_tools_with_ticket(client, world):
    import asyncio
    h = world.hermes
    assert asyncio.run(h.mcp("tools/list", token="wrong")).status_code == 401
    init = asyncio.run(h.mcp("initialize", {"protocolVersion": "2025-03-26"})).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"]["tools"] == {"listChanged": False}
    tools = asyncio.run(h.mcp("tools/list")).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"get_forecast_risk", "add_expected_payment", "suggest_followups", "get_counterparty_risks"} <= names
    assert all(t["inputSchema"]["required"][0] == "ticket" for t in tools)
    r = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert r.status_code == 202
    assert TestClient(app).get("/mcp").status_code == 405


def test_ticket_of_one_company_reads_only_its_data(client, world):
    import asyncio
    t = tickets.issue("c2", "u9", "", "ru", ["read_forecast"])
    r = asyncio.run(world.hermes.mcp("tools/call", {"name": "get_forecast_risk", "arguments": {"ticket": t.id}})).json()
    assert not r["result"]["isError"]
    assert any("/companies/c2/" in line for line in world.log) and not any("/companies/c1/" in line for line in world.log)
    r = asyncio.run(world.hermes.mcp("tools/call", {"name": "get_transactions_by_category", "arguments": {"ticket": t.id}})).json()
    assert r["result"]["isError"] and "permission_denied" in r["result"]["content"][0]["text"]
    tickets.close(t.id)


def test_hermes_down_falls_back_to_template_with_notice(client, world):
    world.hermes.status = 502
    j = ask(client, READ_ONLY).json()
    assert j["provider"] == "none" and j["notice"] == "provider_unavailable" and "14.10.2026" in j["answer"]


def test_model_error_inside_200_is_not_an_answer(client, world):
    world.hermes.fail_text = "HTTP 400: The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
    j = ask(client, READ_ONLY).json()
    assert j["provider"] == "none" and j["notice"] == "provider_unavailable" and "not supported" not in j["answer"]


def test_prompts_follow_data_and_permissions(client, world):
    items = client.get("/internal/v1/companies/c1/prompts", params={"permissions": ",".join(READ_ONLY)}).json()["items"]
    texts = [i["text"] for i in items]
    assert texts[0].startswith("Как закрыть разрыв 14.10") and "210 000 ₽" in texts[0]
    assert any("ДОНАГРО ООО" in t and "не заплатит" in t for t in texts), texts
    assert len(items) <= 4
    world.level = "none"  # разрыва в горизонте не ждём: подсказка не обещает разрыв
    calm = client.get("/internal/v1/companies/c1/prompts", params={"permissions": "read_forecast"}).json()["items"]
    assert calm[0]["text"] == "Хватит ли денег до конца месяца?"
    world.level = "high"
    only = client.get("/internal/v1/companies/c1/prompts", params={"permissions": "read_operations"}).json()["items"]
    assert [i["kind"] for i in only] == ["general"] and not any("разрыв" in i["text"] for i in only)
    writer = client.get("/internal/v1/companies/c1/prompts", params={"permissions": "add_payments", "lang": "en"}).json()["items"]
    assert writer[0]["kind"] == "action" and "rent" in writer[0]["text"]


def test_engine_status(client, world):
    j = client.get("/internal/v1/engine").json()
    h = j["hermes"]
    assert j["engine"] == "hermes" and (h["configured"], h["available"], h["models"]) == (True, True, ["hermes-agent"])
    world.hermes.status = 502
    ask(client, READ_ONLY)
    assert client.get("/internal/v1/engine").json()["hermes"]["last"]["error"] == "provider_unavailable"
    assert {p["code"] for p in j["permissions"] if p["needs_edit"]} == {"add_payments", "recalc_forecast"}
