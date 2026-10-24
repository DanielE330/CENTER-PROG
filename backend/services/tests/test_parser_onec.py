"""Формат 1С 1CClientBankExchange. Идея и правила: проект gleb (swimshadow)."""
import os

import pytest
from fastapi.testclient import TestClient

import parser.app as parser_app
from parser.statement import decode_1c, is_1c, parse_statement

FX = os.path.join(os.path.dirname(__file__), "fixtures", "onec")
client = TestClient(parser_app.app)


def fx(name):
    with open(os.path.join(FX, name), "rb") as f:
        return f.read()


def doc(num="1", date="01.10.2024", amount="100.00", payer="40702810000000000001", recv="40702810999999999999", purpose="Оплата", extra=""):
    return (
        f"СекцияДокумент=Платежное поручение\nНомер={num}\nДата={date}\nСумма={amount}\n"
        f"ПлательщикСчет={payer}\nПолучательСчет={recv}\nНазначениеПлатежа={purpose}\n{extra}КонецДокумента\n"
    )


HEAD = "1CClientBankExchange\nВерсияФормата=1.03\nРасчСчет=40702810000000000001\nСекцияРасчСчет\nРасчСчет=40702810000000000001\nНачальныйОстаток=1000.00\n{close}КонецРасчСчет\n"


def file_(*docs, close="КонечныйОстаток=1000.00\n", end="КонецФайла\n"):
    return (HEAD.format(close=close) + "".join(docs) + end).encode("utf-8")


# ---- фикстуры ----
@pytest.mark.parametrize("name,fname", [("statement_cp1251.txt", "a.txt"), ("statement_utf8.1c", "a.1c"), ("statement_utf8.1c", "a.txt")])
def test_fixture_parses_same_everywhere(name, fname):
    r = parse_statement(fname, fx(name))
    assert r["errors"] == []
    t = r["transactions"]
    assert [x["amount_kop"] for x in t] == ["-2600000", "-80000", "17089206"]
    assert [x["direction"] for x in t] == ["out", "out", "in"]
    assert [x["date"] for x in t] == ["2024-10-01", "2024-10-01", "2024-10-02"]
    assert [x["category"] for x in t] == ["rent", "bank_fees", "revenue"]
    # контрагент и ИНН берутся с «чужой» стороны
    assert t[0]["counterparty"] == "ООО Арендодатель-925" and t[0]["inn"] == "0098524470"
    assert t[1]["counterparty"] == "ПАО «Банк»" and t[1]["inn"] == "0068275202"  # из «ИНН … имя» в поле без «1»
    assert t[2]["counterparty"] == "ООО «Элеватор-242»" and t[2]["inn"] == "0048401219"
    assert t[0]["source_id"] == "17@2024-10-01"
    s = r["stats"]
    assert s["source_format"] == "1c" and s["file_complete"] is True
    assert s["opening_balance_kop"] == "100000000" and s["closing_balance_kop"] == "114409206"
    assert s["closing_balance_ok"] is True and s["balance_mismatches"] == 0
    assert s["balance_mode"] == "eod"
    assert [x["balance_kop"] for x in t] == ["97320000", "97320000", "114409206"]
    assert (s["date_from"], s["date_to"], s["rows_ok"]) == ("2024-10-01", "2024-10-02", 3)


def test_same_json_shape_as_csv():
    csv_r = parse_statement("a.csv", "date;amount;balance\n2024-01-01;100;1100\n".encode())
    one_r = parse_statement("a.txt", fx("statement_cp1251.txt"))
    assert set(csv_r) == set(one_r)
    assert set(csv_r["transactions"][0]) <= set(one_r["transactions"][0]) | {"source_id"}
    assert set(csv_r["stats"]) <= set(one_r["stats"])


def test_cp866_dos_encoding():
    text = file_(doc(purpose="Аренда офиса"), close="КонечныйОстаток=900.00\n").decode().replace("ВерсияФормата=1.03", "ВерсияФормата=1.03\nКодировка=DOS")
    r = parse_statement("a.txt", text.encode("cp866"))
    assert r["errors"] == [] and r["transactions"][0]["purpose"] == "Аренда офиса"
    assert r["transactions"][0]["category"] == "rent"


def test_detect_and_decode_helpers():
    assert is_1c(b"\xef\xbb\xbf\n1CClientBankExchange\n") and not is_1c(b"date;amount")
    assert decode_1c("Привет".encode("cp1251")) == "Привет"
    assert decode_1c(b"\xef\xbb\xbf" + "Привет".encode()) == "Привет"


def test_txt_with_csv_content_still_csv():
    r = parse_statement("a.txt", "date;amount\n2024-01-01;1\n".encode())
    assert r["transactions"][0]["amount_kop"] == "100" and "source_format" not in r["stats"]


# ---- направление ----
def test_direction_by_our_account():
    r = parse_statement("a.1c", file_(doc("1", amount="10.00"), doc("2", amount="5.00", payer="40702810999999999999", recv="40702810000000000001")))
    assert [t["amount_kop"] for t in r["transactions"]] == ["-1000", "500"]


def test_account_with_spaces_and_alt_field_names():
    d = "СекцияДокумент=Платежное поручение\nДата=01.10.2024\nСумма=1,50\nПлательщикРасчСчет=4070 2810 0000 0000 0001\nПолучательРасчСчет=1\nНазначениеПлатежа=x\nКонецДокумента\n"
    r = parse_statement("a.1c", file_(d))
    assert r["transactions"][0]["amount_kop"] == "-150"


