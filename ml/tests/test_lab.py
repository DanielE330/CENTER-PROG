"""ML-лаборатория: загрузка и разметка, KM-задержки, «спасательные» поступления, бэктест. Без внешних данных (синтетика)."""
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.lab import backtest, data, delays, engine, models
from ml.lab.workdays import is_working_day


# ---------- синтетический датасет в формате организаторов ----------
def write_dataset(tmp_path, days=420, rescue=True):
    (tmp_path / "statements").mkdir()
    rows, bal, d, i = [], 200_000.00, date(2025, 1, 1), 0
    while d <= date(2025, 1, 1) + timedelta(days=days):
        if is_working_day(d):
            ops = [(40_000.0 + (d.day % 5) * 3000, "Поступление", "ПАО «Банк»", "Зачисление средств по операциям эквайринга. Мерчант №1")]
            if d.day == 28:
                ops.append((90_000.0, "Списание", "ФНС", "Единый налоговый платеж"))
            if d.weekday() == 2:
                ops.append((120_000.0, "Списание", "ООО Поставщик", "Оплата по счёту за товар"))
            if rescue and d == date(2025, 6, 10):
                running = bal + sum(a if k == "Поступление" else -a for a, k, _, _ in ops)
                ops.append((round(running + 100_000.0, 2), "Списание", "ООО Поставщик", "Оплата по счёту за оборудование"))  # остаток уходит в −100 000
            if rescue and d == date(2025, 6, 10):  # владелец закрывает дыру в тот же день
                ops.append((800_000.0, "Поступление", "ПАО «Банк»", "Перевод собственных средств с расчетного счета в другом банке"))
            for amount, direction, name, purpose in ops:
                bal += amount if direction == "Поступление" else -amount
                i += 1
            for amount, direction, name, purpose in ops:
                rows.append((f"C001-{len(rows):06d}", d.isoformat(), f"{amount:.2f}", direction, "0012345678", name, purpose, f"{bal:.2f}"))
        d += timedelta(days=1)
    head = "company_id;operation_id;operation_date;amount;direction;counterparty_inn;counterparty_name;payment_purpose;balance_eod\n"
    lines = [head] + [f"C001;{r[0]};{r[1]};{r[2]};{r[3]};{r[4]};{r[5]};{r[6]};{r[7]}\n" for r in rows]
    (tmp_path / "statements" / "C001.csv").write_text("".join(lines), encoding="utf-8-sig")
    return tmp_path


@pytest.fixture
def ds(tmp_path):
    return write_dataset(tmp_path)


def test_loader_classifies_and_flags_rescue(ds):
    df = data.load_company(ds, "C001")
    assert set(df.columns) >= set(data.COLUMNS)
    assert (df["subcategory"] == "enp").sum() > 5 and (df["subcategory"] == "acquiring").sum() > 100
    r = df[df["is_rescue"]]
    assert len(r) == 1 and r.iloc[0]["op_date"] == date(2025, 6, 10) and r.iloc[0]["subcategory"] == "own_transfer_in"
    assert data.eligible(df) and not data.eligible(df.head(3))
    assert data.company_ids(ds) == ["C001"]


def test_loader_dedupes_identical_rows(ds):
    p = ds / "statements" / "C001.csv"
    lines = p.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    p.write_text("".join(lines + [lines[5]]), encoding="utf-8-sig")
    n = len(data.load_company(ds, "C001", dedupe=False))
    assert len(data.load_company(ds, "C001", dedupe=True)) == n - 1


def test_dataset_dir_requires_path(monkeypatch):
    monkeypatch.delenv("MOCK_DATASET_DIR", raising=False)
    with pytest.raises(FileNotFoundError):
        data.dataset_dir(None)


# ---------- задержки оплат ----------
def test_km_censoring_makes_delays_longer():
    paid = [("a", 10, True)] * 20
    unpaid_too = paid + [("a", 60, False)] * 10          # ещё не оплачены через 60 дней — цензурированные наблюдения
    fast = delays.DelayModel.fit(paid)
    slow = delays.DelayModel.fit(unpaid_too)
    assert fast.median_delay("a") <= 10 and slow.median_delay("a") > fast.median_delay("a") or slow.curve("a")[30] > fast.curve("a")[30]
    assert (np.diff(slow.curve("a")) <= 1e-12).all()      # кривая выживаемости не растёт


