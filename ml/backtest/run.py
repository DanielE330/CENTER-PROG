"""Единый бэктест на датасете организаторов: сравнение подходов, абляция источников, разметка разрывов.

    python -m ml.backtest.run --data "<каталог датасета>" --companies 30 --test-start 2025-11-01

Датасет только читается. Отчёт: ml/reports/backtest-<дата>.json и .md (sha256 данных внутри).
Модели `gb_quantile` и `chronos` требуют отложенного периода: они обучаются только на данных до --test-start.
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ml.lab import backtest, data, models  # noqa: E402


def daily_flow(df: pd.DataFrame) -> pd.Series:
    """Суммарный чистый поток по календарным дням без «спасательных» поступлений (то, что учит остаточная модель)."""
    flows = df[~df["is_rescue"]].groupby("op_date")["amount_kop"].sum()
    idx = pd.date_range(df["op_date"].min(), df["op_date"].max(), freq="D").date
    return flows.reindex(idx, fill_value=0)


def to_markdown(rows: list[dict], meta: dict) -> str:
    cols = [
        ("variant", "подход"), ("nmae_h7_mean", "NMAE 7д, %"), ("nmae_h14_mean", "14д"), ("nmae_h30_mean", "30д"), ("nmae_h30_ci95", "95% ДИ 30д"),
        ("coverage_p10_p90", "покрытие P10–P90"), ("pinball_norm", "pinball"), ("pinball_ci95", "95% ДИ pinball"), ("gap_precision_adj", "точность разрыва"),
        ("gap_recall_adj", "полнота"), ("gap_lead_days_median", "заблаговр., дн"), ("brier_adj", "Brier"), ("ece_adj", "ECE"),
    ]
    out = [f"# Бэктест {meta['date']}", "", f"Компаний: {meta['companies_n']}, срезов на компанию: до {meta['cutoffs']}, горизонт {meta['horizon']} дн., "
           f"тестовый период с {meta['test_start']}, траекторий {meta['paths']}. sha256 данных: `{meta['data_sha256'][:16]}…`", "",
           "| " + " | ".join(c[1] for c in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("—" if r.get(k) is None else str(r[k]) for k, _ in cols) + " |")
    out += ["", "ДИ — бутстреп по компаниям (срезы одной компании зависимы). Различия внутри перекрывающихся интервалов считать шумом.", "Разметка разрыва: *adjusted* — остаток < 0 без «спасательных» поступлений собственных денег. Чем ниже NMAE и Brier, тем лучше.",
            f"Бустинг: {meta.get('gb_backend', 'не использовался')}."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--companies", type=int, default=30)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--cutoffs", type=int, default=8)
    ap.add_argument("--step", type=int, default=14)
    ap.add_argument("--paths", type=int, default=200)
    ap.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 11, 1))
    ap.add_argument("--variants", default=",".join(backtest.DEFAULT_VARIANTS))
    ap.add_argument("--out", type=Path, default=ROOT / "ml" / "reports")
    args = ap.parse_args()
    variants = [v for v in args.variants.split(",") if v]
    unknown = set(variants) - set(backtest.VARIANTS)
    if unknown:
        sys.exit(f"неизвестные подходы: {sorted(unknown)}")

    d = data.dataset_dir(args.data)
    ids = data.company_ids(d)
    step = max(1, len(ids) // args.companies)
    chosen = ids[::step][: args.companies]
    t0 = time.time()
    frames = {c: data.load_company(d, c) for c in chosen}
    chosen = [c for c in chosen if data.eligible(frames[c])]
    print(f"данные загружены: {len(chosen)} компаний, {time.time() - t0:.0f} с", file=sys.stderr)

    if any(v in ("gb_quantile",) for v in variants):
        series = {c: daily_flow(frames[c]) for c in chosen}
        models.POOL["pool"] = models.GBPool(series, args.test_start - timedelta(days=1), horizon=args.horizon)

    results = []
    for c in chosen:
        for cut in backtest.cutoffs_for(frames[c], args.horizon, args.cutoffs, args.step, start=args.test_start):
            results += backtest.run_cutoff(frames[c], c, cut, args.horizon, variants, args.paths)
        print(f"{c}: {len(results)} результатов, {time.time() - t0:.0f} с", file=sys.stderr)

    rows = backtest.summarize(results)
    sha = hashlib.sha256(b"".join((d / "statements" / f"{c}.csv").read_bytes() for c in chosen)).hexdigest()
    pool = models.POOL["pool"]
    meta = {
        "date": str(date.today()), "data_sha256": sha, "companies": chosen, "companies_n": len(chosen), "horizon": args.horizon,
        "cutoffs": args.cutoffs, "step_days": args.step, "paths": args.paths, "test_start": str(args.test_start),
        "variants": variants, "gb_backend": pool.backend if pool else None,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"backtest-{date.today()}"
    stem.with_suffix(".json").write_text(json.dumps({**meta, "results": rows}, ensure_ascii=False, indent=2))
    stem.with_suffix(".md").write_text(to_markdown(rows, meta))
    print(to_markdown(rows, meta))
    print(f"отчёт: {stem}.json / .md", file=sys.stderr)


if __name__ == "__main__":
    main()
