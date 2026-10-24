"""Провайдеры облачных LLM через httpx (без SDK). Единый внутренний формат: сообщения и инструменты как в OpenAI
(role/content/tool_calls/tool_call_id, tools=[{type:function,function:{name,description,parameters}}]).
Ключи читаются только из переменных окружения при каждом запросе."""
import json
import os
from dataclasses import dataclass

import httpx

PROVIDERS = ("anthropic", "openai", "gemini", "openrouter")
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
# модель по умолчанию для провайдера; рекомендуется задавать LLM_MODEL явно
DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "openrouter": "anthropic/claude-sonnet-4.5",
}
BASE_URL = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Config:
    provider: str
    model: str
    key: str


@dataclass
class Completion:
    message: dict
    prompt_tokens: int
    completion_tokens: int


def current_config() -> Config:
    name = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if name in ("", "none", "template"):
        raise GatewayError(503, "provider_not_configured", "Провайдер LLM не выбран (LLM_PROVIDER)")
    if name not in PROVIDERS:
        raise GatewayError(503, "provider_not_configured", f"Неизвестный провайдер LLM: {name}")
    key = os.environ.get(KEY_ENV[name], "").strip()
    if not key:
        raise GatewayError(503, "provider_not_configured", f"Не задан ключ {KEY_ENV[name]}")
    model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL[name]
    return Config(name, model, key)


def status() -> dict:
    name = os.environ.get("LLM_PROVIDER", "").strip().lower()
    try:
        cfg = current_config()
        return {"configured": True, "provider": cfg.provider, "model": cfg.model}
    except GatewayError as e:
        return {"configured": False, "provider": name or None, "model": None, "reason": e.code}


def timeout() -> httpx.Timeout:
    total = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
    return httpx.Timeout(total, connect=min(5.0, total))


async def complete(client: httpx.AsyncClient, cfg: Config, messages: list[dict], tools: list[dict], max_tokens: int) -> Completion:
    try:
        if cfg.provider == "anthropic":
            return await _anthropic(client, cfg, messages, tools, max_tokens)
        return await _openai_like(client, cfg, messages, tools, max_tokens)
    except GatewayError:
        raise
    except httpx.HTTPError as e:
        # без текста исключения: в нём может быть URL с параметрами
        raise GatewayError(503, "provider_unavailable", f"Провайдер {cfg.provider} недоступен ({type(e).__name__})") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise GatewayError(502, "provider_unavailable", f"Провайдер {cfg.provider} вернул неожиданный ответ") from None


def _http_error(provider: str, status_code: int) -> GatewayError:
    if status_code in (401, 403):
        return GatewayError(503, "provider_unavailable", f"Провайдер {provider} отклонил ключ (HTTP {status_code})")
    return GatewayError(503, "provider_unavailable", f"Провайдер {provider} ответил HTTP {status_code}")


async def _openai_like(client: httpx.AsyncClient, cfg: Config, messages, tools, max_tokens) -> Completion:
    body: dict = {"model": cfg.model, "messages": messages, "stream": False}
    # OpenAI принимает max_completion_tokens, остальные OpenAI-совместимые API — max_tokens
    body["max_completion_tokens" if cfg.provider == "openai" else "max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
    resp = await client.post(BASE_URL[cfg.provider], json=body, headers={"authorization": f"Bearer {cfg.key}"})
    if resp.status_code >= 400:
        raise _http_error(cfg.provider, resp.status_code)
    data = resp.json()
    msg = data["choices"][0]["message"]
    out: dict = {"role": "assistant", "content": msg.get("content") or ""}
    calls = [
        {"id": c.get("id") or f"call_{i}", "type": "function",
         "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
        for i, c in enumerate(msg.get("tool_calls") or [])
    ]
    if calls:
        out["tool_calls"] = calls
    usage = data.get("usage") or {}
    return Completion(out, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        # подряд идущие сообщения одной роли (несколько tool_result) склеиваем
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(str(m.get("content") or ""))
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": str(m.get("content") or "")}])
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            if blocks:
                push("assistant", blocks)
        else:
            push("user", [{"type": "text", "text": str(m.get("content") or "")}])
    return "\n\n".join(s for s in system if s), out


async def _anthropic(client: httpx.AsyncClient, cfg: Config, messages, tools, max_tokens) -> Completion:
    system, msgs = _to_anthropic(messages)
    body: dict = {"model": cfg.model, "max_tokens": max_tokens, "messages": msgs}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in tools
        ]
    resp = await client.post(
        BASE_URL["anthropic"], json=body,
        headers={"x-api-key": cfg.key, "anthropic-version": "2023-06-01"},
    )
    if resp.status_code >= 400:
        raise _http_error("anthropic", resp.status_code)
    data = resp.json()
    text, calls = [], []
    for b in data["content"]:
        if b["type"] == "text":
            text.append(b["text"])
        elif b["type"] == "tool_use":
            calls.append({"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    out: dict = {"role": "assistant", "content": "".join(text)}
    if calls:
        out["tool_calls"] = calls
    u = data.get("usage") or {}
    return Completion(out, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0))
