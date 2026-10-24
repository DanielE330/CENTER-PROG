"""Rule-based categorization, document references and rescue inflows."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

DOC_RE = re.compile(
    r"(?:УПД|сч[её]т[ау]?|счет-фактур\w*|договор\w*)\s*№\s*([\w/-]+)\s+от\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE
)
RESCUE_SUBS = {"own_transfer_in", "founder_loan_in"}


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
    rules: list[Rule]

    def group(self, sub: str) -> str:
        return self.subcategories.get(sub, {}).get("group", "other")

    def fallback(self, amount_kop: int) -> str:
        return "other_in" if amount_kop >= 0 else "other_out"


def load(path: Path) -> Taxonomy:
    raw = yaml.safe_load(path.read_text())
    rules = [
        Rule(
            sub=r["sub"],
            purpose=re.compile(r["purpose"], re.IGNORECASE) if r.get("purpose") else None,
            name=re.compile(r["name"], re.IGNORECASE) if r.get("name") else None,
            direction=r.get("direction"),
        )
        for r in raw["rules"]
    ]
    unknown = {r.sub for r in rules} - set(raw["subcategories"])
    if unknown:
        raise ValueError(f"rules reference unknown subcategories: {unknown}")
    return Taxonomy(groups=raw["groups"], subcategories=raw["subcategories"], rules=rules)


def match(tax: Taxonomy, purpose: str, name: str, amount_kop: int) -> str | None:
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
    m = DOC_RE.search(purpose or "")
    if not m:
        return None, None
    try:
        return m.group(1), datetime.strptime(m.group(2), "%d.%m.%Y").date()
    except ValueError:
        return m.group(1), None


def is_rescue(sub: str, amount_kop: int, balance_eod_kop: int) -> bool:
    """An inflow of own money that kept the balance from going negative is a reaction
    to a cash gap, not a plan. The forecast excludes it and the backtest labels the gap."""
    return sub in RESCUE_SUBS and amount_kop > 0 and balance_eod_kop - amount_kop < 0
