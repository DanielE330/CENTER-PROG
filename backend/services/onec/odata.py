"""Имитация 1С:Предприятие (Бухгалтерия) — стандартный интерфейс OData v3 (/odata/standard.odata)."""
import base64
import os
import re
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

import mockdata as data
from mockdata import delay_and_should_fail


def chaos_check():
    if delay_and_should_fail():
        raise HTTPException(503, "Service Unavailable (simulated failure)")


def company_param(company: str = Query("C001", pattern=r"^C\d{3}$")) -> str:
    if not data.company_exists(company):
        raise HTTPException(404, f"Unknown company {company}")
    return company


BASIC = "Basic realm=\"1C:Enterprise odata\""


def auth(request: Request):
    h = request.headers.get("authorization", "")
    ok = False
    if h.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            ok = (secrets.compare_digest(u, os.environ.get("MOCK_1C_USER", "demo"))
                  and secrets.compare_digest(p, os.environ.get("MOCK_1C_PASSWORD", "demo")))
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": BASIC})


router = APIRouter(tags=["1C"], dependencies=[Depends(chaos_check), Depends(auth)])
ROOT = "/odata/standard.odata"
ENTITIES = ["Catalog_Контрагенты", "Catalog_ДоговорыКонтрагентов",
            "Document_СчетНаОплатуПокупателю", "Document_СчетНаОплатуПоставщика"]
ALIASES = {"Date": "Дата", "Number": "Номер", "Posted": "Проведен", "DeletionMark": "ПометкаУдаления"}


def dt(d: date | None) -> str:
    return d.strftime("%Y-%m-%dT00:00:00") if d else "0001-01-01T00:00:00"


def org_key(cid):
    return data.guid(cid, "org")


def counterparties(m):
    out = []
    for inn, c in sorted(m.counterparties.items()):
        ip = len(inn) == 12
        out.append({
            "Ref_Key": data.guid(m.cid, "cp", inn), "DataVersion": "AAAAAQAAAAA=", "ПометкаУдаления": False,
            "Parent_Key": "00000000-0000-0000-0000-000000000000", "IsFolder": False,
            "Code": f"{int(data.guid(m.cid, 'code', inn)[:6], 16) % 100000:05d}",
            "Description": c["name"], "НаименованиеПолное": c["name"], "ИНН": inn,
            "КПП": "" if ip else "770101001",
            "ЮридическоеФизическоеЛицо": "ИндивидуальныйПредприниматель" if ip else "ЮридическоеЛицо",
        })
    return out


def contracts(m):
    kinds = {}
    for o in m.obligations:
        kinds.setdefault(o.inn, set()).add("СПокупателем" if o.kind == "receivable" else "СПоставщиком")
    out = []
    for inn in sorted(m.counterparties):
        for k in sorted(kinds.get(inn, {"СПокупателем"})):
            d = m.counterparties[inn]["first"]
            out.append({
                "Ref_Key": data.guid(m.cid, "ctr", inn, k), "DataVersion": "AAAAAQAAAAA=", "ПометкаУдаления": False,
                "Owner_Key": data.guid(m.cid, "cp", inn), "Организация_Key": org_key(m.cid),
                "Description": f"Договор {'поставки' if k == 'СПокупателем' else 'закупки'} № {d:%y}/{inn[-3:]}",
                "Номер": f"{d:%y}/{inn[-3:]}", "Дата": dt(d), "ВидДоговора": k,
                "СрокДействия": dt(date(d.year + 1, d.month, min(d.day, 28))),
                "ВалютаВзаиморасчетов_Key": data.guid("RUB"), "ВедениеВзаиморасчетов": "ПоДоговоруВЦелом",
                "Комментарий": "Новый договор" if any(o.new_contract and o.inn == inn for o in m.obligations) else "",
            })
    return out


def invoices(m, kind):
    out = []
    for o in m.obligations:
        if o.kind != kind or not o.in_onec:
            continue
        k = "СПокупателем" if kind == "receivable" else "СПоставщиком"
        out.append({
            "Ref_Key": data.guid(m.cid, "inv", o.key), "DataVersion": "AAAAAQAAAAA=",
            "ПометкаУдаления": o.deleted,
            "Номер": o.number, "Дата": dt(o.issue), "Проведен": o.posted,
            "Организация_Key": org_key(m.cid), "Контрагент_Key": data.guid(m.cid, "cp", o.inn),
            "ДоговорКонтрагента_Key": data.guid(m.cid, "ctr", o.inn, k),
            "СуммаДокумента": o.amount, "ВалютаДокумента_Key": data.guid("RUB"),
            "СуммаВключаетНДС": True, "ДатаОплаты": dt(o.due), "СрокОплаты": dt(o.due),
            "Комментарий": "",
        })
    return out


