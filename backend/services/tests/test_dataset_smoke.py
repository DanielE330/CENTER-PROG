"""Дымовой тест на синтетическом датасете хакатона (пропускается, если data/dataset нет)."""
import collections
import glob
import os
import time

import pytest

from parser.statement import parse_statement

DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "dataset", "statements")
FILES = sorted(glob.glob(os.path.join(DIR, "*.csv")))
pytestmark = pytest.mark.skipif(not FILES, reason="data/dataset отсутствует")


def test_all_statements_parse_and_coverage(capsys):
    cats = collections.Counter()
    fallback = total = rejected = dups = mism = checked = gaps = 0
    slowest = 0.0
    for f in FILES:
        with open(f, "rb") as fh:
            data = fh.read()
        t0 = time.perf_counter()
        r = parse_statement(os.path.basename(f), data)
        slowest = max(slowest, time.perf_counter() - t0)
        s = r["stats"]
        rejected += s["rows_rejected"]
        dups += s["suspected_duplicates"]
        mism += s["balance_mismatches"]
        checked += s["balance_checked"]
        gaps += s["missing_period_count"]
        assert s["balance_mode"] == "eod"
        assert s["opening_balance_kop"] is not None
        for t in r["transactions"]:
            total += 1
            cats[(t["direction"], t["category"])] += 1
            if t["category_confidence"] <= 0.2:
                fallback += 1
            if t["inn"]:
                assert len(t["inn"]) in (10, 12)
            assert t.get("source_id")
    with capsys.disabled():
        print(f"\nfiles={len(FILES)} rows={total} rejected={rejected} fallback={fallback} ({fallback / total:.3%}) "
              f"dups={dups} balance_checked={checked} mismatches={mism} gaps={gaps} slowest={slowest:.2f}s")
        for (d, c), n in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"  {d:3} {c:14} {n:7} {n / total:.2%}")
    assert rejected <= total * 0.001
    assert fallback / total <= 0.03
    assert slowest < 1.0
