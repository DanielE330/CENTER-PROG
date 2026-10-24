"""Клиент 1С (OData v3): счета на оплату + ИНН контрагентов."""
import httpx

from . import base
from .base import SourceError

B = "/odata/standard.odata/"
PAGE = 100
FILTER = "Проведен eq true and ПометкаУдаления eq false"
DOCS = {  # сущность -> направление (in = поступление, out = списание)
    "Document_СчетНаОплатуПокупателю": "in",
    "Document_СчетНаОплатуПоставщика": "out",
}


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return base.make_http(
        base.env("ONEC_URL", "http://localhost:8004"), transport,
        auth=(base.env("ONEC_USER", "demo"), base.env("ONEC_PASSWORD", "demo")),
    )


def fetch_all(http: httpx.Client, entity: str, flt: str | None = None, page: int = PAGE, **kw) -> list[dict]:
    """Пагинация $top/$skip до пустой (или неполной) страницы."""
    out, skip = [], 0
    while True:
        params = {"$format": "json", "$top": page, "$skip": skip}
        if flt:
            params["$filter"] = flt
        value = base.request(http, "GET", B + entity, params=params, **kw).json().get("value", [])
        out += value
        if len(value) < page:
            return out
        skip += page


def fetch(http: httpx.Client, page: int = PAGE) -> list[dict]:
    """Нормализованные записи: kind=invoice, дата — срок оплаты."""
    try:
        inns = {c["Ref_Key"]: c for c in fetch_all(http, "Catalog_Контрагенты", page=page)}
        items = []
        for entity, direction in DOCS.items():
            for d in fetch_all(http, entity, FILTER, page=page):
                items.append(parse_invoice(d, direction, inns))
        return items
    except (ValueError, KeyError) as e:  # битый JSON
        raise SourceError(f"1С: некорректный ответ: {e}") from e


def parse_invoice(d: dict, direction: str, contractors: dict[str, dict]) -> dict:
    cp = contractors.get(d.get("Контрагент_Key"), {})
    due = d.get("ДатаОплаты") or d.get("СрокОплаты") or d.get("Дата")
    return {
        "external_id": d["Ref_Key"], "source": "onec", "kind": "invoice", "direction": direction,
        "date": str(due)[:10], "amount": d["СуммаДокумента"],
        "title": f"Счёт на оплату {d.get('Номер', '')}".strip() + (f" ({d['Комментарий']})" if d.get("Комментарий") else ""),
        "counterparty": cp.get("Description") or cp.get("НаименованиеПолное") or "",
        "inn": cp.get("ИНН") or "", "number": d.get("Номер") or "",
        "posted": bool(d.get("Проведен")), "deleted": bool(d.get("ПометкаУдаления")),
        "doc_status": "Проведён" if d.get("Проведен") else "Не проведён",
    }