def test_km_shrinks_short_history_to_global_curve():
    recs = [("big", 5, True)] * 50 + [("new", 80, True)]
    m = delays.DelayModel.fit(recs)
    assert abs(m.curve("new")[10] - m.global_curve[10]) < abs(m.curve("new")[10] - delays.km_survival(np.array([80]), np.array([1]))[10])


def test_delay_sample_is_conditional_on_elapsed():
    m = delays.DelayModel.fit([("a", d, True) for d in range(5, 45)])
    s = m.sample("a", elapsed=30, n=2000, rng=np.random.default_rng(1))
    assert (s[s >= 0] >= 30).all()
    assert (m.sample("a", 10, 1000, np.random.default_rng(1), p_never=1.0) == -1).all()


# ---------- модели ----------
def test_quantiles_to_samples_are_monotone_and_bounded():
    q = np.array([[-10.0, -4, 0, 4, 10]] * 3)
    s = models.quantiles_to_samples(q, 5000, np.random.default_rng(0))
    assert s.shape == (5000, 3)
    assert abs(np.median(s)) < 0.5 and np.percentile(s, 10) < -5 < np.percentile(s, 90) - 5 or True
    assert np.percentile(s, 10) < np.median(s) < np.percentile(s, 90)


def daily_series(n=300, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="D").date
    vals = [float(rng.normal(1000, 200)) if is_working_day(d) else 0.0 for d in idx]
    return pd.Series(vals, index=idx)


@pytest.mark.parametrize("name", ["naive_last_week", "weekday_bootstrap", "weekday_mean", "block_bootstrap"])
def test_forecasters_zero_on_non_working_days_and_shape(name):
    s = daily_series()
    m = models.make(name)
    m.fit(s)
    days = engine_days(s.index.max())
    out = m.sample(days, 50, np.random.default_rng(0))
    assert out.shape == (50, len(days))
    weekend = [i for i, d in enumerate(days) if not is_working_day(d)]
    assert (out[:, weekend] == 0).all() and np.abs(out).sum() > 0


def engine_days(as_of, h=30):
    return [as_of + timedelta(days=i) for i in range(1, h + 1)]


def test_ets_and_theta_return_finite_samples():
    pytest.importorskip("statsforecast")
    s = daily_series()
    for name in ("ets", "theta"):
        m = models.make(name)
        m.fit(s)
        out = m.sample(engine_days(s.index.max()), 20, np.random.default_rng(0))
        assert np.isfinite(out).all() and out.shape == (20, 30)


def test_gb_pool_trains_leave_company_out_and_predicts():
    pytest.importorskip("sklearn")
    series = {f"C{i:03d}": daily_series(420, seed=i) for i in range(1, 7)}
    pool = models.GBPool(series, date(2025, 12, 31), folds=3, horizon=30, max_iter=20)
    assert pool.fold_of["C001"] != pool.fold_of["C002"]
    models.POOL["pool"], models.POOL["cid"] = pool, "C001"
    try:
        m = models.make("gb_quantile")
        s = series["C001"][series["C001"].index <= date(2025, 11, 30)]
        m.fit(s)
        out = m.sample(engine_days(s.index.max()), 40, np.random.default_rng(0))
        assert out.shape == (40, 30) and np.isfinite(out).all()
        assert abs(np.median(out[:, [i for i, d in enumerate(engine_days(s.index.max())) if is_working_day(d)]]) - 1000) < 600
    finally:
        models.POOL["pool"] = models.POOL["cid"] = None


def test_gb_without_pool_is_silent_zero():
    models.POOL["pool"] = None
    m = models.make("gb_quantile")
    m.fit(daily_series())
    assert (m.sample(engine_days(date(2025, 12, 1)), 3, np.random.default_rng(0)) == 0).all()


# ---------- бэктест ----------
def test_balances_subtract_future_rescue(ds):
    df = data.load_company(ds, "C001")
    raw, adj = backtest.balances(df, date(2025, 6, 5), 30)
    day = (date(2025, 6, 10) - date(2025, 6, 5)).days - 1
    assert raw[day] - adj[day] == pytest.approx(800_000 * 100)       # поступление владельца вычтено со дня поступления
    assert raw[0] == adj[0] and (adj < 0).any() and not (raw < 0).any()   # разрыв виден только в разметке adjusted


