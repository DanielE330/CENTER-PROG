"""Мир имитатора ФНС: ИНН из выписок датасета, «проблемные» контрагенты, ЕНП компаний.

Всё детерминировано: сценарий контрагента определяется хэшем ИНН (плюс несколько «подсаженных» проблемных
клиентов среди крупнейших у каждой компании, чтобы в демо были видны предупреждения).
"""
import csv
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import median

import mockdata
from mockdata.model import EXCLUDE

ENP = re.compile(r"ЕНП|Единый налоговый плат[её]ж", re.I)
ENFORCE = re.compile(r"Взыскание задолженности", re.I)
ORG = re.compile(r"^(ООО|ИП|АО|ЗАО|ПАО|ОАО)")
SALT = "fns-sim-v1"

# вероятности сценариев по хэшу ИНН (нарастающие границы)
BUCKETS = [
    (0.005, "bankrupt"), (0.012, "liquidating"), (0.016, "liquidated"), (0.026, "unreliable"),
    (0.034, "disqualified"), (0.054, "mass_address"), (0.104, "tax_debt"),
]
SCENARIOS = {"ok", "bankrupt", "liquidating", "liquidated", "unreliable", "disqualified", "mass_address", "tax_debt"}


def h01(*parts) -> float:
    """Детерминированное число в [0, 1) по набору значений."""
    d = hashlib.sha256((SALT + ":" + ":".join(str(p) for p in parts)).encode()).digest()
    return int.from_bytes(d[:8], "big") / 2 ** 64


@dataclass
class World:
    names: dict = field(default_factory=dict)       # inn -> наименование из выписки
    planted: dict = field(default_factory=dict)     # inn -> сценарий
    companies: dict = field(default_factory=dict)   # inn компании -> company_id
    enp: dict = field(default_factory=dict)         # company_id -> [(date, amount_rub)]
    enforced: set = field(default_factory=set)      # company_id с принудительным взысканием в выписке


@lru_cache(maxsize=4)
def _scan(base: str) -> World:
    w = World()
    root = Path(base)
    try:
        with (root / "companies.csv").open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                w.companies[r["company_inn"]] = r["company_id"]
                w.names.setdefault(r["company_inn"], r.get("company_name") or r["company_id"])
    except OSError:
        pass
    for p in sorted((root / "statements").glob("C*.csv")):
        cid = p.stem
        sums: dict[str, float] = {}
        enp = []
        with p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                inn, name, purpose = r["counterparty_inn"], r["counterparty_name"], r["payment_purpose"] or ""
                if inn:
                    w.names.setdefault(inn, name)
                credit = r["direction"] == "Поступление"
                if not credit and ENFORCE.search(purpose):
                    w.enforced.add(cid)
                elif not credit and ENP.search(purpose):
                    enp.append((date.fromisoformat(r["operation_date"]), float(r["amount"])))
                if credit and inn and not EXCLUDE.search(purpose) and ORG.match(name or ""):
                    sums[inn] = sums.get(inn, 0.0) + float(r["amount"])
        w.enp[cid] = enp
        top = [i for i, _ in sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
        if len(top) >= 4:
            w.planted.setdefault(top[1 + int(h01(cid, "debt") * 3)], "tax_debt")           # 2-4 по выручке
            if h01(cid, "liq") < 0.35 and len(top) >= 6:
                w.planted.setdefault(top[4 + int(h01(cid, "liqpos") * 3) % (len(top) - 4)], "liquidating")
    return w


def world() -> World:
    return _scan(str(mockdata.dataset_dir()))


def reset_cache():
    _scan.cache_clear()


def _env_overrides() -> dict:
    out = {}
    for part in (os.environ.get("FNS_SIM_PROBLEM_INNS") or "").split(","):
        inn, _, sc = part.strip().partition("=")
        if inn and sc in SCENARIOS:
            out[inn] = sc
    return out


def scenario(inn: str) -> str:
    ov = _env_overrides()
    if inn in ov:
        return ov[inn]
    w = world()
    if inn in w.planted:
        return w.planted[inn]
    if (w.names.get(inn) or "").startswith(("Казначейство", "ПАО «Банк")):
        return "ok"  # бюджет и банк не «проблемные»
    x = h01("sc", inn)
    for bound, name in BUCKETS:
        if x < bound:
            return name
    return "ok"


def known(inn: str) -> bool:
    w = world()
    return inn in w.names or inn in _env_overrides()


def today_for(cid: str) -> date:
    return mockdata.today(cid)


def registered_at(inn: str) -> date:
    return date(1999, 1, 11) + timedelta(days=int(h01("reg", inn) * 9600))


def counterparty(inn: str) -> dict:
    """Сведения о контрагенте; found=False, если ИНН нет в «реестре» (в выписках датасета)."""
    if not known(inn):
        return {"inn": inn, "found": False}
    sc = scenario(inn)
    debt = sc in ("tax_debt", "bankrupt")
    amount = 0
    if debt:
        amount = (15_000 + int(h01("debt", inn) ** 2 * 2_500_000)) * 100
    status = {"liquidating": "liquidating", "liquidated": "liquidated"}.get(sc, "active")
    return {
        "inn": inn, "found": True, "status": status,
        "registered_at": registered_at(inn).isoformat(),
        "mass_address": sc == "mass_address",
        "unreliable": sc == "unreliable",
        "tax_debt": {"has": debt, "amount_kop": amount if debt else 0,
                     "as_of": os.environ.get("MOCK_TODAY") or "2026-09-29"},
        "disqualified": sc == "disqualified",
        "bankruptcy": {"flag": sc == "bankrupt", "source": "fns_sim"},
        "scenario": sc,
        "name": world().names.get(inn),
    }


def ogrn(inn: str) -> str:
    return ("1" if len(inn) == 10 else "3") + "".join(str(int(h01("ogrn", inn, i) * 10)) for i in range(12))


# ---------- ЕНС ----------

def ens(inn: str) -> dict | None:
    w = world()
    cid = w.companies.get(inn)
    if cid is None:
        return None
    t = today_for(cid)
    hist = [a for d, a in w.enp.get(cid, []) if d <= t][-6:]
    base = median(hist) if hist else 15_000.0
    # ближайшие три срока: 28-е число с переносом по выходным
    due = []
    y, m = t.year, t.month
    for _ in range(14):
        d = date(y, m, 28)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d >= t and len(due) < 3:
            due.append(d)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    upcoming = []
    for i, d in enumerate(due):
        k = 0.9 + 0.2 * h01("enp", inn, d.isoformat())
        upcoming.append({"date": d.isoformat(), "kind": "enp", "amount_kop": int(round(base * k)) * 100,
                         "title": "Единый налоговый платёж"})
    nxt = upcoming[0]["amount_kop"] if upcoming else int(base) * 100
    if cid in w.enforced:
        balance = -int(nxt * (0.5 + h01("bal", inn)))
    elif h01("neg", inn) < 0.08:
        balance = -int(nxt * 0.3 * (0.2 + h01("neg2", inn)))
    else:
        balance = int(nxt * (0.15 + 0.9 * h01("bal", inn)))
    debt = max(0, -balance)
    return {"inn": inn, "as_of": t.isoformat(), "balance_kop": balance, "debt_kop": debt,
            "overpayment_kop": max(0, balance), "penalty_kop": int(debt * 0.03), "upcoming": upcoming}
