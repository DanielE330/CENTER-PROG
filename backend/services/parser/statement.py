"""Разбор банковской выписки CSV/XLSX в каноничный формат.

Принципы: ничего не исправляем молча; сомнительные строки идут в errors.
Суммы — целые копейки (строкой), без float-арифметики.
"""
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .categorize import categorize
from .taxonomy import classify

MAX_ROWS = 200_000

ALIASES = {
    "date": ["дата операции", "дата", "date", "operation_date", "дата проводки", "дата платежа"],
    "amount": ["сумма", "amount", "сумма операции"],
    "debit": ["списание", "дебет", "расход", "debit", "сумма списания"],
    "credit": ["поступление", "кредит", "приход", "credit", "сумма поступления"],
    "direction": ["направление", "тип операции", "тип", "direction", "type"],
    "counterparty": ["контрагент", "наименование контрагента", "наименование", "counterparty", "название контрагента", "counterparty_name"],
    "inn": ["инн", "инн контрагента", "inn", "counterparty_inn"],
    "purpose": ["назначение платежа", "назначение", "purpose", "description", "описание", "payment_purpose"],
    "source_id": ["operation_id", "id операции", "номер операции"],
    "balance": ["остаток", "остаток на конец дня", "остаток на счете", "balance", "balance_after", "остаток на счёте", "balance_eod"],
}
IN_WORDS = {"in", "credit", "приход", "поступление", "кредит", "income", "+", "входящий"}
OUT_WORDS = {"out", "debit", "расход", "списание", "дебет", "expense", "-", "исходящий"}


class RowError(Exception):
    def __init__(self, code: str):
        self.code = code


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", str(h or "").strip().lower().replace("﻿", ""))


def map_columns(headers: list[str]) -> dict[str, int]:
    normed = [_norm(h) for h in headers]
    out: dict[str, int] = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in normed:
                out[key] = normed.index(n)
                break
    return out


def parse_money(raw) -> int:
    """'1 234,56' | 1234.56 | '-1234.5' -> копейки."""
    if raw is None or str(raw).strip() == "":
        raise RowError("empty_amount")
    s = str(raw).strip().replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise RowError("bad_amount")
    if not d.is_finite():
        raise RowError("bad_amount")
    return int((d * 100).to_integral_value())


def parse_date(raw) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise RowError("bad_date")


def read_rows(filename: str, data: bytes) -> list[list]:
    name = filename.lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)  # формулы не исполняются
        ws = wb.active
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r))
            if len(rows) > MAX_ROWS + 1:
                raise ValueError("too_many_rows")
        return rows
    if name.endswith(".csv") or name.endswith(".txt"):
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("bad_encoding")
        sample = text[:4096]
        delim = max(";,\t|", key=sample.count)
        rows = []
        for r in csv.reader(io.StringIO(text), delimiter=delim):
            rows.append(r)
            if len(rows) > MAX_ROWS + 1:
                raise ValueError("too_many_rows")
        return rows
    raise ValueError("unsupported_type")


