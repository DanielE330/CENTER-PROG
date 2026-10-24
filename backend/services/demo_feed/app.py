"""Лента демо «Прошла неделя»: uvicorn demo_feed.app:app --port 8012

Одна задача: по курсору (дате последней загруженной операции) отдавать операции следующей недели демо-компании.
Данные берутся из датасета (DEMO_DATASET_DIR / MOCK_DATASET_DIR). Загрузку в ledger делает api обычным импортом.
"""
import re
from datetime import date

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

from . import feed

app = FastAPI(title="demo-feed", docs_url=None, redoc_url=None)
COMPANY_RE = re.compile(r"^C\d{3}$")


def _error(status: int, code: str, message: str) -> JSONResponse:
    # формат ошибок Rust-сервисов: api пробрасывает его в SPA как есть
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _params(company: str | None, after: str | None):
    company = company or feed.default_company()
    if not COMPANY_RE.match(company):
        return _error(422, "invalid_company", "Некорректный код компании")
    if not feed.company_exists(company):
        return _error(404, "unknown_company", "Нет данных для этой компании")
    try:
        cur = date.fromisoformat(after) if after else feed.cutoff()
    except ValueError:
        return _error(422, "invalid_after", "Дата курсора в формате YYYY-MM-DD")
    return company, cur


@app.get("/live")
def live():
    return {"status": "ok"}


@app.get("/internal/v1/feed/status")
def status(company: str | None = Query(None), after: str | None = Query(None)):
    p = _params(company, after)
    if isinstance(p, JSONResponse):
        return p
    company, cur = p
    st = feed.statement(company)
    nxt = feed.next_week(st, cur)
    return {
        "company": company,
        "cutoff": feed.cutoff().isoformat(),
        "cursor": cur.isoformat(),
        "first_date": st.dates[0].isoformat(),
        "last_date": st.dates[-1].isoformat(),
        "next_from": nxt.start.isoformat() if nxt else None,
        "weeks_left": feed.weeks_left(st, cur),
        "ops_left": feed.ops_left(st, cur),
        "finished": nxt is None,
    }


def _week(company: str | None, after: str | None):
    p = _params(company, after)
    if isinstance(p, JSONResponse):
        return p
    company, cur = p
    st = feed.statement(company)
    w = feed.next_week(st, cur)
    if w is None:
        return _error(409, "feed_exhausted", "Операции демо закончились")
    return company, cur, st, w


@app.get("/internal/v1/feed/week")
def week(company: str | None = Query(None), after: str | None = Query(None)):
    """Операции следующей недели: метаданные и CSV (та же структура, что у загружаемой выписки) одним JSON."""
    r = _week(company, after)
    if isinstance(r, JSONResponse):
        return r
    company, _, st, w = r
    return {
        "company": company,
        "from": w.start.isoformat(),
        "to": w.last_op.isoformat(),
        "window_end": w.end.isoformat(),
        "count": len(w.rows),
        "weeks_left": feed.weeks_left(st, w.last_op),
        "filename": f"{company}_{w.start.isoformat()}_{w.last_op.isoformat()}.csv",
        "csv": feed.to_csv(st, w),
    }


@app.get("/internal/v1/feed/week.csv")
def week_csv(company: str | None = Query(None), after: str | None = Query(None)):
    r = _week(company, after)
    if isinstance(r, JSONResponse):
        return r
    company, _, st, w = r
    return Response(
        feed.to_csv(st, w).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"x-feed-from": w.start.isoformat(), "x-feed-to": w.last_op.isoformat(), "x-feed-count": str(len(w.rows))},
    )
