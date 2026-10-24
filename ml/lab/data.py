"""Загрузка датасета организаторов (только чтение) и разметка: подкатегория, документ-основание, «спасательные» поступления.

Формат: `statements/C001.csv`…, разделитель `;`, UTF-8 с BOM, суммы положительные + направление, ИНН строкой.
Каталог задаётся аргументом или переменной `MOCK_DATASET_DIR`; в датасете ничего не меняется и не копируется.
"""
import os
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "services"))

from parser import taxonomy  # noqa: E402  (тот же код, что в боевом парсере)

COLUMNS = ["op_date", "amount_kop", "balance_eod_kop", "counterparty_ref", "subcategory", "group", "is_rescue", "doc_no", "doc_date"]


def dataset_dir(path: str | Path | None = None) -> Path:
    p = path or os.environ.get("MOCK_DATASET_DIR")
    if not p:
        raise FileNotFoundError("укажите каталог датасета (--data или MOCK_DATASET_DIR)")
    return Path(p)


def company_ids(data_dir: Path) -> list[str]:
    return sorted(f.stem for f in (data_dir / "statements").glob("C*.csv"))


@lru_cache(maxsize=65536)
def _classify(purpose: str, name: str, positive: bool) -> tuple[str, str, str | None, str | None]:
    # результат зависит от текста и знака суммы; остальное (остаток) учитывается отдельно
    c = taxonomy.classify(purpose, name, 1 if positive else -1, None)
    return c["subcategory"], c["group"], c["doc_no"], c["doc_date"]


def load_company(data_dir: Path, cid: str, dedupe: bool = True) -> pd.DataFrame:
    """Выписка компании в копейках с разметкой. Дубли строк (≈21 на весь датасет) по умолчанию убираются."""
    raw = pd.read_csv(data_dir / "statements" / f"{cid}.csv", sep=";", encoding="utf-8-sig", dtype={"counterparty_inn": str})
    positive = raw["direction"].eq("Поступление")
    df = pd.DataFrame(
        {
            "op_date": pd.to_datetime(raw["operation_date"]).dt.date,
            "amount_kop": (raw["amount"].where(positive, -raw["amount"]) * 100).round().astype("int64"),
            "balance_eod_kop": (raw["balance_eod"] * 100).round().astype("int64"),
            "counterparty_ref": raw["counterparty_inn"].fillna(raw["counterparty_name"]),
            "purpose": raw["payment_purpose"].fillna(""),
            "name": raw["counterparty_name"].fillna(""),
        }
    )
    if dedupe:
        df = df.drop_duplicates(["op_date", "amount_kop", "counterparty_ref", "purpose"])
    cls = [_classify(p, n, a > 0) for p, n, a in zip(df["purpose"], df["name"], df["amount_kop"], strict=True)]
    df["subcategory"] = [c[0] for c in cls]
    df["group"] = [c[1] for c in cls]
    df["doc_no"] = [c[2] for c in cls]
    df["doc_date"] = [pd.to_datetime(c[3]).date() if c[3] else None for c in cls]
    df["is_rescue"] = [
        taxonomy.is_rescue(s, a, b) for s, a, b in zip(df["subcategory"], df["amount_kop"], df["balance_eod_kop"], strict=True)
    ]
    return df.sort_values("op_date", kind="stable").reset_index(drop=True)[COLUMNS + ["purpose"]]


def eligible(df: pd.DataFrame, min_days: int = 150) -> bool:
    """Достаточно ли истории для бэктеста (в README датасета: 142 из 150 компаний)."""
    return len(df) > 0 and (df["op_date"].max() - df["op_date"].min()).days >= min_days
