"""Источник fns_sim: наш имитатор (отдельный сервис, порт 8016)."""
import httpx

from .. import config
from ..errors import SourceUnavailable


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=config.sim_url(), transport=transport, timeout=5.0,
                        headers={"User-Agent": config.user_agent()})


def _get(http: httpx.Client, path: str, **kw):
    last = None
    for _ in range(2):  # один повтор при сетевом сбое или 5xx
        try:
            r = http.get(path, **kw)
        except httpx.TimeoutException:
            last = SourceUnavailable("timeout", "fns_sim: таймаут")
            continue
        except httpx.HTTPError as e:
            last = SourceUnavailable("network", f"fns_sim: {type(e).__name__}")
            continue
        if r.status_code >= 500:
            last = SourceUnavailable(f"http_{r.status_code}", f"fns_sim: HTTP {r.status_code}")
            continue
        return r
    raise last


def counterparty(http: httpx.Client, inn: str) -> dict:
    r = _get(http, "/sim/v1/counterparty", params={"inn": inn})
    if r.status_code != 200:
        raise SourceUnavailable(f"http_{r.status_code}", f"fns_sim: HTTP {r.status_code}")
    return r.json()


def ens(http: httpx.Client, inn: str) -> dict | None:
    r = _get(http, "/sim/v1/ens", params={"inn": inn}, headers={"Authorization": "Bearer " + config.sim_token()})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise SourceUnavailable(f"http_{r.status_code}", f"fns_sim: HTTP {r.status_code}")
    return r.json()
