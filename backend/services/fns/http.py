"""Вежливый HTTP-клиент для публичных сайтов: честный User-Agent, robots.txt, не чаще 1 запроса в секунду на хост,
таймауты, лимит размера, ручной обход редиректов с проверкой robots для каждого адреса.

Капчи и защиту от ботов не обходим; User-Agent не подменяем.
"""
import threading
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import config
from .errors import SourceUnavailable
from .logsafe import log

ROBOTS_TTL = 24 * 3600
MAX_BYTES = 8 * 1024 * 1024
UA_TOKEN = "CI-hack-fns"


class PoliteHttp:
    def __init__(self, transport: httpx.BaseTransport | None = None, min_interval: float | None = None,
                 timeout: float = 15.0, sleep=time.sleep, clock=time.monotonic, respect_robots: bool = True):
        self.transport = transport
        self.min_interval = config.min_interval() if min_interval is None else min_interval
        self.timeout = timeout
        self._sleep, self._clock = sleep, clock
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # --- частота ---
    def _throttle(self, host: str):
        with self._lock:
            now = self._clock()
            slot = max(now, self._last.get(host, float("-inf")) + self.min_interval)
            self._last[host] = slot
        if slot > now:
            self._sleep(slot - now)

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False,
                            headers={"User-Agent": config.user_agent(), "Accept-Language": "ru"})

    # --- robots.txt ---
    def _raw_get(self, url: str, max_bytes: int = 512 * 1024):
        self._throttle(urlsplit(url).netloc)
        with self._client() as c:
            return c.get(url)

    def robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        hit = self._robots.get(origin)
        if hit is None or time.time() - hit[0] > ROBOTS_TTL:
            try:
                r = self._raw_get(origin + "/robots.txt")
            except httpx.HTTPError:
                # robots недоступен из-за сети: осторожно считаем доступ запрещённым (RFC 9309)
                raise SourceUnavailable("robots_unreachable", f"{parts.netloc}: robots.txt недоступен") from None
            if r.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                state: object = rp
            elif r.status_code >= 500:
                raise SourceUnavailable("robots_unreachable", f"{parts.netloc}: robots.txt вернул {r.status_code}")
            else:
                state = None  # 4xx: файла нет, ограничений нет (RFC 9309 п. 2.3.1.3)
            hit = (time.time(), state)
            self._robots[origin] = hit
        rp = hit[1]
        return True if rp is None else rp.can_fetch(UA_TOKEN, url)

    # --- запросы ---
    def request(self, method: str, url: str, *, data=None, json_body=None, params=None, headers=None,
                max_bytes: int = MAX_BYTES, _hops: int = 0) -> httpx.Response:
        host = urlsplit(url).netloc
        if not self.robots_allowed(url):
            raise SourceUnavailable("robots_disallowed", f"{host}: запрещено robots.txt")
        self._throttle(host)
        try:
            with self._client() as c:
                req = c.build_request(method, url, data=data, json=json_body, params=params, headers=headers)
                # httpx.Response не контекстный менеджер: поток закрываем сами
                r = c.send(req, stream=True)
                try:
                    if r.is_redirect and _hops < 3 and r.headers.get("location"):
                        nxt = urljoin(url, r.headers["location"])
                        return self.request("GET", nxt, headers=headers, max_bytes=max_bytes, _hops=_hops + 1)
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise SourceUnavailable("too_large", f"{host}: ответ больше {max_bytes} байт")
                    # тело уже раскодировано (gzip и т. п.): заголовки сжатия не переносим, иначе httpx распакует ещё раз
                    hdrs = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
                    out = httpx.Response(r.status_code, headers=hdrs, content=bytes(body), request=req)
                finally:
                    r.close()
        except httpx.TimeoutException:
            raise SourceUnavailable("timeout", f"{host}: таймаут") from None
        except httpx.HTTPError as e:
            raise SourceUnavailable("network", f"{host}: {type(e).__name__}") from None
        log.info("request host=%s method=%s status=%s", host, method, out.status_code)
        return out

    def download(self, url: str, dest: Path, max_bytes: int) -> int:
        """Скачивание большого файла на диск (потоком). Возвращает размер."""
        host = urlsplit(url).netloc
        if not self.robots_allowed(url):
            raise SourceUnavailable("robots_disallowed", f"{host}: запрещено robots.txt")
        self._throttle(host)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        try:
            with self._client() as c, c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise SourceUnavailable(f"http_{r.status_code}", f"{host}: HTTP {r.status_code}")
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourceUnavailable("too_large", f"{host}: файл больше {max_bytes} байт")
                        f.write(chunk)
        except httpx.TimeoutException:
            tmp.unlink(missing_ok=True)
            raise SourceUnavailable("timeout", f"{host}: таймаут") from None
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise SourceUnavailable("network", f"{host}: {type(e).__name__}") from None
        except SourceUnavailable:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)
        log.info("download host=%s bytes=%s", host, size)
        return size
