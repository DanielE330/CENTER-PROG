"""Режим налогообложения по выписке (владелец подтверждает в интерфейсе). Из проекта «ЦИ1» (ветка gleb).

Сигналы: НДС в оплатах клиентов (ОСНО), ритм ЕНП (квартальные пики — аванс по УСН, пики в июле и марте — ЕСХН)
и доля налогов в выручке. Нужно около полугода данных.
"""
import re
from collections import defaultdict
from datetime import date
from statistics import median

VAT_RE = re.compile(r"НДС\s*\(?\s*(20|22|10)\s*%", re.IGNORECASE)
NO_VAT_RE = re.compile(r"без\s+(налога\s+\(?)?ндс|ндс\s+не\s+облагается", re.IGNORECASE)
REVENUE_SUBS = ("acquiring", "sbp", "cash_deposit", "marketplace_payout", "customer_payment")


def infer(rows: list[dict]) -> dict | None:
    """rows: op_date (date), amount_kop, subcategory, purpose. None, если данных мало."""
    customer = [r for r in rows if r["subcategory"] == "customer_payment"]
    vat = sum(1 for r in customer if VAT_RE.search(r["purpose"] or ""))
    no_vat = sum(1 for r in customer if NO_VAT_RE.search(r["purpose"] or ""))
    months: dict[tuple[int, int], int] = defaultdict(int)
    revenue = 0
    for r in rows:
        d: date = r["op_date"]
        if r["subcategory"] == "enp":
            months[(d.year, d.month)] += -r["amount_kop"]
        elif r["amount_kop"] > 0 and r["subcategory"] in REVENUE_SUBS:
            revenue += r["amount_kop"]
    if len(months) < 6:
        return None

    amounts = sorted(months.values())
    base = median(amounts)
    by_month: dict[int, list[int]] = defaultdict(list)
    for (_, m), v in months.items():
        by_month[m].append(v)

    def peak(m: int) -> float:
        values = by_month.get(m, [])
        return (median(values) / base) if values and base else 0.0

    quarterly = sum(peak(m) > 1.5 for m in (4, 7, 10))
    half_year = peak(7) > 1.5 and peak(3) > 1.5 and quarterly < 2
    total_tax = sum(months.values())
    tax_share = total_tax / revenue if revenue else 0.0

    if customer and vat / len(customer) > 0.4:
        regime, confidence = "osno", min(0.95, 0.5 + vat / len(customer) / 2)
    elif half_year:
        regime, confidence = "eshn", 0.6
    elif quarterly >= 2:
        regime = "usn_income" if tax_share < 0.09 else "usn_income_expense"
        confidence = 0.55 + 0.1 * quarterly
    else:
        regime, confidence = "patent" if no_vat and tax_share < 0.02 else "usn_income", 0.4

    peaks = [v for (_, m), v in months.items() if m in (1, 3, 4, 7, 10)]
    monthly = int(median([v for (_, m), v in months.items() if m not in (1, 3, 4, 7, 10)] or amounts))
    return {
        "regime": regime,
        "confidence": round(confidence, 2),
        "estimate": {
            "monthly_kop": monthly,
            "peak_extra_kop": max(int(median(peaks)) - monthly, 0) if peaks else 0,
            "tax_share_of_revenue": round(tax_share, 4),
            "months_observed": len(months),
        },
    }
