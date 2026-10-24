"""Производственный календарь РФ: открытые данные xmlcalendar.ru (JSON)."""
import json
from dataclasses import dataclass, asdict
from datetime import date

from ..errors import ParseError

SOURCE = "xmlcalendar.ru"


def url(year: int) -> str:
    return f"https://xmlcalendar.ru/data/ru/{year}/calendar.json"


@dataclass
class ProductionCalendar:
    year: int
    non_working: list      # нерабочие дни (ISO): выходные и праздники, с учётом переносов
    transferred_off: list  # из них дни, объявленные выходными переносом ('+')
    short_days: list       # предпраздничные сокращённые рабочие дни ('*')
    transitions: list      # переносы как в источнике: [{"from","to"}] (формат ММ.ДД)
    statistic: dict
    source: str = SOURCE

    def is_working(self, d: date) -> bool:
        return d.year == self.year and d.isoformat() not in set(self.non_working)

    def to_dict(self):
        return asdict(self)


def parse_calendar(text: str, year: int) -> ProductionCalendar:
    try:
        data = json.loads(text)
        months = data["months"]
        if data.get("year") != year:
            raise ParseError(f"год в данных {data.get('year')} != {year}", SOURCE)
        off, moved, short = [], [], []
        seen = set()
        for m in months:
            mo = int(m["month"])
            seen.add(mo)
            for tok in str(m["days"]).split(","):
                tok = tok.strip()
                if not tok:
                    continue
                flag = tok[-1] if tok[-1] in "+*" else ""
                iso = date(year, mo, int(tok.rstrip("+*"))).isoformat()
                if flag == "*":
                    short.append(iso)
                else:
                    off.append(iso)
                    if flag == "+":
                        moved.append(iso)
        if seen != set(range(1, 13)):
            raise ParseError("в календаре не 12 месяцев", SOURCE)
    except ParseError:
        raise
    except (ValueError, KeyError, TypeError) as e:
        raise ParseError(f"неожиданный формат календаря: {type(e).__name__}: {e}", SOURCE) from e
    return ProductionCalendar(year, sorted(off), sorted(moved), sorted(short),
                              data.get("transitions", []), data.get("statistic", {}))
