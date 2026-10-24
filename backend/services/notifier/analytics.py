"""Чистая аналитика поверх прогноза, план-факта и операций: алерты, риски, итоги периодов, цели, советы.
Здесь нет сети и времени — только данные на входе, поэтому всё покрывается юнит-тестами."""
from dataclasses import dataclass, field
from datetime import date, timedelta

from .upstream import Tx, kop

TAX_WORDS = ("налог", "енп", "ндс", "усн", "страхов", "взнос", "фнс")


@dataclass
class Day:
    date: date
    p10: int
    p50: int
    p90: int
    prob_neg: float


@dataclass
class Forecast:
    as_of: date
    opening: int
    days: list[Day]
    level: str
    prob: float
    first_break: date | None
    possible_break: date | None
    max_deficit_p90: int
    median_deficit: int
    warnings: list[dict]
    planned: list[dict]
    stale: bool = False

    @property
    def break_date(self) -> date | None:
        return self.first_break or self.possible_break


def _d(v) -> date | None:
    return date.fromisoformat(v) if v else None


def parse_forecast(payload: dict) -> Forecast:
    r = payload["result"]
    risk = r["risk"]
    days = [Day(date.fromisoformat(x["date"]), kop(x["p10"]), kop(x["p50"]), kop(x["p90"]), float(x.get("prob_negative", 0))) for x in r["days"]]
    planned = [{**p, "date": date.fromisoformat(p["date"]), "amount": kop(p["amount"])} for p in r.get("planned", [])]
    warnings = []
    for w in risk.get("warnings", []):
        reasons = [{**p, "date": date.fromisoformat(p["date"]), "amount": kop(p["amount"])} for p in w.get("reasons", [])]
        warnings.append({**w, "date": _d(w.get("date")), "reasons": reasons})
    return Forecast(
        as_of=date.fromisoformat(r["as_of"]), opening=kop(r["opening_balance"]), days=days, level=risk["level"],
        prob=float(risk["shortfall_probability"]), first_break=_d(risk.get("first_break_date")),
        possible_break=_d(risk.get("possible_break_date")), max_deficit_p90=kop(risk.get("max_deficit_p90")),
        median_deficit=kop(risk.get("deficit_given_break_median")), warnings=warnings, planned=planned,
        stale=bool(payload.get("stale")),
    )


# ---------- границы периодов ----------
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def month_start(d: date) -> date:
    return d.replace(day=1)


def prev_month(d: date) -> tuple[date, date]:
    end = month_start(d) - timedelta(days=1)
    return end.replace(day=1), end


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


# ---------- итоги периода ----------
def period_stats(txs: list[Tx], start: date, end: date) -> dict:
    inc = exp = 0
    cat_out: dict[str, int] = {}
    cat_in: dict[str, int] = {}
    payers: dict[str, int] = {}
    for t in txs:
        if not start <= t.date <= end or t.category == "transfers":
            continue
        if t.amount > 0:
            inc += t.amount
            cat_in[t.category] = cat_in.get(t.category, 0) + t.amount
            payers[t.counterparty] = payers.get(t.counterparty, 0) + t.amount
        else:
            exp += -t.amount
            cat_out[t.category] = cat_out.get(t.category, 0) + -t.amount
    top_payer = max(payers.items(), key=lambda kv: kv[1]) if payers else None
    return {
        "start": start, "end": end, "income": inc, "expense": exp, "net": inc - exp,
        "by_cat_out": dict(sorted(cat_out.items(), key=lambda kv: -kv[1])),
        "by_cat_in": dict(sorted(cat_in.items(), key=lambda kv: -kv[1])),
        "top_payer": top_payer, "opening": balance_at(txs, start - timedelta(days=1)), "closing": balance_at(txs, end),
    }


def balance_at(txs: list[Tx], d: date) -> int | None:
    last = None
    for t in txs:
        if t.date > d:
            break
        if t.balance is not None:
            last = t.balance
    return last


def daily_balances(txs: list[Tx], start: date, end: date) -> list[tuple[date, int]]:
    out, cur, d = [], balance_at(txs, start - timedelta(days=1)), start
    byday = {}
    for t in txs:
        if start <= t.date <= end and t.balance is not None:
            byday[t.date] = t.balance
    while d <= end:
        cur = byday.get(d, cur)
        if cur is not None:
            out.append((d, cur))
        d += timedelta(days=1)
    return out


