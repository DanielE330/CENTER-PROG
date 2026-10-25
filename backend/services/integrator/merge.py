"""Чистая склейка: фильтрация, дедупликация, просрочка, confidence. Без I/O."""
from decimal import Decimal

PRIORITY = {"onec": 0, "edo": 1}


def to_kop(amount) -> int:
    return int((Decimal(str(amount)) * 100).to_integral_value())


def confidence(rec: dict, overdue: bool) -> float:
    st = rec.get("doc_status", "")
    if rec.get("kind") == "payment_order":  # платёжка уже создана в 1С: банк её исполнит
        return 0.97
    if rec["source"] == "onec":
        c = 0.9 if rec.get("posted", True) else 0.5
    elif st == "Подписан":
        c = 0.95
    elif st == "Отправлен":
        c = 0.7
    else:
        c = 0.5
    if overdue:
        c -= 0.25
    return round(max(0.05, min(1.0, c)), 2)


def merge(records: list[dict], as_of: str) -> tuple[list[dict], dict[str, dict]]:
    """Возвращает (items, stats по источнику: count/duplicates/rejected/skipped).

    Отбрасываются: удалённые/непроведённые (1С), отклонённые (ЭДО).
    Дубль по (номер, ИНН, сумма): остаётся запись из более приоритетного источника
    (1С), у второй duplicate_of указывает на оставленную; в items дубль не попадает.
    """
    stats: dict[str, dict] = {}
    st = lambda s: stats.setdefault(s, {"count": 0, "duplicates": 0, "rejected": 0, "skipped": 0})
    kept: dict[tuple, dict] = {}
    for r in sorted(records, key=lambda r: PRIORITY.get(r["source"], 9)):
        s = st(r["source"])
        if r.get("deleted") or r.get("posted") is False:
            s["skipped"] += 1
            continue
        if r.get("doc_status") == "Отклонён":
            s["rejected"] += 1
            continue
        s["count"] += 1
        kop = to_kop(r["amount"])
        key = (r.get("number", ""), r.get("inn", ""), kop)
        if r.get("number") and key in kept:
            first = kept[key]
            r["duplicate_of"] = first["external_id"]
            s["duplicates"] += 1
            first["confidence"] = min(1.0, round(first["confidence"] + 0.03, 2))  # подтверждён двумя источниками
            continue
        when = max(r["date"], as_of) if r["kind"] == "payment_order" else r["date"]  # неисполненная — уйдёт сегодня
        overdue = when < as_of
        item = {
            "external_id": r["external_id"], "source": r["source"], "kind": r["kind"], "direction": r["direction"],
            "date": when, "amount_kop": str(kop), "title": r["title"], "counterparty": r["counterparty"],
            "inn": r["inn"], "number": r["number"], "confidence": confidence(r, overdue),
            "status": r.get("doc_status", ""), "overdue": overdue, "duplicate_of": None,
        }
        kept[key] = item
    items = sorted(kept.values(), key=lambda i: (i["date"], i["external_id"]))
    return items, stats
