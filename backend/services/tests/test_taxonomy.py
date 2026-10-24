"""Таксономия, документы-основания, «спасательные» поступления, режим налогов, ML на хвост."""
from datetime import date

import pytest

import mockdata
from parser import regime, tail, taxonomy
from parser.statement import parse_statement


def cl(purpose, amount, name="", balance=None):
    return taxonomy.classify(purpose, name, amount, balance)


def test_taxonomy_loads_and_every_subcategory_maps_to_ledger_category():
    t = taxonomy.default()
    assert len(t.groups) == 10 and len(t.subcategories) >= 28
    assert set(taxonomy.LEGACY) == set(t.subcategories)
    assert {taxonomy.LEGACY[s] for s in t.subcategories} <= {
        "revenue", "other_income", "suppliers", "payroll", "rent", "taxes", "loans", "utilities", "bank_fees", "marketing",
        "transfers", "refunds", "other_expense",
    }


@pytest.mark.parametrize(
    "purpose,amount,sub,group",
    [
        ("Единый налоговый платеж", -180_000_00, "enp", "taxes"),
        ("ЕНП", -50_000_00, "enp", "taxes"),
        ("Заработная плата за октябрь", -400_000_00, "salary", "payroll"),
        ("Оплата аренды помещения", -85_000_00, "rent", "rent_utilities"),
        ("Зачисление средств по операциям эквайринга. Мерчант №1234", 45_000_00, "acquiring", "revenue"),
        ("Оплата по УПД № 279 от 19.08.2024. НДС не облагается", 170_000_00, "customer_payment", "revenue"),
        ("Оплата по счёту за товар", -70_000_00, "supplier_payment", "purchases"),
        ("Комиссия за ведение счета за сентябрь 2024 г.", -990_00, "bank_fee", "bank_fees"),
        ("Перевод собственных средств с расчетного счета в другом банке", 300_000_00, "own_transfer_in", "own_funds"),
        ("Предоставление беспроцентного займа учредителем", 500_000_00, "founder_loan_in", "own_funds"),
        ("Погашение основного долга по кредитному договору", -120_000_00, "loan_payment", "loans"),
        ("что-то совсем непонятное", 1_000_00, "other_in", "other"),
        ("что-то совсем непонятное", -1_000_00, "other_out", "other"),
    ],
)
def test_rules(purpose, amount, sub, group):
    r = cl(purpose, amount)
    assert (r["subcategory"], r["group"]) == (sub, group)
    assert r["legacy_category"] == taxonomy.LEGACY[sub]


def test_unmatched_is_marked_fallback_and_matched_is_rule():
    assert cl("что-то совсем непонятное", 5)["method"] == "fallback"
    assert cl("ЕНП", -5)["method"] == "rule"


def test_direction_separates_meaning():
    assert cl("Возврат платежа по счёту", 10_00)["subcategory"] == "refund_in"
    assert cl("Возврат платежа по счёту", -10_00)["subcategory"] == "refund_out"


def test_document_reference():
    assert taxonomy.document("Оплата по УПД № 279 от 19.08.2024. НДС не облагается") == ("279", date(2024, 8, 19))
    assert taxonomy.document("Оплата по счёту № А-15/2 от 01.02.2025") == ("А-15/2", date(2025, 2, 1))
    assert taxonomy.document("Оплата без номера") == (None, None)
    assert taxonomy.document("Оплата по УПД № 5 от 31.02.2024")[1] is None      # нереальная дата не роняет разбор
    r = cl("Оплата по УПД № 279 от 19.08.2024", 100_00)
    assert (r["doc_no"], r["doc_date"]) == ("279", "2024-08-19")


def test_rescue_only_when_own_money_kept_balance_from_going_negative():
    own = "Перевод собственных средств с расчетного счета в другом банке"
    assert cl(own, 300_000_00, balance=100_000_00)["is_rescue"] is True        # до поступления было −200 000
    assert cl(own, 300_000_00, balance=900_000_00)["is_rescue"] is False       # плановый перевод при нормальном остатке
    assert cl(own, 300_000_00, balance=None)["is_rescue"] is False             # без остатка не гадаем
    assert cl("Оплата по УПД № 1 от 01.01.2025", 300_000_00, balance=100_000_00)["is_rescue"] is False
    assert cl(own.replace("с расчетного счета", "на расчетный счет"), -300_000_00, balance=-5)["is_rescue"] is False


