"""Гейт качества по отчёту бэктеста (для CI на синтетике или локально на датасете): падает, если вероятностный прогноз
перестал быть лучше «плоской» модели или интервалы перестали покрывать факт.

    python -m ml.backtest.gate ml/reports        # берёт самый свежий backtest-*.json
"""
import json
import sys
from pathlib import Path


def check(rows: list[dict]) -> list[str]:
    by = {r["variant"]: r for r in rows}
    failures = []
    need = {"naive_flat", "weekday_bootstrap"}
    if not need <= set(by):
        return [f"в отчёте нет подходов: {sorted(need - set(by))}"]
    flat, boot = by["naive_flat"], by["weekday_bootstrap"]
    if boot["pinball_norm"] >= flat["pinball_norm"]:
        failures.append("квантильная ошибка (pinball) не лучше «плоской» модели")
    if not 0.6 <= boot["coverage_p10_p90"] <= 0.97:
        failures.append(f"покрытие P10–P90 {boot['coverage_p10_p90']} вне [0,6; 0,97]")
    if boot["brier_adj"] >= flat["brier_adj"]:
        failures.append("Brier вероятности разрыва не лучше «плоской» модели")
    return failures


def main() -> None:
    path = sorted(Path(sys.argv[1]).glob("backtest-*.json"))[-1]
    report = json.loads(path.read_text())
    failures = check(report["results"])
    print(f"{path.name}: " + ("гейт пройден" if not failures else "гейт НЕ пройден: " + "; ".join(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
