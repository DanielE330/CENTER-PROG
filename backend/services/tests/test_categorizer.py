"""Категоризатор: правила gleb, модель на «хвост», спасательные поступления, режим налогообложения.
Идея и правила: проект gleb (swimshadow)."""
import glob
import os
import random

import pytest

from parser.categorize import categorize, categorize_ex
from parser.gleb_rules import GLEB_RULES, SUB_TO_CATEGORY, match_sub
from parser.statement import parse_statement
from parser.tail_model import MIN_SAMPLES, TailModel, apply_tail_model
from parser.tax_regime import guess_tax_regime

OUR = {"revenue", "other_income", "suppliers", "payroll", "rent", "taxes", "loans", "utilities", "bank_fees", "marketing", "transfers", "refunds", "other_expense"}
DS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "dataset", "statements")


# ---- соответствие подкатегорий gleb нашим категориям ----
def test_mapping_complete_and_valid():
    assert {r[0] for r in GLEB_RULES} <= set(SUB_TO_CATEGORY)
    assert set(SUB_TO_CATEGORY.values()) <= OUR
    assert len(GLEB_RULES) == 31


def test_rules_compile_and_blank_rule_ignored():
    assert match_sub("", "", "in") is None and match_sub("", "", "out") is None


# ---- правила ----
@pytest.mark.parametrize(
    "purpose,direction,cat",
    [
        ("Предоставление беспроцентного займа учредителем по договору № 168/21", "in", "transfers"),
        ("Возврат беспроцентного займа учредителю по договору № 1/2", "out", "transfers"),
        ("Взнос в уставный капитал ООО", "in", "transfers"),
        ("Выдача кредита (транш) по договору кредитной линии № КЛ-1", "in", "loans"),
        ("Оплата по счету за разработку программного обеспечения", "in", "revenue"),  # у gleb уходило в utilities
        ("Оплата по счету за право использования ПО (подписка)", "out", "utilities"),
        ("Плата за аренду POS-терминалов за февраль", "out", "bank_fees"),
        ("Возврат гарантийного удержания по договору № 119/26", "in", "revenue"),
        ("Грант на развитие субъекта МСП", "in", "other_income"),
        ("Списание по исполнительному листу ФС № 1", "out", "other_expense"),
        ("Оплата услуг по договору ГПХ, чек № 5", "out", "payroll"),
    ],
)
def test_purpose_rules(purpose, direction, cat):
    assert categorize(purpose, direction)[0] == cat


@pytest.mark.parametrize(
    "name,direction,cat",
    [
        ("Казначейство России (ФНС России)", "out", "taxes"),
        ("ПАО «Телеком-754»", "out", "utilities"),
        ("АО «Энергосбыт-12»", "out", "utilities"),
    ],
)
def test_name_rules_when_purpose_empty(name, direction, cat):
    c, conf, method = categorize_ex("", direction, name)
    assert (c, method) == (cat, "rule_gleb") and 0.2 < conf < 0.9
    assert categorize("", direction)[0] in ("other_income", "other_expense")  # без имени — запасной вариант


def test_fallback_and_methods():
    assert categorize_ex("что-то непонятное", "in", "") == ("other_income", 0.2, "fallback")
    assert categorize_ex("Аренда офиса", "out", "")[2] == "rule"
    assert categorize("Аренда", "out")[1] > 0.5


# ---- модель на «хвост» ----
def _tx(purpose, name, direction, cat, amount=100000, method="rule"):
    return {"purpose": purpose, "counterparty": name, "direction": direction, "amount_kop": str(amount if direction == "in" else -amount),
            "category": cat, "category_method": method, "category_confidence": 0.9}


def _train_set(n=150):
    rnd = random.Random(0)
    rows = []
    for i in range(n):
        rows.append(_tx(f"Оплата по счету {i} за молоко", f"ООО «Молочный комбинат-{i % 3}»", "out", "suppliers"))
        rows.append(_tx(f"Аренда за месяц {i}", f"ИП Арендодатель-{i % 2}", "out", "rent"))
        rows.append(_tx(f"Оплата по УПД {i}", f"ООО «Клиент-{rnd.randint(1, 9)}»", "in", "revenue"))
        rows.append(_tx(f"Заём учредителя {i}", "Учредитель", "in", "transfers"))
    return rows


def test_tail_model_learns_counterparty_from_name_only():
    rows = _train_set()
    tail = [_tx("", "ООО «Молочный комбинат-1»", "out", "other_expense", method="fallback"),
            _tx("", "ИП Арендодатель-0", "out", "other_expense", method="fallback"),
            _tx("", "ООО «Клиент-4»", "in", "other_income", method="fallback")]
    assert apply_tail_model(rows + tail) == 3
    assert [t["category"] for t in tail] == ["suppliers", "rent", "revenue"]
    assert all(t["category_method"] == "ml" and 0.2 < t["category_confidence"] <= 0.7 for t in tail)


