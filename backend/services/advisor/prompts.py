"""Подсказки «что написать» под полем чата до первого вопроса: строятся по данным компании (прогноз, просрочки,
контрагенты с риском ФНС) и только из того, что консультанту разрешено. Без модели: быстро и всегда одинаково.
После ответа подсказки дают сам консультант (suggest_followups) — их показывает интерфейс."""
import datetime as dt

from . import tools as tl

MAX = 4


def _money(rub: int | None, lang: str) -> str:
    v = f"{abs(rub or 0):,}".replace(",", " ")
    return f"{v} ₽" if lang == "ru" else f"{v} RUB"


def _date(s: str, lang: str) -> str:
    d = dt.date.fromisoformat(s)
    return d.strftime("%d.%m") if lang == "ru" else d.strftime("%b %d")


async def build(box: tl.Toolbox, perms: list[str], lang: str, mode: str = "chat") -> list[dict]:
    """[{text, kind}] — kind: gap | overdue | counterparty | scenario | action | plan | guide | general."""
    ru = lang == "ru"
    out: list[dict] = []

    def add(text: str, kind: str):
        if len(out) < MAX and all(o["text"] != text for o in out):
            out.append({"text": text, "kind": kind})

    if mode == "plan":
        return plan_prompts(perms, ru)

    forecast = None
    if "read_forecast" in perms:
        try:
            forecast = tl.summarize_forecast(await box.svc.latest_forecast())
        except tl.ToolError:
            forecast = None
    if forecast:
        r = forecast["risk"]
        # уровень none — разрыва в горизонте не ждём, даже если дата первого пересечения нуля посчитана
        risky = r.get("level") not in (None, "none")
        brk = r.get("first_break_date") if risky else None
        maybe = r.get("possible_break_date") if risky else None
        if brk:
            add(f"Как закрыть разрыв {_date(brk, lang)} на {_money(r.get('typical_deficit_rub'), lang)}?" if ru
                else f"How do I cover the {_money(r.get('typical_deficit_rub'), lang)} gap on {_date(brk, lang)}?", "gap")
        elif maybe:  # разрыв возможен только в плохом сценарии
            add(f"Насколько вероятен разрыв {_date(maybe, lang)} и как подстраховаться?" if ru
                else f"How likely is a gap on {_date(maybe, lang)} and how do I hedge?", "gap")
        else:
            add("Хватит ли денег до конца месяца?" if ru else "Will the money last until the end of the month?", "gap")
        try:
            up = await box.call("get_upcoming_payments", {"days": 14})
        except tl.ToolError:
            up = {}
        if up.get("overdue"):
            add(f"Что делать с просрочками на {_money(up.get('overdue_total_rub'), lang)}?" if ru
                else f"What should I do about {_money(up.get('overdue_total_rub'), lang)} overdue?", "overdue")
    if "read_counterparties" in perms:
        try:
            cps = (await box.call("get_counterparty_risks", {"limit": 10, "direction": "in"})).get("counterparties") or []
        except tl.ToolError:
            cps = []
        risky = [c for c in cps if (c.get("fns") or {}).get("risk_level") in ("medium", "high")]
        if risky:
            name = risky[0]["name"]
            if "run_scenarios" in perms:
                add(f"Что будет, если {name} не заплатит вовремя?" if ru else f"What if {name} pays late?", "counterparty")
            else:
                add(f"Почему у {name} риск по данным ФНС?" if ru else f"Why is {name} flagged by the tax registry?", "counterparty")
    if "run_scenarios" in perms:
        add("Что если выручка упадёт на 20%?" if ru else "What if revenue drops by 20%?", "scenario")
    if "read_operations" in perms:
        add("На что уходит больше всего денег за 3 месяца?" if ru else "Where does most money go over 3 months?", "general")
    if "add_payments" in perms:
        add("Добавь платёж за аренду 120 000 ₽ на 5-е число" if ru else "Add a 120,000 RUB rent payment on the 5th", "action")
    elif "propose_payments" in perms and forecast:
        add("Какие платежи стоит перенести?" if ru else "Which payments should I move?", "action")
    if not out:
        add("Что ты умеешь?" if ru else "What can you do?", "general")
    return out


def plan_prompts(perms: list[str], ru: bool) -> list[dict]:
    """Стартовые запросы режима «План»: стратегия для экрана «План и факт»."""
    if "make_plans" not in perms:
        return [{"text": "Что нужно, чтобы составить стратегию?" if ru else "What is needed to build a strategy?", "kind": "general"}]
    items = [
        ("Составь план, чтобы не уйти в минус в ближайшие 3 месяца", "Build a plan to stay out of the red for the next 3 months"),
        ("Как собрать резерв в размере месяца расходов?", "How do I build a reserve of one month of costs?"),
        ("План по просроченным оплатам клиентов", "A plan for overdue client payments"),
        ("Подготовь компанию к налоговым платежам квартала", "Prepare the company for this quarter's tax payments"),
    ]
    return [{"text": r if ru else e, "kind": "plan"} for r, e in items]
