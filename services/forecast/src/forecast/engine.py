"""Layered cash-flow forecast and gap probability.

balance[d] = balance[as_of]
           + known future events (invoices, taxes, manual input)
           + recurring flows found in the history (rent, payroll, subscriptions…)
           + irregular remainder (a `Forecaster` from `models`)

All layers are simulated together (Monte Carlo), which gives P10/P50/P90 per day
and the probability that the balance drops below a threshold. Rescue inflows
(own money brought in to cover a hole) are excluded: the forecast shows what
would happen without the owner stepping in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
from forecast import models
from forecast.calendar import days_after, is_working_day, next_working_day
from forecast.delays import DelayModel

# Revenue streams that are many small payments: they belong to the irregular layer.
AGGREGATE_SUBS = {
    "acquiring",
    "sbp",
    "cash_deposit",
    "marketplace_payout",
    "other_in",
    "other_out",
    "refund_in",
    "refund_out",
}
GAP_PROBABILITY = 0.2
INVOICE_SUBS = {"customer_payment"}


@dataclass
class Pattern:
    key: str
    counterparty_ref: str | None
    subcategory: str
    group: str
    day_of_month: int
    dom_spread: int
    amount_kop: int
    amount_sd: float
    last_date: date
    occurrences: int
    label: str

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "counterparty_ref": self.counterparty_ref,
            "subcategory": self.subcategory,
            "group": self.group,
            "day_of_month": self.day_of_month,
            "amount_kop": self.amount_kop,
            "last_date": str(self.last_date),
            "occurrences": self.occurrences,
            "label": self.label,
        }


@dataclass
class Obligation:
    id: str
    kind: str
    direction: str
    amount_kop: int
    due_date: date
    certainty: float
    counterparty_ref: str | None
    doc_date: date | None
    status: str
    label: str


@dataclass
class Settings:
    min_balance_kop: int = 0
    overdraft_limit_kop: int = 0


@dataclass
class Fitted:
    """Everything learned from the history. Reused when only future events or
    settings change, so those updates do not refit anything."""

    as_of: date
    opening_kop: int
    patterns: list[Pattern]
    delays: DelayModel
    residual: models.Forecaster
    customer_share: float
    history_days: int
    warnings: list[str] = field(default_factory=list)
    model_name: str = "weekday_bootstrap"
    customer_model: models.Forecaster | None = None


SUB_LABELS = {
    "salary": "Зарплата",
    "rent": "Аренда",
    "utilities": "Коммунальные и связь",
    "enp": "Налоги (ЕНП)",
    "loan_payment": "Кредит",
    "leasing": "Лизинг",
    "software": "Подписки",
    "advertising": "Реклама",
    "supplier_payment": "Поставщик",
    "services": "Услуги",
    "contractors_gph": "ГПХ",
    "customer_payment": "Оплата клиента",
    "fuel_transport": "Топливо и дороги",
    "bank_fee": "Комиссия банка",
    "founder_loan_return": "Возврат займа учредителю",
    "owner_withdrawal": "Вывод владельцу",
    "own_transfer_out": "Перевод на свой счёт",
}


def _label(sub: str, amount: int) -> str:
    base = SUB_LABELS.get(sub, "Платёж" if amount < 0 else "Поступление")
    return base


def detect_patterns(hist: pd.DataFrame, as_of: date) -> tuple[list[Pattern], pd.Series]:
    """Monthly recurring flows: same counterparty and subcategory, a stable day of
    month, stable amount, seen in most months. Returns patterns and a mask of the
    history rows they explain (those rows leave the irregular layer)."""
    explained = pd.Series(False, index=hist.index)
    patterns: list[Pattern] = []
    candidates = hist[~hist["subcategory"].isin(AGGREGATE_SUBS) & ~hist["is_rescue"]]
    for (cp, sub), g in candidates.groupby(["counterparty_ref", "subcategory"], dropna=False):
        if len(g) < 3:
            continue
        g = g.sort_values("op_date")
        dom = g["op_date"].map(lambda d: d.day).to_numpy()
        # Up to three clusters by day of month (e.g. salary advance and final payment).
        order = np.argsort(dom, kind="stable")
        splits = np.nonzero(np.diff(dom[order]) > 5)[0]
        if len(splits) > 2:
            continue
        clusters = np.split(order, splits + 1)
        span_months = max(1, (g["op_date"].max() - g["op_date"].min()).days / 30.4)
        for ci, idx in enumerate(clusters):
            part = g.iloc[np.sort(idx)]
            months = part["op_date"].map(lambda d: (d.year, d.month)).nunique()
            if months < 3 or months < 0.6 * span_months:
                continue
            # Exactly monthly: several payments a month belong to the irregular layer.
            if len(part) > 1.25 * months:
                continue
            amounts = part["amount_kop"].to_numpy(dtype=float)
            med = float(np.median(amounts))
            if med == 0:
                continue
            mad = float(np.median(np.abs(amounts - med)))
            if mad / abs(med) > 0.25:
                continue
            last = part["op_date"].max()
            # Pattern must still be alive: last seen within ~1.6 months.
            if (as_of - last).days > 48:
                continue
            d = part["op_date"].map(lambda x: x.day).to_numpy()
            dom_med = int(np.median(d))
            patterns.append(
                Pattern(
                    key=f"{cp}:{sub}:{ci}",
                    counterparty_ref=cp if isinstance(cp, str) else None,
                    subcategory=sub,
                    group=str(part["group"].iloc[-1]),
                    day_of_month=dom_med,
                    dom_spread=int(min(5, max(1, np.median(np.abs(d - dom_med))))),
                    amount_kop=int(med),
                    amount_sd=float(1.4826 * mad),
                    last_date=last,
                    occurrences=len(part),
                    label=_label(sub, int(med)),
                )
            )
            explained.loc[part.index] = True
    return patterns, explained


def _invoice_records(hist: pd.DataFrame, open_invoices: list[Obligation], as_of: date) -> list[tuple]:
    paid = hist[(hist["amount_kop"] > 0) & hist["doc_date"].notna() & hist["subcategory"].isin(INVOICE_SUBS)]
    recs = [
        (r.counterparty_ref, max(0, (r.op_date - r.doc_date).days), True)
        for r in paid.itertuples()
        if r.doc_date <= r.op_date
    ]
    recs += [(o.counterparty_ref, max(0, (as_of - o.doc_date).days), False) for o in open_invoices if o.doc_date]
    return recs


def fit(
    hist: pd.DataFrame,
    as_of: date,
    obligations: list[Obligation],
    model_name: str = "weekday_bootstrap",
    layers: frozenset[str] = frozenset({"recurring", "invoices"}),
) -> Fitted:
    """hist columns: op_date, amount_kop, balance_eod_kop, counterparty_ref, subcategory, group, is_rescue, doc_date.
    `layers` switches the recurring and invoice layers off for the backtest comparison."""
    warnings: list[str] = []
    hist = hist[hist["op_date"] <= as_of].copy()
    if hist.empty:
        return Fitted(as_of, 0, [], DelayModel.fit([]), models.make(model_name), 0.0, 0, ["no_history"], model_name)
    last_day = hist["op_date"].max()
    opening = int(hist.loc[hist["op_date"] == last_day, "balance_eod_kop"].iloc[0])
    history_days = (last_day - hist["op_date"].min()).days
    if history_days < 90:
        warnings.append("short_history")
    if (as_of - last_day).days > 10:
        warnings.append("stale_data")

    if "recurring" in layers:
        patterns, explained = detect_patterns(hist, as_of)
    else:
        patterns, explained = [], pd.Series(False, index=hist.index)
    invoices = [o for o in obligations if o.kind == "invoice_out" and o.status in ("planned", "overdue")]
    if "invoices" not in layers:
        invoices = []
    if invoices:
        # Customer payments come from invoices now; a monthly pattern would count them twice.
        keep = [not (p.subcategory in INVOICE_SUBS and p.amount_kop > 0) for p in patterns]
        dropped = {p.key for p, k in zip(patterns, keep, strict=True) if not k}
        patterns = [p for p, k in zip(patterns, keep, strict=True) if k]
        if dropped:
            explained = explained & ~hist["subcategory"].isin(INVOICE_SUBS)
    delays = DelayModel.fit(_invoice_records(hist, invoices, as_of))

    flows = hist[~hist["is_rescue"] & ~explained]
    # With an invoice source, customer payments are forecast from invoices; the
    # irregular layer keeps them only for invoices not issued yet (see customer_share).
    customer_share = 0.0
    if invoices:
        cust = flows[flows["subcategory"].isin(INVOICE_SUBS) & (flows["amount_kop"] > 0)]
        customer_share = 1.0
        flows = flows.drop(cust.index)
        daily_customer = cust.groupby("op_date")["amount_kop"].sum()
    else:
        daily_customer = pd.Series(dtype=float)
    idx = pd.date_range(hist["op_date"].min(), as_of, freq="D").date
    daily = flows.groupby("op_date")["amount_kop"].sum().reindex(idx, fill_value=0)
    residual = models.make(model_name)
    residual.fit(daily)
    fitted = Fitted(as_of, opening, patterns, delays, residual, customer_share, history_days, warnings, model_name)
    if invoices:
        fitted.customer_model = models.make(model_name)
        fitted.customer_model.fit(daily_customer.reindex(idx, fill_value=0))
    return fitted


@dataclass
class Scenario:
    delays: dict[str, int] = field(default_factory=dict)  # counterparty_ref -> extra days
    group_scale: dict[str, float] = field(default_factory=dict)  # group -> factor for irregular+recurring
    recurring_scale: dict[str, float] = field(default_factory=dict)
    one_offs: list[tuple[date, int]] = field(default_factory=list)
    revenue_scale: float = 1.0

    @staticmethod
    def parse(transforms: list[dict]) -> Scenario:
        s = Scenario()
        for t in transforms or []:
            kind = t.get("type") or t.get("kind")
            if kind == "delay_counterparty":
                s.delays[str(t["counterparty_ref"])] = int(np.clip(int(t["days"]), 0, 120))
            elif kind == "delay_all_customers":
                s.delays["*"] = int(np.clip(int(t["days"]), 0, 120))
            elif kind == "scale_group":
                factor = float(np.clip(float(t["factor"]), 0.0, 5.0))
                if t["group"] == "revenue":
                    s.revenue_scale = factor
                else:
                    s.group_scale[str(t["group"])] = factor
            elif kind == "scale_recurring":
                s.recurring_scale[str(t["group"])] = float(np.clip(float(t["factor"]), 0.0, 5.0))
            elif kind == "one_off":
                amount = abs(int(t["amount_kop"]))
                s.one_offs.append(
                    (date.fromisoformat(str(t["date"])), amount if t.get("direction") == "inflow" else -amount)
                )
            else:
                raise ValueError(f"unknown scenario transform: {kind}")
        return s


def simulate(
    f: Fitted,
    obligations: list[Obligation],
    settings: Settings,
    horizon: int = 90,
    n_paths: int = 1000,
    seed: int = 7,
    scenario: Scenario | None = None,
    registry: dict[str, str] | None = None,
) -> dict:
    rng = np.random.default_rng(seed)
    sc = scenario or Scenario()
    days = days_after(f.as_of, horizon)
    H = len(days)
    flows = np.zeros((n_paths, H))
    events: list[tuple[int, str, float]] = []  # (day index, label, expected amount) for drivers

    # Irregular layer. Revenue scaling applies to inflows of the irregular layer.
    resid = f.residual.sample(days, n_paths, rng)
    if sc.revenue_scale != 1.0:
        resid = np.where(resid > 0, resid * sc.revenue_scale, resid)
    flows += resid

    # Customer payments for invoices not issued yet: share of payments at day h that
    # come from invoices issued after as_of equals P(delay < h).
    if f.customer_share and f.customer_model is not None:
        cdf = 1.0 - f.delays.global_curve[1 : H + 1]
        extra = f.customer_model.sample(days, n_paths, rng) * cdf[None, :] * sc.revenue_scale
        flows += extra

    # Recurring layer.
    for p in f.patterns:
        if p.group == "taxes" and any(o.kind == "tax" for o in obligations):
            continue  # the tax calendar replaces the pattern
        scale = sc.recurring_scale.get(p.group, 1.0) * sc.group_scale.get(p.group, 1.0)
        if p.amount_kop > 0:
            scale *= sc.revenue_scale if p.group == "revenue" else 1.0
        month = f.as_of.replace(day=1)
        for _ in range(4):
            try:
                due = month.replace(day=min(p.day_of_month, 28))
            except ValueError:
                due = month
            month = (month + timedelta(days=32)).replace(day=1)
            if due <= f.as_of or due <= p.last_date:
                continue
            jitter = rng.integers(-p.dom_spread, p.dom_spread + 1, size=n_paths)
            shift = sc.delays.get(p.counterparty_ref or "", sc.delays.get("*", 0)) if p.amount_kop > 0 else 0
            base_idx = (next_working_day(due) - f.as_of).days - 1 + shift
            idx = base_idx + jitter
            amount = rng.normal(p.amount_kop, max(p.amount_sd, abs(p.amount_kop) * 0.02), size=n_paths) * scale
            ok = (idx >= 0) & (idx < H)
            np.add.at(flows, (np.nonzero(ok)[0], idx[ok]), amount[ok])
            if 0 <= base_idx < H:
                events.append((base_idx, p.label, p.amount_kop * scale))

    # Known future events.
    for o in obligations:
        if o.status not in ("planned", "overdue"):
            continue
        sign = 1 if o.direction == "inflow" else -1
        amount = o.amount_kop * sign
        if o.kind == "invoice_out" and o.doc_date:
            p_never = 0.9 if (registry or {}).get(o.counterparty_ref or "") == "liquidation" else 0.0
            elapsed = (f.as_of - o.doc_date).days
            delay = f.delays.sample(o.counterparty_ref, elapsed, n_paths, rng, p_never)
            shift = sc.delays.get(o.counterparty_ref or "", sc.delays.get("*", 0))
            idx = np.where(delay >= 0, (o.doc_date - f.as_of).days + delay - 1 + shift, -1)
            ok = (idx >= 0) & (idx < H)
            np.add.at(flows, (np.nonzero(ok)[0], idx[ok]), amount)
            med = (o.doc_date - f.as_of).days + f.delays.median_delay(o.counterparty_ref) - 1 + shift
            if 0 <= med < H:
                events.append((med, o.label, amount))
            continue
        due = o.due_date
        if o.status == "overdue" and sign > 0:
            due = f.as_of + timedelta(days=max(3, f.delays.median_delay(o.counterparty_ref) // 3))
        shift = sc.delays.get(o.counterparty_ref or "", sc.delays.get("*", 0)) if sign > 0 else 0
        i = (next_working_day(max(due, f.as_of + timedelta(days=1))) - f.as_of).days - 1 + shift
        if 0 <= i < H:
            happens = rng.random(n_paths) < o.certainty
            flows[happens, i] += amount
            events.append((i, o.label, amount * o.certainty))

    for d, amount in sc.one_offs:
        i = (d - f.as_of).days - 1
        if 0 <= i < H:
            flows[:, i] += amount
            events.append((i, "Внеплановая операция", amount))

    balance = f.opening_kop + np.cumsum(flows, axis=1)
    p10, p50, p90 = np.percentile(balance, [10, 50, 90], axis=0)
    inflow = np.where(flows > 0, flows, 0).mean(axis=0)
    outflow = np.where(flows < 0, flows, 0).mean(axis=0)
    points = [
        {
            "date": str(d),
            "p10_kop": int(p10[j]),
            "p50_kop": int(p50[j]),
            "p90_kop": int(p90[j]),
            "inflow_kop": int(inflow[j]),
            "outflow_kop": int(outflow[j]),
            "working_day": is_working_day(d),
        }
        for j, d in enumerate(days)
    ]
    gaps = find_gaps(balance, days, settings, events)
    return {
        "as_of": str(f.as_of),
        "horizon": H,
        "opening_kop": f.opening_kop,
        "model": f.model_name,
        "paths": n_paths,
        "seed": seed,
        "warnings": f.warnings,
        "points": points,
        "gaps": gaps,
        "patterns": [p.to_dict() for p in f.patterns],
    }


def find_gaps(balance: np.ndarray, days: list[date], s: Settings, events: list[tuple[int, str, float]]) -> list[dict]:
    """Intervals where P(balance < threshold) > GAP_PROBABILITY. Two levels:
    below the minimum balance (warning) and beyond the overdraft limit (critical)."""
    out = []
    levels = [("critical", -s.overdraft_limit_kop)] if s.overdraft_limit_kop > 0 else []
    levels.append(("warning", s.min_balance_kop))
    for level, threshold in levels:
        prob_day = (balance < threshold).mean(axis=0)
        j = 0
        H = len(days)
        while j < H:
            if prob_day[j] <= GAP_PROBABILITY:
                j += 1
                continue
            k = j
            while k + 1 < H and prob_day[k + 1] > GAP_PROBABILITY / 2:
                k += 1
            window = balance[:, j : k + 1]
            low = window.min(axis=1)
            breach = low < threshold
            shortfall = (threshold - low)[breach]
            drivers = sorted((e for e in events if j - 5 <= e[0] <= j + 1), key=lambda e: e[2])[:4]
            out.append(
                {
                    "start_date": str(days[j]),
                    "end_date": str(days[k]),
                    "level": level,
                    "probability": round(float(breach.mean()), 3),
                    "shortfall_p50_kop": int(np.percentile(shortfall, 50)) if len(shortfall) else 0,
                    "shortfall_p90_kop": int(np.percentile(shortfall, 90)) if len(shortfall) else 0,
                    "threshold_kop": int(threshold),
                    "drivers": [{"date": str(days[e[0]]), "label": e[1], "amount_kop": int(e[2])} for e in drivers],
                }
            )
            j = k + 1
    # A critical gap is also below the warning threshold; keep the critical one.
    crit = {(g["start_date"]) for g in out if g["level"] == "critical"}
    return [g for g in out if g["level"] == "critical" or g["start_date"] not in crit]


def history_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "op_date",
                "amount_kop",
                "balance_eod_kop",
                "counterparty_ref",
                "subcategory",
                "group",
                "is_rescue",
                "doc_date",
            ]
        )
    df["op_date"] = pd.to_datetime(df["op_date"]).dt.date
    df["doc_date"] = pd.to_datetime(df["doc_date"], errors="coerce").dt.date
    df["doc_date"] = df["doc_date"].where(df["doc_date"].notna(), None)
    df["is_rescue"] = df["is_rescue"].astype(bool)
    return df.sort_values("op_date").reset_index(drop=True)
