"""Rolling-origin backtest. At each cut-off the model sees only the past and is
scored on the next `horizon` days against the balance the company would have had
without rescue inflows. The same code runs in CI (synthetic data), in the
service (per tenant) and in `ml/backtest` (whole dataset)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
from forecast import engine

VARIANTS: dict[str, tuple[str | None, frozenset[str]]] = {
    "naive_flat": (None, frozenset()),
    "naive_last_week": ("naive_last_week", frozenset()),
    "weekday_bootstrap": ("weekday_bootstrap", frozenset()),
    "layered": ("weekday_bootstrap", frozenset({"recurring"})),
    "layered_invoices": ("weekday_bootstrap", frozenset({"recurring", "invoices"})),
}


def reconstructed_invoices(hist: pd.DataFrame, cutoff: date, horizon: int) -> list[engine.Obligation]:
    """Invoices known at the cut-off: documents dated on or before it and paid after it.
    The amount is the sum of payments found for the document (partial payments)."""
    later = hist[
        (hist["op_date"] > cutoff)
        & (hist["op_date"] <= cutoff + timedelta(days=horizon + 120))
        & (hist["amount_kop"] > 0)
        & hist["doc_date"].notna()
        & hist["subcategory"].isin(engine.INVOICE_SUBS)
    ]
    later = later[later["doc_date"] <= cutoff]
    out = []
    for (cp, doc_no, doc_date), g in later.groupby(["counterparty_ref", "doc_no", "doc_date"], dropna=False):
        out.append(
            engine.Obligation(
                id=f"rec:{cp}:{doc_no}",
                kind="invoice_out",
                direction="inflow",
                amount_kop=int(g["amount_kop"].sum()),
                due_date=doc_date + timedelta(days=30),
                certainty=1.0,
                counterparty_ref=cp if isinstance(cp, str) else None,
                doc_date=doc_date,
                status="planned",
                label="Счёт клиенту",
            )
        )
    return out


def actual_balance(hist: pd.DataFrame, cutoff: date, horizon: int) -> np.ndarray | None:
    """End-of-day balance for cutoff+1..cutoff+horizon minus rescue inflows after the cut-off."""
    days = [cutoff + timedelta(days=i) for i in range(1, horizon + 1)]
    future = hist[(hist["op_date"] > cutoff) & (hist["op_date"] <= days[-1])]
    if hist["op_date"].max() < days[-1]:
        return None
    eod = hist.groupby("op_date")["balance_eod_kop"].last()
    past = eod[eod.index <= cutoff]
    if past.empty:
        return None
    series = eod.reindex(sorted(set(eod.index) | set(days))).ffill()
    rescue = future[future["is_rescue"]].groupby("op_date")["amount_kop"].sum()
    cum_rescue = rescue.reindex(days, fill_value=0).cumsum()
    return series.reindex(days).to_numpy(dtype=float) - cum_rescue.to_numpy(dtype=float)


@dataclass
class CutoffResult:
    variant: str
    cutoff: date
    mae: float
    mae_h7: float
    mae_h30: float
    coverage: float
    turnover: float
    actual_gap: bool
    predicted_gap: bool
    lead_days: int | None


def run_cutoff(
    hist: pd.DataFrame, cutoff: date, horizon: int, variants=VARIANTS, n_paths: int = 300
) -> list[CutoffResult]:
    actual = actual_balance(hist, cutoff, horizon)
    if actual is None:
        return []
    past = hist[hist["op_date"] <= cutoff]
    recent = past[past["op_date"] > cutoff - timedelta(days=90)]
    turnover = float(recent["amount_kop"].abs().sum() / 90) or 1.0
    actual_gap_days = np.nonzero(actual < 0)[0]
    actual_gap = len(actual_gap_days) > 0
    results = []
    for name, (model, layers) in variants.items():
        if model is None:
            opening = float(past.loc[past["op_date"] == past["op_date"].max(), "balance_eod_kop"].iloc[0])
            p10 = p50 = p90 = np.full(horizon, opening)
            gaps = [] if opening >= 0 else [{"start_date": str(cutoff + timedelta(days=1))}]
        else:
            obligations = reconstructed_invoices(hist, cutoff, horizon) if "invoices" in layers else []
            fitted = engine.fit(past, cutoff, obligations, model_name=model, layers=layers)
            out = engine.simulate(fitted, obligations, engine.Settings(), horizon=horizon, n_paths=n_paths, seed=11)
            p10 = np.array([p["p10_kop"] for p in out["points"]], dtype=float)
            p50 = np.array([p["p50_kop"] for p in out["points"]], dtype=float)
            p90 = np.array([p["p90_kop"] for p in out["points"]], dtype=float)
            gaps = out["gaps"]
        err = np.abs(p50 - actual)
        predicted_gap = bool(gaps)
        lead = None
        if actual_gap and predicted_gap:
            lead = int(actual_gap_days[0]) + 1
        results.append(
            CutoffResult(
                variant=name,
                cutoff=cutoff,
                mae=float(err.mean()),
                mae_h7=float(err[:7].mean()),
                mae_h30=float(err.mean()),
                coverage=float(((actual >= p10) & (actual <= p90)).mean()),
                turnover=turnover,
                actual_gap=actual_gap,
                predicted_gap=predicted_gap,
                lead_days=lead,
            )
        )
    return results


def cutoffs_for(hist: pd.DataFrame, horizon: int, count: int = 12, step_days: int = 14) -> list[date]:
    last = hist["op_date"].max() - timedelta(days=horizon)
    first = hist["op_date"].min() + timedelta(days=120)
    out = []
    c = last
    while c >= first and len(out) < count:
        out.append(c)
        c -= timedelta(days=step_days)
    return sorted(out)


def summarize(results: list[CutoffResult]) -> list[dict]:
    rows = []
    by_variant: dict[str, list[CutoffResult]] = {}
    for r in results:
        by_variant.setdefault(r.variant, []).append(r)
    for name, rs in by_variant.items():
        tp = sum(r.actual_gap and r.predicted_gap for r in rs)
        fp = sum((not r.actual_gap) and r.predicted_gap for r in rs)
        fn = sum(r.actual_gap and not r.predicted_gap for r in rs)
        leads = [r.lead_days for r in rs if r.lead_days is not None]
        rows.append(
            {
                "variant": name,
                "cutoffs": len(rs),
                "mae_rub": round(float(np.mean([r.mae for r in rs])) / 100, 0),
                "mae_h7_rub": round(float(np.mean([r.mae_h7 for r in rs])) / 100, 0),
                "mae_turnover_days": round(float(np.mean([r.mae / r.turnover for r in rs])), 2),
                "coverage_p10_p90": round(float(np.mean([r.coverage for r in rs])), 3),
                "gap_precision": round(tp / (tp + fp), 3) if tp + fp else None,
                "gap_recall": round(tp / (tp + fn), 3) if tp + fn else None,
                "gap_lead_days_median": float(np.median(leads)) if leads else None,
                "actual_gap_windows": tp + fn,
            }
        )
    return sorted(rows, key=lambda r: r["mae_turnover_days"])
