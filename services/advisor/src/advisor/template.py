"""Answer without a model, used when no cloud provider is connected or it is unavailable.
The facts (gaps, forecast, category summary) come from the same MCP tools the agent uses,
so every figure on screen is traceable to the forecast."""

import json
from datetime import date

import httpx
from advisor.runtime import McpClient, Turn

TOOLS = ["get_gaps", "get_forecast", "get_category_summary"]


def rub(value: int | float) -> str:
    return f"{round(value):,}".replace(",", "\u00a0") + "\u00a0₽"


def day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def facts(forecast: dict, summary: dict) -> str:
    lines: list[str] = []
    if forecast.get("opening_rub") is not None:
        lines.append(f"Остаток сегодня ({day(forecast['as_of'])}): {rub(forecast['opening_rub'])}.")
    points = forecast.get("points", [])
    if points:
        low = min(points, key=lambda p: p["p50_rub"])
        lines.append(
            f"Самый низкий ожидаемый остаток за {len(points)} дней: {rub(low['p50_rub'])} ({day(low['date'])})."
        )
    groups = sorted(summary.get("groups", []), key=lambda g: g.get("outflow_rub", 0))[:3]
    if groups:
        top = ", ".join(f"{g['name']} ({rub(abs(g['outflow_rub']))})" for g in groups)
        lines.append(f"Крупнейшие расходы за {summary.get('days', 90)} дней: {top}.")
    return "\n".join(lines)


def answer(gaps: dict, forecast: dict, summary: dict) -> str:
    g = (gaps.get("gaps") or [None])[0]
    if not g:
        return "Разрывов в ближайшие недели не видно.\n\n" + facts(forecast, summary)
    drivers = ", ".join(f"{d['label']} ({rub(abs(d['amount_rub']))})" for d in g.get("drivers", [])[:3])
    why = f" На эти дни приходятся: {drivers}." if drivers else ""
    return (
        f"Ближайший риск — {day(g['start_date'])}: не хватит около {rub(g.get('shortfall_p50_rub', 0))} "
        f"(вероятность {round(g['probability'] * 100)}%).{why}\n\n"
        "Что можно сделать:\n"
        "1. Попросить предоплату или ускорить оплату у клиентов с открытыми счетами.\n"
        f"2. Перенести несрочные закупки на дату после {day(g['end_date'])}.\n"
        f"3. Заранее согласовать с банком короткий овердрафт на {rub(g.get('shortfall_p90_rub', 0))}."
    )


class TemplateRuntime:
    name = "template"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def run(self, message: str, history: list[dict], llm_url: str, mcp_url: str) -> Turn:
        async with httpx.AsyncClient(timeout=60, transport=self._transport) as http:
            mcp = McpClient(mcp_url, http)
            await mcp.tools()  # initialize the session
            gaps = json.loads(await mcp.call("get_gaps", {}))
            forecast = json.loads(await mcp.call("get_forecast", {"horizon": 30}))
            summary = json.loads(await mcp.call("get_category_summary", {"days": 90}))
        return Turn(answer=answer(gaps, forecast, summary), tools_used=TOOLS, runtime=self.name)
