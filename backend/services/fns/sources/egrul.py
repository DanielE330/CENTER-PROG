"""ЕГРЮЛ/ЕГРИП: публичный поиск egrul.nalog.ru (JSON-ответы того же веб-интерфейса, что видит человек).

Поток: POST / (query=ИНН) -> {t, captchaRequired}; GET /search-result/{t} -> {rows:[...]}.
Если сайт просит капчу (captchaRequired:true), мы НЕ пытаемся её пройти: источник считается недоступным,
дальнейшие запросы к нему приостанавливаются на FNS_CAPTCHA_COOLDOWN секунд.
В ответе есть ФИО руководителя (поле g) и наименование ИП: их мы не читаем и не сохраняем.
Подробности и условия: docs/fns.md.
"""
import re
import threading
import time
from datetime import datetime

from .. import config
from ..errors import SourceUnavailable
from ..http import PoliteHttp

BASE = "https://egrul.nalog.ru"
TOKEN = re.compile(r"^[A-Za-z0-9]{8,200}$")


class Breaker:
    """Пауза после капчи или массовых отказов."""

    def __init__(self):
        self.until = 0.0
        self.lock = threading.Lock()

    def check(self):
        if time.time() < self.until:
            raise SourceUnavailable("captcha_cooldown", "ЕГРЮЛ просил капчу; повторим позже, капчу не обходим")

    def trip(self):
        self.until = time.time() + config.float_env("FNS_CAPTCHA_COOLDOWN", 3600.0)


BREAKER = Breaker()


def _date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def parse_rows(rows: list, inn: str) -> dict:
    """Строки поиска -> компактный результат (без ФИО и наименований)."""
    for r in rows:
        if str(r.get("i")) == inn:
            return {"found": True, "kind": r.get("k"), "registered_at": _date(r.get("r")),
                    "terminated_at": _date(r.get("e")), "terminated": bool(r.get("e"))}
    return {"found": False}


def fetch(http: PoliteHttp, inn: str, base: str | None = None) -> dict:
    base = (base or BASE).rstrip("/")
    BREAKER.check()
    r = http.request("POST", base + "/", data={"vyp3CaptchaToken": "", "page": "", "query": inn, "region": "",
                                               "PreventChromeAutocomplete": ""},
                     headers={"Accept": "application/json"}, max_bytes=64 * 1024)
    if r.status_code != 200:
        raise SourceUnavailable(f"http_{r.status_code}", f"egrul: HTTP {r.status_code}")
    try:
        j = r.json()
    except ValueError:
        raise SourceUnavailable("bad_response", "egrul: не JSON") from None
    if j.get("captchaRequired"):
        BREAKER.trip()
        raise SourceUnavailable("captcha_required", "ЕГРЮЛ просит капчу; автоматический доступ приостановлен")
    t = j.get("t")
    if not isinstance(t, str) or not TOKEN.match(t):
        raise SourceUnavailable("bad_response", "egrul: нет токена результата")
    r2 = http.request("GET", f"{base}/search-result/{t}", headers={"Accept": "application/json"}, max_bytes=1024 * 1024)
    if r2.status_code != 200:
        raise SourceUnavailable(f"http_{r2.status_code}", f"egrul: HTTP {r2.status_code}")
    try:
        rows = r2.json()["rows"]
    except (ValueError, KeyError, TypeError):
        raise SourceUnavailable("bad_response", "egrul: неожиданный формат") from None
    return parse_rows(rows, inn)
