from datetime import date, timedelta

import numpy as np
from forecast import backtest, engine
from forecast.delays import DelayModel, km_survival


def make_history(days=400, start=date(2025, 1, 1), rescue_on=None):
    rows, balance = [], 1_000_000_00
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        flows = [(50_000_00, "acquiring", "revenue", None)]
        if d.day == 5:
            flows.append((-300_000_00, "rent", "rent_utilities", "cp_rent"))
        if d.day == 15:
            flows.append((-600_000_00, "salary", "payroll", "cp_bank"))
        for amount, sub, grp, cp in flows:
            balance += amount
            rows.append(
                {
                    "op_date": d,
                    "amount_kop": amount,
                    "balance_eod_kop": balance,
                    "counterparty_ref": cp,
                    "subcategory": sub,
                    "group": grp,
                    "is_rescue": False,
                    "doc_date": None,
                    "doc_no": None,
                }
            )
        if rescue_on and d == rescue_on:
            balance += 500_000_00
            rows.append(
                {
                    "op_date": d,
                    "amount_kop": 500_000_00,
                    "balance_eod_kop": balance,
                    "counterparty_ref": "cp_owner",
                    "subcategory": "own_transfer_in",
                    "group": "own_funds",
                    "is_rescue": True,
                    "doc_date": None,
                    "doc_no": None,
                }
            )
    for r in rows:
        r["balance_eod_kop"] = None
    # end-of-day balance is the same for all operations of a day
    running, by_day = 1_000_000_00, {}
    for r in rows:
        running += r["amount_kop"]
        by_day[r["op_date"]] = running
    for r in rows:
        r["balance_eod_kop"] = by_day[r["op_date"]]
    return engine.history_frame(rows)


def test_monthly_rent_and_salary_are_detected():
    hist = make_history()
    patterns, _ = engine.detect_patterns(hist, hist["op_date"].max())
    subs = {p.subcategory for p in patterns}
    assert {"rent", "salary"} <= subs


def test_forecast_is_reproducible_with_seed():
    hist = make_history()
    as_of = hist["op_date"].max()
    f = engine.fit(hist, as_of, [])
    a = engine.simulate(f, [], engine.Settings(), horizon=30, n_paths=200, seed=3)
    b = engine.simulate(f, [], engine.Settings(), horizon=30, n_paths=200, seed=3)
    assert a["points"] == b["points"]


def test_intervals_are_ordered():
    hist = make_history()
    f = engine.fit(hist, hist["op_date"].max(), [])
    out = engine.simulate(f, [], engine.Settings(), horizon=30, n_paths=300)
    assert all(p["p10_kop"] <= p["p50_kop"] <= p["p90_kop"] for p in out["points"])


def test_large_known_payment_creates_gap_with_driver():
    hist = make_history()
    as_of = hist["op_date"].max()
    f = engine.fit(hist, as_of, [])
    big = engine.Obligation(
        id="x",
        kind="manual",
        direction="outflow",
        amount_kop=10**13,
        due_date=as_of + timedelta(days=10),
        certainty=1.0,
        counterparty_ref=None,
        doc_date=None,
        status="planned",
        label="Оборудование",
    )
    out = engine.simulate(f, [big], engine.Settings(), horizon=30, n_paths=200)
    assert out["gaps"], "a payment far above the balance must produce a gap"
    gap = out["gaps"][0]
    assert gap["probability"] > 0.9
    assert any(d["label"] == "Оборудование" for d in gap["drivers"])


def test_neutral_scenario_equals_base():
    hist = make_history()
    f = engine.fit(hist, hist["op_date"].max(), [])
    base = engine.simulate(f, [], engine.Settings(), horizon=20, n_paths=100, seed=5)
    same = engine.simulate(
        f,
        [],
        engine.Settings(),
        horizon=20,
        n_paths=100,
        seed=5,
        scenario=engine.Scenario.parse([{"type": "scale_group", "group": "revenue", "factor": 1.0}]),
    )
    assert base["points"] == same["points"]


def test_revenue_drop_lowers_balance():
    hist = make_history()
    f = engine.fit(hist, hist["op_date"].max(), [])
    base = engine.simulate(f, [], engine.Settings(), horizon=30, n_paths=200, seed=5)
    worse = engine.simulate(
        f,
        [],
        engine.Settings(),
        horizon=30,
        n_paths=200,
        seed=5,
        scenario=engine.Scenario.parse([{"type": "scale_group", "group": "revenue", "factor": 0.7}]),
    )
    assert worse["points"][-1]["p50_kop"] < base["points"][-1]["p50_kop"]


def test_short_history_is_flagged():
    hist = make_history(days=40)
    f = engine.fit(hist, hist["op_date"].max(), [])
    assert "short_history" in f.warnings


def test_rescue_inflow_is_removed_from_actual_balance():
    rescue = date(2025, 6, 2)
    hist = make_history(rescue_on=rescue)
    cutoff = rescue - timedelta(days=5)
    actual = backtest.actual_balance(hist, cutoff, 30)
    raw = hist.groupby("op_date")["balance_eod_kop"].last()
    after = [d for d in raw.index if d > rescue][0]
    idx = (after - cutoff).days - 1
    assert raw[after] - actual[idx] == 500_000_00


def test_km_survival_is_monotone_and_censoring_raises_it():
    paid = km_survival(np.array([5, 10, 20, 30]), np.array([1, 1, 1, 1]))
    censored = km_survival(np.array([5, 10, 20, 30]), np.array([1, 1, 0, 0]))
    assert np.all(np.diff(paid) <= 1e-12)
    assert censored[25] >= paid[25]


def test_delay_sample_respects_elapsed_days():
    model = DelayModel.fit([("a", d, True) for d in range(5, 40)])
    rng = np.random.default_rng(1)
    s = model.sample("a", 20, 500, rng)
    assert (s[s >= 0] >= 20).all()
