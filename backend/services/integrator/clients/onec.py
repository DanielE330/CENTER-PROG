"""Клиент 1С (OData v3, стандартный интерфейс): счета на оплату, оплаты по ним, договоры и неисполненные платёжки.

Работает и с настоящей «1С:Бухгалтерией» (в т. ч. 1С:Фреш), и с имитатором `onec`. Отличия настоящей базы:
служебные поля по-английски (Number, Date, Posted, DeletionMark), у счёта нет даты оплаты — срок берётся из
договора (СрокОплаты в днях), оплаты связаны со счётом через РасшифровкаПлатежа.СчетНаОплату_Key.
"""
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from . import base
from .base import SourceError

B = "/odata/standard.odata/"
PAGE = 100
FILTER = "Posted eq true and DeletionMark eq false"
DOCS = {  # сущность -> направление (in = поступление, out = списание)
    "Document_СчетНаОплатуПокупателю": "in",
    "Document_СчетНаОплатуПоставщика": "out",
}
PAYMENTS = ("Document_ПоступлениеНаРасчетныйСчет", "Document_СписаниеСРасчетногоСчета")
ORDERS = "Document_ПлатежноеПоручение"
ZERO = "00000000-0000-0000-0000-000000000000"


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return base.make_http(
        base.env("ONEC_URL", "http://localhost:8004"), transport,
        auth=(base.env("ONEC_USER", "demo"), base.env("ONEC_PASSWORD", "demo")),
    )


def live_client(conn, transport: httpx.BaseTransport | None = None, resolver=None) -> httpx.Client:
    """Клиент настоящей 1С по реквизитам, которые ввёл пользователь компании (`conn.url/username/password`).

    Защита от SSRF (guard.py): соединение только с публичными адресами, без редиректов, размер и время ответа ограничены,
    переменные окружения прокси не используются. `transport` подменяется в тестах (проверка адреса при этом остаётся)."""
    from .. import guard  # локальный импорт: guard сам зависит от clients.base

    inner = transport or guard.guarded_transport(resolver)
    return base.make_http(
        conn.url, guard.LimitedTransport(inner), auth=(conn.username, conn.password), trust_env=False,
        timeout=float(base.env("INTEGRATIONS_TIMEOUT", "20")),
    )


def fetch_all(http: httpx.Client, entity: str, flt: str | None = None, page: int = PAGE, **kw) -> list[dict]:
    """Пагинация $top/$skip до пустой (или неполной) страницы."""
    out, skip = [], 0
    while True:
        params = {"$format": "json", "$top": page, "$skip": skip}
        if flt:
            params["$filter"] = flt
        value = base.request(http, "GET", f"{B}{entity}?{query(params)}", **kw).json().get("value", [])
        out += value
        if len(value) < page:
            return out
        skip += page


def query(params: dict) -> str:
    """Пробел только как %20: с «+» (как кодирует httpx) 1С молча игнорирует $filter и отдаёт всё."""
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())


def fetch_optional(http: httpx.Client, entity: str, flt: str | None = None, page: int = PAGE) -> list[dict]:
    """Сущность, которой может не быть в публикации (или в имитаторе): 4xx — пустой список."""
    try:
        return fetch_all(http, entity, flt, page=page)
    except SourceError as e:
        if "HTTP 4" in str(e):
            return []
        raise


def field(d: dict, *names, default=None):
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return default


def fetch(http: httpx.Client, page: int = PAGE) -> list[dict]:
    """Нормализованные записи: счета (kind=invoice, остаток к оплате) и неисполненные платёжки (kind=payment_order)."""
    try:
        contractors = {c["Ref_Key"]: c for c in fetch_all(http, "Catalog_Контрагенты", page=page)}
        contracts = {c["Ref_Key"]: c for c in fetch_optional(http, "Catalog_ДоговорыКонтрагентов", page=page)}
        paid, executed = payments(http, page)
        orders = [o for o in fetch_optional(http, ORDERS, FILTER, page=page) if o["Ref_Key"] not in executed]
        in_orders: dict[str, float] = {}  # счёт поставщика -> сумма в неисполненных платёжках
        for o in orders:
            basis = o.get("ДокументОснование")
            if basis and basis != ZERO:
                in_orders[basis] = in_orders.get(basis, 0) + float(o.get("СуммаДокумента") or 0)
        items = []
        for entity, direction in DOCS.items():
            for d in fetch_all(http, entity, FILTER, page=page):
                rec = parse_invoice(d, direction, contractors, contracts)
                rest = float(rec["amount"]) - paid.get(d["Ref_Key"], 0) - in_orders.get(d["Ref_Key"], 0)
                if rest <= 0.005:
                    continue
                if rest < float(rec["amount"]) - 0.005:
                    rec["title"] = rec["title"].replace(" · ", f" · остаток {rub(rest)} из {rub(float(rec['amount']))} · ", 1) \
                        if " · " in rec["title"] else rec["title"] + f" · остаток {rub(rest)} из {rub(float(rec['amount']))}"
                    rec["amount"] = round(rest, 2)
                items.append(rec)
        items += [parse_order(o, contractors) for o in orders]
        return items
    except (ValueError, KeyError, TypeError) as e:  # битый JSON
        raise SourceError(f"1С: некорректный ответ: {e}") from e


