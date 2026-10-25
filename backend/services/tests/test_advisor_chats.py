"""Чаты консультанта: сохранение и контекст, владелец чата, модель и уровень размышления, режим «План» со стратегией."""
import json

import pytest
from fastapi.testclient import TestClient

from advisor import store
from advisor.app import app
from test_advisor_hermes import READ_ONLY, World, world  # noqa: F401 — фикстура world

PERMS = [*READ_ONLY, "make_plans"]
CO = "/internal/v1/companies/c1/advisor"


@pytest.fixture
def client(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ADVISOR_DB", str(tmp_path / "advisor.db"))
    monkeypatch.setenv("HERMES_AGENT_MODEL", "gpt-6.1-sol")
    world_models(world)
    return TestClient(app)


def world_models(w: World):
    ids = ["hermes-agent"] + [f"{m}{s}" for m in ("gpt-6.1-sol", "gpt-5.6-terra") for s in ("", "@low", "@medium", "@high", "@xhigh")]
    orig = w.handler

    async def handler(req):
        if req.url.host == "hermes" and req.url.path == "/v1/models":
            import httpx
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return await orig(req)

    import httpx
    app.state.transport = httpx.MockTransport(handler)


def h(user="u1"):
    return {"x-actor": user, "x-request-id": "rid"}


def send(client, chat_id, q, user="u1", perms=PERMS):
    return client.post(f"{CO}/chats/{chat_id}/messages", json={"question": q, "permissions": perms, "context": {"company_name": "ООО «БЕЛОСНЕЖКА»"}},
                       headers=h(user))


def test_models_from_subscription_routes(client):
    j = client.get(f"{CO}/models").json()
    assert j == {"models": ["gpt-6.1-sol", "gpt-5.6-terra"], "efforts": ["low", "medium", "high", "xhigh"],
                 "default_model": "gpt-6.1-sol", "default_effort": "medium"}


def test_chat_keeps_context_title_and_model_with_reasoning(client, world):  # noqa: F811
    c = client.post(f"{CO}/chats", json={"model": "gpt-5.6-terra", "reasoning": "high"}, headers=h()).json()
    assert (c["mode"], c["model"], c["reasoning"], c["title"]) == ("chat", "gpt-5.6-terra", "high", "Новый чат")
    world.hermes.plan = [("get_forecast_risk", {}), ("suggest_followups", {"questions": ["А что с налогами?"]})]
    r = send(client, c["id"], "Почему будет разрыв?").json()
    assert r["chat"]["title"] == "Почему будет разрыв?" and r["message"]["meta"]["followups"] == ["А что с налогами?"]
    assert r["message"]["meta"]["model"] == "gpt-5.6-terra" and r["message"]["meta"]["reasoning"] == "high"
    first = world.hermes.requests[-1]["body"]
    assert first["model"] == "gpt-5.6-terra@high" and len(first["messages"]) == 2  # system + вопрос
    world.hermes.plan = []
    send(client, c["id"], "А если клиент заплатит позже?")
    second = world.hermes.requests[-1]["body"]["messages"]
    # контекст чата: прошлый вопрос и ответ ушли в Hermes
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"] and second[1]["content"] == "Почему будет разрыв?"
    full = client.get(f"{CO}/chats/{c['id']}", headers=h()).json()
    assert [m["role"] for m in full["messages"]] == ["user", "assistant", "user", "assistant"]
    # смена модели в чате; неизвестная модель — ошибка; уровень размышления не меняется
    assert client.patch(f"{CO}/chats/{c['id']}", json={"model": "gpt-6.1-sol"}, headers=h()).json()["model"] == "gpt-6.1-sol"
    assert client.patch(f"{CO}/chats/{c['id']}", json={"model": "gpt-4o"}, headers=h()).json()["error"]["code"] == "invalid_model"
    send(client, c["id"], "Ещё вопрос")
    assert world.hermes.requests[-1]["body"]["model"] == "gpt-6.1-sol@high"


def test_chats_are_private_and_deletable(client):
    a = client.post(f"{CO}/chats", json={}, headers=h("u1")).json()
    client.post(f"{CO}/chats", json={}, headers=h("u2"))
    assert [c["id"] for c in client.get(f"{CO}/chats", headers=h("u1")).json()["items"]] == [a["id"]]
    assert client.get(f"{CO}/chats/{a['id']}", headers=h("u2")).status_code == 404
    assert send(client, a["id"], "q", user="u2").status_code == 404
    assert client.delete(f"{CO}/chats/{a['id']}", headers=h("u2")).status_code == 404
    assert client.delete(f"{CO}/chats/{a['id']}", headers=h("u1")).status_code == 204
    assert client.get(f"{CO}/chats", headers=h("u1")).json()["items"] == []
    assert client.post(f"{CO}/chats", json={"reasoning": "ultra"}, headers=h()).status_code == 422
    assert client.post(f"{CO}/chats", json={"model": "gpt-4o"}, headers=h()).json()["error"]["code"] == "invalid_model"


def test_plan_mode_saves_strategy_for_plan_fact(client, world):  # noqa: F811
    c = client.post(f"{CO}/chats", json={"mode": "plan"}, headers=h()).json()
    assert c["title"] == "Новая стратегия"
    steps = [{"title": "Попросить предоплату 30% у ДОНАГРО", "due_date": "2026-10-20", "amount_rub": 270000, "kind": "negotiation"},
             {"title": "Перенести закупку", "due_date": "bad", "amount_rub": "x"}]
    world.hermes.plan = [("get_forecast_risk", {}), ("save_strategy_plan", {"title": "Без минуса до декабря", "summary": "Держим резерв",
                                                                          "goal": "Остаток не ниже 300 000 ₽", "steps": steps, "risks": ["ДОНАГРО задержит"]})]
    r = send(client, c["id"], "Составь план, чтобы не уйти в минус").json()
    system = world.hermes.requests[-1]["body"]["messages"][0]["content"]
    assert "РЕЖИМ «ПЛАН»" in system and "налог" in system.lower()
    plan = client.get(f"{CO}/plans/active").json()["plan"]
    assert plan["title"] == "Без минуса до декабря" and plan["chat_id"] == c["id"] and r["message"]["meta"]["plan_id"] == plan["id"]
    assert plan["steps"][0] == {"title": "Попросить предоплату 30% у ДОНАГРО", "detail": "", "kind": "negotiation", "due_date": "2026-10-20", "amount_rub": 270000}
    assert plan["steps"][1]["due_date"] is None and plan["steps"][1]["amount_rub"] is None and plan["steps"][1]["kind"] == "control"
    # без права make_plans стратегия не сохраняется
    world.hermes.plan = [("save_strategy_plan", {"title": "Другой", "summary": "", "steps": [{"title": "x"}]})]
    send(client, c["id"], "Ещё план", perms=READ_ONLY)
    assert client.get(f"{CO}/plans/active").json()["plan"]["title"] == "Без минуса до декабря"
    assert client.delete(f"{CO}/plans/{plan['id']}").status_code == 204
    assert client.get(f"{CO}/plans/active").json()["plan"] is None


def test_app_guide_always_available(client, world):  # noqa: F811
    c = client.post(f"{CO}/chats", json={}, headers=h()).json()
    world.hermes.plan = [("get_app_guide", {"topic": "выписк"})]
    send(client, c["id"], "Как загрузить выписку?", perms=[])
    res = world.hermes.results[-1]
    assert not res["error"] and any(s["name"] == "Настройки" for s in res["data"]["screens"])


def test_plan_prompts(client):
    items = client.get("/internal/v1/companies/c1/prompts", params={"permissions": ",".join(PERMS), "mode": "plan"}).json()["items"]
    assert items and all(i["kind"] == "plan" for i in items)


def test_store_limits(tmp_path):
    s = store.Store(str(tmp_path / "x.db"))
    c = s.create("c", "u", "t", "chat", "m", "low")
    for i in range(store.MAX_MESSAGES + 5):
        s.add(c["id"], "user", str(i))
    msgs = s.messages(c["id"])
    assert len(msgs) == store.MAX_MESSAGES and msgs[-1]["content"] == str(store.MAX_MESSAGES + 4)
    assert json.dumps(s.chats("c", "u"))