def parse_statement(filename: str, data: bytes) -> dict:
    rows = read_rows(filename, data)
    if not rows:
        raise ValueError("empty_file")
    cols = map_columns([str(c) for c in rows[0]])
    if "date" not in cols:
        raise ValueError("no_date_column")
    has_amount = "amount" in cols or ("debit" in cols or "credit" in cols)
    if not has_amount:
        raise ValueError("no_amount_column")

    txs, errors = [], []
    for i, row in enumerate(rows[1:], start=2):
        if not any(str(c).strip() for c in row if c is not None):
            continue

        def get(k):
            j = cols.get(k)
            return row[j] if j is not None and j < len(row) else None

        try:
            d = parse_date(get("date"))
            deb_s = str(get("debit") or "").strip()
            cre_s = str(get("credit") or "").strip()
            if deb_s or cre_s:
                deb = parse_money(deb_s) if deb_s else 0
                cre = parse_money(cre_s) if cre_s else 0
                if deb and cre:
                    raise RowError("both_debit_credit")
                signed = cre if cre else -abs(deb)
            else:
                signed = parse_money(get("amount"))
                dir_raw = str(get("direction") or "").strip().lower()
                if dir_raw:
                    if dir_raw in IN_WORDS:
                        signed = abs(signed)
                    elif dir_raw in OUT_WORDS:
                        signed = -abs(signed)
                    else:
                        raise RowError("bad_direction")
                elif "direction" in cols:
                    raise RowError("bad_direction")
            if signed == 0:
                raise RowError("zero_amount")
            balance = None
            if str(get("balance") or "").strip():
                balance = parse_money(get("balance"))
            direction = "in" if signed > 0 else "out"
            sid = str(get("source_id") or "").strip()[:100]
            purpose = str(get("purpose") or "").strip()[:1000]
            cat, conf = categorize(purpose, direction)
            counterparty = str(get("counterparty") or "").strip()[:300]
            cl = classify(purpose, counterparty, signed, balance)  # признаки для ML: подкатегория, группа, документ, «спасательное»
            txs.append(
                {
                    "row_no": i,
                    "date": d.isoformat(),
                    "amount_kop": str(signed),
                    "direction": direction,
                    "counterparty": counterparty,
                    "inn": re.sub(r"\D", "", str(get("inn") or ""))[:12],
                    "purpose": purpose,
                    "balance_kop": None if balance is None else str(balance),
                    "category": cat,
                    "category_confidence": conf,
                    "subcategory": cl["subcategory"],
                    "group": cl["group"],
                    "doc_no": cl["doc_no"],
                    "doc_date": cl["doc_date"],
                    "is_rescue": cl["is_rescue"],
                }
            )
            if sid:
                txs[-1]["source_id"] = sid
        except RowError as e:
            errors.append({"row": i, "code": e.code})  # само содержимое строки не возвращаем

    return {"transactions": txs, "errors": errors, "stats": _stats(txs, errors, "balance" in cols)}


def _stats(txs: list[dict], errors: list[dict], has_balance: bool) -> dict:
    stats = {"rows_ok": len(txs), "rows_rejected": len(errors), "has_balance": has_balance}
    if not txs:
        return stats
    ordered = sorted(txs, key=lambda t: (t["date"], t["row_no"]))
    ds = [t["date"] for t in ordered]
    d0, d1 = date.fromisoformat(ds[0]), date.fromisoformat(ds[-1])
    stats["date_from"], stats["date_to"] = ds[0], ds[-1]
    stats["days_span"] = (d1 - d0).days + 1

    # пропуски: разрыв между соседними датами операций > 5 календарных дней
    gaps, gap_days, prev = [], 0, None
    for dd in sorted(set(ds)):
        cur = date.fromisoformat(dd)
        if prev is not None and (cur - prev).days > 5:
            gaps.append([prev.isoformat(), dd])
            gap_days += (cur - prev).days - 1
        prev = cur
    stats["gap_days"] = gap_days
    stats["missing_period_count"] = len(gaps)
    stats["first_gap_dates"] = gaps[:5]

    # вероятные точные дубли (не удаляем; импортёр дедуплицирует по номеру вхождения)
    seen: set = set()
    dups = 0
    for t in ordered:
        k = (t["date"], t["amount_kop"], t["inn"], t["purpose"], t["counterparty"])
        if k in seen:
            dups += 1
        else:
            seen.add(k)
    stats["suspected_duplicates"] = dups

    # сверка остатков
    days: dict[str, list] = {}
    for t in ordered:
        if t["balance_kop"] is not None:
            days.setdefault(t["date"], []).append(t)
    checked = mism = 0
    mode = None
    opening = None
    if days:
        running = any(len({x["balance_kop"] for x in v}) > 1 for v in days.values())
        mode = "running" if running else "eod"
        if mode == "eod":
            net: dict[str, int] = {}
            for t in ordered:
                net[t["date"]] = net.get(t["date"], 0) + int(t["amount_kop"])
            pb = None
            for dd in sorted(days):
                b = int(days[dd][-1]["balance_kop"])
                if pb is None:
                    opening = b - net[dd]
                else:
                    checked += 1
                    if b - pb != net[dd]:
                        mism += 1
                pb = b
        else:
            pb = None
            for t in ordered:
                if t["balance_kop"] is None:
                    continue
                b = int(t["balance_kop"])
                if pb is None:
                    opening = b - int(t["amount_kop"])
                else:
                    checked += 1
                    if b - pb != int(t["amount_kop"]):
                        mism += 1
                pb = b
    stats["balance_mode"] = mode
    stats["balance_checked"] = checked
    stats["balance_mismatches"] = mism
    stats["opening_balance_kop"] = None if opening is None else str(opening)
    return stats
