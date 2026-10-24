"""Сбор данных для отчётов: один набор цифр на текст, график и Excel."""
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import analytics as an
from .store import Store
from .upstream import Tx, Upstream


@dataclass
class Bundle:
    company: str
    today: date
    prefs: dict
    fc: an.Forecast
    txs: list[Tx]
    pf: dict | None
    risk: an.RiskReport
    week: dict  # итоги 7 дней перед сегодняшним днём
    week_avg: tuple[int, int]
    month_last: dict
    month_prev: dict
    month_avg3: tuple[int, int]
    pf_week: dict | None
    pf_month: dict | None
    goals_prev_week: str | None
    goals_prev: list[dict]
    vel: float | None
    goals_now: list[dict] = field(default_factory=list)
    tips: list[dict] = field(default_factory=list)
    advice: dict | None = None  # ответ консультанта Hermes (None — недоступен или выключен)


async def collect(up: Upstream, store: Store, cid: str, today: date, prefs: dict, now_goals: str | None = None) -> Bundle:
    try:
        company = (await up.company(cid)).get("company_name", "")
    except Exception:
        company = ""
    fc = an.parse_forecast(await up.forecast(cid))
    txs = await up.transactions(cid)
    try:
        pf = await up.plan_fact(cid, 90)
    except Exception:
        pf = None  # план-факт не критичен для отчёта
    risk = an.risk_report(fc, pf, txs, today, prefs["gap_threshold"])
    wk_end = today - timedelta(days=1)
    week = an.period_stats(txs, today - timedelta(days=7), wk_end)
    m_start, m_end = an.prev_month(today)
    month_last = an.period_stats(txs, m_start, m_end)
    p_start, p_end = an.prev_month(m_start)
    month_prev = an.period_stats(txs, p_start, p_end)
    ms = [month_last, month_prev, an.period_stats(txs, *an.prev_month(p_start))]
    avg3 = (sum(m["income"] for m in ms) // 3, sum(m["expense"] for m in ms) // 3)
    gw, goals_prev = store.prev_goals(cid, today.isoformat())
    if gw and (today - date.fromisoformat(gw)).days > 21:
        gw, goals_prev = None, []  # слишком старые цели в ретро не тянем
    b = Bundle(
        company=company, today=today, prefs=prefs, fc=fc, txs=txs, pf=pf, risk=risk, week=week,
        week_avg=an.avg_weekly(txs, today), month_last=month_last, month_prev=month_prev, month_avg3=avg3,
        pf_week=an.plan_fact_stats(pf, today - timedelta(days=7), wk_end) if pf else None,
        pf_month=an.plan_fact_stats(pf, m_start, m_end) if pf else None,
        goals_prev_week=gw, goals_prev=goals_prev, vel=an.velocity(goals_prev),
        goals_now=store.goals(cid, now_goals) if now_goals else [],
    )
    b.tips = an.best_practices(fc, risk, week, month_last, b.pf_week, b.vel, today)
    return b