def test_tail_model_stays_quiet_when_unsure_or_small():
    rows = _train_set()
    unknown = [_tx("", "Совершенно Неизвестный Субъект", "out", "other_expense", method="fallback")]
    n = apply_tail_model(rows + unknown)
    assert n in (0, 1)  # допустимо угадать, но только с уверенностью >= порога
    if n:
        assert unknown[0]["category_confidence"] >= 0.6
    few = _train_set(10)[: MIN_SAMPLES // 4]
    t = [_tx("", "ООО «Молочный комбинат-1»", "out", "other_expense", method="fallback")]
    assert apply_tail_model(few + t) == 0 and t[0]["category_method"] == "fallback"


def test_tail_model_corrections_and_one_class():
    assert not TailModel().fit([_tx("a", "b", "out", "rent")] * 500).ready  # один класс — модель не строится
    rows = _train_set()
    corr = [_tx("", "ООО «Новый Партнёр»", "out", "marketing") for _ in range(4)]
    m = TailModel().fit(rows, corr)
    assert m.predict([_tx("", "ООО «Новый Партнёр»", "out", "x")])[0][0] == "marketing"


def test_tail_only_changes_fallback_rows():
    rows = _train_set()
    before = [dict(r) for r in rows]
    apply_tail_model(rows)
    assert rows == before


# ---- спасательные поступления ----
CSV_HDR = "date;amount;direction;counterparty;purpose;balance\n"


def test_rescue_inflow_flag():
    csv_ = (
        CSV_HDR
        + "2024-10-01;300;расход;ООО А;Оплата по счету 1 за товар;-200\n"
        + "2024-10-02;500;приход;Учредитель;Предоставление беспроцентного займа учредителем по договору 1;300\n"
        + "2024-10-03;100;приход;Учредитель;Предоставление беспроцентного займа учредителем по договору 2;5000\n"
        + "2024-10-04;50;приход;ООО Б;Оплата по счету 2;-100\n"
    )
    r = parse_statement("a.csv", csv_.encode())
    t = r["transactions"]
    assert [x["is_rescue"] for x in t] == [False, True, False, False]
    assert r["stats"]["rescue_inflows"] == 1


def test_rescue_off_without_balance():
    r = parse_statement("a.csv", (CSV_HDR.replace(";balance", "") + "2024-10-02;500;приход;Уч;Предоставление займа учредителем;\n").encode())
    assert r["transactions"][0]["is_rescue"] is False and r["stats"]["has_balance"] is False


# ---- режим налогообложения ----
def _month_txs(vat: bool, peaks: bool, tax_share=0.03, months=range(1, 13), year=2025):
    out = []
    for m in months:
        purpose = "Оплата по счету, в т.ч. НДС 20% 1000" if vat else "Оплата по счету. НДС не облагается"
        out.append({"date": f"{year}-{m:02d}-10", "amount_kop": "100000000", "direction": "in", "category": "revenue", "purpose": purpose})
        tax = int(100000000 * tax_share)
        if peaks and m in (4, 7, 10):
            tax *= 6
        out.append({"date": f"{year}-{m:02d}-28", "amount_kop": str(-tax), "direction": "out", "category": "taxes", "purpose": "ЕНП"})
    return out


def test_tax_regime_osno_by_vat():
    g = guess_tax_regime(_month_txs(vat=True, peaks=False))
    assert g["regime"] == "osno" and g["label"] == "ОСНО" and g["confidence"] >= 0.7


def test_tax_regime_usn_income_vs_income_expense():
    low = guess_tax_regime(_month_txs(vat=False, peaks=True, tax_share=0.01))
    assert low["regime"] == "usn_income" and "доходы" in low["label"]
    high = guess_tax_regime(_month_txs(vat=False, peaks=True, tax_share=0.06))
    assert high["regime"] == "usn_income_expense"
    assert set(low["estimate"]) >= {"monthly_kop", "peak_extra_kop", "tax_share_of_revenue", "months_observed"}


def test_tax_regime_needs_history():
    assert guess_tax_regime(_month_txs(False, True, months=range(1, 5))) is None
    assert guess_tax_regime([]) is None


def test_tax_regime_in_import_preview_stats_only_additive():
    lines = ["date;amount;direction;purpose"]
    for t in _month_txs(vat=True, peaks=False):
        lines.append(f"{t['date']};{abs(int(t['amount_kop'])) / 100:.2f};{'приход' if t['direction'] == 'in' else 'расход'};{t['purpose']}")
    r = parse_statement("a.csv", "\n".join(lines).encode())
    g = r["stats"]["tax_regime_guess"]
    assert g["regime"] == "osno"
    short = parse_statement("a.csv", b"date;amount\n2024-01-01;1\n")
    assert short["stats"]["tax_regime_guess"] is None
    # прежние поля на месте
    assert {"rows_ok", "rows_rejected", "has_balance", "balance_mode", "opening_balance_kop"} <= set(r["stats"])


# ---- покрытие на датасете ----
@pytest.mark.skipif(not glob.glob(os.path.join(DS, "*.csv")), reason="data/dataset отсутствует")
def test_dataset_rule_coverage_and_ml_tail():
    files = sorted(glob.glob(os.path.join(DS, "*.csv")))[::10]
    tot = fb = rules = ml = 0
    for f in files:
        with open(f, "rb") as fh:
            r = parse_statement(os.path.basename(f), fh.read())
        for t in r["transactions"]:
            tot += 1
            m = t["category_method"]
            rules += m in ("rule", "rule_gleb")
            ml += m == "ml"
            fb += m == "fallback"
            assert t["category"] in OUR
    assert rules / tot >= 0.995
    assert fb / tot <= 0.002
