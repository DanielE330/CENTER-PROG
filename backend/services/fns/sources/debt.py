"""Открытые данные ФНС «Сведения о суммах задолженности по уплате налогов, сборов и страховых взносов, пеней и
штрафов» (набор 7707329152-debtam): XML в ZIP, только организации (ИННЮЛ), сведения на 10-е число второго месяца
квартала, публикуются раз в квартал. Лицензия: Типовые условия использования открытых данных (свободное
использование, в том числе коммерческое, со ссылкой на источник), договор не нужен.

Файл большой (~70 МБ), поэтому: скачиваем один раз, строим индекс ИНН -> сумма в SQLite и затем отвечаем локально.
Отсутствие ИНН в наборе означает «в опубликованном списке на дату нет», а не «долга нет сегодня». ИП в наборе нет.
"""
import re
import threading
import time
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .. import config
from ..errors import SourceUnavailable
from ..http import PoliteHttp
from ..logsafe import log
from ..store import Store

PASSPORT = "https://www.nalog.gov.ru/opendata/7707329152-debtam/"
URL_RE = re.compile(r"https://[A-Za-z0-9./_-]+/7707329152-debtam/data-\d{8}-structure-\d+\.zip")
DOC = re.compile(r'<Документ [^>]*?ДатаСост="([^"]+)"[^>]*>(.*?)</Документ>', re.S)
INN_RE = re.compile(r'ИННЮЛ="(\d{10})"')
ROW = re.compile(r'<СведНедоим [^>]*?ОбщСумНедоим="([\d.]+)"')
MAX_ZIP = 400 * 1024 * 1024
SOURCE_NAME = "data_nalog"


def parse_documents(xml: str):
    """XML набора -> (инн, сумма в копейках, дата сведений ISO)."""
    for m in DOC.finditer(xml):
        inn = INN_RE.search(m.group(2))
        if not inn:
            continue
        total = sum((Decimal(x) for x in ROW.findall(m.group(2))), Decimal(0))
        yield inn.group(1), int(total * 100), datetime.strptime(m.group(1), "%d.%m.%Y").date().isoformat()


def resolve_url(http: PoliteHttp) -> str:
    forced = config.env("FNS_DEBT_URL")
    if forced:
        return forced
    r = http.request("GET", PASSPORT, max_bytes=2 * 1024 * 1024)
    if r.status_code != 200:
        raise SourceUnavailable(f"http_{r.status_code}", "паспорт набора debtam недоступен")
    m = URL_RE.search(r.text)
    if not m:
        raise SourceUnavailable("passport_changed", "в паспорте набора не найдена ссылка на данные")
    return m.group(0)


class DebtIndex:
    def __init__(self, store: Store, http_factory):
        self.store = store
        self.http_factory = http_factory
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def state(self) -> dict:
        return self.store.meta_get("debt_state") or {"state": "none"}

    def _set(self, **kw):
        self.store.meta_put("debt_state", kw)

    # --- построение ---
    def build(self):
        http: PoliteHttp = self.http_factory()
        prev = self.state()
        try:
            url = resolve_url(http)
            if prev.get("state") == "ready" and prev.get("url") == url:
                self._set(**{**prev, "checked_at": time.time()})
                return
            self._set(state="building", started_at=time.time(), url=url, prev=prev if prev.get("state") == "ready" else None)
            zpath = config.db_path().parent / "opendata" / "debtam.zip"
            http.download(url, zpath, MAX_ZIP)
            n, as_of = self._load(zpath)
            zpath.unlink(missing_ok=True)
            self._set(state="ready", url=url, as_of=as_of, records=n, built_at=time.time(), checked_at=time.time())
            log.info("debt index built records=%s", n)
        except SourceUnavailable as e:
            log.warning("debt index failed reason=%s", e.reason)
            self._set(state="error", reason=e.reason, detail=e.detail, failed_at=time.time(),
                      **({"stale_from": prev} if prev.get("state") == "ready" else {}))
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            log.warning("debt index failed error=%s", type(e).__name__)
            self._set(state="error", reason="bad_archive", detail=type(e).__name__, failed_at=time.time())

    def _load(self, zpath: Path):
        count, as_of = 0, None
        with self.store.conn() as c, zipfile.ZipFile(zpath) as z:
            c.execute("DROP TABLE IF EXISTS debt_new")
            c.execute("CREATE TABLE debt_new(inn TEXT PRIMARY KEY, total_kop INTEGER NOT NULL) WITHOUT ROWID")
            for info in z.infolist():
                if not info.filename.lower().endswith(".xml"):
                    continue
                rows = []
                for inn, kop, d in parse_documents(z.read(info).decode("utf-8")):
                    rows.append((inn, kop))
                    as_of = max(as_of or d, d)
                c.executemany("INSERT OR REPLACE INTO debt_new VALUES(?,?)", rows)
                count += len(rows)
            if count == 0:
                raise ValueError("пустой набор")
            c.execute("DROP TABLE debt")
            c.execute("ALTER TABLE debt_new RENAME TO debt")
        return count, as_of

    def ensure(self, block: bool = False):
        """Запустить построение/обновление, если индекса нет или он старше FNS_DEBT_REFRESH (по умолчанию 7 суток)."""
        st = self.state()
        refresh = config.float_env("FNS_DEBT_REFRESH", 7 * 86400.0)
        fresh = st.get("state") == "ready" and time.time() - st.get("checked_at", 0) < refresh
        retry_ok = st.get("state") != "error" or time.time() - st.get("failed_at", 0) > 900
        if fresh or not retry_ok:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if block:
                self.build()
                return
            self._thread = threading.Thread(target=self.build, name="debt-index", daemon=True)
            self._thread.start()

    def wait(self, timeout: float = 60.0):
        t = self._thread
        if t:
            t.join(timeout)

    # --- запрос ---
    @staticmethod
    def _usable_as_of(st: dict):
        """Дата сведений, если в базе есть пригодная таблица (текущая или предыдущая, пока строится новая)."""
        if st.get("state") == "ready":
            return st.get("as_of")
        old = st.get("prev") or st.get("stale_from")
        return old.get("as_of") if old else None

    def lookup(self, inn: str) -> dict:
        if len(inn) != 10:
            raise SourceUnavailable("not_covered", "набор содержит только организации, ИП в нём нет")
        st = self.state()
        as_of = self._usable_as_of(st)
        self.ensure()
        if as_of is None:
            if st.get("state") == "error":
                raise SourceUnavailable("index_error", f"индекс недоимки не построен: {st.get('reason')}")
            raise SourceUnavailable("index_building", "индекс недоимки строится, повторите позже")
        with self.store.conn() as c:
            r = c.execute("SELECT total_kop FROM debt WHERE inn=?", (inn,)).fetchone()
        return {"has": r is not None, "amount_kop": r[0] if r else 0, "as_of": as_of}
