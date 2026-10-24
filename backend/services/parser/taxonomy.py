"""Таксономия операций: 10 фиксированных групп и ~28 подкатегорий, правила из YAML.

Идея и правила — из проекта «ЦИ1» (ветка gleb). Группы фиксированы, потому что на них опирается прогноз;
подкатегории — стабильный словарь для обучения. Правила идут по порядку, первое совпадение побеждает.
Существующая 13-категорийная схема ledger (`categorize.py`) остаётся: здесь дополнительные признаки для ML
(подкатегория, группа, документ-основание, «спасательное» поступление).
"""
import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import yaml

PATH = Path(__file__).with_name("categories.yaml")
DOC_RE = re.compile(
    r"(?:УПД|сч[её]т[ау]?|счет-фактур\w*|договор\w*)\s*№\s*([\w/-]+)\s+от\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE
)
RESCUE_SUBS = {"own_transfer_in", "founder_loan_in"}

# подкатегория -> категория схемы ledger: прогноз и интерфейс продолжают работать на прежних 13 категориях
LEGACY = {
    "acquiring": "revenue", "sbp": "revenue", "cash_deposit": "revenue", "marketplace_payout": "revenue", "customer_payment": "revenue",
    "supplier_payment": "suppliers", "advertising": "marketing", "software": "utilities", "services": "suppliers", "fuel_transport": "suppliers",
    "salary": "payroll", "contractors_gph": "payroll", "enp": "taxes", "rent": "rent", "utilities": "utilities",
    "loan_payment": "loans", "leasing": "loans", "loan_received": "loans",
    "own_transfer_in": "transfers", "own_transfer_out": "transfers", "founder_loan_in": "transfers", "founder_loan_return": "transfers",
    "owner_withdrawal": "transfers", "refund_in": "refunds", "refund_out": "refunds", "bank_fee": "bank_fees",
    "other_in": "other_income", "other_out": "other_expense",
}


@dataclass(frozen=True)
class Rule:
    sub: str
    purpose: re.Pattern | None
    name: re.Pattern | None
    direction: str | None


@dataclass(frozen=True)
class Taxonomy:
    groups: dict[str, str]
    subcategories: dict[str, dict]
    rules: tuple[Rule, ...]

    def group(self, sub: str) -> str:
        return self.subcategories.get(sub, {}).get("group", "other")

    @staticmethod
    def fallback(amount_kop: int) -> str:
        return "other_in" if amount_kop >= 0 else "other_out"


def load(path: Path = PATH) -> Taxonomy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = tuple(
        Rule(
            sub=r["sub"],
            purpose=re.compile(r["purpose"], re.IGNORECASE) if r.get("purpose") else None,
            name=re.compile(r["name"], re.IGNORECASE) if r.get("name") else None,
            direction=r.get("direction"),
        )
        for r in raw["rules"]
    )
    unknown = {r.sub for r in rules} - set(raw["subcategories"])
    if unknown:
        raise ValueError(f"правила ссылаются на неизвестные подкатегории: {sorted(unknown)}")
    missing = set(raw["subcategories"]) - set(LEGACY)
    if missing:
        raise ValueError(f"нет соответствия схеме ledger для: {sorted(missing)}")
    return Taxonomy(groups=raw["groups"], subcategories=raw["subcategories"], rules=rules)


@lru_cache(maxsize=1)
def default() -> Taxonomy:
    return load()


def match(tax: Taxonomy, purpose: str, name: str, amount_kop: int) -> str | None:
    """Подкатегория по первому подошедшему правилу или None. Правило без purpose и name не срабатывает."""
    direction = "inflow" if amount_kop >= 0 else "outflow"
    for rule in tax.rules:
        if rule.direction and rule.direction != direction:
            continue
        if rule.purpose and not rule.purpose.search(purpose or ""):
            continue
        if rule.name and not rule.name.search(name or ""):
            continue
        if rule.purpose is None and rule.name is None:
            continue
        return rule.sub
    return None


def document(purpose: str) -> tuple[str | None, date | None]:
    """Номер и дата документа-основания из назначения («по УПД № 279 от 19.08.2024»)."""
    m = DOC_RE.search(purpose or "")
    if not m:
        return None, None
    try:
        return m.group(1), datetime.strptime(m.group(2), "%d.%m.%Y").date()
    except ValueError:
        return m.group(1), None


def is_rescue(sub: str, amount_kop: int, balance_eod_kop: int | None) -> bool:
    """Поступление собственных денег, без которого остаток ушёл бы в минус, — реакция владельца на разрыв, а не план.
    Прогноз показывает, что было бы без вмешательства владельца, а в бэктесте такой день считается разрывом."""
    return balance_eod_kop is not None and sub in RESCUE_SUBS and amount_kop > 0 and balance_eod_kop - amount_kop < 0


def classify(purpose: str, name: str, amount_kop: int, balance_eod_kop: int | None = None) -> dict:
    tax = default()
    sub = match(tax, purpose, name, amount_kop)
    method = "rule" if sub else "fallback"
    sub = sub or tax.fallback(amount_kop)
    doc_no, doc_date = document(purpose)
    return {
        "subcategory": sub,
        "group": tax.group(sub),
        "legacy_category": LEGACY[sub],
        "method": method,
        "doc_no": doc_no,
        "doc_date": doc_date.isoformat() if doc_date else None,
        "is_rescue": is_rescue(sub, amount_kop, balance_eod_kop),
    }
