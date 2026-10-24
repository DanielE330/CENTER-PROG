"""Клиент банка: OAuth2 client_credentials, остаток по счетам."""
import httpx

from . import base
from .base import SourceError


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return base.make_http(base.env("BANK_URL", "http://localhost:8006"), transport)


def token(http: httpx.Client) -> str:
    r = base.request(http, "POST", "/bank/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": base.env("BANK_CLIENT_ID", "demo-client"),
        "client_secret": base.env("BANK_CLIENT_SECRET", "demo-secret"),
    })
    try:
        return r.json()["access_token"]
    except (ValueError, KeyError) as e:
        raise SourceError(f"банк: нет access_token: {e}") from e


def balance(http: httpx.Client, company: str) -> dict:
    h = {"Authorization": "Bearer " + token(http)}
    try:
        accs = base.request(http, "GET", "/bank/v1/accounts", params={"company": company}, headers=h).json()["accounts"]
        return parse_balance(accs)
    except (ValueError, KeyError) as e:
        raise SourceError(f"банк: некорректный ответ: {e}") from e


def parse_balance(accounts: list[dict]) -> dict:
    """Сумма остатков (в копейках, строкой) и последняя дата."""
    from decimal import Decimal
    total, dates = Decimal(0), []
    for a in accounts:
        b = a["balance"]
        total += Decimal(str(b["amount"]))
        dates.append(b["as_of"])
    return {
        "balance_kop": str(int((total * 100).to_integral_value())), "currency": "RUB",
        "last_date": max(dates) if dates else None,
        "accounts": [{"id": a["id"], "number": a.get("number"), "balance_kop": str(int((Decimal(str(a["balance"]["amount"])) * 100).to_integral_value())),
                      "as_of": a["balance"]["as_of"]} for a in accounts],
    }