def test_date_written_off_vs_received():
    d_out = doc(extra="ДатаСписано=03.10.2024\n")
    d_in = doc("2", payer="40702810999999999999", recv="40702810000000000001", extra="ДатаПоступило=04.10.2024\n")
    r = parse_statement("a.1c", file_(d_out, d_in))
    assert [t["date"] for t in r["transactions"]] == ["2024-10-03", "2024-10-04"]


def test_amount_in_kopecks_no_float():
    r = parse_statement("a.1c", file_(doc(amount="0.10"), doc("2", amount="0.20"), doc("3", amount="1 234,56")))
    assert [t["amount_kop"] for t in r["transactions"]] == ["-10", "-20", "-123456"]


# ---- остатки ----
def test_closing_balance_mismatch_reported():
    r = parse_statement("a.1c", file_(doc(amount="100.00"), close="КонечныйОстаток=999.00\n"))
    s = r["stats"]
    assert s["closing_balance_ok"] is False and s["balance_mismatches"] == 1 and s["balance_checked"] >= 1
    assert r["transactions"][0]["balance_kop"] == "90000"


def test_no_opening_balance_means_no_balances():
    raw = "1CClientBankExchange\nРасчСчет=40702810000000000001\n" + doc()
    r = parse_statement("a.1c", raw.encode())
    assert r["stats"]["has_balance"] is False and r["transactions"][0]["balance_kop"] is None
    assert r["stats"]["closing_balance_ok"] is None


# ---- дедупликация ----
def test_duplicate_document_dropped_and_reported():
    r = parse_statement("a.1c", file_(doc("7"), doc("7"), doc("7", amount="101.00"), doc("8")))
    assert len(r["transactions"]) == 3
    assert r["errors"] == [{"row": 2, "code": "duplicate_document"}]


def test_documents_without_number_not_deduplicated_but_flagged():
    r = parse_statement("a.1c", file_(doc(""), doc("")))
    assert len(r["transactions"]) == 2 and r["stats"]["suspected_duplicates"] == 1


# ---- кривые файлы ----
def test_not_1c_with_1c_extension():
    with pytest.raises(ValueError, match="not_1c_format"):
        parse_statement("a.1c", b"date;amount\n2024-01-01;1\n")


def test_no_account():
    with pytest.raises(ValueError, match="no_account"):
        parse_statement("a.1c", ("1CClientBankExchange\n" + doc()).encode())


def test_no_documents():
    with pytest.raises(ValueError, match="empty_file"):
        parse_statement("a.1c", file_())


def test_truncated_file_mid_document():
    raw = file_(doc("1"), end="").decode() + "СекцияДокумент=Платежное поручение\nДата=02.10.2024\nСумма=5.00\nПлательщикСч"
    r = parse_statement("a.1c", raw.encode())
    assert len(r["transactions"]) == 1
    assert r["errors"] == [{"row": 2, "code": "truncated_document"}]
    assert r["stats"]["file_complete"] is False


def test_unclosed_document_followed_by_new_one():
    raw = file_(end="").decode() + "СекцияДокумент=Платежное поручение\nДата=02.10.2024\n" + doc("2")
    r = parse_statement("a.1c", raw.encode())
    assert [e["code"] for e in r["errors"]] == ["truncated_document"] and len(r["transactions"]) == 1


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"date": "31.02.2024"}, "bad_date"),
        ({"date": ""}, "bad_date"),
        ({"amount": "abc"}, "bad_amount"),
        ({"amount": "-5.00"}, "bad_amount"),
        ({"amount": ""}, "empty_amount"),
        ({"amount": "0.00"}, "zero_amount"),
        ({"payer": "1", "recv": "2"}, "foreign_account"),
        ({"payer": "40702810000000000001", "recv": "40702810000000000001"}, "ambiguous_direction"),
    ],
)
def test_bad_documents_become_row_errors(kw, code):
    secret = "SECRET-PURPOSE-123"
    r = parse_statement("a.1c", file_(doc("1", **{**kw, "purpose": secret}), doc("2")))
    assert r["errors"] == [{"row": 1, "code": code}]
    assert len(r["transactions"]) == 1 and secret not in str(r["errors"])


def test_garbage_bytes_after_header():
    with pytest.raises(ValueError):
        parse_statement("a.1c", b"1CClientBankExchange\n\xff\xfe\x00\x81garbage")


def test_invalid_inn_dropped_valid_kept():
    extra = "ПолучательИНН=123\n"
    r = parse_statement("a.1c", file_(doc(extra=extra), doc("2", extra="ПолучательИНН=7701234567\n")))
    assert [t["inn"] for t in r["transactions"]] == ["", "7701234567"]


def test_values_with_equals_sign():
    r = parse_statement("a.1c", file_(doc(purpose="Оплата по договору N=5 от 01.01.2024")))
    assert r["transactions"][0]["purpose"] == "Оплата по договору N=5 от 01.01.2024"


def test_unknown_lines_and_blank_lines_ignored():
    raw = file_(doc()).decode().replace("КонецРасчСчет\n", "КонецРасчСчет\n\nмусорная строка без равно\n")
    r = parse_statement("a.1c", raw.encode())
    assert len(r["transactions"]) == 1 and r["errors"] == []


# ---- HTTP ----
def test_http_1c_txt():
    r = client.post("/internal/v1/parse", files={"file": ("bank.txt", fx("statement_cp1251.txt"), "text/plain")})
    assert r.status_code == 200 and r.json()["stats"]["source_format"] == "1c" and len(r.json()["transactions"]) == 3


def test_http_1c_dot_1c():
    r = client.post("/internal/v1/parse", files={"file": ("bank.1c", fx("statement_utf8.1c"), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["stats"]["rows_ok"] == 3


def test_http_not_1c_code():
    r = client.post("/internal/v1/parse", files={"file": ("bank.1c", b"hello", "text/plain")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_1c_format"
