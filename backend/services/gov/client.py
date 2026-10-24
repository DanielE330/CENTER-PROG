"""HTTP-клиент: таймауты, User-Agent, robots.txt, пауза между запросами, дисковый кэш с TTL."""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import FetchError

USER_AGENT = "Mozilla/5.0 (compatible; CI-hack-gov/0.1; cash-forecast research; read-only)"
DEFAULT_TTL = 24 * 3600
MIN_INTERVAL = 1.0  # секунд между запросами к одному хосту
MAX_BYTES = 8 * 1024 * 1024


def default_cache_dir() -> Path:
    env = os.environ.get("GOV_CACHE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "run" / "gov_cache"


@dataclass
class Response:
    url: str
    body: str
    fetched_at: float
    from_cache: bool = False
    stale: bool = False  # источник недоступен, отдан просроченный кэш


class GovClient:
    def __init__(self, cache_dir=None, ttl=None, timeout=15.0, min_interval=MIN_INTERVAL,
                 respect_robots=True, opener=None):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.ttl = float(os.environ.get("GOV_CACHE_TTL", DEFAULT_TTL)) if ttl is None else ttl
        self.timeout = timeout
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self._open = opener or self._urlopen
        self._last = {}
        self._robots = {}
        self._lock = threading.Lock()

    # --- сеть ---
    def _urlopen(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "ru"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise FetchError("ответ слишком большой", url)
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(charset, errors="replace")

    def _throttle(self, host):
        wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def _allowed(self, url) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._throttle(parts.netloc)
                status, text = self._open(base + "/robots.txt")
                rp.parse(text.splitlines() if status == 200 else [])
            except urllib.error.HTTPError:
                rp.parse([])  # нет robots.txt (404 и т.п.) = разрешено
            except Exception:
                rp.parse([])  # robots недоступен: не блокируем, но ходим редко и только читаем
            self._robots[base] = rp
        return rp.can_fetch(USER_AGENT, url) or rp.can_fetch("*", url)

    # --- кэш ---
    def _path(self, url):
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")

    def _read_cache(self, url):
        try:
            d = json.loads(self._path(url).read_text("utf-8"))
            return Response(url, d["body"], d["fetched_at"], from_cache=True)
        except Exception:
            return None

    def _write_cache(self, url, resp):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            p = self._path(url)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"url": url, "fetched_at": resp.fetched_at, "body": resp.body},
                                      ensure_ascii=False), "utf-8")
            tmp.replace(p)
        except OSError:
            pass  # кэш необязателен

    def get(self, url, force=False) -> Response:
        with self._lock:
            cached = self._read_cache(url)
            if cached and not force and time.time() - cached.fetched_at < self.ttl:
                return cached
            host = urlsplit(url).netloc
            try:
                if not self._allowed(url):
                    raise FetchError("запрещено robots.txt", url)
                self._throttle(host)
                status, body = self._open(url)
                if status != 200:
                    raise FetchError(f"HTTP {status}", url)
            except FetchError as e:
                return self._fallback(cached, e)
            except urllib.error.HTTPError as e:
                return self._fallback(cached, FetchError(f"HTTP {e.code}", url))
            except Exception as e:
                return self._fallback(cached, FetchError(f"{type(e).__name__}: {e}", url))
            resp = Response(url, body, time.time())
            self._write_cache(url, resp)
            return resp

    @staticmethod
    def _fallback(cached, err):
        if cached:
            cached.stale = True
            return cached
        raise err
