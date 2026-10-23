"""Внутренний сервис разбора выписок. Не хранит файлы и не пишет содержимое в логи."""
from fastapi import FastAPI, File, HTTPException, UploadFile

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
