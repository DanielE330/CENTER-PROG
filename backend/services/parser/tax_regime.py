"""Определение режима налогообложения по выписке (подсказка; подтверждает владелец в UI).

Идея и пороги: проект gleb (swimshadow) (gleb/services/categorizer/src/categorizer/tax.py).
Сигналы: НДС в оплатах клиентов (ОСНО), ритм единых налоговых платежей (пики в апр/июл/окт —
авансы УСН; июль+март — ЕСХН) и доля налогов в выручке (меньше 9% — УСН «доходы»,
больше — УСН «доходы минус расходы»; эвристика, владелец подтверждает режим сам).

Вход — операции из parse_statement (поля date, amount_kop, category, direction, purpose).
Выход — dict или None, если данных мало (нужно хотя бы 6 месяцев с налоговыми платежами).
"""
import re
from collections import defaultdict
from datetime import date
from statistics import median

MIN_TAX_MONTHS = 6

VAT_RE = re.compile(r"НДС\s*\(?\s*(20|22|10)\s*%", re.I)
NO_VAT_RE = re.compile(r"без\s+(налога\s+\(?)?ндс|ндс\s+не\s+облагается", re.I)

LABELS = {
    "osno": "ОСНО",
    "usn_income": "УСН «доходы»",
    "usn_income_expense": "УСН «доходы минус расходы»",
    "eshn": "ЕСХН",
    "patent": "Патент / без НДС и с малыми налогами",
}


def guess_tax_regime(txs: list[dict]) -> dict | None:
    revenue_ops = [t for t in txs if t["direction"] == "in" and t["category"] == "revenue"]
    vat = sum(1 for t in revenue_ops if VAT_RE.search(t["purpose"] or ""))
    no_vat = sum(1 for t in revenue_ops if NO_VAT_RE.search(t["purpose"] or ""))
    revenue = sum(int(t["amount_kop"]) for t in revenue_ops)

    months: dict[tuple[int, int], int] = defaultdict(int)
    for t in txs:
        if t["direction"] == "out" and t["category"] == "taxes":
            d = date.fromisoformat(t["date"])
            months[(d.year, d.month)] += -int(t["amount_kop"])
    if len(months) < MIN_TAX_MONTHS:
        return None

    amounts = sorted(months.values())
    base = median(amounts)
    by_month: dict[int, list[int]] = defaultdict(list)
    for (_, m), v in months.items():
        by_month[m].append(v)

    def peak(m: int) -> float:
        vals = by_month.get(m, [])
        return (median(vals) / base) if vals and base else 0.0

    quarterly = sum(peak(m) > 1.5 for m in (4, 7, 10))
    half_year = peak(7) > 1.5 and peak(3) > 1.5 and quarterly < 2
    total_tax = sum(months.values())
    tax_share = total_tax / revenue if revenue else 0.0
    vat_share = vat / len(revenue_ops) if revenue_ops else 0.0

    if revenue_ops and vat_share > 0.4:
        regime, confidence = "osno", min(0.95, 0.5 + vat_share / 2)
    elif half_year:
        regime, confidence = "eshn", 0.6
    elif quarterly >= 2:
        regime = "usn_income" if tax_share < 0.09 else "usn_income_expense"
        confidence = 0.55 + 0.1 * quarterly
    else:
        regime, confidence = ("patent" if no_vat and tax_share < 0.02 else "usn_income"), 0.4

    peaks = [v for (_, m), v in months.items() if m in (1, 3, 4, 7, 10)]
    flat = [v for (_, m), v in months.items() if m not in (1, 3, 4, 7, 10)]
    monthly = int(median(flat or amounts))
    peak_extra = max(int(median(peaks)) - monthly, 0) if peaks else 0
    return {
        "regime": regime,
        "label": LABELS[regime],
        "confidence": round(min(confidence, 0.95), 2),
        "estimate": {
            "monthly_kop": str(monthly),
            "peak_extra_kop": str(peak_extra),
            "tax_share_of_revenue": round(tax_share, 4),
            "months_observed": len(months),
            "vat_share_of_revenue_ops": round(vat_share, 3),
        },
    }