def weekly_flows(txs: list[Tx], start: date, end: date) -> list[dict]:
    """Поступления и списания по неделям внутри периода (для графика месяца)."""
    out, ws = [], week_start(start)
    while ws <= end:
        a, b = max(ws, start), min(ws + timedelta(days=6), end)
        s = period_stats(txs, a, b)
        out.append({"start": a, "end": b, "income": s["income"], "expense": s["expense"]})
        ws += timedelta(days=7)
    return out


def avg_weekly(txs: list[Tx], before: date, weeks: int = 4) -> tuple[int, int]:
    """Средние за неделю поступления и списания за `weeks` недель до даты `before` (не включая её)."""
    inc = exp = 0
    for i in range(1, weeks + 1):
        e = before - timedelta(days=7 * (i - 1) + 1)
        s = period_stats(txs, e - timedelta(days=6), e)
        inc, exp = inc + s["income"], exp + s["expense"]
    return inc // weeks, exp // weeks


def plan_fact_stats(pf: dict, start: date, end: date) -> dict:
    items = []
    for i in pf.get("items", []):
        d = date.fromisoformat(i["date"])
        if start <= d <= end:
            items.append({**i, "date": d, "plan": kop(i["plan"]), "fact": kop(i["fact"]) if i.get("fact") is not None else None})
    by: dict[str, list] = {"paid": [], "deviation": [], "missed": [], "pending": []}
    for i in items:
        by.setdefault(i["status"], []).append(i)
    return {
        "items": items, "paid": len(by["paid"]), "deviation": len(by["deviation"]), "missed": len(by["missed"]),
        "pending": len(by["pending"]), "missed_items": by["missed"], "deviation_items": by["deviation"],
        "plan_in": sum(i["plan"] for i in items if i["plan"] > 0), "plan_out": -sum(i["plan"] for i in items if i["plan"] < 0),
        "fact_in": sum(i["fact"] or 0 for i in items if i["plan"] > 0), "fact_out": -sum(i["fact"] or 0 for i in items if i["plan"] < 0),
        "done": len(by["paid"]) + len(by["deviation"]), "total": len(items),
    }


def is_tax(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in TAX_WORDS)


# ---------- алерт кассового разрыва ----------
def gap_alert(fc: Forecast, today: date, days_ahead: int, threshold: float) -> dict | None:
    bd = fc.break_date
    if bd is None or fc.prob < threshold:
        return None
    left = (bd - today).days
    if left < 0 or left > days_ahead:
        return None
    stage = 1 if left <= 1 else 3 if left <= 3 and days_ahead >= 3 else days_ahead
    return {"stage": stage, "date": bd, "days_left": left, "prob": fc.prob, "deficit": fc.median_deficit or fc.max_deficit_p90,
            "deficit_p90": fc.max_deficit_p90, "level": fc.level, "reasons": break_reasons(fc, bd), "key": f"{bd.isoformat()}:{stage}"}


