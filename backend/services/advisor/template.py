"""Ответ без модели: когда провайдер не подключён, недоступен или исчерпан бюджет.
Все цифры берутся из последнего прогноза: дата и сумма ближайшего разрыва, причины и три действия."""
import datetime as dt

from .tools import rub, summarize_forecast

T = {
    "ru": {
        "no_data": "Пока нет прогноза: загрузите и подтвердите банковскую выписку, тогда я смогу ответить с цифрами.",
        "none": "В ближайшие {horizon} дн. разрыва не видно (вероятность {prob}%). Остаток сегодня: {opening}. Самый низкий ожидаемый остаток: {low} ({low_date}).",
        "gap": "Ближайший риск — {date}: не хватит около {amount} (вероятность {prob}%, в худшем случае до {p90}).",
        "reasons": " На эти дни приходятся: {items}.",
        "actions": "Что можно сделать:\n1. Попросить предоплату или ускорить оплату у клиентов с открытыми счетами.\n"
                   "2. Перенести несрочные закупки и платежи на дату после {after}.\n3. Заранее согласовать с банком короткий овердрафт на {cover}.",
        "stale": "\n\nВнимание: данные изменились после последнего расчёта, прогноз может быть неточным.",
        "unavailable": "Прогноз сейчас недоступен ({code}). Попробуйте позже.",
    },
    "en": {
        "no_data": "There is no forecast yet: upload and confirm a bank statement and I can answer with numbers.",
        "none": "No cash gap is expected in the next {horizon} days (probability {prob}%). Balance today: {opening}. Lowest expected balance: {low} ({low_date}).",
        "gap": "Nearest risk is {date}: about {amount} short (probability {prob}%, up to {p90} in the worst case).",
        "reasons": " Due on those days: {items}.",
        "actions": "What you can do:\n1. Ask clients with open invoices for a prepayment or faster payment.\n"
                   "2. Move non-urgent purchases and payments to after {after}.\n3. Arrange a short overdraft with the bank in advance for {cover}.",
        "stale": "\n\nNote: data changed after the last calculation, so the forecast may be imprecise.",
        "unavailable": "The forecast is unavailable right now ({code}). Try again later.",
    },
}


def money(v: int | None, lang: str) -> str:
    v = v or 0
    s = f"{abs(v):,}".replace(",", " ")
    s = ("−" if v < 0 else "") + s
    return f"{s} ₽" if lang == "ru" else f"{s} RUB"


def day(iso: str | None, lang: str) -> str:
    if not iso:
        return "—"
    d = dt.date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y") if lang == "ru" else d.isoformat()


def answer_from_forecast(run: dict, lang: str = "ru") -> str:
    t = T.get(lang, T["ru"])
    f = summarize_forecast(run)
    risk = f["risk"]
    prob = risk["shortfall_probability_pct"]
    low = f.get("lowest_expected_balance") or {}
    when = risk["first_break_date"] or risk["possible_break_date"]
    if risk["level"] == "none" or not when:
        text = t["none"].format(horizon=f["horizon_days"], prob=prob, opening=money(f["opening_balance_rub"], lang),
                                low=money(low.get("rub"), lang), low_date=day(low.get("date"), lang))
    else:
        text = t["gap"].format(date=day(when, lang), amount=money(risk["typical_deficit_rub"], lang), prob=prob,
                               p90=money(risk["max_deficit_p90_rub"], lang))
        gap = next((w for w in f["warnings"] if w["code"] == "cash_gap"), None)
        reasons = [r for r in (gap or {}).get("reasons", [])][:3]
        if reasons:
            items = ", ".join(f"{r['title']} ({money(abs(r['amount_rub'] or 0), lang)})" for r in reasons)
            text += t["reasons"].format(items=items)
        after = dt.date.fromisoformat(when) + dt.timedelta(days=3)
        text += "\n\n" + t["actions"].format(after=day(after.isoformat(), lang), cover=money(risk["max_deficit_p90_rub"], lang))
    if f["data_stale"]:
        text += t["stale"]
    return text


def no_data_answer(code: str, lang: str = "ru") -> str:
    t = T.get(lang, T["ru"])
    return t["no_data"] if code in ("no_data", "no_balance") else t["unavailable"].format(code=code)
