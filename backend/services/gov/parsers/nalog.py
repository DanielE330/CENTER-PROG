"""ФНС России: налоговый календарь (https://www.nalog.gov.ru/rn77/calendar/) и страховые взносы.

Календарь встраивается в страницу JS-вызовом NalogCalendar([{date: new Date(Y, M, D), data: [...]}]),
месяц в JS нулевой. Сайт отдаёт только скользящее окно ближайших месяцев, а не весь год.
"""
import html as htmllib
import re
from dataclasses import dataclass, asdict
from datetime import date

from ..errors import ParseError

SOURCE = "nalog.gov.ru"
CALENDAR_URL = "https://www.nalog.gov.ru/rn77/calendar/"
INSURANCE_URL = "https://www.nalog.gov.ru/rn77/taxation/insprem/"

_STR = r'"((?:\\.|[^"\\])*)"'
_EVENT = re.compile(r"message:\s*" + _STR + r"\s*,\s*title:\s*" + _STR)
_DATE = re.compile(r"\{date:\s*new Date\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)")

# (ключ, признак в заголовке/тексте)
CATEGORIES = [
    ("enp", "единый налоговый платеж"),
    ("ausn", "автоматизированная упрощенная"),
    ("usn", "упрощенная система"),
    ("nds", "ндс"),
    ("nds", "добавленную стоимость"),
    ("insurance", "страховые взносы"),
    ("insurance", "страхование"),
    ("ndfl", "доходы физических лиц"),
    ("profit", "налог на прибыль"),
    ("npd", "профессиональный доход"),
]


def _clean(s: str) -> str:
    s = s.replace('\\"', '"').replace("\\/", "/").replace("\\n", " ")
    s = re.sub(r"</p>\s*", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(re.sub(r"[ \t\r]+", " ", s)).strip()


def _categories(title: str, text: str) -> list:
    low = (title + " " + text).lower()
    out = []
    for key, needle in CATEGORIES:
        if needle in low and key not in out:
            out.append(key)
    return out


@dataclass
class TaxEvent:
    date: str
    title: str
    text: str
    categories: list

    def to_dict(self):
        return asdict(self)


def parse_tax_calendar(html: str) -> list:
    i = html.find("NalogCalendar(")
    if i < 0:
        raise ParseError("на странице нет вызова NalogCalendar(...) — разметка изменилась", SOURCE)
    j = html.find("</script>", i)
    block = html[i:j if j > 0 else len(html)]
    starts = list(_DATE.finditer(block))
    if not starts:
        raise ParseError("в календаре нет записей date: new Date(...)", SOURCE)
    events = []
    for n, m in enumerate(starts):
        y, mo, d = int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))
        try:
            iso = date(y, mo, d).isoformat()
        except ValueError as e:
            raise ParseError(f"некорректная дата {y},{mo},{d}", SOURCE) from e
        chunk = block[m.end(): starts[n + 1].start() if n + 1 < len(starts) else len(block)]
        found = _EVENT.findall(chunk)
        if not found:
            raise ParseError(f"у даты {iso} нет message/title", SOURCE)
        for msg, title in found:
            text, title = _clean(msg), _clean(title)
            events.append(TaxEvent(iso, title, text, _categories(title, text)))
    return events


@dataclass
class Insurance:
    rate_main_pct: float          # общий тариф в пределах базы
    rate_above_limit_pct: float   # свыше предельной базы
    base_limits: dict             # {год: руб.}
    source: str = SOURCE

    def to_dict(self):
        return asdict(self)


def parse_insurance(html: str) -> Insurance:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"(\s|&nbsp;|\xa0)+", " ", text)
    limits = {}
    for y, v in re.findall(r"\b(20\d\d) (\d{1,3}(?: \d{3})+) от \d{2}\.\d{2}\.\d{4}", text):
        limits[int(y)] = int(v.replace(" ", ""))
    if not limits:
        raise ParseError("не найдена таблица предельной величины базы", SOURCE)
    m = re.search(r"установлен тариф страховых взносов в размере (\d+(?:[.,]\d+)?) ?%", text)
    k = re.search(r"свыше установленной единой предельной величины базы[^.]*?- (\d+(?:[.,]\d+)?) процент", text)
    if not m:
        raise ParseError("не найден основной тариф страховых взносов", SOURCE)
    if not k:
        raise ParseError("не найден тариф свыше предельной базы", SOURCE)
    num = lambda s: float(s.replace(",", "."))
    return Insurance(num(m.group(1)), num(k.group(1)), limits)
