"""ЕФРСБ (Федресурс): официальный REST-сервис «Сервис получения сведений из ЕФРСБ» v1.0.0 (оператор АО «Интерфакс»).

Доступ только по логину и паролю, которые выдаёт оператор при подключении (публичного доступа нет). Без
FEDRESURS_LOGIN/FEDRESURS_PASSWORD источник честно «недоступен: credentials_required». Публичный сайт
bankrot.fedresurs.ru мы не разбираем. Проверяются только юридические лица (type=Company); для ИП/физлиц
реестр ищется по персональным данным, этого не делаем.
Поиск: GET /v1/bankrupts?type=Company&inn=...; наличие записи означает, что лицо числится «должником» в ЕФРСБ
(термин оператора шире, чем «банкротство по 127-ФЗ»), поэтому в ответе это отдельный признак.
"""
import threading
import time

from .. import config
from ..errors import SourceUnavailable
from ..http import PoliteHttp

DEFAULT_URL = "https://bank-publications-prod.fedresurs.ru"
_tok = {"jwt": None, "until": 0.0}
_lock = threading.Lock()


def credentials():
    login, pw = config.env("FEDRESURS_LOGIN"), config.env("FEDRESURS_PASSWORD")
    return (login, pw) if login and pw else None


def _token(http: PoliteHttp, base: str, login: str, pw: str) -> str:
    with _lock:
        if _tok["jwt"] and time.time() < _tok["until"]:
            return _tok["jwt"]
        r = http.request("POST", base + "/v1/auth", json_body={"login": login, "password": pw},
                         max_bytes=64 * 1024)
        if r.status_code in (400, 401):
            raise SourceUnavailable("bad_credentials", "fedresurs: логин или пароль не приняты")
        if r.status_code != 200 or not r.json().get("jwt"):
            raise SourceUnavailable(f"http_{r.status_code}", "fedresurs: нет токена")
        _tok["jwt"], _tok["until"] = r.json()["jwt"], time.time() + 7 * 3600  # токен живёт 8 часов
        return _tok["jwt"]


def fetch(http: PoliteHttp, inn: str, base: str | None = None) -> dict:
    cred = credentials()
    if not cred:
        raise SourceUnavailable("credentials_required",
                                "ЕФРСБ: нужны логин и пароль сервиса (FEDRESURS_LOGIN/FEDRESURS_PASSWORD), выдаёт оператор")
    if len(inn) != 10:
        raise SourceUnavailable("person_not_queried", "ЕФРСБ: ИП и физлица не проверяем")
    base = (base or config.env("FEDRESURS_URL", DEFAULT_URL)).rstrip("/")
    jwt = _token(http, base, *cred)
    r = http.request("GET", base + "/v1/bankrupts", params={"type": "Company", "inn": inn, "limit": 5, "offset": 0},
                     headers={"Authorization": "Bearer " + jwt}, max_bytes=512 * 1024)
    if r.status_code == 401:
        _tok["jwt"] = None
        raise SourceUnavailable("unauthorized", "fedresurs: токен не принят")
    if r.status_code != 200:
        raise SourceUnavailable(f"http_{r.status_code}", f"fedresurs: HTTP {r.status_code}")
    try:
        total = int(r.json()["total"])
    except (ValueError, KeyError, TypeError):
        raise SourceUnavailable("bad_response", "fedresurs: неожиданный формат") from None
    return {"flag": total > 0, "records": total}
