import io
from datetime import date

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import parser.app as parser_app
from parser.categorize import categorize
from parser.statement import RowError, parse_date, parse_money, parse_statement

client = TestClient(parser_app.app)


def csv_bytes(text, enc="utf-8"):
    return text.encode(enc)


# ---- money ----
@pytest.mark.parametrize(
    "raw,kop",
    [
        ("1 234,56", 123456),
        ("1 234,56", 123456),
        ("1234.56", 123456),
        ("1,234.56", 123456),
        ("-5", -500),
        ("0,1", 10),
        (12.5, 1250),
        ("-1234.5", -123450),
    ],
)
def test_money_ok(raw, kop):
    assert parse_money(raw) == kop


@pytest.mark.parametrize("raw,code", [("abc", "bad_amount"), ("NaN", "bad_amount"), ("Infinity", "bad_amount"), ("", "empty_amount"), (None, "empty_amount")])
def test_money_errors(raw, code):
    with pytest.raises(RowError) as e:
        parse_money(raw)
    assert e.value.code == code


def test_no_float_artifacts():
    assert parse_money("0.10") + parse_money("0.20") == 30
    assert parse_money("0.30") == 30
    assert parse_money("1.15") == 115
    assert parse_money("19.99") == 1999


# ---- dates ----
def test_dates():
    assert parse_date("2024-03-05") == date(2024, 3, 5)
    assert parse_date("05.03.2024") == date(2024, 3, 5)
    assert parse_date("2024-03-05 10:11:12") == date(2024, 3, 5)
    with pytest.raises(RowError) as e:
        parse_date("31.02.2024")
    assert e.value.code == "bad_date"


# ---- CSV ----
def test_csv_semicolon_cp1251():
    text = "Дата;Сумма;Направление;Контрагент;ИНН;Назначение платежа\n05.03.2024;1 234,56;приход;ООО Ромашка;7701234567;Оплата по счету 1\n"
    r = parse_statement("a.csv", csv_bytes(text, "cp1251"))
    assert r["errors"] == []
    t = r["transactions"][0]
    assert t["amount_kop"] == "123456"
    assert t["direction"] == "in"
    assert t["date"] == "2024-03-05"
    assert t["counterparty"] == "ООО Ромашка"
    assert t["inn"] == "7701234567"
    assert r["stats"]["rows_ok"] == 1


def test_csv_utf8_bom_comma():
    text = "date,amount,purpose\n2024-01-01,100.50,x\n"
    r = parse_statement("a.csv", b"\xef\xbb\xbf" + text.encode())
    assert r["transactions"][0]["amount_kop"] == "10050"


def test_debit_credit_columns():
    text = "Дата;Списание;Поступление\n01.01.2024;100,00;\n02.01.2024;;50,25\n03.01.2024;1;2\n"
    r = parse_statement("a.csv", csv_bytes(text))
    assert [t["amount_kop"] for t in r["transactions"]] == ["-10000", "5025"]
    assert [t["direction"] for t in r["transactions"]] == ["out", "in"]
    assert r["errors"] == [{"row": 4, "code": "both_debit_credit"}]


def test_signed_amount_no_direction():
    text = "Дата;Сумма\n01.01.2024;-100,10\n02.01.2024;200\n"
    r = parse_statement("a.csv", csv_bytes(text))
    assert [t["amount_kop"] for t in r["transactions"]] == ["-10010", "20000"]
    assert [t["direction"] for t in r["transactions"]] == ["out", "in"]


def test_direction_words():
    text = "date;amount;direction\n2024-01-01;100;расход\n2024-01-02;-100;приход\n2024-01-03;5;out\n2024-01-04;5;+\n2024-01-05;5;wat\n2024-01-06;5;\n"
    r = parse_statement("a.csv", csv_bytes(text))
    assert [t["amount_kop"] for t in r["transactions"]] == ["-10000", "10000", "-500", "500"]
    assert r["errors"] == [{"row": 6, "code": "bad_direction"}, {"row": 7, "code": "bad_direction"}]


def test_errors_do_not_echo_content():
    secret = "SECRET-PURPOSE-123"
    text = f"Дата;Сумма;Назначение\nbaddate;10;{secret}\n01.01.2024;garbage;{secret}\n01.01.2024;0;{secret}\n01.01.2024;;{secret}\n"
    r = parse_statement("a.csv", csv_bytes(text))
    assert [e["code"] for e in r["errors"]] == ["bad_date", "bad_amount", "zero_amount", "empty_amount"]
    assert all(set(e) == {"row", "code"} for e in r["errors"])
    assert secret not in str(r["errors"])
    assert r["stats"]["rows_rejected"] == 4 and r["stats"]["rows_ok"] == 0


