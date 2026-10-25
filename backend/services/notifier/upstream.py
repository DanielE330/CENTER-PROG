"""Клиент внутренних сервисов: forecast, planfact, ledger и почтовый шлюз notify. Суммы приходят копейками (числом или строкой)."""
import base64
import logging
from dataclasses import dataclass
from datetime import date

import httpx

from . import config

HEADERS = {"x-actor": "notifier"}
log = logging.getLogger("notifier")


class UpstreamError(Exception):
    def __init__(self, service: str, status: int, message: str = ""):
        super().__init__(f"{service}: {status} {message}".strip())
        self.service, self.status = service, status


@dataclass
class Tx:
    date: date
    amount: int
    counterparty: str
    category: str
    balance: int | None


def kop(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class Upstream:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60, headers=HEADERS, transport=self.transport)

    async def _req(self, service: str, method: str, url: str, **kw) -> dict:
        try:
            async with self.client() as c:
                r = await c.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise UpstreamError(service, 503, type(e).__name__) from e
        if r.status_code >= 400:
            raise UpstreamError(service, r.status_code, r.text[:200])
        return r.json() if r.content else {}

    async def audit(self, cid: str, actor: str, action: str, obj: str = "") -> None:
        """Запись в журнал действий компании (ledger audit). Сбой журнала не должен ломать действие пользователя."""
        try:
            await self._req("ledger", "POST", f"{config.ledger_url()}/internal/v1/companies/{cid}/audit",
                            json={"action": action, "object_id": obj}, headers={"x-actor": actor})
        except UpstreamError:
            pass

    async def bot_tokens(self) -> list[dict] | None:
        """Токены собственных ботов компаний [{company_id, token, bot_username}] из ledger (читать их может только svc:notifier).
        None — ledger недоступен: запущенные боты не трогаем."""
        try:
            r = await self._req("ledger", "GET", f"{config.ledger_url()}/internal/v1/integrations/telegram/credentials", headers={"x-actor": "svc:notifier"})
        except UpstreamError as e:
            log.warning("токены ботов компаний недоступны: %s", e.status)
            return None
        return [i for i in r.get("items", []) if isinstance(i, dict) and i.get("company_id") and i.get("token")]

    async def report_bot(self, cid: str, ok: bool, error: str = "", username: str = "") -> None:
        """Результат проверки бота компании в статус подключения (ledger). Сбой не должен ломать работу бота."""
        body: dict = {"ok": ok}
        if error:
            body["error"] = error
        if username:
            body["bot_username"] = username
        try:
            await self._req("ledger", "PUT", f"{config.ledger_url()}/internal/v1/companies/{cid}/integrations/telegram/status", json=body, headers={"x-actor": "svc:notifier"})
        except UpstreamError:
            pass

    # ---------- почта (notify) ----------
    async def mail(self, cid: str, kind: str, key: str, subject: str, html: str, path: str = "/",
                   png: bytes | None = None, chart_alt: str = "", xlsx: bytes | None = None, xlsx_name: str = "") -> int:
        """Письмо подписчикам компании; возвращает число получателей (повтор с тем же key письма не дублирует).
        Недоступная почта не мешает Telegram: тогда 0."""
        if not config.notify_url():
            return 0
        body = {"kind": kind, "key": key, "subject": subject, "html": html, "path": path}
        if png:
            body |= {"chart_png_b64": base64.b64encode(png).decode(), "chart_alt": chart_alt or "График"}
        if xlsx:
            body |= {"xlsx_b64": base64.b64encode(xlsx).decode(), "xlsx_name": xlsx_name or "otchet.xlsx"}
        try:
            r = await self._req("notify", "POST", f"{config.notify_url()}/internal/v1/companies/{cid}/broadcast", json=body)
        except UpstreamError as e:
            log.warning("почта %s %s: %s", kind, cid, e.status)
            return 0
        return int(r.get("recipients") or 0)

    async def mail_recipients(self, cid: str, kind: str) -> int:
        """Сколько участников получают этот вид рассылки письмом (alerts — разрыв и пропущенные платежи, reports — отчёты)."""
        if not config.notify_url():
            return 0
        try:
            r = await self._req("notify", "GET", f"{config.notify_url()}/internal/v1/companies/{cid}/subscribers")
        except UpstreamError:
            return 0
        return int(r.get("alerts" if kind in ("gap", "missed") else "reports") or 0)

    async def mail_companies(self) -> list[str]:
        """Компании, где кто-то получает рассылки письмом: их обходим, даже если Telegram не подключён."""
        if not config.notify_url():
            return []
        try:
            r = await self._req("notify", "GET", f"{config.notify_url()}/internal/v1/companies-with-subscribers")
        except UpstreamError:
            return []
        return [c for c in r.get("items", []) if isinstance(c, str)]

    async def company(self, cid: str) -> dict:
        return await self._req("ledger", "GET", f"{config.ledger_url()}/internal/v1/companies/{cid}")

    async def forecast(self, cid: str) -> dict:
        """Последний базовый прогноз; если его ещё нет — строим на 30 дней."""
        base = f"{config.forecast_url()}/internal/v1/companies/{cid}/forecasts"
        try:
            return await self._req("forecast", "GET", f"{base}/latest")
        except UpstreamError as e:
            if e.status != 404:
                raise
        return await self._req("forecast", "POST", base, json={"horizon_days": 30})

    async def plan_fact(self, cid: str, days: int = 90) -> dict:
        return await self._req("planfact", "GET", f"{config.planfact_url()}/internal/v1/companies/{cid}/plan-fact", params={"days": days})

    async def transactions(self, cid: str) -> list[Tx]:
        snap = await self._req("ledger", "GET", f"{config.ledger_url()}/internal/v1/companies/{cid}/snapshot")
        out = []
        for t in snap.get("transactions", []):
            try:
                out.append(Tx(date.fromisoformat(t["date"]), kop(t["amount"]), t.get("counterparty", ""), t.get("category", ""),
                              kop(t["balance"]) if t.get("balance") is not None else None))
            except (KeyError, ValueError):
                continue
        out.sort(key=lambda t: t.date)
        return out
