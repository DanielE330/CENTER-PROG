"""Настройки из окружения (читаются при каждом обращении, чтобы тесты могли их менять)."""
import os
from pathlib import Path

PROJECT_URL = "https://github.com/DanielE330/CI-hack"


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def mode() -> str:
    """sim — имитатор; live — открытые реестры; auto — имитатор для ИНН из датасета (они выдуманные), реестры для остальных
    (настоящие ИНН, например контрагенты из 1С). У каждого результата свой `mode`: sim или live."""
    m = env("FNS_MODE", "sim").lower()
    return m if m in ("sim", "live", "auto") else "sim"


def cache_ttl() -> float:
    try:
        return float(env("FNS_CACHE_TTL", "86400"))
    except ValueError:
        return 86400.0


def db_path() -> Path:
    p = env("FNS_DB")
    return Path(p) if p else Path(__file__).resolve().parents[3] / "data" / "run" / "fns.db"


def gov_url() -> str:
    return env("GOV_URL", "http://127.0.0.1:8003").rstrip("/")


def sim_url() -> str:
    return env("FNS_SIM_URL", "http://127.0.0.1:8016").rstrip("/")


def sim_token() -> str:
    return env("FNS_SIM_TOKEN", "fns-sim-demo")


def contact() -> str:
    return env("FNS_CONTACT", PROJECT_URL)


def user_agent() -> str:
    # честный User-Agent: имя проекта и контакт; не притворяемся браузером
    return f"CI-hack-fns/0.1 (+{contact()}; cash-forecast research; read-only; polite)"


def min_interval() -> float:
    try:
        return float(env("FNS_MIN_INTERVAL", "1.0"))
    except ValueError:
        return 1.0


def float_env(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default
