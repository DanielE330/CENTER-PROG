"""Backtest on the organizers' dataset (local only: the data is not in git).

    python ml/backtest/run.py --data data --companies 30 --horizon 30

Categorizes raw statements with the categorizer rules, then runs the rolling-origin
backtest of every forecast variant and writes ml/reports/backtest-<date>.json.
"""

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "services/categorizer/src"), str(ROOT / "services/forecast/src")]

from categorizer import rules  # noqa: E402
from forecast import backtest  # noqa: E402


def load(data_dir: Path) -> pd.DataFrame:
    ops = pd.read_csv(data_dir / "all_operations.csv", sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str})
    ops["op_date"] = pd.to_datetime(ops["operation_date"]).dt.date
    ops["amount_kop"] = (
        (ops["amount"].where(ops["direction"] == "Поступление", -ops["amount"]) * 100).round().astype(int)
    )
    ops["balance_eod_kop"] = (ops["balance_eod"] * 100).round().astype(int)
    ops["counterparty_ref"] = ops["counterparty_inn"].fillna(ops["counterparty_name"])
    ops = ops.drop_duplicates(["company_id", "op_date", "amount_kop", "counterparty_ref", "payment_purpose"])
    return ops


def categorize(ops: pd.DataFrame) -> pd.DataFrame:
    tax = rules.load(ROOT / "rules/categories.yaml")
    purpose = ops["payment_purpose"].fillna("")
    name = ops["counterparty_name"].fillna("")
    subs = [
        rules.match(tax, p, n, a) or tax.fallback(a) for p, n, a in zip(purpose, name, ops["amount_kop"], strict=True)
    ]
    ops = ops.assign(subcategory=subs)
    ops["group"] = ops["subcategory"].map(tax.group)
    ops["is_rescue"] = [
        rules.is_rescue(s, a, b)
        for s, a, b in zip(ops["subcategory"], ops["amount_kop"], ops["balance_eod_kop"], strict=True)
    ]
    docs = [rules.document(p) for p in purpose]
    ops["doc_no"] = [d[0] for d in docs]
    ops["doc_date"] = [d[1] for d in docs]
    return ops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--companies", type=int, default=30)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--cutoffs", type=int, default=8)
    ap.add_argument("--paths", type=int, default=300)
    ap.add_argument("--out", type=Path, default=ROOT / "ml/reports")
    args = ap.parse_args()

    ops = categorize(load(args.data))
    ids = sorted(ops["company_id"].unique())
    # Deterministic sample spread over the list, so every industry shows up.
    step = max(1, len(ids) // args.companies)
    chosen = ids[::step][: args.companies]
    results = []
    for cid in chosen:
        hist = ops[ops["company_id"] == cid].sort_values("op_date").reset_index(drop=True)
        for c in backtest.cutoffs_for(hist, args.horizon, count=args.cutoffs):
            results += backtest.run_cutoff(hist, c, args.horizon, n_paths=args.paths)
        print(f"{cid}: done", file=sys.stderr)
    table = backtest.summarize(results)
    sha = hashlib.sha256((args.data / "all_operations.csv").read_bytes()).hexdigest()
    report = {
        "date": str(date.today()),
        "data_sha256": sha,
        "companies": chosen,
        "horizon": args.horizon,
        "cutoffs_per_company": args.cutoffs,
        "paths": args.paths,
        "results": table,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"backtest-{date.today()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(pd.DataFrame(table).to_string(index=False))
    print(f"report: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
