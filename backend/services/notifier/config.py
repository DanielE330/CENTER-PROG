"""Настройки сервиса уведомлений. Токен бота берётся только из окружения и в логи не пишется."""
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def db_path() -> str:
    return env("NOTIFIER_DB", "../data/notifier.db")


def bot_token() -> str:
    return env("TELEGRAM_BOT_TOKEN")


def forecast_url() -> str:
    return env("FORECAST_URL", "http://127.0.0.1:3003")


def planfact_url() -> str:
    return env("PLANFACT_URL", "http://127.0.0.1:3004")


def ledger_url() -> str:
    return env("LEDGER_URL", "http://127.0.0.1:3002")


def public_url() -> str:
    return env("PUBLIC_URL", "http://localhost:3000").rstrip("/")


DEFAULT_TZ = "Europe/Moscow"
LINK_TTL = 15 * 60


def tz_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def now_in(tz: ZoneInfo) -> datetime:
    """Текущее время в поясе пользователя. NOTIFIER_TODAY подменяет только дату (демо-данные заканчиваются в прошлом)."""
    now = datetime.now(tz)
    fixed = env("NOTIFIER_TODAY")
    if fixed:
        d = date.fromisoformat(fixed)
        now = now.replace(year=d.year, month=d.month, day=d.day)
    return now


def hermes_url() -> str:
    """База OpenAI-совместимого API (например http://hermes:8000/v1). Пусто — консультант выключен, работают правила."""
    return env("HERMES_URL").rstrip("/")


def hermes_model() -> str:
    return env("HERMES_MODEL", "NousResearch/Hermes-3-Llama-3.1-8B")


def hermes_key() -> str:
    return env("HERMES_API_KEY")


def hermes_timeout() -> float:
    return float(env("HERMES_TIMEOUT", "45"))
