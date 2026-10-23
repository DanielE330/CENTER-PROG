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

MAX_ROWS = 200_000

ALIASES = {
    "date": ["дата операции", "дата", "date", "operation_date", "дата проводки", "дата платежа"],
    "amount": ["сумма", "amount", "сумма операции"],
    "debit": ["списание", "дебет", "расход", "debit", "сумма списания"],
    "credit": ["поступление", "кредит", "приход", "credit", "сумма поступления"],
    "direction": ["направление", "тип операции", "тип", "direction", "type"],
    "counterparty": ["контрагент", "наименование контрагента", "наименование", "counterparty", "название контрагента"],
    "inn": ["инн", "инн контрагента", "inn", "counterparty_inn"],
    "purpose": ["назначение платежа", "назначение", "purpose", "description", "описание"],
    "balance": ["остаток", "остаток на конец дня", "остаток на счете", "balance", "balance_after", "остаток на счёте"],
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
            purpose = str(get("purpose") or "").strip()[:1000]
            cat, conf = categorize(purpose, direction)
            txs.append(
                {
                    "row_no": i,
                    "date": d.isoformat(),
                    "amount_kop": str(signed),
                    "direction": direction,
                    "counterparty": str(get("counterparty") or "").strip()[:300],
                    "inn": re.sub(r"\D", "", str(get("inn") or ""))[:12],
                    "purpose": purpose,
                    "balance_kop": None if balance is None else str(balance),
                    "category": cat,
                    "category_confidence": conf,
                }
            )
        except RowError as e:
            errors.append({"row": i, "code": e.code})  # само содержимое строки не возвращаем

    return {"transactions": txs, "errors": errors, "stats": _stats(txs, errors, "balance" in cols)}


def _stats(txs: list[dict], errors: list[dict], has_balance: bool) -> dict:
    stats = {"rows_ok": len(txs), "rows_rejected": len(errors), "has_balance": has_balance}
    if txs:
        ds = sorted(t["date"] for t in txs)
        stats["date_from"], stats["date_to"] = ds[0], ds[-1]
        stats["days_span"] = (date.fromisoformat(ds[-1]) - date.fromisoformat(ds[0])).days + 1
        # сверка остатков: баланс(строка) - баланс(пред. строка) == сумма(строка), если строки идут по порядку
        mism, checked = 0, 0
        prev = None
        for t in sorted(txs, key=lambda t: (t["date"], t["row_no"])):
            if t["balance_kop"] is not None and prev is not None:
                checked += 1
                if int(t["balance_kop"]) - prev != int(t["amount_kop"]):
                    mism += 1
            prev = int(t["balance_kop"]) if t["balance_kop"] is not None else prev
        stats["balance_checked"] = checked
        stats["balance_mismatches"] = mism
    return stats
