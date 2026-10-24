"""Тесты llm_gateway: маскирование, бюджет, аудит, отказ без ключа, провайдеры через httpx.MockTransport."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway import store
from llm_gateway.app import app
from llm_gateway.mask import Masker

TEXT = (
    "Оплата от ООО «Ромашка» (ИНН 7707083893), счёт 40702810400000012345, карта 4276 1234 5678 9012, "
    "контакт Иванов Пётр Сергеевич, ip@example.com, +7 (918) 123-45-67. Сумма 1500000 руб."
)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_GATEWAY_DB", str(tmp_path / "llm_gateway.db"))
    for k in ("LLM_PROVIDER", "LLM_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "LLM_DAILY_TOKEN_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    app.state.transport = None
    yield
    app.state.transport = None


@pytest.fixture
def client():
    return TestClient(app)


def body(content="привет", **kw):
    return {"user": "u1", "messages": [{"role": "user", "content": content}], **kw}


# ---------- маскирование ----------
def test_masking_roundtrip():
    m = Masker()
    masked = m.mask(TEXT)
    for secret in ("Ромашка", "7707083893", "40702810400000012345", "4276 1234", "Иванов", "ip@example.com", "918"):
        assert secret not in masked, (secret, masked)
    assert "[КОНТРАГЕНТ_1]" in masked and "[ИНН_1]" in masked and "[СЧЁТ_1]" in masked
    assert "[КАРТА_1]" in masked and "[ФИО_1]" in masked
    assert "1500000" in masked  # суммы не трогаем
    assert m.unmask(masked) == TEXT


def test_same_value_same_token_and_protect_list():
    m = Masker(protect=["Ромашка-2", "Вектор"])
    masked = m.mask("ООО «Ромашка» и снова ООО «Ромашка», ещё вектор и Ромашка-2")
    # protect применяется первым (по убыванию длины), затем шаблоны организаций
    assert masked.count("[КОНТРАГЕНТ_3]") == 2
    assert "[КОНТРАГЕНТ_1]" in masked and "[КОНТРАГЕНТ_2]" in masked
    assert "Вектор" not in masked and "вектор" not in masked
    assert m.unmask("Позвоните [КОНТРАГЕНТ_3]") == "Позвоните ООО «Ромашка»"


def test_mask_json_and_unknown_token_left_alone():
    m = Masker()
    data = m.mask_json({"counterparty": "ИП Петров А.А.", "n": 5, "list": ["ИНН 7707083893"]})
    assert "Петров" not in json.dumps(data, ensure_ascii=False) and data["n"] == 5
    assert m.unmask_json(data) == {"counterparty": "ИП Петров А.А.", "n": 5, "list": ["ИНН 7707083893"]}
    assert m.unmask("[КОНТРАГЕНТ_9]") == "[КОНТРАГЕНТ_9]"


# ---------- отказ без ключа ----------
def test_not_configured_without_provider_and_without_key(client, monkeypatch):
    r = client.post("/internal/v1/chat", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "provider_not_configured"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    r = client.post("/internal/v1/chat", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "provider_not_configured"
    assert "ANTHROPIC_API_KEY" in r.json()["error"]["message"]
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    assert client.post("/internal/v1/chat", json=body()).json()["error"]["code"] == "provider_not_configured"
    st = client.get("/internal/v1/status").json()
    assert st["configured"] is False


def test_invalid_request(client):
    r = client.post("/internal/v1/chat", json={"user": "u", "messages": []})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


# ---------- провайдеры ----------
def mock(handler):
    app.state.transport = httpx.MockTransport(handler)


def test_openai_masks_outgoing_and_restores_incoming(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Напомните [КОНТРАГЕНТ_1] (ИНН [ИНН_1])", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": json.dumps({"counterparty": "[КОНТРАГЕНТ_1]"})}}]}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10},
        })

    mock(handler)
    r = client.post("/internal/v1/chat", json=body(TEXT, tools=[{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]))
    assert r.status_code == 200, r.text
    sent = json.dumps(seen["body"], ensure_ascii=False)
    for secret in ("Ромашка", "7707083893", "40702810400000012345", "Иванов", "ip@example.com"):
        assert secret not in sent
    assert seen["url"].startswith("https://api.openai.com/") and seen["auth"] == "Bearer test-key"
    j = r.json()
    assert j["message"]["content"] == "Напомните ООО «Ромашка» (ИНН 7707083893)"
    assert json.loads(j["message"]["tool_calls"][0]["function"]["arguments"]) == {"counterparty": "ООО «Ромашка»"}
    assert j["usage"]["total_tokens"] == 50 and j["provider"] == "openai"


@pytest.mark.parametrize("provider,key_env,host", [
    ("gemini", "GEMINI_API_KEY", "generativelanguage.googleapis.com"),
    ("openrouter", "OPENROUTER_API_KEY", "openrouter.ai"),
])
def test_openai_compatible_providers(client, monkeypatch, provider, key_env, host):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv(key_env, "k")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    seen = {}

    def handler(req):
        seen["host"], seen["body"] = req.url.host, json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ок"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    mock(handler)
    r = client.post("/internal/v1/chat", json=body())
    assert r.json()["message"]["content"] == "ок"
    assert seen["host"] == host and seen["body"]["model"] == "my-model"


def test_anthropic_translation(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    seen = {}

    def handler(req):
        seen["h"], seen["body"] = req.headers, json.loads(req.content)
        return httpx.Response(200, json={
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Смотрю"},
                        {"type": "tool_use", "id": "tu1", "name": "get_x", "input": {"days": 7}}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    mock(handler)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "get_x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "{\"v\":1}"},
        {"role": "tool", "tool_call_id": "b", "content": "{\"v\":2}"},
    ]
    r = client.post("/internal/v1/chat", json={"user": "u1", "messages": messages, "tools": [
        {"type": "function", "function": {"name": "get_x", "description": "d", "parameters": {"type": "object", "properties": {}}}}]})
    assert r.status_code == 200, r.text
    assert seen["h"]["x-api-key"] == "ak" and seen["h"]["anthropic-version"] == "2023-06-01"
    b = seen["body"]
    assert b["system"] == "sys" and b["model"] == "claude-sonnet-5-5" and b["tools"][0]["input_schema"]["type"] == "object"
    assert b["messages"][1]["content"][0]["type"] == "tool_use"
    assert [c["type"] for c in b["messages"][2]["content"]] == ["tool_result", "tool_result"]
    j = r.json()
    assert j["message"]["content"] == "Смотрю"
    assert j["message"]["tool_calls"][0]["function"] == {"name": "get_x", "arguments": "{\"days\": 7}"}
    assert j["usage"]["total_tokens"] == 120


def test_provider_unavailable(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    mock(lambda req: httpx.Response(500, text="boom"))
    r = client.post("/internal/v1/chat", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "provider_unavailable"

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    mock(timeout)
    r = client.post("/internal/v1/chat", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "provider_unavailable"
    assert "k" != r.json()["error"]["message"]


# ---------- бюджет и аудит ----------
def test_budget_and_audit(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key-123")
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100")
    mock(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 60, "completion_tokens": 30}}))
    r = client.post("/internal/v1/chat", json=body())
    assert r.status_code == 200 and r.json()["budget"] == {"limit": 100, "used": 90}
    r = client.post("/internal/v1/chat", json=body("x" * 300))  # 90 + оценка > 100
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded"
    # другой пользователь не затронут
    r = client.post("/internal/v1/chat", json={**body(), "user": "u2"})
    assert r.status_code == 200
    assert client.get("/internal/v1/status", params={"user": "u1"}).json()["used_today"] == 90

    rows = client.get("/internal/v1/audit").json()["items"]
    assert {(x["user"], x["status"]) for x in rows} == {("u1", "ok"), ("u1", "budget_exceeded"), ("u2", "ok")}
    dump = json.dumps(rows, ensure_ascii=False)
    assert "xxx" not in dump and "secret-key-123" not in dump and "привет" not in dump
    assert set(rows[0]) == {"ts", "user", "provider", "model", "tokens", "status"}


def test_budget_zero_means_unlimited(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "0")
    mock(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 9999, "completion_tokens": 1}}))
    assert client.post("/internal/v1/chat", json=body()).status_code == 200
    assert client.post("/internal/v1/chat", json=body()).status_code == 200


def test_unconfigured_attempts_are_audited(client):
    client.post("/internal/v1/chat", json=body())
    rows = store.audit_rows()
    assert rows[0]["status"] == "provider_not_configured" and rows[0]["tokens"] == 0
