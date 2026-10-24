"""Ключевая ставка Банка России: https://www.cbr.ru/hd_base/KeyRate/ (HTML-таблица)."""
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime

from ..errors import ParseError

SOURCE = "cbr.ru"
BASE = "https://www.cbr.ru/hd_base/KeyRate/"


def url(date_from: date, date_to: date) -> str:
    return (f"{BASE}?UniDbQuery.Posted=True&UniDbQuery.From={date_from:%d.%m.%Y}"
            f"&UniDbQuery.To={date_to:%d.%m.%Y}")


@dataclass
class KeyRate:
    rate: float            # % годовых, действует на as_of
    as_of: str             # дата последней записи (ISO)
    changed_on: str        # с какой даты действует это значение (по доступному окну)
    previous_rate: float | None
    history: list          # точки изменения: [{"date","rate"}], новые первыми
    source: str = SOURCE

    def to_dict(self):
        return asdict(self)


def parse_key_rate(html: str) -> KeyRate:
    m = re.search(r'<table[^>]*class="data"[^>]*>(.*?)</table>', html, re.S)
    if not m:
        raise ParseError("не найдена таблица class=data", SOURCE)
    table = m.group(1)
    if "Ставка" not in table or "Дата" not in table:
        raise ParseError("заголовки таблицы не 'Дата'/'Ставка'", SOURCE)
    rows = []
    for d, r in re.findall(r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td>\s*([\d\s.,]+?)\s*</td>", table):
        rows.append((datetime.strptime(d, "%d.%m.%Y").date(), float(r.replace(",", ".").replace(" ", ""))))
    if not rows:
        raise ParseError("в таблице нет строк дата/ставка", SOURCE)
    rows.sort(key=lambda x: x[0], reverse=True)
    # точки изменения: идём от старых к новым
    changes = []
    prev = None
    for d, r in reversed(rows):
        if r != prev:
            changes.append({"date": d.isoformat(), "rate": r})
            prev = r
    changes.reverse()
    cur = changes[0]
    return KeyRate(rate=cur["rate"], as_of=rows[0][0].isoformat(), changed_on=cur["date"],
                   previous_rate=changes[1]["rate"] if len(changes) > 1 else None,
                   history=changes)
