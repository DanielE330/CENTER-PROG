import asyncio
import json

import httpx
from advisor.runtime import BuiltinRuntime

GAPS = {"gaps": [{"start_date": "2026-07-28", "probability": 0.6, "shortfall_p50_kop": 41_000_000}]}


def fake(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if request.url.path == "/mcp":
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}}
            )
        if method == "tools/list":
            tool = {"name": "get_gaps", "description": "gaps", "inputSchema": {"type": "object", "properties": {}}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [tool]}})
        text = json.dumps(GAPS)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}]}}
        )
    # LLM: first ask for a tool, then answer from its result.
    tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
    if not tool_msgs:
        call = {"id": "c1", "type": "function", "function": {"name": "get_gaps", "arguments": "{}"}}
        msg = {"role": "assistant", "content": "", "tool_calls": [call]}
    else:
        gap = json.loads(tool_msgs[-1]["content"])["gaps"][0]
        msg = {"role": "assistant", "content": f"Разрыв {gap['start_date']}"}
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def test_builtin_loop_calls_tool_and_answers_from_it():
    rt = BuiltinRuntime(transport=httpx.MockTransport(fake))
    turn = asyncio.run(rt.run("что будет?", [], "http://llm/v1", "http://tools/mcp"))
    assert turn.tools_used == ["get_gaps"]
    assert "2026-07-28" in turn.answer


def test_system_prompt_forbids_following_instructions_from_data():
    from advisor.runtime import SYSTEM_PROMPT

    assert "не инструкции" in SYSTEM_PROMPT
    assert "propose_obligation" in SYSTEM_PROMPT
