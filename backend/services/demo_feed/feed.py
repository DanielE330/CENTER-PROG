"""Лента демо: операции датасета «из будущего» для демо-компании. Только данные, без HTTP.

Демо-пользователь получает выписку до даты отсечки (DEMO_CUTOFF), остальное лежит в ленте. Курсор — дата
последней загруженной операции (`after`): лента отдаёт следующие 7 дней после неё. Пустые недели пропускаются,
иначе импорт упёрся бы в «в файле нет ни одной операции».
"""
import csv
import io
import os
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DEFAULT_CUTOFF = date(2026, 7, 12)
WEEK_DAYS = 7
BOM = "﻿"


def dataset_dir() -> Path:
    """DEMO_DATASET_DIR, затем MOCK_DATASET_DIR, затем data/dataset (полный) или data/demo из репозитория."""
    env = os.environ.get("DEMO_DATASET_DIR") or os.environ.get("MOCK_DATASET_DIR")
    if env:
        return Path(env)
    data = Path(__file__).resolve().parents[3] / "data"
    return data / "dataset" if (data / "dataset" / "statements").is_dir() else data / "demo"


def cutoff() -> date:
    raw = os.environ.get("DEMO_CUTOFF", "").strip()
    return date.fromisoformat(raw) if raw else DEFAULT_CUTOFF


def default_company() -> str:
    return os.environ.get("DEMO_COMPANY", "C001")


@dataclass(frozen=True)
class Statement:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]  # отсортированы по дате, порядок внутри даты сохранён
    dates: tuple[date, ...]  # параллельно rows


@dataclass(frozen=True)
class Week:
    start: date
    end: date  # конец окна (start + 6), не дата последней операции
    last_op: date  # новая дата «сегодня» после импорта
    rows: tuple[tuple[str, ...], ...]


def company_exists(company: str) -> bool:
    return (dataset_dir() / "statements" / f"{company}.csv").is_file()


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> Statement:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f, delimiter=";")
        header = tuple(next(rd))
        col = header.index("operation_date")
        items = sorted(((date.fromisoformat(r[col]), r) for r in rd if r), key=lambda x: x[0])
    return Statement(header, tuple(tuple(r) for _, r in items), tuple(d for d, _ in items))


def statement(company: str) -> Statement:
    p = dataset_dir() / "statements" / f"{company}.csv"
    return _load(str(p), p.stat().st_mtime)


def next_week(st: Statement, after: date) -> Week | None:
    """Следующие 7 дней после `after`; если в них пусто, окно сдвигается к первой операции после курсора."""
    i = bisect_right(st.dates, after)
    if i >= len(st.dates):
        return None
    start = after + timedelta(days=1)
    if st.dates[i] > start + timedelta(days=WEEK_DAYS - 1):
        start = st.dates[i]
    end = start + timedelta(days=WEEK_DAYS - 1)
    j = bisect_right(st.dates, end)
    return Week(start, end, st.dates[j - 1], st.rows[i:j])


def weeks_left(st: Statement, after: date) -> int:
    n = 0
    while (w := next_week(st, after)) is not None:
        n += 1
        after = w.last_op
    return n


def ops_left(st: Statement, after: date) -> int:
    return len(st.rows) - bisect_right(st.dates, after)


def to_csv(st: Statement, week: Week) -> str:
    """CSV той же структуры, что и загружаемая выписка: `;`, BOM, те же колонки."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(st.header)
    w.writerows(week.rows)
    return BOM + buf.getvalue()
