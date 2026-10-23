#!/usr/bin/env python3
"""Генератор синтетической выписки (CSV, ';') для разработки и демо.

  python scripts/gen_statement.py --end 2026-10-22 --seed 7 > data/demo_statement.csv
"""
import argparse
import random
from datetime import date, timedelta


def month_add(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, 28))


def R(rub: int) -> int:
    return rub * 100


def fmt(kop: int) -> str:
    sign = "-" if kop < 0 else ""
    kop = abs(kop)
    return f"{sign}{kop // 100}.{kop % 100:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start-balance", type=int, default=1_700_000)  # рублей
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    end = date.fromisoformat(a.end)
    start = end - timedelta(days=a.days)

    clients = [("ООО Альфа-Трейд", "7701000001", 505_000), ("ООО Ромашка", "7702000002", 375_000), ("ИП Волков", "7703000003", 187_000)]
    suppliers = [("ООО СтройСнаб", "7801000011"), ("ООО ТехноПарт", "7802000012"), ("АО ЛогистикПро", "7803000013")]

    plan: dict = {}

    def pay_day(d: date, k: int) -> int:
        # день оплаты клиента выбирается один раз на месяц (разброс 0-3 дня)
        key = (d.year, d.month, k)
        if key not in plan:
            plan[key] = 8 + k * 7 + rnd.choice([0, 0, 0, 1, 2, 3])
        return plan[key]

    ops = []  # (date, kop signed, cpty, inn, purpose)
    d = start
    while d <= end:
        # зарплата 5 и 20
        if d.day in (5, 20):
            ops.append((d, -R(rnd.randint(300, 330) * 1000), "Сотрудники", "", "Выплата заработной платы за период"))
        if d.day == 1:
            ops.append((d, -R(95_000), "ООО Бизнес-Центр", "7810000099", "Оплата аренды офиса по договору"))
        if d.day == 28:
            ops.append((d, -R(rnd.randint(150, 190) * 1000), "ФНС России", "7707329152", "Единый налоговый платеж ЕНП"))
        if d.day == 12:
            ops.append((d, -R(rnd.randint(35, 45) * 1000), "ООО ТелеСвязь", "7811000077", "Оплата услуг связи и интернет"))
        # крупные клиенты: раз в месяц около своей даты с разбросом
        for k, (nm, inn, base) in enumerate(clients):
            if d.day == pay_day(d, k):
                amt = R(int(base * rnd.uniform(0.9, 1.1)))
                ops.append((d, amt, nm, inn, f"Оплата по счету {rnd.randint(100, 999)} за услуги"))
        # розничная выручка по будням
        if d.weekday() < 5:
            for _ in range(rnd.choice([0, 1, 1, 2, 3])):
                ops.append((d, R(rnd.randint(8, 40) * 100), "Покупатели (эквайринг)", "", "Поступление выручки по эквайрингу"))
        # поставщики
        if d.weekday() in (1, 3) and rnd.random() < 0.7:
            nm, inn = rnd.choice(suppliers)
            ops.append((d, -R(rnd.randint(15, 70) * 1000), nm, inn, f"Оплата по счету {rnd.randint(1000, 9999)} за товар"))
        d += timedelta(days=1)

    ops.sort(key=lambda o: o[0])
    bal = R(a.start_balance)
    print("Дата операции;Сумма;Направление;Контрагент;ИНН;Назначение платежа;Остаток на конец дня")
    for dt, amt, nm, inn, purpose in ops:
        bal += amt
        print(f"{dt.isoformat()};{fmt(abs(amt))};{'Поступление' if amt > 0 else 'Списание'};{nm};{inn};{purpose};{fmt(bal)}")


if __name__ == "__main__":
    main()
