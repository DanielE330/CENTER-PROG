"""Клиент внутренних сервисов: forecast, planfact, ledger. Суммы приходят копейками (числом или строкой)."""
from dataclasses import dataclass
from datetime import date

import httpx

from . import config

HEADERS = {"x-actor": "notifier"}


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
