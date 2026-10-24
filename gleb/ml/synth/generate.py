"""Synthetic statements in the organizers' format, for CI and e2e where the real
dataset is not available. Seeded; parameters are aggregate, no real operations.

    python ml/synth/generate.py --out /tmp/synth --companies 4 --days 540
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

FIELDS = [
    "company_id",
    "operation_id",
    "operation_date",
    "amount",
    "direction",
    "counterparty_inn",
    "counterparty_name",
    "payment_purpose",
    "balance_eod",
]


def working(d: date) -> bool:
    return d.weekday() < 5 and (d.month, d.day) not in {(1, 1), (1, 2), (1, 7), (5, 1), (5, 9), (11, 4)}


def company(cid: str, start: date, days: int, rng: random.Random) -> list[dict]:
    rows: list[dict] = []
    balance = rng.randint(300_000, 1_500_000) * 100
    clients = [(f"00{rng.randint(10**7, 10**8 - 1)}", f"ООО «Клиент-{i}»", rng.randint(5, 45)) for i in range(4)]
    invoices: list[tuple[date, int, str, str, int]] = []
    seq = 0
    for i in range(days):
        d = start + timedelta(days=i)
        if not working(d):
            continue
        ops = []
        season = 1.0 + 0.3 * (d.month in (11, 12)) - 0.2 * (d.month in (1, 2))
        ops.append(
            (
                int(rng.gauss(45_000, 12_000) * season) * 100,
                "Поступление",
                "0068275202",
                "ПАО «Банк»",
                f"Зачисление средств по операциям эквайринга. Мерчант №{rng.randint(1000, 9999)}",
            )
        )
        if rng.random() < 0.15:
            inn, name, delay = rng.choice(clients)
            amount = rng.randint(80_000, 400_000) * 100
            invoices.append((d + timedelta(days=int(rng.expovariate(1 / delay))), amount, inn, name, i))
        for inv in [x for x in invoices if x[0] <= d]:
            invoices.remove(inv)
            doc = start + timedelta(days=inv[4])
            ops.append(
                (
                    inv[1],
                    "Поступление",
                    inv[2],
                    inv[3],
                    f"Оплата по УПД № {inv[4]} от {doc.strftime('%d.%m.%Y')}. НДС не облагается",
                )
            )
        if d.day == 5:
            ops.append(
                (-180_000_00, "Списание", "0011223344", "АО «Арендодатель»", f"Оплата аренды за {d.strftime('%m.%Y')}")
            )
        if d.day in (10, 25):
            kind = "аванс" if d.day == 25 else "окончательный расчет"
            ops.append(
                (
                    -int(rng.gauss(220_000, 5_000)) * 100,
                    "Списание",
                    "0068275202",
                    "ПАО «Банк»",
                    f"Перечисление заработной платы ({kind}) за {d.strftime('%m.%Y')}",
                )
            )
        if d.day == 28:
            ops.append(
                (
                    -int(rng.gauss(90_000, 10_000)) * 100,
                    "Списание",
                    "0077700001",
                    "Казначейство России (ФНС России)",
                    "Единый налоговый платеж",
                )
            )
        if rng.random() < 0.25:
            ops.append(
                (
                    -rng.randint(20_000, 160_000) * 100,
                    "Списание",
                    f"00{rng.randint(10**7, 10**8 - 1)}",
                    "ООО «Поставщик»",
                    f"Оплата по счету № {rng.randint(1, 999)} от {d.strftime('%d.%m.%Y')} за товар",
                )
            )
        flow = sum(o[0] for o in ops)
        if balance + flow < 0:
            inject = (-(balance + flow) // 100 + 50_000) * 100
            ops.append(
                (
                    inject,
                    "Поступление",
                    "",
                    "Собственные средства",
                    "Перевод собственных средств с расчетного счета в АО «Банк-2». НДС не облагается",
                )
            )
            flow += inject
        balance += flow
        for amount, direction, inn, name, purpose in ops:
            seq += 1
            rows.append(
                {
                    "company_id": cid,
                    "operation_id": f"{cid}-{seq:06d}",
                    "operation_date": d.isoformat(),
                    "amount": f"{abs(amount) / 100:.2f}",
                    "direction": direction,
                    "counterparty_inn": inn,
                    "counterparty_name": name,
                    "payment_purpose": purpose,
                    "balance_eod": f"{balance / 100:.2f}",
                }
            )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--companies", type=int, default=4)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    (args.out / "statements").mkdir(parents=True, exist_ok=True)
    everything, meta = [], []
    start = date(2025, 1, 1)
    for n in range(args.companies):
        cid = f"S{n + 1:03d}"
        days = args.days if n else 60  # the first company has a short history on purpose
        rows = company(cid, start + timedelta(days=args.days - days), days, rng)
        with (args.out / "statements" / f"{cid}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, FIELDS, delimiter=";")
            w.writeheader()
            w.writerows(rows)
        everything += rows
        meta.append(
            {
                "company_id": cid,
                "company_name": f"ООО «Синтетика-{n + 1}»",
                "company_inn": f"00{n + 1:08d}",
                "legal_form": "ООО",
                "industry": "Синтетика",
                "first_operation_date": rows[0]["operation_date"],
                "last_operation_date": rows[-1]["operation_date"],
                "operations_count": len(rows),
            }
        )
    with (args.out / "all_operations.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS, delimiter=";")
        w.writeheader()
        w.writerows(everything)
    with (args.out / "companies.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, list(meta[0]), delimiter=";")
        w.writeheader()
        w.writerows(meta)


if __name__ == "__main__":
    main()