def break_reasons(fc: Forecast, until: date, limit: int = 4) -> list[dict]:
    seen, out = set(), []
    pool = [r for w in fc.warnings for r in w["reasons"]] or fc.planned
    for r in sorted(pool, key=lambda r: r["amount"]):
        if r["amount"] >= 0 or r["date"] > until:
            continue
        k = (r["date"], r["title"], r["amount"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out[:limit]


# ---------- риск-менеджмент недели ----------
LEVELS = {"low": 1, "medium": 2, "high": 3}


@dataclass
class RiskItem:
    code: str
    level: str
    title: str
    detail: str
    action: str


@dataclass
class RiskReport:
    start: date
    end: date
    level: str
    min_p50: int
    min_p50_date: date | None
    min_p10: int
    max_prob: float
    items: list[RiskItem] = field(default_factory=list)
    outflows: list[dict] = field(default_factory=list)
    inflows: list[dict] = field(default_factory=list)


def _window(fc: Forecast, start: date, end: date) -> list[Day]:
    return [d for d in fc.days if start <= d.date <= end]


def risk_report(fc: Forecast, pf: dict | None, txs: list[Tx], today: date, threshold: float = 0.2) -> RiskReport:
    from . import fmt

    start, end = today, today + timedelta(days=6)
    win = _window(fc, start, end)
    p50 = min(win, key=lambda d: d.p50) if win else None
    rep = RiskReport(start, end, "low", p50.p50 if p50 else fc.opening, p50.date if p50 else None,
                     min((d.p10 for d in win), default=fc.opening), max((d.prob_neg for d in win), default=0.0))
    plans = [p for p in fc.planned if start <= p["date"] <= end]
    rep.outflows = sorted((p for p in plans if p["amount"] < 0), key=lambda p: p["amount"])
    rep.inflows = sorted((p for p in plans if p["amount"] > 0), key=lambda p: -p["amount"])
    items = rep.items

    bd = fc.break_date
    if bd and fc.prob >= threshold and (bd - today).days <= 30:
        left = (bd - today).days
        lvl = "high" if left <= 7 or fc.level == "high" else "medium"
        items.append(RiskItem("cash_gap", lvl, "Кассовый разрыв",
                              f"ожидается около {fmt.day(bd)} (через {fmt.days_word(max(left, 0))}), вероятность {fmt.pct(fc.prob)}, "
                              f"дефицит до {fmt.rub(fc.max_deficit_p90)}", "Договоритесь об отсрочке крупных платежей и ускорьте входящие оплаты до этой даты."))
    elif rep.max_prob >= threshold or rep.min_p10 < 0:
        items.append(RiskItem("cash_gap", "medium", "Остаток может уйти в минус",
                              f"в худшем сценарии (p10) остаток опускается до {fmt.rub(rep.min_p10)}", "Подготовьте резерв ликвидности на случай задержки оплат."))

    taxes = [p for p in rep.outflows if is_tax(p["title"])]
    if taxes:
        s = -sum(p["amount"] for p in taxes)
        items.append(RiskItem("taxes", "medium", "Налоги и взносы на неделе",
                              f"{len(taxes)} {_pl(len(taxes), 'платёж', 'платежа', 'платежей')} на {fmt.rub(s)}, ближайший — {fmt.day(taxes[0]['date'])}",
                              "Отложите сумму на отдельный счёт заранее и не допускайте просрочки: пени увеличивают нагрузку."))

    if fc.opening > 0 and rep.outflows:
        peak = {}
        for p in rep.outflows:
            peak[p["date"]] = peak.get(p["date"], 0) - p["amount"]
        d, v = max(peak.items(), key=lambda kv: kv[1])
        if v >= fc.opening * 0.25:
            items.append(RiskItem("peak_day", "medium", "Пиковая нагрузка по платежам",
                                  f"{fmt.day_wd(d)} уходит {fmt.rub(v)} ({round(v / fc.opening * 100)}% остатка)",
                                  "Разнесите платежи по датам, если контрагенты согласны."))

    overdue_in = [p for p in fc.planned if p["amount"] > 0 and p.get("overdue")]
    if overdue_in:
        s = sum(p["amount"] for p in overdue_in)
        items.append(RiskItem("overdue_in", "high" if s >= fc.opening * 0.3 else "medium", "Просроченные поступления",
                              f"просрочено {len(overdue_in)} {_pl(len(overdue_in), 'поступление', 'поступления', 'поступлений')} на {fmt.rub(s)}",
                              "Свяжитесь с плательщиками, зафиксируйте новую дату и предложите скидку за быструю оплату."))

    if pf:
        past = plan_fact_stats(pf, today - timedelta(days=14), today - timedelta(days=1))
        if past["missed"]:
            s = sum(abs(i["plan"]) for i in past["missed_items"])
            items.append(RiskItem("plan_fact", "medium" if past["missed"] < 3 else "high", "Расхождение с планом",
                                  f"за 2 недели не состоялось {past['missed']} {_pl(past['missed'], 'платёж', 'платежа', 'платежей')} на {fmt.rub(s)}",
                                  "Проверьте, почему платежи не прошли, и обновите ожидаемые даты."))

    ai, ae = avg_weekly(txs, today)
    last = period_stats(txs, today - timedelta(days=7), today - timedelta(days=1))
    if ai and last["income"] < ai * 0.8:
        items.append(RiskItem("income_drop", "medium", "Просадка выручки",
                              f"прошлая неделя {fmt.rub(last['income'])} против среднего {fmt.rub(ai)}", "Проверьте воронку продаж и работу с постоянными клиентами."))
    tot = sum(period_stats(txs, today - timedelta(days=90), today - timedelta(days=1))["by_cat_in"].values())
    pay = period_stats(txs, today - timedelta(days=90), today - timedelta(days=1))["top_payer"]
    if pay and tot and pay[1] / tot >= 0.4:
        items.append(RiskItem("concentration", "low", "Зависимость от одного клиента",
                              f"«{pay[0]}» даёт {round(pay[1] / tot * 100)}% поступлений за 90 дней", "Привлекайте новых клиентов, чтобы доля одного не превышала 30–40%."))

    if ae and fc.opening >= 0:
        runway = fc.opening / (ae / 7)
        if runway < 14:
            items.append(RiskItem("low_buffer", "high" if runway < 7 else "medium", "Тонкая финансовая подушка",
                                  f"остатка хватит примерно на {fmt.days_word(int(runway))} обычных расходов", "Цель — резерв на 2–4 недели обязательных платежей."))

    items.sort(key=lambda i: -LEVELS[i.level])
    rep.level = items[0].level if items else "low"
    return rep


def _pl(n: int, a: str, b: str, c: str) -> str:
    from .fmt import plural
    return plural(n, a, b, c)


# ---------- agile: цели недели ----------
def velocity(goals: list[dict]) -> float | None:
    """Доля выполненных целей прошлой недели (частично = 0.5); пропущенные не считаются. None — нет оценённых целей."""
    scored = [g for g in goals if g["status"] in ("done", "partial", "failed")]
    if not scored:
        return None
    return sum({"done": 1.0, "partial": 0.5, "failed": 0.0}[g["status"]] for g in scored) / len(scored)


def propose_goals(fc: Forecast, txs: list[Tx], pf: dict | None, today: date, prev_goals: list[dict]) -> list[dict]:
    """Цели на неделю, начинающуюся в `today`: из прогноза, обязательств, просрочки и переноса невыполненного."""
    from . import fmt

    start, end = today, today + timedelta(days=6)
    win = _window(fc, start, end)
    goals: list[dict] = []
    min_p10 = min((d.p10 for d in win), default=fc.opening)
    if min_p10 < 0:
        d = min(win, key=lambda d: d.p10).date
        goals.append({"kind": "cover_gap", "title": f"Закрыть риск дефицита: обеспечить ликвидность {fmt.rub(-min_p10)} к {fmt.day(d)}",
                      "target_kop": -min_p10, "due": d.isoformat()})
    else:
        floor = (min_p10 // 100_000) * 100_000
        if floor > 0:
            goals.append({"kind": "cash_floor", "title": f"Держать остаток на счёте не ниже {fmt.rub(floor)}", "target_kop": floor, "due": end.isoformat()})
    taxes = [p for p in fc.planned if start <= p["date"] <= end and p["amount"] < 0 and is_tax(p["title"])]
    if taxes:
        s = -sum(p["amount"] for p in taxes)
        goals.append({"kind": "taxes", "title": f"Оплатить налоги и взносы {fmt.rub(s)} до {fmt.day(taxes[0]['date'])}", "target_kop": s, "due": taxes[0]["date"].isoformat()})
    overdue = [p for p in fc.planned if p["amount"] > 0 and p.get("overdue")]
    if pf:
        past = plan_fact_stats(pf, today - timedelta(days=14), today - timedelta(days=1))
        overdue_pf = [i for i in past["missed_items"] if i["plan"] > 0]
    else:
        overdue_pf = []
    if overdue or overdue_pf:
        s = sum(p["amount"] for p in overdue) or sum(i["plan"] for i in overdue_pf)
        n = len(overdue) or len(overdue_pf)
        goals.append({"kind": "collect", "title": f"Получить просроченные поступления: {n} {_pl(n, 'оплата', 'оплаты', 'оплат')} на {fmt.rub(s)}",
                      "target_kop": s, "due": end.isoformat()})
    ai, ae = avg_weekly(txs, today)
    last = period_stats(txs, today - timedelta(days=7), today - timedelta(days=1))
    if ae and last["expense"] > ae * 1.15:
        cut = (last["expense"] - ae) // 2
        goals.append({"kind": "cut_expense", "title": f"Сократить недельные расходы на {fmt.rub(cut)} (прошлая неделя выше нормы на {round((last['expense'] / ae - 1) * 100)}%)",
                      "target_kop": cut, "due": end.isoformat()})
    carried = [{"kind": g["kind"], "title": g["title"] if g["title"].startswith("↩") else "↩ " + g["title"], "target_kop": g["target_kop"], "due": g["due"]}
               for g in prev_goals if g["status"] in ("open", "failed", "partial")]
    kinds = {g["kind"] for g in carried}
    goals = carried + [g for g in goals if g["kind"] not in kinds]
    v = velocity(prev_goals)
    cap = 3 if v is not None and v < 0.5 else 5
    return goals[:cap]


# ---------- советы (best practice) ----------
def best_practices(fc: Forecast, risks: RiskReport | None, week: dict | None, month: dict | None, pf_stats: dict | None,
                   vel: float | None, today: date) -> list[dict]:
    """Правила, а не нейросеть: каждый совет объясним и привязан к цифре. level: warn/info/ok."""
    from . import fmt

    tips: list[dict] = []
    codes = {i.code for i in risks.items} if risks else set()
    bd = fc.break_date
    if "cash_gap" in codes and bd:
        tips.append({"level": "warn", "title": "Закрывайте разрыв заранее, а не в день разрыва",
                     "text": f"До {fmt.day(bd)}: 1) попросите отсрочку у 1–2 крупнейших кредиторов, 2) выставьте счета с предоплатой 30–50%, "
                             "3) предложите скидку 2–3% за оплату в течение 3 дней, 4) согласуйте овердрафт или краткосрочный заём — пока остаток положительный."})
    if "taxes" in codes:
        tips.append({"level": "warn", "title": "Налоговая копилка",
                     "text": "Откладывайте 6–15% каждого поступления на отдельный счёт под налоги. К сроку ЕНП деньги уже лежат и не создают кассовый разрыв."})
    if "overdue_in" in codes:
        tips.append({"level": "warn", "title": "Дебиторка: правило 3 касаний",
                     "text": "Напоминание в день срока, звонок через 3 дня, письмо с актом сверки через 7. По клиентам с частой просрочкой переходите на предоплату."})
    if "low_buffer" in codes:
        tips.append({"level": "warn", "title": "Финансовая подушка",
                     "text": "Цель — запас на 2–4 недели обязательных расходов (зарплата, аренда, налоги, кредиты). Пополняйте его фиксированной долей поступлений."})
    if "concentration" in codes:
        tips.append({"level": "info", "title": "Диверсификация клиентов",
                     "text": "Один клиент свыше 40% выручки — риск: его задержка оплаты сразу создаёт разрыв. Поставьте цель привлечь 2–3 новых клиента в квартал."})
    if month and month["expense"]:
        for cat, v in list(month["by_cat_out"].items())[:3]:
            share = v / month["expense"]
            if cat in ("other_expense", "marketing") and share > 0.2:
                tips.append({"level": "info", "title": f"Категория «{fmt.category(cat)}» — {round(share * 100)}% расходов",
                             "text": "Разберите крупные операции в этой категории: уберите дубли и неэффективные траты, остальное привяжите к KPI."})
    if pf_stats and pf_stats["total"] and pf_stats["missed"] / pf_stats["total"] > 0.2:
        tips.append({"level": "info", "title": "План не совпадает с фактом",
                     "text": "Больше пятой части плановых платежей не состоялось. Обновляйте даты ожидаемых платежей сразу после договорённостей — прогноз станет точнее."})
    if vel is not None and vel < 0.5:
        tips.append({"level": "info", "title": "Меньше целей — выше результат",
                     "text": "Выполнено меньше половины целей. На следующую неделю взято не больше трёх: закрывайте до начала новых."})
    elif vel is not None and vel >= 0.8:
        tips.append({"level": "ok", "title": "Отличный темп", "text": "Больше 80% целей закрыто. Можно взять амбициозную цель: ускорить сбор дебиторки или нарастить подушку."})
    if not tips or (risks and risks.level == "low"):
        tips.append({"level": "ok", "title": "Свободные деньги — в дело",
                     "text": "Если остаток стабильно выше подушки, размещайте излишек на накопительный счёт или депозит с возможностью снятия, чтобы он приносил доход."})
    tips.append({"level": "info", "title": "Короткий цикл agile",
                 "text": "Раз в неделю сверяйте план и факт, закрывайте цели в боте, а в пятницу смотрите прогноз. Чем чаще данные, тем точнее прогноз и план."})
    return tips