def payments(http: httpx.Client, page: int = PAGE) -> tuple[dict[str, float], set[str]]:
    """Оплачено по каждому счёту (по расшифровке платежа) и платёжки, по которым банк уже провёл списание."""
    paid: dict[str, float] = {}
    executed: set[str] = set()
    for entity in PAYMENTS:
        for p in fetch_optional(http, entity, FILTER, page=page):
            for row in p.get("РасшифровкаПлатежа") or []:
                inv = row.get("СчетНаОплату_Key")
                if inv and inv != ZERO:
                    paid[inv] = paid.get(inv, 0) + float(row.get("СуммаПлатежа") or 0)
            basis = p.get("ДокументОснование")
            if basis and basis != ZERO:
                executed.add(basis)
    return paid, executed


def rub(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ") if abs(v - round(v)) < 0.005 else f"{v:,.2f} ₽".replace(",", " ")


def due_date(d: dict, contract: dict | None) -> str:
    """Срок оплаты: дата оплаты в документе, иначе дата счёта + отсрочка по договору, иначе дата счёта."""
    explicit = field(d, "ДатаОплаты", "СрокОплаты")
    if explicit:
        return str(explicit)[:10]
    issued = date.fromisoformat(str(field(d, "Date", "Дата"))[:10])
    if contract and contract.get("УстановленСрокОплаты"):
        days = int(float(contract.get("СрокОплаты") or 0))
        if contract.get("ВидДнейСрокаОплаты") == "Рабочие":
            due, left = issued, days
            while left > 0:
                due += timedelta(days=1)
                left -= due.weekday() < 5
            return due.isoformat()
        return (issued + timedelta(days=days)).isoformat()
    return issued.isoformat()


def parse_invoice(d: dict, direction: str, contractors: dict[str, dict], contracts: dict[str, dict] | None = None) -> dict:
    cp = contractors.get(d.get("Контрагент_Key"), {})
    contract = (contracts or {}).get(d.get("ДоговорКонтрагента_Key"))
    number = str(field(d, "НомерВходящегоДокумента", "Number", "Номер", default="")).strip()
    posted = bool(field(d, "Posted", "Проведен", default=False))
    comment = (d.get("Комментарий") or "").strip()
    title = f"Счёт {number}".strip() + (f": {comment}" if comment else "")
    if contract and contract.get("УстановленСрокОплаты") and not field(d, "ДатаОплаты", "СрокОплаты"):
        title += f" · отсрочка {int(float(contract.get('СрокОплаты') or 0))} дн. по договору {(contract.get('Номер') or '').strip()}".rstrip()
    return {
        "external_id": d["Ref_Key"], "source": "onec", "kind": "invoice", "direction": direction,
        "date": due_date(d, contract), "amount": d["СуммаДокумента"], "title": title,
        "counterparty": cp.get("Description") or cp.get("НаименованиеПолное") or "",
        "inn": cp.get("ИНН") or "", "number": number,
        "posted": posted, "deleted": bool(field(d, "DeletionMark", "ПометкаУдаления", default=False)),
        "doc_status": "Проведён" if posted else "Не проведён",
    }


def parse_order(o: dict, contractors: dict[str, dict]) -> dict:
    """Платёжное поручение, созданное в 1С, но ещё не исполненное банком: гарантированное списание."""
    cp = contractors.get(o.get("Контрагент"), {})
    number = str(field(o, "Number", "Номер", default="")).strip()
    return {
        "external_id": o["Ref_Key"], "source": "onec", "kind": "payment_order", "direction": "out",
        "date": str(field(o, "Date", "Дата"))[:10], "amount": o["СуммаДокумента"],
        "title": f"Платёжное поручение № {number}: {(o.get('НазначениеПлатежа') or '').strip()}"[:200],
        "counterparty": cp.get("Description") or cp.get("НаименованиеПолное") or "",
        "inn": cp.get("ИНН") or o.get("ИННПолучателя") or "", "number": f"ПП-{number}",
        "posted": True, "deleted": False, "doc_status": "Не исполнено банком",
    }
