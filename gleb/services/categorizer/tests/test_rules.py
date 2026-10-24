from datetime import date
from pathlib import Path

from categorizer import rules, tax

TAX = rules.load(Path(__file__).resolve().parents[3] / "rules" / "categories.yaml")


def test_typical_purposes():
    cases = [
        ("Зачисление средств по операциям эквайринга. Мерчант №1", "", 100, "acquiring"),
        ("Единый налоговый платеж", "Казначейство России (ФНС России)", -100, "enp"),
        ("Перечисление заработной платы (аванс) за май 2026 г.", "ПАО «Банк»", -100, "salary"),
        ("Оплата по УПД № 279 от 19.08.2024. НДС не облагается", "ООО «Элеватор-242»", 100, "customer_payment"),
        ("Перевод собственных средств с расчетного счета в АО «Банк-2»", "", 100, "own_transfer_in"),
        ("Комиссия за ведение счета за сентябрь 2024 г. Без НДС", "ПАО «Банк»", -990, "bank_fee"),
    ]
    for purpose, name, amount, expected in cases:
        assert rules.match(TAX, purpose, name, amount) == expected, purpose


def test_document_reference_is_extracted():
    assert rules.document("Частичная оплата по УПД № 296 от 02.09.2024. НДС не облагается.") == (
        "296",
        date(2024, 9, 2),
    )


def test_rescue_needs_own_money_and_a_hole():
    assert rules.is_rescue("own_transfer_in", 500, 100)
    assert not rules.is_rescue("own_transfer_in", 500, 900)
    assert not rules.is_rescue("customer_payment", 500, 100)


def test_vat_points_to_general_regime():
    rows = []
    for m in range(1, 13):
        d = date(2025, m, 10)
        rows.append(
            {
                "op_date": d,
                "amount_kop": 1_000_000,
                "subcategory": "customer_payment",
                "purpose": "Оплата по счету, в т.ч. НДС 20% 1000",
            }
        )
        rows.append({"op_date": date(2025, m, 28), "amount_kop": -100_000, "subcategory": "enp", "purpose": "ЕНП"})
    assert tax.infer(rows)["regime"] == "osno"


def test_quarterly_peaks_point_to_simplified_regime():
    rows = []
    for m in range(1, 13):
        rows.append({"op_date": date(2025, m, 10), "amount_kop": 1_000_000, "subcategory": "acquiring", "purpose": ""})
        amount = 300_000 if m in (4, 7, 10) else 50_000
        rows.append({"op_date": date(2025, m, 28), "amount_kop": -amount, "subcategory": "enp", "purpose": "ЕНП"})
    assert tax.infer(rows)["regime"].startswith("usn")