def test_blank_rows_skipped():
    r = parse_statement("a.csv", csv_bytes("date;amount\n;\n\n01.01.2024;1\n"))
    assert r["stats"]["rows_ok"] == 1 and r["errors"] == []


def test_missing_columns():
    with pytest.raises(ValueError, match="no_date_column"):
        parse_statement("a.csv", csv_bytes("foo;сумма\n1;2\n"))
    with pytest.raises(ValueError, match="no_amount_column"):
        parse_statement("a.csv", csv_bytes("дата;foo\n1;2\n"))
    with pytest.raises(ValueError, match="empty_file"):
        parse_statement("a.csv", b"")
    with pytest.raises(ValueError, match="unsupported_type"):
        parse_statement("a.pdf", b"x")


def test_balance_mismatch_stats():
    ok = "date;amount;balance\n2024-01-01;100;1000\n2024-01-02;-50;950\n2024-01-03;10;960\n"
    r = parse_statement("a.csv", csv_bytes(ok))
    assert r["stats"]["balance_checked"] == 2 and r["stats"]["balance_mismatches"] == 0
    assert r["stats"]["has_balance"] is True
    bad = "date;amount;balance\n2024-01-01;100;1000\n2024-01-02;-50;900\n2024-01-03;10;910\n"
    r = parse_statement("a.csv", csv_bytes(bad))
    assert r["stats"]["balance_checked"] == 2 and r["stats"]["balance_mismatches"] == 1


def test_stats_dates():
    r = parse_statement("a.csv", csv_bytes("date;amount\n2024-01-10;1\n2024-01-01;1\n"))
    s = r["stats"]
    assert (s["date_from"], s["date_to"], s["days_span"]) == ("2024-01-01", "2024-01-10", 10)
    assert s["has_balance"] is False


# ---- categorization ----
@pytest.mark.parametrize(
    "purpose,direction,cat",
    [
        ("Выплата заработной платы за март", "out", "payroll"),
        ("Аренда офиса за апрель", "out", "rent"),
        ("Уплата ЕНП по уведомлению", "out", "taxes"),
        ("Оплата по счету 15 за товар", "out", "suppliers"),
        ("Оплата по счету 15 за товар", "in", "revenue"),
        ("что-то непонятное", "in", "other_income"),
        ("что-то непонятное", "out", "other_expense"),
        ("", "out", "other_expense"),
    ],
)
def test_categorize(purpose, direction, cat):
    assert categorize(purpose, direction)[0] == cat


def test_categorize_confidence():
    assert categorize("zzz", "in")[1] == 0.2
    assert categorize("Аренда", "out")[1] > 0.5


# ---- xlsx ----
def make_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx():
    data = make_xlsx(
        [
            ["Дата", "Сумма", "Направление", "Назначение платежа", "Остаток"],
            [date(2024, 3, 5), 1234.56, "приход", "Оплата по счету 1", 1234.56],
            ["06.03.2024", "100,10", "расход", "Аренда офиса", None],
        ]
    )
    r = parse_statement("s.xlsx", data)
    assert r["errors"] == []
    assert [t["amount_kop"] for t in r["transactions"]] == ["123456", "-10010"]
    assert r["transactions"][0]["date"] == "2024-03-05"
    assert r["transactions"][0]["category"] == "revenue"
    assert r["transactions"][1]["category"] == "rent"


# ---- HTTP ----
def test_http_ok():
    r = client.post("/internal/v1/parse", files={"file": ("a.csv", b"date;amount\n2024-01-01;1,5\n", "text/csv")})
    assert r.status_code == 200
    assert r.json()["transactions"][0]["amount_kop"] == "150"


def test_http_too_large(monkeypatch):
    monkeypatch.setattr(parser_app, "MAX_BYTES", 100)
    r = client.post("/internal/v1/parse", files={"file": ("a.csv", b"x" * 101, "text/csv")})
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "file_too_large"


def test_http_unsupported_type():
    r = client.post("/internal/v1/parse", files={"file": ("a.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unsupported_type"


def test_http_missing_column_code():
    r = client.post("/internal/v1/parse", files={"file": ("a.csv", b"foo;bar\n1;2\n", "text/csv")})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_date_column"


def test_http_corrupt_xlsx():
    r = client.post("/internal/v1/parse", files={"file": ("a.xlsx", b"notzip", "application/octet-stream")})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unreadable_file"
