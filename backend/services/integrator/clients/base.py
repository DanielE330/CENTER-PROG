"""Общий HTTP-клиент: таймауты и ретраи при 5xx/сетевых сбоях."""
import os
import time

import httpx

RETRIES = 2  # повторов после первой попытки


class SourceError(Exception):
    """Источник недоступен или ответил ошибкой после ретраев."""


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def make_http(base_url: str, transport: httpx.BaseTransport | None = None, **kw) -> httpx.Client:
    timeout = kw.pop("timeout", None) or float(env("INTEGRATOR_TIMEOUT", "5"))
    return httpx.Client(base_url=base_url, timeout=timeout, transport=transport, **kw)


def request(http: httpx.Client, method: str, url: str, *, sleep=time.sleep, pause: float | None = None, **kw) -> httpx.Response:
    """Запрос с ретраями на 5xx и сетевые ошибки. 4xx сразу SourceError."""
    pause = float(env("INTEGRATOR_RETRY_PAUSE", "0.3")) if pause is None else pause
    last = ""
    for attempt in range(RETRIES + 1):
        try:
            r = http.request(method, url, **kw)
            if r.status_code < 500:
                if r.status_code >= 400:
                    raise SourceError(f"{method} {url}: HTTP {r.status_code}")
                return r
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            sleep(pause * (attempt + 1))
    raise SourceError(f"{method} {url}: {last} (после {RETRIES + 1} попыток)")
