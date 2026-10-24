"""Клиент ЭДО: курсорная пагинация, Bearer."""
import httpx

from . import base
from .base import SourceError

KINDS = {"Счёт": "invoice", "Акт": "act", "УПД": "upd", "Договор": "contract"}
DIRECTION = {"incoming": "out", "outgoing": "in"}  # входящий от поставщика -> мы платим


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return base.make_http(
        base.env("EDO_URL", "http://localhost:8005"), transport,
        headers={"Authorization": "Bearer " + base.env("EDO_TOKEN", "edo-demo-token")},
    )


def fetch_all(http: httpx.Client, company: str, limit: int = 50) -> list[dict]:
    out, cursor, seen = [], None, set()
    while True:
        params = {"company": company, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            j = base.request(http, "GET", "/edo/v1/documents", params=params).json()
        except ValueError as e:
            raise SourceError(f"ЭДО: некорректный ответ: {e}") from e
        out += j.get("documents", [])
        cursor = j.get("next_cursor")
        if not cursor:
            return out
        if cursor in seen:
            raise SourceError("ЭДО: зацикленный курсор")
        seen.add(cursor)


def fetch(http: httpx.Client, company: str, limit: int = 50) -> list[dict]:
    try:
        return [parse_doc(d) for d in fetch_all(http, company, limit)]
    except (KeyError, TypeError) as e:
        raise SourceError(f"ЭДО: неожиданная структура: {e}") from e


def parse_doc(d: dict) -> dict:
    cp = d.get("counterparty") or {}
    return {
        "external_id": d["id"], "source": "edo", "kind": KINDS.get(d.get("type"), "invoice"),
        "direction": DIRECTION[d["direction"]],
        "date": str(d.get("payment_due_date") or d["date"])[:10], "amount": d["amount"],
        "title": f"{d.get('type', 'Документ')} {d.get('number', '')}".strip(),
        "counterparty": cp.get("name") or "", "inn": cp.get("inn") or "", "number": d.get("number") or "",
        "doc_status": d.get("status") or "",
    }
