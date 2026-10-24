"""Access to the organizers' dataset for replay: operations of one company up to a
date, invoices that were already issued at that date, and synthetic registry statuses."""

import csv
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

DOC_RE = re.compile(r"(?:УПД|сч[её]т[ау]?|счет-фактур\w*)\s*№\s*([\w/-]+)\s+от\s+(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)


@dataclass(frozen=True)
class Op:
    op_date: date
    amount: str
    direction: str
    inn: str
    name: str
    purpose: str
    balance: str

    @property
    def signed_kop(self) -> int:
        kop = round(float(self.amount) * 100)
        return kop if self.direction == "Поступление" else -kop

    def row(self) -> dict:
        return {
            "operation_date": self.op_date.isoformat(),
            "amount": self.amount,
            "direction": self.direction,
            "counterparty_inn": self.inn or None,
            "counterparty_name": self.name,
            "payment_purpose": self.purpose,
            "balance_eod": self.balance,
        }


def counterparty_ref(tenant: str, op: Op, key: bytes) -> str | None:
    """Same pseudonym as ingest computes (HMAC of tenant and INN, or name without INN)."""
    ident = op.inn.strip() or op.name.strip()
    if not ident:
        return None
    digest = hmac.new(key, f"{tenant}:{ident}".encode(), hashlib.sha256).digest()
    return "cp_" + digest[:16].hex()


@lru_cache(maxsize=4)
def load_company(data_dir: str, company: str) -> tuple[Op, ...]:
    path = Path(data_dir) / "statements" / f"{company}.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        ops = [
            Op(
                op_date=date.fromisoformat(r["operation_date"]),
                amount=r["amount"],
                direction=r["direction"],
                inn=(r.get("counterparty_inn") or "").strip(),
                name=r.get("counterparty_name") or "",
                purpose=r.get("payment_purpose") or "",
                balance=r["balance_eod"],
            )
            for r in csv.DictReader(fh, delimiter=";")
        ]
    return tuple(sorted(ops, key=lambda o: o.op_date))


def companies(data_dir: str) -> list[dict]:
    with (Path(data_dir) / "companies.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def between(ops: tuple[Op, ...], start: date | None, end: date) -> list[Op]:
    return [o for o in ops if (start is None or o.op_date > start) and o.op_date <= end]


def open_invoices(ops: tuple[Op, ...], as_of: date, tenant: str, key: bytes) -> list[dict]:
    """Invoices issued on or before `as_of` and paid after it: what an EDO or
    accounting integration would already know. Partial payments are summed."""
    docs: dict[tuple, dict] = {}
    for o in ops:
        if o.op_date <= as_of or o.direction != "Поступление":
            continue
        m = DOC_RE.search(o.purpose)
        if not m:
            continue
        try:
            doc_date = datetime.strptime(m.group(2), "%d.%m.%Y").date()
        except ValueError:
            continue
        if doc_date > as_of or (o.op_date - doc_date).days > 365:
            continue
        cp = counterparty_ref(tenant, o, key)
        k = (cp, m.group(1), doc_date)
        d = docs.setdefault(
            k,
            {
                "kind": "invoice_out",
                "direction": "inflow",
                "amount_kop": 0,
                "due_date": str(doc_date + timedelta(days=14)),
                "certainty": 0.9,
                "counterparty_ref": cp,
                "doc_no": m.group(1),
                "doc_date": str(doc_date),
                "source": "edo_replay",
                "source_ref": f"{cp}:{m.group(1)}:{doc_date}",
            },
        )
        d["amount_kop"] += o.signed_kop
    return list(docs.values())


def registry_statuses(ops: tuple[Op, ...], as_of: date, tenant: str, key: bytes) -> list[dict]:
    """Synthetic registry for fictitious INNs: a customer who paid regularly and has
    been silent for four months is treated as being liquidated. Uses only the past."""
    last: dict[str, date] = {}
    count: dict[str, int] = {}
    for o in ops:
        if o.op_date > as_of or o.direction != "Поступление" or not o.inn:
            continue
        cp = counterparty_ref(tenant, o, key)
        if cp:
            last[cp] = o.op_date
            count[cp] = count.get(cp, 0) + 1
    return [
        {"counterparty_ref": cp, "status": "liquidation" if (as_of - d).days > 120 else "active"}
        for cp, d in last.items()
        if count[cp] >= 3
    ]


# Demo tenants: companies where the owner covered a hole with own money, "today" set a few
# weeks before it, plus the shortest history for the "not enough data" branch.
# Format: COMPANY:YYYY-MM-DD, an empty date means "two weeks before the data ends".
DEFAULT_DEMO = "C084:2025-12-24,C117:2025-10-24,C061:2025-07-04,C110:"


def pick_demo_companies(data_dir: str, spec: str | None = None) -> list[dict]:
    rows = {r["company_id"]: r for r in companies(data_dir)}
    out = []
    for item in (spec or DEFAULT_DEMO).split(","):
        cid, _, day = item.strip().partition(":")
        if cid not in rows:
            continue
        r = rows[cid]
        as_of = date.fromisoformat(day) if day else date.fromisoformat(r["last_operation_date"]) - timedelta(days=14)
        out.append({"company": cid, "name": r["company_name"], "industry": r["industry"], "as_of": as_of})
    return out
