"""Детерминированная модель данных компании: операции из датасета + будущие обязательства."""
import csv
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import median

NS = uuid.UUID("6f1c0f0e-5b1a-4a53-9a55-0d1c2e3f4a5b")
EXCLUDE = re.compile(r"налог|ЕНП|единый|зарплат|заработн|аванс|комисси|кредит|лизинг|процент|собственн|страх|взнос|возврат",
                     re.I)


def dataset_dir() -> Path:
    env = os.environ.get("MOCK_DATASET_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[3] / "data" / "dataset"


def company_exists(cid: str) -> bool:
    return (dataset_dir() / "statements" / f"{cid}.csv").is_file()


@lru_cache(maxsize=16)
def _load(cid: str, base: str):
    path = Path(base) / "statements" / f"{cid}.csv"
    ops = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            ops.append({
                "id": r["operation_id"], "date": date.fromisoformat(r["operation_date"]),
                "amount": float(r["amount"]), "credit": r["direction"] == "Поступление",
                "inn": r["counterparty_inn"], "name": r["counterparty_name"],
                "purpose": r["payment_purpose"], "balance_eod": float(r["balance_eod"]),
            })
    return ops


def operations(cid: str) -> list[dict]:
    return _load(cid, str(dataset_dir()))


@lru_cache(maxsize=1)
def _companies(base: str) -> dict:
    with (Path(base) / "companies.csv").open(encoding="utf-8-sig", newline="") as f:
        return {r["company_id"]: r for r in csv.DictReader(f, delimiter=";")}


def company_info(cid: str) -> dict:
    return _companies(str(dataset_dir())).get(cid, {"company_id": cid, "company_name": cid, "company_inn": "0000000000"})


def today(cid: str) -> date:
    env = os.environ.get("MOCK_TODAY")
    if env:
        return date.fromisoformat(env)
    return operations(cid)[-1]["date"]


def guid(*parts) -> str:
    return str(uuid.uuid5(NS, ":".join(str(p) for p in parts)))


def opening_balance(cid: str) -> float:
    ops = operations(cid)
    d0 = ops[0]["date"]
    day = [o for o in ops if o["date"] == d0]
    return round(day[0]["balance_eod"] - sum(o["amount"] if o["credit"] else -o["amount"] for o in day), 2)


# ---------- обязательства ----------

@dataclass
class Obligation:
    key: str
    kind: str            # receivable (нам должны) | payable (мы должны)
    scenario: str
    number: str
    issue: date
    due: date
    amount: float
    inn: str
    name: str
    in_onec: bool = True
    posted: bool = True
    deleted: bool = False
    edo_type: str | None = None      # УПД | Счёт | Акт
    edo_status: str | None = None    # Отправлен | Подписан | Отклонён
    edo_number: str | None = None
    contract_date: date | None = None
    new_contract: bool = False
    vat_rate: int = 20


@dataclass
class Model:
    cid: str
    today: date
    obligations: list[Obligation] = field(default_factory=list)
    counterparties: dict = field(default_factory=dict)   # inn -> {name, kind set, first_date}


def _stats(ops, credit):
    g: dict[str, dict] = {}
    for o in ops:
        if o["credit"] != credit or not o["inn"]:
            continue
        if EXCLUDE.search(o["purpose"] or "") or not re.match(r"^(ООО|ИП|АО|ЗАО|ПАО|ОАО)", o["name"] or ""):
            continue
        e = g.setdefault(o["inn"], {"inn": o["inn"], "name": o["name"], "amounts": [], "first": o["date"], "sum": 0.0})
        e["amounts"].append(o["amount"])
        e["sum"] += o["amount"]
        e["first"] = min(e["first"], o["date"])
    out = sorted(g.values(), key=lambda e: (-e["sum"], e["inn"]))
    for e in out:
        e["med"] = median(e["amounts"])
    return out


def _r(x, step=100):
    return round(x / step) * step + 0.0


@lru_cache(maxsize=32)
def _build(cid: str, t: date, base: str) -> Model:
    ops = operations(cid)
    rng = random.Random(f"{cid}:{t.isoformat()}")
    m = Model(cid, t)
    cust = _stats(ops, True)[:8]
    supp = _stats(ops, False)[:8]
    if not cust:
        cust = [{"inn": "0000000001", "name": "ООО «Клиент-001»", "med": 100000.0, "first": t, "amounts": [1e5]}]
    if not supp:
        supp = [{"inn": "0000000002", "name": "ООО «Поставщик-001»", "med": 50000.0, "first": t, "amounts": [5e4]}]
    for e in cust:
        m.counterparties[e["inn"]] = {"name": e["name"], "first": e["first"]}
    for e in supp:
        m.counterparties.setdefault(e["inn"], {"name": e["name"], "first": e["first"]})
    seq = {"c": 100 + rng.randint(0, 50), "s": 500 + rng.randint(0, 50)}

    def add(kind, scenario, e, amount, issue_ago, due_off, **kw):
        k = "c" if kind == "receivable" else "s"
        seq[k] += 1
        pref = "КП" if k == "c" else "ПП"
        n = f"{pref}-{seq[k]:06d}"
        issue = t - timedelta(days=issue_ago)
        due = t + timedelta(days=due_off)
        ob = Obligation(f"{cid}-{scenario}-{n}", kind, scenario, n, issue, due, round(amount, 2),
                        e["inn"], e["name"], contract_date=e.get("first"), **kw)
        m.obligations.append(ob)
        return ob

    # регулярные счета клиентам (ожидаемые поступления)
    for i, e in enumerate(cust[:5]):
        a = _r(e["med"] * rng.uniform(0.8, 1.3))
        status = rng.choice(["Подписан", "Отправлен"])
        add("receivable", "regular", e, a, rng.randint(1, 20), rng.randint(3, 55),
            edo_type=rng.choice(["УПД", "Счёт"]) if i % 2 == 0 else None, edo_status=status)
    # регулярные счета поставщиков (будущие платежи)
    for i, e in enumerate(supp[:4]):
        a = _r(e["med"] * rng.uniform(0.8, 1.2))
        add("payable", "regular", e, a, rng.randint(1, 15), rng.randint(5, 50),
            edo_type=rng.choice(["Счёт", "УПД"]) if i % 2 == 0 else None, edo_status="Подписан")
    # крупный счёт поставщику (в 1С и ЭДО)
    big = max(supp[0]["amounts"]) if supp else 1e5
    add("payable", "large_supplier", supp[0], _r(max(big, supp[0]["med"]) * rng.uniform(3, 5), 1000),
        2, rng.randint(10, 20), edo_type="Счёт", edo_status="Отправлен")
    # просроченные: клиент не заплатил, поставщику не заплатили мы
    add("receivable", "overdue", cust[min(1, len(cust) - 1)], _r(cust[min(1, len(cust) - 1)]["med"] * 1.1),
        40, -12, edo_type="УПД", edo_status="Подписан")
    add("payable", "overdue", supp[min(1, len(supp) - 1)], _r(supp[min(1, len(supp) - 1)]["med"] * 0.9),
        30, -5)
    # дубль между 1С и ЭДО: один и тот же счёт поставщика, одинаковые номер/ИНН/сумма/дата
    ob = add("payable", "duplicate", supp[min(2, len(supp) - 1)], _r(supp[min(2, len(supp) - 1)]["med"] * 1.4),
             6, rng.randint(15, 30), edo_type="Счёт", edo_status="Отправлен")
    # отклонённый документ (только в ЭДО)
    add("receivable", "rejected", cust[0], _r(cust[0]["med"] * 0.7), 4, 25,
        in_onec=False, edo_type="УПД", edo_status="Отклонён")
    # новый договор клиента (+ счёт по нему, пока только в 1С)
    inn = "00" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    ne = {"inn": inn, "name": f"ООО «Новый клиент-{rng.randint(100, 999)}»", "first": t - timedelta(days=3)}
    m.counterparties[inn] = {"name": ne["name"], "first": ne["first"]}
    mx = max(c["med"] for c in cust)
    add("receivable", "new_contract", ne, _r(mx * rng.uniform(1.5, 3), 1000), 2, rng.randint(14, 30),
        new_contract=True)
    # не проведённый и помеченный на удаление счета (шум для 1С)
    add("receivable", "draft", cust[-1], _r(cust[-1]["med"]), 1, 40, posted=False)
    add("payable", "deleted", supp[-1], _r(supp[-1]["med"]), 9, 20, deleted=True)
    # акт в ЭДО по поставщику без счёта в 1С
    add("payable", "edo_only_act", supp[min(3, len(supp) - 1)], _r(supp[min(3, len(supp) - 1)]["med"] * 0.6),
        3, 30, in_onec=False, edo_type="Акт", edo_status="Отправлен")
    for o in m.obligations:
        if o.edo_type:
            o.edo_number = o.number
    return m


def model(cid: str) -> Model:
    return _build(cid, today(cid), str(dataset_dir()))
