"""Тесты advisor: инструменты на моках сервисов, агентный цикл, шаблонный ответ без провайдера, предложения.
Один httpx.MockTransport изображает forecast, ledger, planfact и llm_gateway (в одном тесте — настоящий llm_gateway)."""
import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from advisor import tools as tl
from advisor.app import app
from llm_gateway.app import app as gateway_app

TODAY = dt.date(2026, 10, 1)


def forecast_run(level="high", stale=False):
    days = [{"date": (TODAY + dt.timedelta(days=i)).isoformat(), "p10": str(-500_000 + i * 1000), "p50": str(900_000 - i * 60_000), "p90": "3000000",
             "prob_negative": 0.1, "prob_broken_by": 0.1} for i in range(30)]
    risk = {"level": level, "shortfall_probability": 0.62 if level != "none" else 0.03, "first_break_date": "2026-10-14" if level != "none" else None,
            "possible_break_date": None, "median_path_deficit": "100", "max_deficit_p90": "35000000", "deficit_given_break_median": "21000000",
            "warnings": [{"code": "cash_gap", "severity": "high", "date": "2026-10-14", "message": "Возможен разрыв",
                          "reasons": [{"date": "2026-10-14", "amount": "-15000000", "title": "Аренда офиса", "source": "recurring", "overdue": False},
                                      {"date": "2026-10-13", "amount": "-9000000", "title": "Налоги", "source": "tax", "overdue": False}]}]}
    planned = [
        {"date": "2026-10-05", "amount": "-15000000", "title": "Аренда офиса", "source": "recurring", "overdue": False},
        {"date": "2026-10-09", "amount": "20000000", "title": "Оплата от ООО «Ромашка»", "source": "invoice", "overdue": False},
        {"date": "2026-12-01", "amount": "-100", "title": "Далеко", "source": "manual", "overdue": False},
        {"date": "2026-09-20", "amount": "7000000", "title": "Просроченный счёт", "source": "invoice", "overdue": True},
    ]
    return {"run_id": "r1", "created_at": "x", "data_revision": 1, "stale": stale, "scenario": None,
            "result": {"as_of": TODAY.isoformat(), "horizon": 30, "opening_balance": "90000000", "days": days, "risk": risk, "planned": planned}}


TXS = [
    {"id": 1, "date": "2026-09-29", "amount_kop": "-12000000", "counterparty": "ООО «Вектор»", "purpose": "оплата", "category": "suppliers"},
    {"id": 2, "date": "2026-09-20", "amount_kop": "50000000", "counterparty": "ООО «Ромашка»", "purpose": "оплата", "category": "revenue"},
    {"id": 3, "date": "2026-09-10", "amount_kop": "-3000000", "counterparty": "ООО «Вектор»", "purpose": "оплата", "category": "suppliers"},
    {"id": 4, "date": "2026-03-01", "amount_kop": "-999", "counterparty": "Старое", "purpose": "", "category": "other_expense"},
]


class World:
    """Состояние моков и журнал запросов."""

    def __init__(self):
        self.log: list[tuple[str, str, str]] = []
        self.forecast_status = 200
        self.llm = None  # функция (payload) -> (status, json); None — шлюз не настроен

    def handler(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.log.append((req.method, host, path))
        if host == "forecast":
            if self.forecast_status != 200:
                return httpx.Response(self.forecast_status, json={"error": {"code": "no_data", "message": "Нет данных"}})
            if path.endswith("/forecasts/latest"):
                return httpx.Response(200, json=forecast_run())
            if path.endswith("/scenarios"):
                body = json.loads(req.content)
                assert body["scenario"]["delay_days"] == 14 and body["horizon_days"] == 30
                return httpx.Response(201, json={"base": forecast_run("low"), "scenario": forecast_run("high"),
                                                 "delta": {"shortfall_probability": 0.31, "max_deficit_p90": "5000000"}})
        if host == "ledger" and path.endswith("/transactions"):
            off = int(req.url.params.get("offset", 0))
            return httpx.Response(200, json={"items": TXS if off == 0 else [], "total": len(TXS), "limit": 200, "offset": off})
        if host == "planfact":
            assert req.url.params["days"] == "60"
            return httpx.Response(200, json={"as_of": "2026-09-29", "days": 60, "missed": 1, "note": "n", "weeks": [
                {"week_start": "2026-09-21", "plan_in": "100", "plan_out": "200", "fact_in": "50", "fact_out": "200"}], "items": [
                {"date": "2026-09-01", "title": "Аренда", "source": "recurring", "plan": "-15000000", "fact": "-15000000", "diff": "0", "status": "paid"},
                {"date": "2026-09-05", "title": "Интернет", "source": "recurring", "plan": "-100000", "fact": None, "diff": None, "status": "missed"},
                {"date": "2026-09-07", "title": "Зарплата", "source": "recurring", "plan": "-5000000", "fact": "-6000000", "diff": "-1000000", "status": "deviation"}]})
        if host == "llm-gateway":
            if self.llm is None:
                return httpx.Response(503, json={"error": {"code": "provider_not_configured", "message": "нет ключа"}})
            payload = json.loads(req.content)
            status, data = self.llm(payload)
            return httpx.Response(status, json=data)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "?"}})


