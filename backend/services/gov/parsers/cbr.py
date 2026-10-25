"""Ключевая ставка Банка России: https://www.cbr.ru/hd_base/KeyRate/ (HTML-таблица)."""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

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


# --- официальные курсы валют: https://www.cbr.ru/scripts/XML_daily.asp (XML, windows-1251, запятая в числах) ---
RATES_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
# валюты, которые интерфейс умеет показывать вместо рубля (RUB — база, в XML его нет)
SUPPORTED = ("USD", "EUR", "CNY", "GBP", "KZT", "BYN", "TRY", "AED")
# приблизительные курсы на случай, когда нет ни ЦБ, ни кэша: {код: (номинал, рублей за номинал)}
FALLBACK_RATES = {
    "USD": (1, 80.0), "EUR": (1, 90.0), "CNY": (1, 12.0), "GBP": (1, 107.0),
    "KZT": (100, 18.0), "BYN": (1, 27.0), "TRY": (10, 16.0), "AED": (1, 22.0),
}


@dataclass
class Rates:
    date: str | None       # дата курсов (ISO) по атрибуту Date; None у запасного набора
    rates: dict            # {"USD": {"nominal": 1, "value": 80.12}}: value — рублей за nominal единиц
    source: str = SOURCE
    approx: bool = False   # запасной набор приблизительных курсов, не ЦБ

    def to_dict(self):
        return asdict(self)


def fallback_rates() -> Rates:
    return Rates(date=None, approx=True,
                 rates={c: {"nominal": n, "value": v} for c, (n, v) in FALLBACK_RATES.items()})


def parse_rates(xml: str) -> Rates:
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ParseError("в XML курсов недопустимо объявление DOCTYPE/ENTITY", SOURCE)
    # str с объявлением кодировки expat не принимает во всех сборках: тело уже декодировано клиентом
    xml = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ParseError(f"некорректный XML курсов: {e}", SOURCE) from e
    if root.tag != "ValCurs":
        raise ParseError("корневой элемент не ValCurs", SOURCE)
    try:
        d = datetime.strptime(root.get("Date", ""), "%d.%m.%Y").date()
    except ValueError as e:
        raise ParseError("у ValCurs нет корректного атрибута Date", SOURCE) from e
    rates = {}
    for v in root.iter("Valute"):
        code = (v.findtext("CharCode") or "").strip().upper()
        if code not in SUPPORTED:
            continue
        try:
            nominal = int((v.findtext("Nominal") or "").strip())
            value = Decimal((v.findtext("Value") or "").strip().replace(" ", "").replace(",", "."))
        except (ValueError, InvalidOperation) as e:
            raise ParseError(f"некорректный номинал/курс {code}", SOURCE) from e
        if nominal <= 0 or value <= 0:
            raise ParseError(f"неположительный номинал/курс {code}", SOURCE)
        rates[code] = {"nominal": nominal, "value": float(value)}
    if "USD" not in rates or "EUR" not in rates:
        raise ParseError("в XML нет курсов USD/EUR", SOURCE)
    # порядок стабилен и совпадает с SUPPORTED
    return Rates(date=d.isoformat(), rates={c: rates[c] for c in SUPPORTED if c in rates})
