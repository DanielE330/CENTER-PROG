import asyncio
import json

import httpx
from advisor.grounded import GroundedRuntime, facts_from, grounded_enough

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
FORECAST = {
    "as_of": "2025-12-24",
    "opening_rub": 1_812_026,
    "points": [{"date": "2026-01-22", "p50_rub": -1_200_000}, {"date": "2026-01-10", "p50_rub": 900_000}],
}
SUMMARY = {"days": 90, "groups": [{"name": "Закупки", "outflow_rub": -9_000_000}]}


def test_facts_include_gap_date_and_amounts():
    text, amounts, dates = facts_from(GAPS, FORECAST, SUMMARY)
    assert "22.01.2026" in text and dates == ["22.01.2026"]
    assert {4_654_437, 6_100_000, 174_450, 1_812_026} <= amounts


def test_answer_with_invented_amount_is_rejected():
    _, amounts, dates = facts_from(GAPS, FORECAST, SUMMARY)
    assert grounded_enough("22.01 не хватит 4 654 437 ₽", amounts, dates)
    assert not grounded_enough("22.01 не хватит 475 097 072 ₽", amounts, dates)
    assert not grounded_enough("Не хватит 4 654 437 ₽", amounts, dates), "the gap date is required"
    assert grounded_enough("22.01 не хватит около 4,7 млн", amounts, dates), "rounded fact is fine"
    assert not grounded_enough("22.01 не хватит около 4,2 млн и 467 тыс.", amounts, dates)


def fake(answer: str):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/mcp":
            if body["method"] in ("initialize", "tools/list"):
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
            name = body["params"]["name"]
            data = {"get_gaps": GAPS, "get_forecast": FORECAST, "get_category_summary": SUMMARY}[name]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    return httpx.MockTransport(handler)


def test_good_model_answer_is_kept():
    turn = asyncio.run(
        GroundedRuntime(fake("22.01 не хватит около 4 654 437 ₽ из-за зарплаты.")).run(
            "?", [], "http://l/v1", "http://t/mcp"
        )
    )
    assert turn.runtime == "grounded" and "4 654 437" in turn.answer


def test_bad_model_answer_falls_back_to_template():
    turn = asyncio.run(
        GroundedRuntime(fake("Увеличьте коммуналку на 50%, не хватит 475 097 072 ₽")).run(
            "?", [], "http://l/v1", "http://t/mcp"
        )
    )
    assert turn.runtime == "grounded-template"
    assert "22.01.2026" in turn.answer