def test_regime_needs_history_and_detects_vat():
    assert regime.infer([]) is None
    rows = []
    for m in range(1, 13):
        rows.append({"op_date": date(2025, m, 28), "amount_kop": -100_000_00, "subcategory": "enp", "purpose": "ЕНП"})
        rows.append({"op_date": date(2025, m, 10), "amount_kop": 1_000_000_00, "subcategory": "customer_payment", "purpose": "Оплата по УПД. В том числе НДС (20%) 166 666 руб."})
    r = regime.infer(rows)
    assert r["regime"] == "osno" and r["confidence"] >= 0.9 and r["estimate"]["months_observed"] == 12


def test_regime_quarterly_peaks_mean_usn():
    rows = []
    for m in range(1, 13):
        amount = 300_000_00 if m in (4, 7, 10) else 60_000_00
        rows.append({"op_date": date(2025, m, 28), "amount_kop": -amount, "subcategory": "enp", "purpose": "ЕНП"})
        rows.append({"op_date": date(2025, m, 10), "amount_kop": 4_000_000_00, "subcategory": "acquiring", "purpose": "эквайринг"})
    assert regime.infer(rows)["regime"].startswith("usn")


def test_tail_model_learns_per_tenant_and_declines_when_data_is_small():
    texts = [tail.features(f"оплата за слесарные работы {i}", "ИП Иванов", -5_000_00) for i in range(150)]
    texts += [tail.features(f"услуги грузоперевозки рейс {i}", "ООО Транс", -9_000_00) for i in range(150)]
    labels = ["services"] * 150 + ["fuel_transport"] * 150
    pytest.importorskip("sklearn")
    m = tail.TfidfLogReg().fit(texts, labels)
    assert m.ready
    (a, ca), (b, cb) = m.predict([tail.features("слесарные работы 77", "ИП Иванов", -5_000_00), tail.features("грузоперевозки рейс 5", "ООО Транс", -9_000_00)])
    assert (a, b) == ("services", "fuel_transport") and min(ca, cb) > 0.5
    small = tail.TfidfLogReg().fit(texts[:10], labels[:10])
    assert not small.ready and small.predict(["x"]) == [("", 0.0)]


def test_parse_statement_adds_ml_features_and_keeps_ledger_category():
    csv = (
        "Дата;Сумма;Направление;Контрагент;ИНН;Назначение;Остаток\n"
        "2025-01-10;300000.00;Поступление;ПАО Банк;0068275202;Перевод собственных средств с расчетного счета;100000.00\n"
        "2025-01-11;180000.00;Списание;ФНС;;Единый налоговый платеж;50000.00\n"
    ).encode("utf-8")
    out = parse_statement("s.csv", csv)
    a, b = out["transactions"]
    assert (a["subcategory"], a["is_rescue"], a["category"]) == ("own_transfer_in", True, "transfers")
    assert (b["subcategory"], b["group"], b["category"]) == ("enp", "taxes", "taxes")
    assert b["doc_no"] is None and b["is_rescue"] is False


@pytest.mark.skipif(not mockdata.company_exists("C001"), reason="нет датасета")
def test_dataset_rules_cover_almost_everything_and_agree_with_legacy():
    import csv

    from parser.categorize import categorize

    path = mockdata.dataset_dir() / "statements" / "C001.csv"
    n = fallback = agree = 0
    for r in csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"):
        amount = round(float(r["amount"]) * 100) * (1 if r["direction"] == "Поступление" else -1)
        c = taxonomy.classify(r["payment_purpose"], r["counterparty_name"], amount, round(float(r["balance_eod"]) * 100))
        old, _ = categorize(r["payment_purpose"], "in" if amount > 0 else "out")
        n += 1
        fallback += c["method"] == "fallback"
        agree += old == c["legacy_category"]
    assert fallback / n < 0.02
    assert agree / n > 0.8
