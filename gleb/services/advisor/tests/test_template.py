import asyncio
import json

import httpx
from advisor.template import TemplateRuntime

GAPS = {
    "gaps": [
        {
            "start_date": "2026-01-22",
            "end_date": "2026-01-30",
            "probability": 0.77,
            "shortfall_p50_rub": 4_654_437,
            "shortfall_p90_rub": 6_100_000,
            "drivers": [{"date": "2026-01-22", "label": "Зарплата", "amount_rub": -174_450}],
        }
    ]
}
FORECAST = {"as_of": "2025-12-24", "opening_rub": 1_812_026, "points": [{"date": "2026-01-22", "p50_rub": -1_200_000}]}
SUMMARY = {"days": 90, "groups": [{"name": "Закупки", "outflow_rub": -9_000_000}]}


def fake(gaps: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] in ("initialize", "tools/list"):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        data = {"get_gaps": gaps, "get_forecast": FORECAST, "get_category_summary": SUMMARY}[body["params"]["name"]]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
            },
        )

    return httpx.MockTransport(handler)


def run(gaps: dict):
    return asyncio.run(TemplateRuntime(fake(gaps)).run("?", [], "http://l/v1", "http://t/mcp"))


def test_template_names_the_gap_and_its_drivers():
    turn = run(GAPS)
    assert turn.runtime == "template"
    assert "22.01.2026" in turn.answer and "4\u00a0654\u00a0437" in turn.answer and "Зарплата" in turn.answer


def test_template_without_gaps_shows_facts():
    turn = run({"gaps": []})
    assert "Разрывов" in turn.answer and "Закупки" in turn.answer
