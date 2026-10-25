"""Внутренний сервис государственных данных (порт 8003).
uvicorn gov.app:app --host 127.0.0.1 --port 8003"""
from datetime import date

from fastapi import FastAPI, HTTPException, Query

from . import service
from .errors import FetchError, GovError, ParseError

app = FastAPI(title="gov-service", docs_url=None, redoc_url=None)


def _call(fn, *a):
    try:
        return fn(*a)
    except ParseError as e:
        raise HTTPException(502, {"code": e.code, "source": e.source, "message": str(e)})
    except FetchError as e:
        raise HTTPException(503, {"code": e.code, "source": e.source, "message": str(e)})
    except GovError as e:
        raise HTTPException(500, {"code": e.code, "source": e.source, "message": str(e)})


@app.get("/live")
def live():
    return {"status": "ok"}


@app.get("/internal/v1/gov/key-rate")
def key_rate():
    return _call(service.key_rate)


@app.get("/internal/v1/gov/rates")
def rates():
    # service.rates сам деградирует (кэш -> запасной набор), поэтому ошибок источника наружу не отдаёт
    return service.rates()


@app.get("/internal/v1/gov/calendar")
def calendar(year: int = Query(default_factory=lambda: date.today().year, ge=2013, le=2100)):
    return _call(service.production_calendar, year)


@app.get("/internal/v1/gov/tax-calendar")
def tax_calendar(year: int = Query(default_factory=lambda: date.today().year, ge=2013, le=2100)):
    return _call(service.tax_calendar, year)


@app.get("/internal/v1/gov/insurance")
def insurance():
    return _call(service.insurance)
