"""Тесты ленты демо: окна недель, курсор, формат CSV, ошибки (без сети)."""
import csv
import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from demo_feed import feed
from demo_feed.app import app

HEADER = "company_id;operation_id;operation_date;amount;direction;counterparty_inn;counterparty_name;payment_purpose;balance_eod"


def row(i: int, d: str, amount: str = "100.00", direction: str = "Поступление") -> str:
    return f"C001;C001-{i:06d};{d};{amount};{direction};0012345678;ООО «Тест»;Оплата; с точкой с запятой;1000.00"


@pytest.fixture
def ds(tmp_path, monkeypatch):
    (tmp_path / "statements").mkdir()
    # 1 июля .. 3 июля, пауза, 20 июля (пустые недели), 21 июля, 28 июля
    days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-20", "2026-07-21", "2026-07-28"]
    lines = [HEADER] + [row(i + 1, d).replace("Оплата; с точкой с запятой", '"Оплата; с точкой с запятой"') for i, d in enumerate(days)]
    (tmp_path / "statements" / "C001.csv").write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("DEMO_DATASET_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_CUTOFF", "2026-07-02")
    feed._load.cache_clear()
    return tmp_path


c = TestClient(app)


def test_live():
    assert c.get("/live").json() == {"status": "ok"}


def test_status_defaults_to_cutoff(ds):
    s = c.get("/internal/v1/feed/status").json()
    assert s["cutoff"] == "2026-07-02" and s["cursor"] == "2026-07-02"
    assert s["next_from"] == "2026-07-03" and s["ops_left"] == 4 and s["finished"] is False
    # окна: [3 июля] -> [20-21 июля] -> [28 июля]
    assert s["weeks_left"] == 3


def test_week_returns_csv_of_same_structure(ds):
    w = c.get("/internal/v1/feed/week", params={"after": "2026-07-02"}).json()
    assert (w["from"], w["to"], w["count"]) == ("2026-07-03", "2026-07-03", 1)
    assert w["csv"].startswith("﻿" + HEADER)
    rows = list(csv.reader(io.StringIO(w["csv"].lstrip("﻿")), delimiter=";"))
    assert rows[0] == HEADER.split(";") and len(rows) == 2
    assert rows[1][7] == "Оплата; с точкой с запятой"  # кавычки сохраняются при ";" внутри поля
    assert w["filename"].endswith(".csv")


def test_empty_week_is_skipped(ds):
    w = c.get("/internal/v1/feed/week", params={"after": "2026-07-03"}).json()
    assert (w["from"], w["to"], w["count"]) == ("2026-07-20", "2026-07-21", 2)
    assert w["window_end"] == "2026-07-26"


def test_cursor_walks_to_exhaustion(ds):
    after, total = "2026-07-02", 0
    for _ in range(10):
        r = c.get("/internal/v1/feed/week", params={"after": after})
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "feed_exhausted"
            break
        total += r.json()["count"]
        after = r.json()["to"]
    assert total == 4 and after == "2026-07-28"
    assert c.get("/internal/v1/feed/status", params={"after": after}).json()["finished"] is True


def test_week_csv_endpoint(ds):
    r = c.get("/internal/v1/feed/week.csv", params={"after": "2026-07-21"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-feed-count"] == "1" and r.headers["x-feed-to"] == "2026-07-28"


def test_errors(ds):
    assert c.get("/internal/v1/feed/status", params={"company": "../x"}).status_code == 422
    assert c.get("/internal/v1/feed/status", params={"company": "C999"}).status_code == 404
    r = c.get("/internal/v1/feed/status", params={"after": "вчера"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_after"


def test_real_demo_statement_has_enough_weeks(monkeypatch):
    """Демо-выписка из репозитория: после даты отсечки по умолчанию остаётся минимум 8 недель."""
    monkeypatch.delenv("DEMO_DATASET_DIR", raising=False)
    monkeypatch.delenv("MOCK_DATASET_DIR", raising=False)
    monkeypatch.delenv("DEMO_CUTOFF", raising=False)
    feed._load.cache_clear()
    if not feed.company_exists("C001"):
        pytest.skip("нет датасета")
    st = feed.statement("C001")
    assert feed.cutoff() == date(2026, 7, 12)
    assert feed.weeks_left(st, feed.cutoff()) >= 8
