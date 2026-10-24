"""Grounded mode for the small local model. The loop fetches the facts itself
(gaps, forecast, category summary), the model only writes the text, and the text
is checked against the facts. If the check fails, a template built from the
same facts answers instead. Small models skip tools and invent numbers; this
keeps every figure on screen traceable to the forecast."""

import json
import logging
import re
from datetime import date

import httpx
from advisor.runtime import SYSTEM_PROMPT, McpClient, Turn

log = logging.getLogger("advisor.grounded")
NUMBER_RE = re.compile(r"\d[\d\s ]{2,}\d|\d{4,}")
MIN_CHECKED = 1000  # small numbers (days, percents) are not checked


def rub(value: int | float) -> str:
    return f"{round(value):,}".replace(",", " ") + " ₽"


def day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y")


def facts_from(gaps: dict, forecast: dict, summary: dict) -> tuple[str, set[int], list[str]]:
    """Text for the model, the amounts it may use and the dates it must mention."""
    lines: list[str] = []
    amounts: set[int] = set()
    must_dates: list[str] = []
    points = forecast.get("points", [])
    if forecast.get("opening_rub") is not None:
        lines.append(f"Остаток сегодня ({day(forecast['as_of'])}): {rub(forecast['opening_rub'])}.")
        amounts.add(round(forecast["opening_rub"]))
    if points:
        low = min(points, key=lambda p: p["p50_rub"])
        lines.append(
            f"Самый низкий ожидаемый остаток за {len(points)} дней: {rub(low['p50_rub'])} ({day(low['date'])})."
        )
        amounts.add(round(low["p50_rub"]))
    for g in gaps.get("gaps", [])[:2]:
        p50, p90 = g.get("shortfall_p50_rub", 0), g.get("shortfall_p90_rub", 0)
        lines.append(
            f"Разрыв с {day(g['start_date'])} по {day(g['end_date'])}: вероятность {round(g['probability'] * 100)}%, "
            f"не хватит около {rub(p50)}, в плохом случае до {rub(p90)}."
        )
        amounts.update({round(p50), round(p90)})
        for d in g.get("drivers", [])[:4]:
            lines.append(f"  В эти дни: {d['label']} {rub(abs(d['amount_rub']))} ({day(d['date'])}).")
            amounts.add(round(abs(d["amount_rub"])))
    if gaps.get("gaps"):
        must_dates.append(day(gaps["gaps"][0]["start_date"]))
    else:
        lines.append("Разрывов в горизонте прогноза не видно.")
    groups = sorted(summary.get("groups", []), key=lambda g: g.get("outflow_rub", 0))[:4]
    if groups:
        lines.append(f"Крупнейшие расходы за {summary.get('days', 90)} дней:")
        for g in groups:
            lines.append(f"  {g['name']}: {rub(abs(g['outflow_rub']))}.")
            amounts.add(round(abs(g["outflow_rub"])))
    return "\n".join(lines), amounts, must_dates


SCALED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(млн|тыс)", re.IGNORECASE)


def scaled_in(text: str) -> list[int]:
    """Amounts written like "4,2 млн" or "467 тыс."."""
    out = []
    for num, unit in SCALED_RE.findall(text):
        value = float(num.replace(",", "."))
        out.append(round(value * (1_000_000 if unit.lower() == "млн" else 1_000)))
    return out


def numbers_in(text: str) -> list[int]:
    out = []
    for m in NUMBER_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if digits:
            out.append(int(digits))
    return out


def grounded_enough(answer: str, amounts: set[int], must_dates: list[str]) -> bool:
    """Every large number must be one of the facts (±1 ₽ for rounding), the first
    gap date must be named (either 22.01.2026 or 22.01)."""
    if not answer.strip():
        return False
    plain = SCALED_RE.sub("", answer)
    for n in numbers_in(plain):
        if n >= MIN_CHECKED and not any(abs(n - a) <= 1 for a in amounts):
            return False
    # Rounded amounts ("4,2 млн") must be within 5% of some fact.
    for n in scaled_in(answer):
        if not any(abs(n - abs(a)) <= 0.05 * abs(a) for a in amounts if a):
            return False
    return all(d in answer or d[:5] in answer for d in must_dates)


def template_answer(gaps: dict, facts: str) -> str:
    g = (gaps.get("gaps") or [None])[0]
    if not g:
        return "Разрывов в ближайшие недели не видно.\n\n" + facts
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


class GroundedRuntime:
    name = "grounded"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def run(self, message: str, history: list[dict], llm_url: str, mcp_url: str) -> Turn:
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as http:
            mcp = McpClient(mcp_url, http)
            await mcp.tools()  # initialize the session
            gaps = json.loads(await mcp.call("get_gaps", {}))
            forecast = json.loads(await mcp.call("get_forecast", {"horizon": 30}))
            summary = json.loads(await mcp.call("get_category_summary", {"days": 90}))
            facts, amounts, must_dates = facts_from(gaps, forecast, summary)
            prompt = (
                f"Факты из прогноза (других цифр не существует):\n{facts}\n\n"
                f"Вопрос владельца: {message}\n\n"
                "Ответь по этим фактам: что происходит, почему, и 2–3 конкретных действия. "
                "Используй только суммы и даты из фактов."
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT + "\n/no_think"},
                *history[-6:],
                {"role": "user", "content": prompt},
            ]
            answer = ""
            try:
                resp = await http.post(f"{llm_url}/chat/completions", json={"model": "ci1", "messages": messages})
                resp.raise_for_status()
                answer = resp.json()["choices"][0]["message"].get("content") or ""
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("local model unavailable: %s", type(exc).__name__)
            used = ["get_gaps", "get_forecast", "get_category_summary"]
            if grounded_enough(answer, amounts, must_dates):
                return Turn(answer=answer.strip(), tools_used=used, runtime=self.name)
            log.info("model answer failed the facts check, using the template")
            return Turn(answer=template_answer(gaps, facts), tools_used=used, runtime=f"{self.name}-template")
