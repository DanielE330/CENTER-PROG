# Адаптировано из проекта «ЦИ1» (ветка gleb, swimshadow): идея rolling-origin и метка «спасательных» сохранены;
# добавлены сравнение семейств моделей, абляция источников, две разметки разрыва и калибровка вероятности.
"""Rolling-origin бэктест. В каждой точке среза модель видит только прошлое и оценивается на следующие `horizon` дней.

Разрыв размечен двумя способами:
- `raw` — остаток действительно ушёл в минус;
- `adjusted` — то же, но без «спасательных» поступлений собственных денег (владелец закрыл дыру): это настоящие
  разрывы, которые видны только после вычитания его вмешательства. По `adjusted` и строится основная оценка.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ml.lab import engine, models

NONE: frozenset[str] = frozenset()
REC = frozenset({"recurring"})


@dataclass(frozen=True)
class Variant:
    model: str | None  # None — «остаток не меняется»
    layers: frozenset[str] = NONE
    extras: frozenset[str] = NONE  # источники из obligations: invoices (реконструкция счетов), tax (налоговый календарь)
    family: str = "baseline"


VARIANTS: dict[str, Variant] = {
    "naive_flat": Variant(None, family="baseline"),
    "naive_last_week": Variant("naive_last_week", family="baseline"),
    "weekday_bootstrap": Variant("weekday_bootstrap", family="residual"),
    "block_bootstrap": Variant("block_bootstrap", family="residual"),
    "ets": Variant("ets", family="residual"),
    "theta": Variant("theta", family="residual"),
    "gb_quantile": Variant("gb_quantile", family="residual"),
    "chronos": Variant("chronos", family="residual"),
    "layered": Variant("weekday_bootstrap", REC, family="layers"),
    "layered_block": Variant("block_bootstrap", REC, family="layers"),
    "layered+invoices": Variant("weekday_bootstrap", REC, frozenset({"invoices"}), family="sources"),
    "layered+tax": Variant("weekday_bootstrap", REC, frozenset({"tax"}), family="sources"),
    "layered+invoices+tax": Variant("weekday_bootstrap", REC, frozenset({"invoices", "tax"}), family="sources"),
}
DEFAULT_VARIANTS = [v for v in VARIANTS if v != "chronos"]


def reconstructed_invoices(hist: pd.DataFrame, cutoff: date, horizon: int) -> list[engine.Obligation]:
    """Счета, известные на срез: документ датирован не позже среза, оплата пришла позже. Сумма — по найденным платежам."""
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
                id=f"rec:{cp}:{doc_no}", kind="invoice_out", direction="inflow", amount_kop=int(g["amount_kop"].sum()),
                due_date=doc_date + timedelta(days=30), certainty=1.0, counterparty_ref=cp if isinstance(cp, str) else None,
                doc_date=doc_date, status="planned", label="Счёт клиенту",
            )
        )
    return out


def tax_obligations(hist: pd.DataFrame, cutoff: date, horizon: int) -> list[engine.Obligation]:
    """Налоговый календарь: ЕНП 28-го; сумма — медиана месячных налоговых платежей за последние полгода (как `estimate_tax`)."""
    past = hist[(hist["subcategory"] == "enp") & (hist["op_date"] <= cutoff) & (hist["op_date"] > cutoff - timedelta(days=200))]
    if past.empty:
        return []
    monthly = past.groupby(past["op_date"].map(lambda d: (d.year, d.month)))["amount_kop"].sum()
    if len(monthly) < 3:
        return []
    amount = int(-np.median(monthly.to_numpy()))
    if amount <= 0:
        return []
    out, end = [], cutoff + timedelta(days=horizon)
    y, m = cutoff.year, cutoff.month
    for _ in range(3):
        due = date(y, m, 28)
        if cutoff < due <= end:
            out.append(
                engine.Obligation(
                    id=f"tax:{due}", kind="tax", direction="outflow", amount_kop=amount, due_date=due, certainty=0.95,
                    counterparty_ref=None, doc_date=None, status="planned", label="Налоги (ЕНП)",
                )
            )
        y, m = y + (m == 12), m % 12 + 1
    return out


def balances(hist: pd.DataFrame, cutoff: date, horizon: int) -> tuple[np.ndarray, np.ndarray] | None:
    """(остаток как есть, остаток без «спасательных» поступлений после среза) на дни cutoff+1..cutoff+horizon."""
    days = [cutoff + timedelta(days=i) for i in range(1, horizon + 1)]
    if hist["op_date"].max() < days[-1]:
        return None
    eod = hist.groupby("op_date")["balance_eod_kop"].last()
    if eod[eod.index <= cutoff].empty:
        return None
    series = eod.reindex(sorted(set(eod.index) | set(days))).ffill()
    future = hist[(hist["op_date"] > cutoff) & (hist["op_date"] <= days[-1])]
    rescue = future[future["is_rescue"]].groupby("op_date")["amount_kop"].sum()
    cum = rescue.reindex(days, fill_value=0).cumsum().to_numpy(dtype=float)
    raw = series.reindex(days).to_numpy(dtype=float)
    return raw, raw - cum


@dataclass
class CutoffResult:
    variant: str
    company: str
    cutoff: date
    err: dict[int, float]  # |p50 − факт| на днях 7/14/30, в копейках
    mae: float
    scale: float  # недельный оборот |операций|, копейки
    turnover_day: float
    coverage: float
    pinball: float
    actual_gap: bool  # разметка adjusted
    actual_gap_raw: bool
    predicted_gap: bool
    p_gap: float
    lead_days: int | None
    extra: dict = field(default_factory=dict)


def _pinball(actual: np.ndarray, qs: dict[float, np.ndarray]) -> float:
    return float(np.mean([np.mean(np.maximum(q * (actual - v), (q - 1) * (actual - v))) for q, v in qs.items()]))


def run_cutoff(hist: pd.DataFrame, cid: str, cutoff: date, horizon: int, variants: list[str], n_paths: int = 200) -> list[CutoffResult]:
    bal = balances(hist, cutoff, horizon)
    if bal is None:
        return []
    raw, adj = bal
    past = hist[hist["op_date"] <= cutoff]
    recent = past[past["op_date"] > cutoff - timedelta(days=90)]
    turnover_day = float(recent["amount_kop"].abs().sum() / 90) or 1.0
    scale = models.company_scale(past.groupby("op_date")["amount_kop"].sum()) or turnover_day * 7
    gap_idx = np.nonzero(adj < 0)[0]
    models.POOL["cid"] = cid
    results = []
    for name in variants:
        v = VARIANTS[name]
        if v.model is None:
            opening = float(past.loc[past["op_date"] == past["op_date"].max(), "balance_eod_kop"].iloc[0])
            p10 = p50 = p90 = np.full(horizon, opening)
            predicted, p_gap = opening < 0, float(opening < 0)
            gaps = [{"start_date": str(cutoff + timedelta(days=1))}] if predicted else []
        else:
            obligations: list[engine.Obligation] = []
            if "invoices" in v.extras:
                obligations += reconstructed_invoices(hist, cutoff, horizon)
            if "tax" in v.extras:
                obligations += tax_obligations(hist, cutoff, horizon)
            fitted = engine.fit(past, cutoff, obligations, model_name=v.model, layers=v.layers | (frozenset({"invoices"}) if "invoices" in v.extras else NONE))
            out = engine.simulate(fitted, obligations, engine.Settings(), horizon=horizon, n_paths=n_paths, seed=11)
            p10, p50, p90 = (np.array([p[k] for p in out["points"]], dtype=float) for k in ("p10_kop", "p50_kop", "p90_kop"))
            gaps, p_gap = out["gaps"], float(out["p_gap"])
            predicted = bool(gaps)
        err = np.abs(p50 - adj)
        lead = int(gap_idx[0]) + 1 if len(gap_idx) and predicted else None
        results.append(
            CutoffResult(
                variant=name, company=cid, cutoff=cutoff, err={h: float(err[h - 1]) for h in (7, 14, 30) if h <= horizon},
                mae=float(err.mean()), scale=scale, turnover_day=turnover_day,
                coverage=float(((adj >= p10) & (adj <= p90)).mean()),
                pinball=_pinball(adj, {0.1: p10, 0.5: p50, 0.9: p90}),
                actual_gap=len(gap_idx) > 0, actual_gap_raw=bool((raw < 0).any()), predicted_gap=predicted, p_gap=p_gap, lead_days=lead,
            )
        )
    return results


def cutoffs_for(hist: pd.DataFrame, horizon: int, count: int = 12, step_days: int = 14, start: date | None = None) -> list[date]:
    """Срезы назад от последней даты с шагом `step_days`; не раньше `start` (если задан) и не раньше 120 дней истории."""
    last = hist["op_date"].max() - timedelta(days=horizon)
    first = hist["op_date"].min() + timedelta(days=120)
    if start:
        first = max(first, start)
    out, c = [], last
    while c >= first and len(out) < count:
        out.append(c)
        c -= timedelta(days=step_days)
    return sorted(out)


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 6) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total, e = len(p), 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & ((p < hi) | (hi == 1.0)) if hi == 1.0 else (p >= lo) & (p < hi)
        if m.any():
            e += m.sum() / total * abs(p[m].mean() - y[m].mean())
    return float(e)


def _ci(values_by_company: dict[str, list[float]], n_boot: int = 1000, seed: int = 5) -> tuple[float, float]:
    """95 % доверительный интервал среднего: бутстреп по компаниям (срезы одной компании зависимы, поэтому ресэмплим компании)."""
    means = np.array([np.mean(v) for v in values_by_company.values()])
    if len(means) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    boots = rng.choice(means, size=(n_boot, len(means))).mean(axis=1)
    return float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def summarize(results: list[CutoffResult]) -> list[dict]:
    by: dict[str, list[CutoffResult]] = {}
    for r in results:
        by.setdefault(r.variant, []).append(r)
    rows = []
    for name, rs in by.items():
        row = {"variant": name, "family": VARIANTS[name].family, "cutoffs": len(rs), "companies": len({r.company for r in rs})}
        for h in (7, 14, 30):
            vals = [r.err[h] / r.scale * 100 for r in rs if h in r.err]
            row[f"nmae_h{h}_mean"] = round(float(np.mean(vals)), 1) if vals else None
            row[f"nmae_h{h}_median"] = round(float(np.median(vals)), 1) if vals else None
        for key, vals in (
            ("nmae_h30", [(r.company, r.err[30] / r.scale * 100) for r in rs if 30 in r.err]),
            ("pinball", [(r.company, r.pinball / r.scale) for r in rs]),
        ):
            per: dict[str, list[float]] = {}
            for c, v in vals:
                per.setdefault(c, []).append(v)
            lo, hi = _ci(per)
            row[f"{key}_ci95"] = [round(lo, 3 if key == "pinball" else 1), round(hi, 3 if key == "pinball" else 1)] if lo == lo else None
        row["mae_turnover_days"] = round(float(np.mean([r.mae / r.turnover_day for r in rs])), 2)
        row["coverage_p10_p90"] = round(float(np.mean([r.coverage for r in rs])), 3)
        row["pinball_norm"] = round(float(np.mean([r.pinball / r.scale for r in rs])), 4)
        for label, key in (("adj", "actual_gap"), ("raw", "actual_gap_raw")):
            tp = sum(getattr(r, key) and r.predicted_gap for r in rs)
            fp = sum((not getattr(r, key)) and r.predicted_gap for r in rs)
            fn = sum(getattr(r, key) and not r.predicted_gap for r in rs)
            row[f"gap_precision_{label}"] = round(tp / (tp + fp), 3) if tp + fp else None
            row[f"gap_recall_{label}"] = round(tp / (tp + fn), 3) if tp + fn else None
            row[f"gap_windows_{label}"] = tp + fn
        leads = [r.lead_days for r in rs if r.lead_days is not None]
        row["gap_lead_days_median"] = float(np.median(leads)) if leads else None
        p = np.array([r.p_gap for r in rs])
        y = np.array([r.actual_gap for r in rs], dtype=float)
        row["brier_adj"] = round(float(np.mean((p - y) ** 2)), 4)
        row["ece_adj"] = round(_ece(p, y), 4)
        rows.append(row)
    return sorted(rows, key=lambda r: r["nmae_h30_mean"] if r["nmae_h30_mean"] is not None else 1e9)