BUILDERS = {
    "Catalog_Контрагенты": counterparties,
    "Catalog_ДоговорыКонтрагентов": contracts,
    "Document_СчетНаОплатуПокупателю": lambda m: invoices(m, "receivable"),
    "Document_СчетНаОплатуПоставщика": lambda m: invoices(m, "payable"),
}
COND = re.compile(r"^\s*(\(?)\s*([\w/]+)\s+(eq|ne|ge|gt|le|lt)\s+(.+?)\s*(\)?)\s*$", re.S)


def parse_value(s: str):
    s = s.strip()
    m = re.match(r"^datetime'([^']*)'$", s)
    if m:
        v = m.group(1)
        return v if "T" in v else v + "T00:00:00"
    m = re.match(r"^guid'([^']*)'$", s)
    if m:
        return m.group(1).lower()
    if s in ("true", "false"):
        return s == "true"
    m = re.match(r"^'(.*)'$", s)
    if m:
        return m.group(1).replace("''", "'")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"bad literal {s}")


def parse_filter(expr: str):
    conds = []
    for part in re.split(r"\s+and\s+", expr):
        m = COND.match(part)
        if not m:
            raise ValueError(f"cannot parse: {part}")
        field = ALIASES.get(m.group(2), m.group(2))
        conds.append((field, m.group(3), parse_value(m.group(4))))
    return conds


def match(rec, conds):
    ops = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "ge": lambda a, b: a >= b,
           "gt": lambda a, b: a > b, "le": lambda a, b: a <= b, "lt": lambda a, b: a < b}
    for f, op, v in conds:
        if f not in rec:
            raise ValueError(f"unknown field {f}")
        a = rec[f]
        if not ops[op](a, v):
            return False
    return True


def odata_error(status, msg):
    return JSONResponse({"odata.error": {"code": str(status), "message": {"lang": "ru", "value": msg}}},
                        status_code=status)


def base_url(request: Request):
    return str(request.base_url).rstrip("/") + ROOT


@router.get("/odata/standard.odata/")
@router.get("/odata/standard.odata")
def service_root(request: Request, company: str = Depends(company_param)):
    return {"odata.metadata": base_url(request) + "/$metadata",
            "value": [{"name": n, "url": n} for n in ENTITIES]}


@router.get("/odata/standard.odata/$metadata")
def metadata():
    types = "".join(f'<EntitySet Name="{n}" EntityType="StandardODATA.{n}"/>' for n in ENTITIES)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><edmx:Edmx Version="1.0" '
           'xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices>'
           f'<Schema Namespace="StandardODATA"><EntityContainer Name="StandardODATA">{types}'
           '</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>')
    return Response(xml, media_type="application/xml")


def _fmt(fmt):
    if fmt and not fmt.lower().startswith("json"):
        raise HTTPException(406, "Only $format=json is supported")


@router.get("/odata/standard.odata/{entity}")
def list_entity(request: Request, entity: str, company: str = Depends(company_param),
                top: int | None = Query(None, alias="$top", ge=0),
                skip: int = Query(0, alias="$skip", ge=0),
                filt: str | None = Query(None, alias="$filter"),
                orderby: str | None = Query(None, alias="$orderby"),
                fmt: str | None = Query(None, alias="$format"),
                inlinecount: str | None = Query(None, alias="$inlinecount")):
    # ключ вида Entity(guid'...')
    key = None
    km = re.match(r"^(.+?)\(guid'([0-9a-fA-F-]{36})'\)$", entity)
    if km:
        entity, key = km.group(1), km.group(2).lower()
    if entity not in BUILDERS:
        return odata_error(404, f"Не найден ресурс {entity}")
    try:
        _fmt(fmt)
    except HTTPException as e:
        return odata_error(e.status_code, e.detail)
    rows = BUILDERS[entity](data.model(company))
    meta = base_url(request) + f"/$metadata#{entity}"
    if key:
        for r in rows:
            if r["Ref_Key"] == key:
                return {"odata.metadata": meta + "/@Element", **r}
        return odata_error(404, "Объект не найден")
    try:
        if filt:
            conds = parse_filter(filt)
            rows = [r for r in rows if match(r, conds)]
        ob = (orderby or ("Дата" if "Дата" in (rows[0] if rows else {}) else "Description")).split(",")
        for spec in reversed(ob):
            parts = spec.strip().split()
            f = ALIASES.get(parts[0], parts[0])
            rows.sort(key=lambda r: str(r.get(f, "")), reverse=len(parts) > 1 and parts[1].lower() == "desc")
    except ValueError as e:
        return odata_error(400, f"Ошибка разбора запроса: {e}")
    total = len(rows)
    rows = rows[skip:] if top is None else rows[skip:skip + top]
    body = {"odata.metadata": meta}
    if inlinecount == "allpages":
        body["odata.count"] = str(total)
    body["value"] = rows
    return body
