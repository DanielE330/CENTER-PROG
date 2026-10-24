"""Имитатор 1С:Предприятие: uvicorn onec.app:app --port 8004"""
from fastapi import FastAPI

from .odata import router

app = FastAPI(title="onec-mock", docs_url=None, redoc_url=None)


@app.get("/live")
def live():
    return {"status": "ok"}


app.include_router(router)
