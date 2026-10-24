"""Наполнение демо-базы 1С:Фреш через стандартный интерфейс OData (идемпотентно, повторный запуск дублей не создаёт).

Контрагенты — реальные компании с реальными ИНН; у части из них есть настоящая задолженность перед бюджетом
по открытым данным ФНС (набор 7707329152-debtam на 01.09.2026). Документы — демо-договоры клининговой компании:
счета покупателям (часть оплачена, часть просрочена), счета поставщиков и платёжные поручения, созданные, но ещё
не исполненные банком. Даты подогнаны под демо-выписку (последний день 29.09.2026 = MOCK_TODAY): уже заведённые
документы с другой датой или текстом обновляются и перепроводятся.

Запуск: ONEC_URL=https://msk1.1cfresh.com/a/ea/<область> ONEC_USER=... ONEC_PASSWORD=... \
        python -m integrator.seed_1c_fresh
"""
import os
import sys
from datetime import date
from urllib.parse import quote

import httpx

MARK = "CI-hack demo"
ZERO = "00000000-0000-0000-0000-000000000000"
BIK, BANK_NAME, CORR = "046015762", 'ПАО КБ "ЦЕНТР-ИНВЕСТ"', "30101810100000000762"  # справочник БИК ЦБ
CITY, ADDR = "г. Ростов-на-Дону", "пр. Соколова, 62"
SERVICE = "Комплексная уборка помещений"

