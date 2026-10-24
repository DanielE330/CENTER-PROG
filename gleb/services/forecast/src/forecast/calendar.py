"""Working days. Operations in the dataset happen only on working days, so past
dates come from the data; future dates use weekends plus fixed public holidays.
The official production calendar also moves some days; replace `HOLIDAYS` with
it before production."""

from datetime import date, timedelta

HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and (d.month, d.day) not in HOLIDAYS


def next_working_day(d: date) -> date:
    while not is_working_day(d):
        d += timedelta(days=1)
    return d


def days_after(start: date, horizon: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(1, horizon + 1)]
