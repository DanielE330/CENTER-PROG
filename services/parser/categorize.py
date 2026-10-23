"""Категоризация операций по назначению платежа: правила + confidence.

Правила — первый уровень (baseline); ML-классификатор можно подключить позже
за тем же интерфейсом categorize(purpose, direction) -> (category, confidence).
"""
import re

# (категория, направление или None, regex, confidence)
RULES: list[tuple[str, str | None, str, float]] = [
    ("taxes", "out", r"\bенп\b|единый налоговый платеж|налог|усн|ндфл|ндс\b|страховые взносы|взнос.{0,20}(пфр|сфр|фнс)|ифнс|фнс|пфр|сфр", 0.95),
    ("payroll", "out", r"зарплат|заработн|аванс.{0,15}(сотрудник|работник)|оплата труда|выплата.{0,10}персонал", 0.95),
    ("rent", "out", r"аренд|субаренд", 0.95),
    ("loans", None, r"кредит|займ|проценты по|погашение.{0,15}(долг|основн)|лизинг", 0.9),
    ("utilities", "out", r"коммунал|электроэнерг|водоснаб|интернет|связь|телефон|хостинг", 0.85),
    ("bank_fees", "out", r"комисси|обслуживание счета|рко|эквайринг", 0.9),
    ("marketing", "out", r"реклам|продвижени|маркетинг|яндекс\.?директ|vk\b", 0.85),
    ("transfers", None, r"перевод собственных|между своими счетами|внутренний перевод|пополнение счета", 0.9),
    ("revenue", "in", r"оплата по сч[её]ту|оплата по договору|за товар|за услуг|выручк|поступлени|оплата заказа|реализац", 0.8),
    ("suppliers", "out", r"оплата по сч[её]ту|по договору|за товар|за материал|закупк|поставк|за услуг|сырь[её]", 0.7),
]
_COMPILED = [(c, d, re.compile(p, re.I), conf) for c, d, p, conf in RULES]


def categorize(purpose: str, direction: str) -> tuple[str, float]:
    text = purpose or ""
    for cat, d, rx, conf in _COMPILED:
        if d and d != direction:
            continue
        if rx.search(text):
            return cat, conf
    return ("other_income" if direction == "in" else "other_expense"), 0.2
