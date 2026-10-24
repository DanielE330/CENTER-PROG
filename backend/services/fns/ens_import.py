"""Разбор справки о состоянии расчётов с бюджетом (КНД 1160080, XML из личного кабинета) и простого CSV.

В личный кабинет ФНС мы не входим и вход по ЭЦП не автоматизируем: владелец сам выгружает справку (docs/fns.md) и
загружает файл. XML разбирается терпимо — по именам атрибутов («Сальдо», «Недоим», «Пен»), потому что точная схема
зависит от версии формата ФНС; если нужных полей нет, файл отклоняется с понятной причиной, а не подставляется «как есть».
CSV — запасной путь с понятными столбцами.
"""
import base64
import binascii
import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_BYTES = 1024 * 1024  # base64 этого файла (1,4 МБ) помещается в лимит тела запроса api (2 МБ)
KND = "1160080"


class ImportError_(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def decode_b64(s: str) -> bytes:
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise ImportError_("fns_import_bad_file", "Файл не удалось прочитать") from None
    if not raw:
        raise ImportError_("fns_import_bad_file", "Файл пустой")
    if len(raw) > MAX_BYTES:
        raise ImportError_("fns_import_too_large", "Файл больше 1 МБ", 413)
    return raw


def to_kop(v) -> int | None:
    if v is None:
        return None
    s = re.sub(r"[\s ]", "", str(v)).replace(",", ".")
    if not s or s in ("-", "—"):
        return None
    try:
        return int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation:
        return None


def to_date(v) -> str | None:
    s = str(v or "").strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10] if f == "%Y-%m-%d" else s, f).date().isoformat()
        except ValueError:
            continue
    return None


def _snapshot(as_of, balance, debt, penalty, upcoming, fmt, warnings) -> dict:
    if balance is None and debt is None:
        raise ImportError_("fns_import_unrecognized",
                           "В файле не найдены сальдо ЕНС и недоимка. Выгрузите «Справку о состоянии расчётов» (КНД 1160080) "
                           "или загрузите CSV со столбцами as_of, balance, debt")
    if balance is None:
        balance = -debt
    if debt is None:
        debt = max(0, -balance)
    debt = abs(debt)
    return {"as_of": as_of or date.today().isoformat(), "balance_kop": balance, "debt_kop": debt,
            "overpayment_kop": max(0, balance), "penalty_kop": penalty, "upcoming": upcoming, "format": fmt,
            "warnings": warnings}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(raw: bytes, inn: str | None) -> dict:
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw, re.I):
        raise ImportError_("fns_import_bad_file", "XML с объявлением DOCTYPE/ENTITY не принимается")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ImportError_("fns_import_bad_file", "Файл не похож на корректный XML") from None
    attrs: list[tuple[str, str, str]] = []  # (тег, имя атрибута в нижнем регистре, значение)
    for el in root.iter():
        for k, v in el.attrib.items():
            attrs.append((_local(el.tag), _local(k).lower(), v))

    def first(*needles, exclude=()):
        for _, k, v in attrs:
            if any(n in k for n in needles) and not any(x in k for x in exclude):
                return v
        return None

    warnings = []
    knd = first("кнд")
    if knd and knd.strip() != KND:
        raise ImportError_("fns_import_wrong_form", f"Это документ КНД {knd.strip()}, нужна справка КНД {KND}")
    if not knd:
        warnings.append("в файле нет КНД: проверьте, что это справка о состоянии расчётов")
    file_inn = first("иннюл", "иннфл") or first("инн")
    if inn and file_inn and file_inn.strip() != inn:
        raise ImportError_("fns_import_inn_mismatch", "ИНН в справке не совпадает с ИНН компании")
    as_of = None
    for key in ("датасост", "датаспр", "датадок", "датаформ", "дата"):
        as_of = to_date(first(key))
        if as_of:
            break
    if not as_of:
        warnings.append("дата справки не найдена, использована сегодняшняя")
    balance = to_kop(first("сальдо", exclude=("развернут",)))
    debt = to_kop(first("недоим", "задолж"))
    penalty = to_kop(first("пен"))
    return _snapshot(as_of, balance, debt, penalty, [], "knd_1160080", warnings)


_ALIASES = {
    "as_of": ("as_of", "date", "дата", "дата_справки"), "balance": ("balance", "saldo", "сальдо", "сальдо_енс"),
    "debt": ("debt", "недоимка", "задолженность"), "penalty": ("penalty", "пени"),
    "due_date": ("due_date", "срок", "дата_платежа"), "due_amount": ("due_amount", "сумма_платежа", "сумма"),
}


def parse_csv(raw: bytes, inn: str | None) -> dict:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("fns_import_bad_file", "Кодировка файла не определена (нужна UTF-8 или Windows-1251)")
    head = text.splitlines()[0] if text.strip() else ""
    delim = max(";,\t", key=head.count)
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    if not rows:
        raise ImportError_("fns_import_unrecognized", "В CSV нет строк с данными")

    def col(row, name):
        low = {(k or "").strip().lower(): v for k, v in row.items()}
        for a in _ALIASES[name]:
            if a in low and (low[a] or "").strip() != "":
                return low[a].strip()
        return None

    main = rows[0]
    upcoming = []
    for r in rows:
        d, a = to_date(col(r, "due_date")), to_kop(col(r, "due_amount"))
        if d and a and a > 0:
            upcoming.append({"date": d, "kind": "enp", "amount_kop": a, "title": "Единый налоговый платёж"})
    return _snapshot(to_date(col(main, "as_of")), to_kop(col(main, "balance")), to_kop(col(main, "debt")),
                     to_kop(col(main, "penalty")), upcoming, "csv", [])


def parse(raw: bytes, inn: str | None = None) -> dict:
    head = raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    return parse_xml(raw, inn) if head == b"<" else parse_csv(raw, inn)


def iso_utc(ts: float) -> str:
    from datetime import timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