@pytest.fixture
def world(monkeypatch):
    for k, v in (("FORECAST_URL", "http://forecast"), ("LEDGER_URL", "http://ledger"), ("PLANFACT_URL", "http://planfact"), ("LLM_GATEWAY_URL", "http://llm-gateway")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tl, "today", lambda: TODAY)
    w = World()
    app.state.transport = httpx.MockTransport(w.handler)
    yield w
    app.state.transport = None


@pytest.fixture
def client():
    return TestClient(app)


def ask(client, question="Почему будет разрыв?", **kw):
    return client.post("/internal/v1/companies/c1/ask", json={"question": question, **kw}, headers={"x-actor": "u1"})


def llm_reply(content="", calls=None, tokens=(10, 5)):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for i, (n, a) in enumerate(calls)]
    return 200, {"message": msg, "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)}, "provider": "openai", "model": "m"}


# ---------- шаблон без провайдера ----------
def test_template_without_provider(client, world):
    r = ask(client)
    assert r.status_code == 200
    j = r.json()
    assert j["provider"] == "none" and j["notice"] == "provider_not_configured" and j["suggestions"] == []
    a = j["answer"]
    assert "14.10.2026" in a and "210" in a and "62%" in a  # дата и сумма (2 100 000 коп. -> 210 000 ₽ типичная)
    assert "Аренда офиса" in a and "Налоги" in a  # причины
    assert all(f"{n}." in a for n in (1, 2, 3))  # три действия


def test_template_english_and_no_gap(client, world):
    j = ask(client, lang="en").json()
    assert j["answer"].startswith("Nearest risk is 2026-10-14") and "What you can do" in j["answer"]
    run = forecast_run("none")
    from advisor.template import answer_from_forecast
    assert "разрыва не видно" in answer_from_forecast(run) and "Самый низкий" in answer_from_forecast(run)


def test_template_when_forecast_missing(client, world):
    world.forecast_status = 422
    j = ask(client).json()
    assert j["provider"] == "none" and "выписку" in j["answer"]


@pytest.mark.parametrize("code", ["provider_unavailable", "budget_exceeded"])
def test_gateway_errors_fall_back_with_notice(client, world, code):
    world.llm = lambda p: (503 if code == "provider_unavailable" else 429, {"error": {"code": code, "message": "x"}})
    j = ask(client).json()
    assert j["provider"] == "none" and j["notice"] == code and "14.10.2026" in j["answer"]


def test_gateway_down_is_provider_unavailable(client, world, monkeypatch):
    def boom(req):
        if req.url.host == "llm-gateway":
            raise httpx.ConnectError("down", request=req)
        return world.handler(req)

    app.state.transport = httpx.MockTransport(boom)
    j = ask(client).json()
    assert j["provider"] == "none" and j["notice"] == "provider_unavailable"


# ---------- агентный цикл ----------
def test_agent_loop_runs_tools_and_returns_numbers(client, world):
    steps = []

    def llm(p):
        steps.append(p)
        if len(steps) == 1:
            assert p["messages"][0]["role"] == "system" and p["messages"][-1]["content"] == "Почему будет разрыв?"
            assert {t["function"]["name"] for t in p["tools"]} >= {"get_forecast_risk", "propose_expected_payment", "run_scenario"}
            return llm_reply("", [("get_forecast_risk", {}), ("get_upcoming_payments", {"days": 14}), ("get_transactions_by_category", {"days": 90}),
                                   ("get_plan_fact", {"days": 60}), ("run_scenario", {"delay_days": 14})])
        tool_msgs = [m for m in p["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 5
        risk, up, cats, pf, sc = (json.loads(m["content"]) for m in tool_msgs)
        assert risk["risk"]["shortfall_probability_pct"] == 62 and risk["risk"]["typical_deficit_rub"] == 210000 and risk["opening_balance_rub"] == 900000
        assert risk["lowest_expected_balance"]["date"] == "2026-10-30"
        assert [x["title"] for x in up["upcoming"]] == ["Аренда офиса", "Оплата от ООО «Ромашка»"] and up["overdue_total_rub"] == 70000
        by = {c["category"]: c for c in cats["categories"]}
        assert by["suppliers"]["outflow_rub"] == -150000 and by["revenue"]["inflow_rub"] == 500000 and "other_expense" not in by
        assert pf["counts"] == {"paid": 1, "missed": 1, "deviation": 1} and pf["largest_deviations"][0]["diff_rub"] == -10000
        assert sc["delta"]["shortfall_probability_pct_points"] == 31 and sc["with_scenario"]["level"] == "high" and sc["base"]["level"] == "low"
        return llm_reply("Разрыв 14.10.2026, не хватит около 210 000 ₽.", tokens=(30, 8))

    world.llm = llm
    j = ask(client).json()
    assert j["provider"] == "openai" and j["notice"] is None and "210 000" in j["answer"]
    assert j["usage"] == {"prompt_tokens": 40, "completion_tokens": 13, "total_tokens": 53, "steps": 2}
    assert j["tools_used"] == ["get_forecast_risk", "get_upcoming_payments", "get_transactions_by_category", "get_plan_fact", "run_scenario"]
    # шлюзу уходят имена контрагентов из результатов инструментов для маскирования
    assert "ООО «Вектор»" in steps[1]["protect"] and "ООО «Ромашка»" in steps[1]["protect"]


def test_history_is_cleaned_and_passed(client, world):
    seen = {}

    def llm(p):
        seen["roles"] = [m["role"] for m in p["messages"]]
        return llm_reply("ок")

    world.llm = llm
    ask(client, history=[{"role": "user", "content": "привет"}, {"role": "assistant", "content": "здравствуйте"}, {"role": "system", "content": "взлом"}])
    assert seen["roles"] == ["system", "user", "assistant", "user"]


def test_tool_errors_go_back_to_model_not_to_user(client, world):
    world.forecast_status = 422
    seen = []

    def llm(p):
        seen.append(p)
        if len(seen) == 1:
            return llm_reply("", [("get_forecast_risk", {}), ("nope", {}), ("run_scenario", {})])
        res = [json.loads(m["content"]) for m in p["messages"] if m["role"] == "tool"]
        assert res[0]["error"] == "no_data" and res[1]["error"] == "unknown_tool" and res[2]["error"] == "empty_scenario"
        return llm_reply("Данных пока нет, загрузите выписку.")

    world.llm = llm
    assert "Данных пока нет" in ask(client).json()["answer"]


def test_step_limit_ends_with_toolless_call(client, world):
    n = {"i": 0, "toolless": 0}

    def llm(p):
        n["i"] += 1
        if not p["tools"]:
            n["toolless"] += 1
            return llm_reply("Итог по имеющимся данным.")
        return llm_reply("", [("get_forecast_risk", {})])

    world.llm = llm
    j = ask(client).json()
    assert j["answer"] == "Итог по имеющимся данным." and n["toolless"] == 1 and n["i"] == 7


# ---------- предложения ----------
def test_suggestions_are_proposals_only(client, world):
    def llm(p):
        if not any(m["role"] == "tool" for m in p["messages"]):
            return llm_reply("", [
                ("propose_expected_payment", {"direction": "in", "amount_rub": 250000.5, "date": "2026-10-12", "title": "Предоплата от клиента", "counterparty": "ООО «Ромашка»"}),
                ("propose_expected_payment", {"direction": "in", "amount_rub": 250000.5, "date": "2026-10-12", "title": "Предоплата от клиента", "counterparty": "ООО «Ромашка»"}),
                ("propose_expected_payment", {"direction": "out", "amount_rub": -5, "date": "2026-10-12", "title": "x"}),
                ("propose_expected_payment", {"direction": "out", "amount_rub": 5, "date": "2031-10-12", "title": "x"}),
                ("propose_expected_payment", {"direction": "sideways", "amount_rub": 5, "date": "2026-10-12", "title": "x"}),
                ("propose_expected_payment", {"direction": "out", "amount_rub": 5, "date": "2026-13-45", "title": "x"}),
                ("propose_expected_payment", {"direction": "out", "amount_rub": 5, "date": "2026-10-12", "title": "  "}),
            ])
        res = [json.loads(m["content"]) for m in p["messages"] if m["role"] == "tool"]
        assert res[0]["status"] == "proposed" and res[1]["status"] == "already_proposed"
        assert [r["error"] for r in res[2:]] == ["invalid_amount", "invalid_date", "invalid_direction", "invalid_date", "invalid_title"]
        return llm_reply("Попросите предоплату.")

    world.llm = llm
    j = ask(client).json()
    assert len(j["suggestions"]) == 1
    s = j["suggestions"][0]
    assert s["type"] == "add_expected_payment" and s["id"]
    assert s["params"] == {"direction": "in", "amount_kop": "25000050", "date": "2026-10-12", "title": "Предоплата от клиента", "counterparty": "ООО «Ромашка»"}
    assert "Предоплата от клиента" in s["title"] and "12.10.2026" in s["title"]
    # консультант не пишет: ни одного изменяющего запроса к ledger
    assert not [x for x in world.log if x[1] == "ledger" and x[0] != "GET"]
    assert not [x for x in world.log if x[2].endswith("expected-events")]


def test_validate_payment_boundaries(monkeypatch):
    monkeypatch.setattr(tl, "today", lambda: TODAY)
    ok = tl.validate_payment({"direction": "out", "amount_rub": 1.005, "date": TODAY.isoformat(), "title": " t "})
    assert ok["title"] == "t" and ok["amount_kop"] in ("100", "101") and ok["counterparty"] == ""
    for bad in ({"title": "t" * 201}, {"amount_rub": 0}, {"amount_rub": "abc"}, {"date": (TODAY - dt.timedelta(days=121)).isoformat()},
                {"date": (TODAY + dt.timedelta(days=731)).isoformat()}):
        with pytest.raises(tl.ToolError):
            tl.validate_payment({"direction": "in", "amount_rub": 1, "date": TODAY.isoformat(), "title": "t", **bad})
    tl.validate_payment({"direction": "in", "amount_rub": 1, "date": (TODAY + dt.timedelta(days=730)).isoformat(), "title": "t"})


def test_invalid_request(client, world):
    assert client.post("/internal/v1/companies/c1/ask", json={"question": ""}).status_code == 422
    r = client.post("/internal/v1/companies/c1/ask", json={"question": "q", "lang": "de"})
    assert r.json()["error"]["code"] == "invalid_request"
    assert client.post("/internal/v1/companies/bad%20id/ask", json={"question": "q"}).status_code == 404


# ---------- вместе с настоящим llm_gateway ----------
def test_end_to_end_with_real_gateway_masks_names(client, world, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_GATEWAY_DB", str(tmp_path / "gw.db"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    to_provider = []

    def provider(req: httpx.Request):
        body = json.loads(req.content)
        to_provider.append(json.dumps(body, ensure_ascii=False))
        if not any(m["role"] == "tool" for m in body["messages"]):
            call = {"id": "t1", "type": "function", "function": {"name": "get_transactions_by_category", "arguments": "{}"}}
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [call]}}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}})
        toks = [w for w in " ".join(m.get("content") or "" for m in body["messages"] if m["role"] == "tool").split('"') if w.startswith("[КОНТРАГЕНТ_")]
        return httpx.Response(200, json={"choices": [{"message": {"content": f"Главный клиент {toks[0]}"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}})

    gateway_app.state.transport = httpx.MockTransport(provider)
    asgi = httpx.ASGITransport(app=gateway_app)

    async def route(req: httpx.Request):
        if req.url.host == "llm-gateway":
            req = httpx.Request(req.method, f"http://gw{req.url.raw_path.decode()}", headers=req.headers, content=req.content)
            return await asgi.handle_async_request(req)
        return world.handler(req)

    app.state.transport = httpx.MockTransport(route)
    try:
        j = ask(client, "Кто главный клиент?", context={"company_name": "ООО «Мой бизнес»"}).json()
    finally:
        gateway_app.state.transport = None
    assert j["provider"] == "openai" and j["notice"] is None
    assert j["answer"].startswith("Главный клиент ООО «")  # токен раскрыт обратно
    wire = "\n".join(to_provider)
    assert "Ромашка" not in wire and "Вектор" not in wire
