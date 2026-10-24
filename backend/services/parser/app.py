"""Внутренний сервис разбора выписок. Не хранит файлы и не пишет содержимое в логи."""
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import taxonomy, tail
from .statement import parse_statement

MAX_BYTES = 25 * 1024 * 1024

app = FastAPI(title="parser-service", docs_url=None, redoc_url=None)


@app.get("/live")
def live():
    return {"status": "ok"}


@app.post("/internal/v1/parse")
async def parse(file: UploadFile = File(...)):
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, {"code": "file_too_large"})
    try:
        return parse_statement(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(422, {"code": str(e)})
    except Exception:
        raise HTTPException(422, {"code": "unreadable_file"})


@app.get("/internal/v1/taxonomy")
def get_taxonomy():
    """Словарь подкатегорий: группа, название и категория схемы ledger. Нужен ledger для правок и валидации."""
    tax = taxonomy.default()
    subs = {k: {"group": v["group"], "name": v.get("name", k), "category": taxonomy.LEGACY[k]} for k, v in tax.subcategories.items()}
    return {"groups": tax.groups, "subcategories": subs}


class TailRow(BaseModel):
    purpose: str = ""
    name: str = ""
    amount_kop: int


class TrainRow(TailRow):
    subcategory: str


class TailBody(BaseModel):
    train: list[TrainRow] = Field(max_length=50000)
    rows: list[TailRow] = Field(max_length=200000)


@app.post("/internal/v1/tail")
def predict_tail(b: TailBody):
    """Подкатегории для операций, которых не распознали правила. Модель — только по данным этого запроса (одного тенанта)."""
    tax = taxonomy.default()
    fallback = {"other_in", "other_out"}
    train = [r for r in b.train if r.subcategory in tax.subcategories and r.subcategory not in fallback]
    model = tail.TfidfLogReg().fit(
        [tail.features(r.purpose, r.name, r.amount_kop) for r in train], [r.subcategory for r in train]
    )
    if not model.ready:
        return {"ready": False, "predictions": [None] * len(b.rows)}
    preds = model.predict([tail.features(r.purpose, r.name, r.amount_kop) for r in b.rows])
    out = []
    for r, (sub, conf) in zip(b.rows, preds):
        # знак операции уже в признаках, но вход/выход подкатегории сверяем явно
        if conf < tail.MIN_CONFIDENCE or sub not in tax.subcategories or sub in fallback or _direction_clash(sub, r.amount_kop):
            out.append(None)
        else:
            out.append({"subcategory": sub, "group": tax.group(sub), "category": taxonomy.LEGACY[sub], "confidence": round(conf, 4)})
    return {"ready": True, "predictions": out}


_IN_ONLY = {"acquiring", "sbp", "cash_deposit", "marketplace_payout", "customer_payment", "loan_received", "own_transfer_in", "founder_loan_in", "refund_in", "other_in"}
_OUT_ONLY = {"supplier_payment", "advertising", "software", "services", "fuel_transport", "salary", "contractors_gph", "enp", "rent", "utilities",
             "loan_payment", "leasing", "own_transfer_out", "founder_loan_return", "owner_withdrawal", "refund_out", "bank_fee", "other_out"}


def _direction_clash(sub: str, amount_kop: int) -> bool:
    return (sub in _IN_ONLY and amount_kop < 0) or (sub in _OUT_ONLY and amount_kop > 0)
