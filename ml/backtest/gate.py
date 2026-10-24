"""CI quality gate on the synthetic backtest report: prints a Markdown table and
fails when the layered forecast stops beating the naive baselines or its
intervals stop covering reality."""

import json
import sys
from pathlib import Path


def main() -> None:
    report_dir = Path(sys.argv[1])
    report = json.loads(sorted(report_dir.glob("backtest-*.json"))[-1].read_text())
    rows = {r["variant"]: r for r in report["results"]}
    print("| variant | MAE, days of turnover | coverage P10–P90 | gap recall |\n|---|---|---|---|")
    for r in report["results"]:
        print(f"| {r['variant']} | {r['mae_turnover_days']} | {r['coverage_p10_p90']} | {r['gap_recall']} |")
    best = min(rows["layered"]["mae_turnover_days"], rows["layered_invoices"]["mae_turnover_days"])
    failures = []
    if best >= rows["naive_last_week"]["mae_turnover_days"]:
        failures.append("layered forecast does not beat repeating last week")
    if best >= rows["naive_flat"]["mae_turnover_days"]:
        failures.append("layered forecast does not beat a flat balance")
    if not 0.6 <= rows["layered"]["coverage_p10_p90"] <= 0.97:
        failures.append(f"P10–P90 coverage {rows['layered']['coverage_p10_p90']} outside [0.6, 0.97]")
    if failures:
        print("\n**Gate failed:** " + "; ".join(failures))
        sys.exit(1)
    print("\nGate passed.")


if __name__ == "__main__":
    main()
