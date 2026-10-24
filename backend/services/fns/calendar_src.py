"""Рабочие дни и окно налогового календаря через сервис gov (GOV_URL); запасной вариант: только выходные."""
import threading
import time
from datetime import date, timedelta

import httpx

from .deps import Deps
from .logsafe import log

OK_TTL, FAIL_TTL = 6 * 3600, 120


class Workdays:
    """Производственный календарь: gov /internal/v1/gov/calendar; если недоступен, учитываем только сб и вс."""

    def __init__(self, deps: Deps):
        self.deps = deps
        self._lock = threading.Lock()
        self._years = deps.__dict__.setdefault("_wd_cache", {})
        self.used_fallback = False
        self.used_gov = False

    def _non_working(self, year: int):
        with self._lock:
            hit = self._years.get(year)
            if hit and time.time() - hit[1] < (OK_TTL if hit[0] is not None else FAIL_TTL):
                return hit[0]
        data = None
        try:
            with self.deps.gov() as c:
                r = c.get("/internal/v1/gov/calendar", params={"year": year})
            if r.status_code == 200:
                data = set(r.json()["non_working"])
        except (httpx.HTTPError, ValueError, KeyError):
            data = None
        if data is None:
            log.warning("gov calendar unavailable year=%s: fallback to weekends", year)
        with self._lock:
            self._years[year] = (data, time.time())
        return data

    def is_working(self, d: date) -> bool:
        nw = self._non_working(d.year)
        if nw is None:
            self.used_fallback = True
            return d.weekday() < 5
        self.used_gov = True
        return d.isoformat() not in nw

    def shift(self, d: date) -> date:
        for _ in range(15):
            if self.is_working(d):
                return d
            d += timedelta(days=1)
        return d

    @property
    def source(self) -> str:
        if self.used_gov and not self.used_fallback:
            return "gov"
        return "weekends_only" if self.used_fallback and not self.used_gov else ("gov_partial" if self.used_gov else "none")


def site_window(deps: Deps, year: int):
    """Окно календаря nalog.gov.ru через gov: (events, coverage) или None."""
    try:
        with deps.gov() as c:
            r = c.get("/internal/v1/gov/tax-calendar", params={"year": year})
        if r.status_code != 200:
            return None
        j = r.json()
        return j.get("events", []), j.get("coverage")
    except (httpx.HTTPError, ValueError):
        return None
