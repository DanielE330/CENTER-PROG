"""Имитатор источников ФНС (порт 8016): uvicorn fns_sim.app:app --port 8016

Что имитируется (формы ответов как у настоящих источников, данные из датасета, всё детерминировано по ИНН):
- поиск ЕГРЮЛ/ЕГРИП в форме egrul.nalog.ru: POST /egrul/ -> {t, captchaRequired}, GET /egrul/search-result/{t} -> {rows};
- сводка по контрагенту (статус, массовый адрес, недостоверность, недоимка, дисквалификация, банкротство);
- ЕНС налогоплательщика (в жизни доступен только в личном кабинете: здесь нужен Bearer FNS_SIM_TOKEN).
Сбои: MOCK_FAIL_RATE (доля 503), MOCK_DELAY_MS, FNS_SIM_CAPTCHA_RATE (доля ответов «нужна капча» в /egrul/),
FNS_SIM_PROBLEM_INNS ("инн=сценарий,..."), как у имитаторов 1С/ЭДО/банка.
"""
import logging
import os
import random
import re
import secrets
from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request

import mockdata
from mockdata import delay_and_should_fail

from . import universe

INN = re.compile(r"^(\d{10}|\d{12})$")


class _MaskInn(logging.Filter):
    """ИНН не попадает в журнал доступа."""

    def filter(self, record):
        try:
            record.args = tuple(re.sub(r"(inn|query)=\d+", r"\1=***", a) if isinstance(a, str) else a
                                for a in (record.args or ()))
        except Exception:  # noqa: BLE001
            pass
        return True


logging.getLogger("uvicorn.access").addFilter(_MaskInn())

app = FastAPI(title="fns-sim", docs_url=None, redoc_url=None)


def chaos():
    if delay_and_should_fail():
        raise HTTPException(503, {"code": "sim_unavailable", "message": "Service Unavailable (simulated failure)"})


def inn_param(inn: str = Query(...)) -> str:
    if not INN.match(inn):
        raise HTTPException(400, {"code": "bad_inn", "message": "ИНН: 10 или 12 цифр"})
    return inn


def lk_auth(request: Request):
    h = request.headers.get("authorization", "")
    tok = os.environ.get("FNS_SIM_TOKEN", "fns-sim-demo")
    if not (h.lower().startswith("bearer ") and secrets.compare_digest(h[7:], tok)):
        raise HTTPException(401, {"code": "unauthorized", "message": "нужен вход в личный кабинет (Bearer)"},
                            headers={"WWW-Authenticate": "Bearer"})


@app.get("/live")
def live():
    return {"status": "ok"}


@app.get("/sim/v1/counterparty", dependencies=[Depends(chaos)])
def counterparty(inn: str = Depends(inn_param)):
    return universe.counterparty(inn)


@app.get("/sim/v1/ens", dependencies=[Depends(chaos), Depends(lk_auth)])
def ens(inn: str = Depends(inn_param)):
    r = universe.ens(inn)
    if r is None:
        raise HTTPException(404, {"code": "unknown_taxpayer", "message": "налогоплательщик не найден"})
    return r


# --- форма ответов egrul.nalog.ru (поиск по ИНН) ---

@app.post("/egrul/", dependencies=[Depends(chaos)])
def egrul_search(query: str = Form(...)):
    rate = float(os.environ.get("FNS_SIM_CAPTCHA_RATE", "0") or 0)
    if rate > 0 and random.random() < rate:
        return {"captchaRequired": True}
    if not INN.match(query):
        return {"t": "00", "captchaRequired": False}
    return {"t": "SIM" + query.encode().hex().upper(), "captchaRequired": False}


@app.get("/egrul/search-result/{token}", dependencies=[Depends(chaos)])
def egrul_result(token: str):
    try:
        inn = bytes.fromhex(token[3:]).decode() if token.startswith("SIM") else ""
    except ValueError:
        inn = ""
    c = universe.counterparty(inn) if INN.match(inn) else {"found": False}
    if not c["found"]:
        return {"rows": []}
    d = date.fromisoformat(c["registered_at"])
    row = {"c": (c["name"] or "")[:60], "cnt": "1", "i": inn, "k": "ul" if len(inn) == 10 else "fl",
           "n": c["name"], "o": universe.ogrn(inn), "r": d.strftime("%d.%m.%Y"), "t": "SIMPDF" + inn,
           "pg": "1", "tot": "1", "rn": "Г.Москва"}
    if len(inn) == 10:
        row["p"] = "770101001"
    if c["status"] == "liquidated":
        row["e"] = date(2026, 3, 12).strftime("%d.%m.%Y")
    return {"rows": [row]}
