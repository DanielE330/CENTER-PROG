"""Форматирование сумм и дат по-русски."""
from datetime import date

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

CATEGORIES = {
    "revenue": "Выручка", "other_income": "Прочие поступления", "suppliers": "Поставщики", "payroll": "Зарплата",
    "rent": "Аренда", "taxes": "Налоги", "loans": "Кредиты", "utilities": "Коммунальные и связь",
    "bank_fees": "Банковские комиссии", "marketing": "Маркетинг", "transfers": "Переводы между счетами",
    "refunds": "Возвраты", "other_expense": "Прочие расходы",
}


def category(code: str) -> str:
    return CATEGORIES.get(code, code)


def rub(kop: int, sign: bool = False) -> str:
    """Копейки -> «1 234 567 ₽» (копейки отбрасываются: для отчётов важны рубли)."""
    rub_ = round(abs(kop) / 100)
    s = f"{rub_:,}".replace(",", " ") + " ₽"
    if kop < 0:
        return "−" + s
    return ("+" + s) if sign and kop > 0 else s


def pct(x: float) -> str:
    return f"{round(x * 100)}%"


def day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]}"


def day_wd(d: date) -> str:
    return f"{day(d)} ({WEEKDAYS_SHORT[d.weekday()]})"


def period(a: date, b: date) -> str:
    if a.month == b.month and a.year == b.year:
        return f"{a.day}–{b.day} {MONTHS[b.month - 1]}"
    return f"{day(a)} – {day(b)}"


def month_title(d: date) -> str:
    return f"{MONTHS_NOM[d.month - 1].capitalize()} {d.year}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def days_word(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def esc(s: str) -> str:
    """Экранирование для parse_mode=HTML."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
