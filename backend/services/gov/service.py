"""Высокоуровневые функции: клиент + парсер -> простые dict (JSON)."""
import time
from datetime import date, timedelta

from .client import GovClient
from .errors import FetchError, ParseError
from .parsers import calendar as cal
from .parsers import cbr, nalog

_client = None


def get_client() -> GovClient:
    global _client
    if _client is None:
        _client = GovClient()
    return _client


def _meta(resp):
    return {"fetched_at": int(resp.fetched_at), "from_cache": resp.from_cache, "stale": resp.stale}


def key_rate(client=None, today=None) -> dict:
    client = client or get_client()
    today = today or date.today()
    # окно в ~13 месяцев, чтобы увидеть последнее изменение ставки
    resp = client.get(cbr.url(today - timedelta(days=400), today))
    return {**cbr.parse_key_rate(resp.body).to_dict(), "url": resp.url, **_meta(resp)}


def rates(client=None) -> dict:
    """Курсы ЦБ РФ на сегодня. Кэш на сутки; ЦБ недоступен -> просроченный кэш (stale: true);
    нет и кэша -> запасной набор приблизительных курсов (approx: true). Только для пересчёта отображения."""
    client = client or get_client()
    try:
        resp = client.get(cbr.RATES_URL)
        return {**cbr.parse_rates(resp.body).to_dict(), "url": resp.url, **_meta(resp)}
    except (FetchError, ParseError):
        return {**cbr.fallback_rates().to_dict(), "url": cbr.RATES_URL, "fetched_at": int(time.time()),
                "from_cache": False, "stale": False}


def production_calendar(year: int, client=None) -> dict:
    client = client or get_client()
    resp = client.get(cal.url(year))
    return {**cal.parse_calendar(resp.body, year).to_dict(), "url": resp.url, **_meta(resp)}


def tax_calendar(year: int, client=None) -> dict:
    client = client or get_client()
    resp = client.get(nalog.CALENDAR_URL)
    events = nalog.parse_tax_calendar(resp.body)
    dates = sorted({e.date for e in events})
    sel = [e.to_dict() for e in events if e.date.startswith(f"{year}-")]
    out = {
        "year": year, "events": sel, "source": nalog.SOURCE, "url": resp.url,
        "coverage": {"from": dates[0], "to": dates[-1]},
        "note": "ФНС публикует только ближайшее окно (несколько месяцев), не весь год",
        **_meta(resp),
    }
    if not sel:
        out["warning"] = f"в опубликованном окне нет событий за {year} год"
    return out


def insurance(client=None) -> dict:
    client = client or get_client()
    resp = client.get(nalog.INSURANCE_URL)
    return {**nalog.parse_insurance(resp.body).to_dict(), "url": resp.url, **_meta(resp)}


def snapshot(year=None, client=None) -> dict:
    """Все источники сразу; ошибка одного не ломает остальные."""
    year = year or date.today().year
    out = {}
    for name, fn in [("key_rate", lambda: key_rate(client)), ("calendar", lambda: production_calendar(year, client)),
                     ("tax_calendar", lambda: tax_calendar(year, client)), ("insurance", lambda: insurance(client))]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": getattr(e, "code", "error"), "detail": str(e)}
    return out