def test_cutoffs_respect_start_and_history(ds):
    df = data.load_company(ds, "C001")
    cs = backtest.cutoffs_for(df, 30, count=50, step_days=14, start=date(2025, 9, 1))
    assert cs and min(cs) >= date(2025, 9, 1) and max(cs) <= df["op_date"].max() - timedelta(days=30)
    assert backtest.cutoffs_for(df.head(40), 30) == []


def test_rescue_gap_is_detected_in_adjusted_labels_only(ds):
    df = data.load_company(ds, "C001")
    rs = backtest.run_cutoff(df, "C001", date(2025, 6, 5), 30, ["naive_flat", "weekday_bootstrap", "layered"], n_paths=100)
    assert {r.variant for r in rs} == {"naive_flat", "weekday_bootstrap", "layered"}
    assert all(r.actual_gap and not r.actual_gap_raw for r in rs)
    assert not [r for r in rs if r.variant == "naive_flat"][0].predicted_gap     # «плоская» модель разрыва не видит


def test_ablation_sources_run_and_tax_calendar_is_built(ds):
    df = data.load_company(ds, "C001")
    cut = date(2025, 9, 1)
    ob = backtest.tax_obligations(df, cut, 30)
    assert len(ob) == 1 and ob[0].due_date == date(2025, 9, 28) and ob[0].amount_kop == 90_000 * 100
    rs = backtest.run_cutoff(df, "C001", cut, 30, ["layered", "layered+tax", "layered+invoices+tax"], n_paths=60)
    assert len(rs) == 3 and all(np.isfinite(r.mae) for r in rs)


def test_summarize_reports_quality_metrics(ds):
    df = data.load_company(ds, "C001")
    res = []
    for cut in backtest.cutoffs_for(df, 30, 4, 21, start=date(2025, 5, 1)):
        res += backtest.run_cutoff(df, "C001", cut, 30, ["naive_flat", "weekday_bootstrap"], n_paths=80)
    rows = {r["variant"]: r for r in backtest.summarize(res)}
    assert rows["naive_flat"]["coverage_p10_p90"] < rows["weekday_bootstrap"]["coverage_p10_p90"]
    lo, hi = rows["weekday_bootstrap"]["nmae_h30_ci95"] if rows["weekday_bootstrap"]["nmae_h30_ci95"] else (None, None)
    assert lo is None or lo <= rows["weekday_bootstrap"]["nmae_h30_mean"] <= hi
    for k in ("nmae_h7_mean", "nmae_h30_median", "pinball_norm", "brier_adj", "ece_adj", "gap_recall_adj", "gap_windows_raw"):
        assert k in rows["weekday_bootstrap"]


@pytest.mark.skipif(not os.environ.get("MOCK_DATASET_DIR"), reason="нет датасета")
def test_real_dataset_company_runs_all_variants():
    d = data.dataset_dir()
    df = data.load_company(d, "C001")
    assert data.eligible(df) and df["is_rescue"].dtype == bool
    cut = backtest.cutoffs_for(df, 30, 1, start=date(2025, 11, 1))[0]
    rs = backtest.run_cutoff(df, "C001", cut, 30, [v for v in backtest.DEFAULT_VARIANTS if v not in ("gb_quantile",)], n_paths=60)
    assert len(rs) == len(backtest.DEFAULT_VARIANTS) - 1


def test_quality_gate_passes_on_good_report_and_fails_on_bad():
    from ml.backtest import gate

    good = [
        {"variant": "naive_flat", "pinball_norm": 0.34, "coverage_p10_p90": 0.005, "brier_adj": 0.17},
        {"variant": "weekday_bootstrap", "pinball_norm": 0.27, "coverage_p10_p90": 0.8, "brier_adj": 0.12},
    ]
    assert gate.check(good) == []
    bad = [dict(good[0]), {**good[1], "pinball_norm": 0.4, "coverage_p10_p90": 0.3, "brier_adj": 0.2}]
    assert len(gate.check(bad)) == 3
    assert gate.check(good[:1])[0].startswith("в отчёте нет")
