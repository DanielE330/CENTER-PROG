"""Зависимости сервиса: хранилище, HTTP-клиенты, индекс открытых данных. Подменяются в тестах через app.state."""
import httpx

from . import config
from .http import PoliteHttp
from .sources.debt import DebtIndex
from .store import Store


class Deps:
    def __init__(self, store: Store | None = None, public_transport: httpx.BaseTransport | None = None,
                 sim_transport: httpx.BaseTransport | None = None, gov_transport: httpx.BaseTransport | None = None,
                 http: PoliteHttp | None = None):
        self.store = store or Store()
        self.public_transport = public_transport
        self.sim_transport = sim_transport
        self.gov_transport = gov_transport
        self._http = http
        self.debt = DebtIndex(self.store, lambda: self.http)

    @property
    def http(self) -> PoliteHttp:
        if self._http is None:
            self._http = PoliteHttp(transport=self.public_transport)
        return self._http

    def sim(self) -> httpx.Client:
        from .sources import sim
        return sim.client(self.sim_transport)

    def gov(self) -> httpx.Client:
        return httpx.Client(base_url=config.gov_url(), transport=self.gov_transport, timeout=6.0,
                            headers={"User-Agent": config.user_agent()})
