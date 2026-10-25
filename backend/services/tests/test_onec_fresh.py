"""Клиент 1С на ответах настоящей «1С:Бухгалтерии» (1С:Фреш): фикстуры записаны из демо-базы через OData."""
import json
from pathlib import Path
from urllib.parse import unquote

import httpx

from integrator import merge
from integrator.clients import onec

FIX = Path(__file__).parent / "fixtures" / "onec_fresh"


def fresh(hits: list[str] | None = None) -> httpx.MockTransport:
    """Отдаёт записанные ответы; сущности, которой нет в публикации, — 404 как у 1С."""
    def handler(req: httpx.Request) -> httpx.Response:
        entity = unquote(req.url.path).rsplit("/", 1)[1]
        if hits is not None:
            hits.append(req.url.query.decode())
        f = FIX / f"{entity}.json"
        if not f.exists():
            return httpx.Response(404, json={"odata.error": {"code": "8", "message": {"lang": "ru", "value": f"Сущность '{entity}' не найдена"}}})
        if int(req.url.params.get("$skip", "0")) > 0:
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json=json.loads(f.read_text(encoding="utf-8")))
    return httpx.MockTransport(handler)


def items(as_of="2026-09-29"):
    with onec.client(fresh()) as h:
        out, _ = merge.merge(onec.fetch(h), as_of)
    return {i["number"]: i for i in out}


def test_filter_uses_standard_odata_field_names():
    hits: list[str] = []
    with onec.client(fresh(hits)) as h:
        onec.fetch(h)
    assert any("Posted eq true and DeletionMark eq false" in unquote(q) for q in hits)
    assert not any("+" in q for q in hits)                          # «+» вместо %20 1С молча игнорирует


def test_paid_invoices_dropped_partial_paid_as_rest():
    got = items()
    assert "BS-0901" not in got and "BS-0910" not in got          # оплачены полностью
    part = got["BS-1012"]
    assert part["amount_kop"] == "6000000" and "остаток 60 000 ₽ из 160 000 ₽" in part["title"]


def test_due_date_from_contract_and_overdue():
    got = items()
    assert got["BS-0905"]["date"] == "2026-08-25" and got["BS-0905"]["overdue"]   # 11.08 + 14 дн.
    assert got["BS-1001"]["date"] == "2026-10-06" and not got["BS-1001"]["overdue"]
    assert "отсрочка 30 дн. по договору Д-1475" in got["BS-1001"]["title"]
    assert got["BS-1001"]["inn"] == "2310031475" and got["BS-1001"]["direction"] == "in"


def test_pending_payment_orders_replace_their_supplier_invoices():
    got = items()
    assert got["ПП-101"]["kind"] == "payment_order" and got["ПП-101"]["direction"] == "out"
    assert got["ПП-101"]["confidence"] == 0.97 and got["ПП-101"]["date"] == "2026-09-29"
    assert "ЦТ-1025" not in got and "ЯД-88120" not in got           # уже в платёжках, второй раз не считаем
    assert got["ХТ-4471"]["date"] == "2026-10-07"                    # счёт поставщика без платёжки остаётся
    later = items(as_of="2026-10-03")
    assert later["ПП-101"]["date"] == "2026-10-03" and not later["ПП-101"]["overdue"]


def test_working_days_contract():
    c = {"УстановленСрокОплаты": True, "СрокОплаты": "5", "ВидДнейСрокаОплаты": "Рабочие"}
    assert onec.due_date({"Date": "2026-10-23T00:00:00"}, c) == "2026-10-30"   # пятница + 5 рабочих
    assert onec.due_date({"Date": "2026-10-23T00:00:00", "ДатаОплаты": "2026-11-02T00:00:00"}, c) == "2026-11-02"
    assert onec.due_date({"Date": "2026-10-23T00:00:00"}, None) == "2026-10-23"