# ИНН, краткое и полное наименование, роль (in — покупатель, out — поставщик), отсрочка по договору (дн)
PARTIES = [
    ("2310031475", "ТАНДЕР АО", 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ТАНДЕР"', "in", 30),
    ("7707049388", "РОСТЕЛЕКОМ ПАО", 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОСТЕЛЕКОМ"', "in", 20),
    ("6166065050", "ДОНАГРО ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ДОНАГРО"', "in", 14),
    ("6163158242", "ЮГСТРОЙ ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЮГСТРОЙ"', "in", 10),
    ("6166129320", "ЮЖНЫЙ ТОРГОВЫЙ ДОМ ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЮЖНЫЙ ТОРГОВЫЙ ДОМ"', "in", 7),
    ("6122010840", "ХИМТРЕЙД ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ХИМТРЕЙД"', "out", 14),
    ("6165235781", "ЦТСК ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЦЕНТРАЛЬНАЯ ТОРГОВО-СТРОИТЕЛЬНАЯ КОМПАНИЯ"', "out", 7),
    ("7736207543", "ЯНДЕКС ООО", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЯНДЕКС"', "out", 5),
]

# номер, ИНН, дата счёта, сумма (руб), что оплачено: [(дата, сумма)], содержание
INVOICES_IN = [
    ("BS-0901", "2310031475", "2026-08-07", 620_000, [("2026-09-06", 620_000)], "Уборка гипермаркетов, август"),
    ("BS-1001", "2310031475", "2026-09-06", 620_000, [], "Уборка гипермаркетов, сентябрь"),
    ("BS-1020", "2310031475", "2026-09-25", 120_000, [], "Генеральная уборка после ремонта"),
    ("BS-0910", "7707049388", "2026-08-16", 290_000, [("2026-09-03", 290_000)], "Уборка офисов, август"),
    ("BS-1010", "7707049388", "2026-09-15", 290_000, [], "Уборка офисов, сентябрь"),
    ("BS-0905", "6166065050", "2026-08-11", 450_000, [], "Уборка производственных помещений, август"),
    ("BS-1005", "6166065050", "2026-09-10", 450_000, [], "Уборка производственных помещений, сентябрь"),
    ("BS-1015", "6163158242", "2026-09-20", 380_000, [], "Уборка после строительства, ЖК"),
    ("BS-1012", "6166129320", "2026-09-17", 160_000, [("2026-09-25", 100_000)], "Уборка торгового зала"),
]
# черновики (не проведены): в прогноз не попадают
DRAFTS = [("BS-1030", "6163158242", "2026-09-28", 900_000, "Черновик: уборка нового ЖК (не согласован)")]
# номер поставщика, ИНН, дата, сумма, содержание
INVOICES_OUT = [
    ("ХТ-4471", "6122010840", "2026-09-23", 85_000, "Моющие средства и химия"),
    ("ЦТ-1025", "6165235781", "2026-09-29", 150_000, "Аренда склада и офиса, октябрь"),
    ("ЯД-88120", "7736207543", "2026-09-25", 45_000, "Реклама в Яндекс Директ"),
]
# платёжки, созданные, но не исполненные: номер, ИНН получателя, дата, сумма, назначение, номер счёта поставщика
ORDERS = [
    ("101", "6165235781", "2026-09-29", 150_000, "Оплата по счёту ЦТ-1025 от 29.09.2026, аренда за октябрь. Без НДС", "ЦТ-1025"),
    ("102", "7736207543", "2026-09-29", 45_000, "Оплата по счёту ЯД-88120 от 25.09.2026, рекламные услуги. Без НДС", "ЯД-88120"),
]
CONTRACT_DATE = "2026-08-01"


def account_number(bik: str, body: str) -> str:
    """Расчётный счёт с правильным контрольным ключом (9-я цифра) по БИК."""
    raw = bik[-3:] + body[:8] + "0" + body[9:]
    weights = [7, 1, 3] * 8
    key = sum(int(c) * w % 10 for c, w in zip(raw, weights)) % 10 * 3 % 10
    return body[:8] + str(key) + body[9:]


class OData:
    def __init__(self, url: str, user: str, password: str):
        self.base = url.rstrip("/") + "/odata/standard.odata/"
        self.http = httpx.Client(auth=(user, password), timeout=120)

    def _url(self, path: str, params: dict) -> str:
        qs = "&".join(f"{k}={quote(str(v), safe=',()/')}" for k, v in {"$format": "json", **params}.items())
        return f"{self.base}{quote(path, safe='()/,')}?{qs}"

    def get(self, path: str, **params) -> list[dict]:
        r = self.http.get(self._url(path, params))
        if r.status_code >= 400:
            raise RuntimeError(f"GET {path}: {r.status_code} {r.text[:500]}")
        return r.json().get("value", [])

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.http.post(self._url(path, {}), json=body or {})
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path}: {r.status_code} {r.text[:800]}")
        return r.json() if r.content else {}

    def patch(self, path: str, body: dict) -> None:
        r = self.http.patch(self._url(path, {}), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"PATCH {path}: {r.status_code} {r.text[:500]}")

    def one(self, entity: str, flt: str) -> dict | None:
        rows = self.get(entity, **{"$filter": flt, "$top": 1})
        return rows[0] if rows else None

    def ensure(self, entity: str, flt: str, body: dict) -> str:
        found = self.one(entity, flt)
        if found:
            return found["Ref_Key"]
        return self.post(entity, body)["Ref_Key"]

    def post_doc(self, entity: str, key: str) -> None:
        self.post(f"{entity}(guid'{key}')/Post")

    def align(self, entity: str, found: dict, want: dict, repost: bool = True) -> bool:
        """Уже заведённый документ: если дата или тексты отличаются от нужных — исправить и перепровести."""
        diff = {k: v for k, v in want.items() if str(found.get(k) or "").strip() != str(v).strip()}
        if not diff:
            return False
        self.patch(f"{entity}(guid'{found['Ref_Key']}')", diff)
        if repost:
            self.post_doc(entity, found["Ref_Key"])
        return True


def iso(d: str) -> str:
    return d + "T00:00:00"


def seed(od: OData, out=print) -> dict:
    org = od.get("Catalog_Организации", **{"$top": 1})[0]
    org_key = org["Ref_Key"]
    rub = od.one("Catalog_Валюты", "Code eq '643'")["Ref_Key"]
    unit = od.one("Catalog_КлассификаторЕдиницИзмерения", "Code eq '796'")["Ref_Key"]
    kind = od.one("Catalog_ВидыНоменклатуры", "Description eq 'Услуги'")["Ref_Key"]

    bank = od.ensure("Catalog_Банки", f"Code eq '{BIK}'",
                     {"Code": BIK, "Description": BANK_NAME, "КоррСчет": CORR, "Город": CITY, "Адрес": ADDR})
    acc_no = account_number(BIK, "40702810052090010001")
    acc = od.ensure("Catalog_БанковскиеСчета", f"НомерСчета eq '{acc_no}'", {
        "Description": f"{acc_no}, {BANK_NAME}", "НомерСчета": acc_no, "Банк_Key": bank, "ВидСчета": "Расчетный",
        "Owner": org_key, "Owner_Type": "StandardODATA.Catalog_Организации", "ВалютаДенежныхСредств_Key": rub})
    item = od.ensure("Catalog_Номенклатура", f"Description eq '{SERVICE}'", {
        "Description": SERVICE, "НаименованиеПолное": SERVICE, "Услуга": True, "ВидНоменклатуры_Key": kind,
        "ЕдиницаИзмерения_Key": unit, "Комментарий": MARK})
    out(f"организация {org['Description']} (ИНН {org['ИНН']}), счёт {acc_no} в {BANK_NAME}")

    parties, contracts = {}, {}
    for inn, short, full, role, days in PARTIES:
        cp = od.ensure("Catalog_Контрагенты", f"ИНН eq '{inn}'", {
            "Description": short, "НаименованиеПолное": full, "ИНН": inn,
            "ЮридическоеФизическоеЛицо": "ЮридическоеЛицо", "Комментарий": MARK})
        number = f"Д-{inn[-4:]}"
        contracts[inn] = od.ensure("Catalog_ДоговорыКонтрагентов", f"Owner_Key eq guid'{cp}' and Номер eq '{number}'", {
            "Description": f"Договор № {number} от {date.fromisoformat(CONTRACT_DATE):%d.%m.%Y}", "Owner_Key": cp, "Организация_Key": org_key,
            "ВидДоговора": "СПокупателем" if role == "in" else "СПоставщиком", "Номер": number,
            "Дата": iso(CONTRACT_DATE), "ВалютаВзаиморасчетов_Key": rub, "УстановленСрокОплаты": True,
            "СрокОплаты": days, "ВидДнейСрокаОплаты": "Календарные", "Комментарий": MARK})
        parties[inn] = cp
        c = od.one("Catalog_ДоговорыКонтрагентов", f"Ref_Key eq guid'{contracts[inn]}'")
        od.align("Catalog_ДоговорыКонтрагентов", c, {
            "Дата": iso(CONTRACT_DATE), "Description": f"Договор № {number} от {date.fromisoformat(CONTRACT_DATE):%d.%m.%Y}"},
            repost=False)
    out(f"контрагентов и договоров: {len(parties)}")

    made = {"invoices_in": 0, "invoices_out": 0, "payments": 0, "orders": 0, "aligned": 0}
    invoice_keys = {}
    for number, inn, d, amount, paid, text in INVOICES_IN:
        found = od.one("Document_СчетНаОплатуПокупателю", f"Number eq '{number}'")
        if found:
            key = found["Ref_Key"]
            made["aligned"] += od.align("Document_СчетНаОплатуПокупателю", found, {"Date": iso(d), "Комментарий": text})
        else:
            key = od.post("Document_СчетНаОплатуПокупателю", {
                "Number": number, "Date": iso(d), "Организация_Key": org_key, "Контрагент_Key": parties[inn],
                "ДоговорКонтрагента_Key": contracts[inn], "ВалютаДокумента_Key": rub, "КратностьВзаиморасчетов": "1",
                "КурсВзаиморасчетов": 1, "СуммаВключаетНДС": True, "ДокументБезНДС": True, "СуммаДокумента": amount,
                "СтруктурнаяЕдиница_Key": acc, "Комментарий": text,
                "Товары": [{"LineNumber": "1", "Номенклатура_Key": item, "Содержание": text, "Количество": 1,
                            "Цена": amount, "Сумма": amount, "СтавкаНДС": "БезНДС", "СуммаНДС": 0}]})["Ref_Key"]
            od.post_doc("Document_СчетНаОплатуПокупателю", key)
            made["invoices_in"] += 1
        invoice_keys[number] = key
        for i, (pd, pamount) in enumerate(paid, 1):
            pnum = f"{number}-P{i}"
            found = od.one("Document_ПоступлениеНаРасчетныйСчет", f"НомерВходящегоДокумента eq '{pnum}'")
            if found:
                made["aligned"] += od.align("Document_ПоступлениеНаРасчетныйСчет", found, {
                    "Date": iso(pd), "ДатаВходящегоДокумента": iso(pd),
                    "НазначениеПлатежа": f"Оплата по счёту {number} от {date.fromisoformat(d):%d.%m.%Y}. Без НДС"})
                continue
            pkey = od.post("Document_ПоступлениеНаРасчетныйСчет", {
                "Date": iso(pd), "Организация_Key": org_key, "ВидОперации": "ОплатаПокупателя",
                "СчетОрганизации_Key": acc, "Контрагент": parties[inn], "Контрагент_Type": "StandardODATA.Catalog_Контрагенты",
                "ДоговорКонтрагента_Key": contracts[inn], "ВалютаДокумента_Key": rub, "СуммаДокумента": pamount,
                "НомерВходящегоДокумента": pnum, "ДатаВходящегоДокумента": iso(pd),
                "НазначениеПлатежа": f"Оплата по счёту {number} от {date.fromisoformat(d):%d.%m.%Y}. Без НДС",
                "Комментарий": MARK,
                "РасшифровкаПлатежа": [{"LineNumber": "1", "ДоговорКонтрагента_Key": contracts[inn],
                                        "СпособПогашенияЗадолженности": "Автоматически", "СуммаПлатежа": pamount,
                                        "СуммаВзаиморасчетов": pamount, "КурсВзаиморасчетов": 1,
                                        "КратностьВзаиморасчетов": "1", "СтавкаНДС": "БезНДС", "СуммаНДС": 0,
                                        "СчетНаОплату_Key": key}]})["Ref_Key"]
            od.post_doc("Document_ПоступлениеНаРасчетныйСчет", pkey)
            made["payments"] += 1

    for number, inn, d, amount, text in DRAFTS:
        found = od.one("Document_СчетНаОплатуПокупателю", f"Number eq '{number}'")
        if found:
            made["aligned"] += od.align("Document_СчетНаОплатуПокупателю", found, {"Date": iso(d), "Комментарий": text}, repost=False)
            continue
        od.post("Document_СчетНаОплатуПокупателю", {
            "Number": number, "Date": iso(d), "Организация_Key": org_key, "Контрагент_Key": parties[inn],
            "ДоговорКонтрагента_Key": contracts[inn], "ВалютаДокумента_Key": rub, "КратностьВзаиморасчетов": "1",
            "КурсВзаиморасчетов": 1, "СуммаВключаетНДС": True, "ДокументБезНДС": True, "СуммаДокумента": amount,
            "СтруктурнаяЕдиница_Key": acc, "Комментарий": text,
            "Товары": [{"LineNumber": "1", "Номенклатура_Key": item, "Содержание": text, "Количество": 1,
                        "Цена": amount, "Сумма": amount, "СтавкаНДС": "БезНДС", "СуммаНДС": 0}]})
        made["drafts"] = made.get("drafts", 0) + 1

    supplier_keys = {}
    for number, inn, d, amount, text in INVOICES_OUT:
        found = od.one("Document_СчетНаОплатуПоставщика", f"НомерВходящегоДокумента eq '{number}'")
        if found:
            supplier_keys[number] = found["Ref_Key"]
            made["aligned"] += od.align("Document_СчетНаОплатуПоставщика", found, {
                "Date": iso(d), "ДатаВходящегоДокумента": iso(d), "Комментарий": text})
            continue
        key = od.post("Document_СчетНаОплатуПоставщика", {
            "Date": iso(d), "Организация_Key": org_key, "Контрагент_Key": parties[inn],
            "ДоговорКонтрагента_Key": contracts[inn], "ВалютаДокумента_Key": rub, "КратностьВзаиморасчетов": "1",
            "КурсВзаиморасчетов": 1, "СуммаВключаетНДС": True, "СуммаДокумента": amount,
            "НомерВходящегоДокумента": number, "ДатаВходящегоДокумента": iso(d), "Комментарий": text,
            "Товары": [{"LineNumber": "1", "Номенклатура_Key": item, "Количество": 1, "Цена": amount,
                        "Сумма": amount, "СтавкаНДС": "БезНДС", "СуммаНДС": 0}]})["Ref_Key"]
        od.post_doc("Document_СчетНаОплатуПоставщика", key)
        supplier_keys[number] = key
        made["invoices_out"] += 1

    for number, inn, d, amount, purpose, inv in ORDERS:
        found = od.one("Document_ПлатежноеПоручение", f"Number eq '{number}'")
        if found:
            made["aligned"] += od.align("Document_ПлатежноеПоручение", found, {"Date": iso(d), "НазначениеПлатежа": purpose})
            continue
        key = od.post("Document_ПлатежноеПоручение", {
            "Number": number, "Date": iso(d), "Организация_Key": org_key, "ВидОперации": "ОплатаПоставщику",
            "СчетОрганизации_Key": acc, "Контрагент": parties[inn], "Контрагент_Type": "StandardODATA.Catalog_Контрагенты",
            "ДоговорКонтрагента_Key": contracts[inn], "СуммаДокумента": amount, "СтавкаНДС": "БезНДС", "СуммаНДС": 0,
            "ВидПлатежа": "Электронно", "ОчередностьПлатежа": 5, "НазначениеПлатежа": purpose,
            "ВалютаДокумента_Key": rub, "ИННПлательщика": org["ИНН"], "КПППлательщика": org.get("КПП") or "",
            "ИННПолучателя": inn, "ДокументОснование": supplier_keys.get(inv, ZERO),
            "ДокументОснование_Type": "StandardODATA.Document_СчетНаОплатуПоставщика", "Комментарий": MARK})["Ref_Key"]
        od.post_doc("Document_ПлатежноеПоручение", key)
        made["orders"] += 1
    cleaned = 0
    for entity in ("Document_СчетНаОплатуПокупателю", "Document_СчетНаОплатуПоставщика"):
        for d in od.get(entity, **{"$select": "Ref_Key,Комментарий"}):
            text = d.get("Комментарий") or ""
            if text.startswith(f"{MARK}. "):
                od.patch(f"{entity}(guid'{d['Ref_Key']}')", {"Комментарий": text[len(MARK) + 2:]})
                cleaned += 1
    made["cleaned"] = cleaned
    out(f"создано: {made}")
    return made


def main() -> int:
    try:
        od = OData(os.environ["ONEC_URL"], os.environ["ONEC_USER"], os.environ["ONEC_PASSWORD"])
    except KeyError as e:
        print(f"не задана переменная {e}", file=sys.stderr)
        return 2
    seed(od)
    return 0


if __name__ == "__main__":
    sys.exit(main())
