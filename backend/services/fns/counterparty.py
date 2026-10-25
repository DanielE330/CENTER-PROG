"""Проверка контрагента: источники по режиму, кэш с TTL (сутки), просроченный кэш с пометкой stale, риск, батч."""
import concurrent.futures as cf
import re
import time
from datetime import datetime, timezone

from . import config
from .deps import Deps
from .errors import SourceUnavailable
from .logsafe import log
from .sources import egrul, fedresurs
from .store import inn_key

INN_RE = re.compile(r"^(\d{10}|\d{12})$")
BATCH_MAX = 50
HIGH_DEBT_KOP = 50_000_000  # 500 тыс. руб.


def valid_inn_checksum(inn: str) -> bool:
    d = [int(c) for c in inn]
    if len(d) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return sum(a * b for a, b in zip(d, w)) % 11 % 10 == d[9]
    if len(d) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        return (sum(a * b for a, b in zip(d, w1)) % 11 % 10 == d[10]
                and sum(a * b for a, b in zip(d, w2)) % 11 % 10 == d[11])
    return False


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Res:
    """Результат одного источника: данные или причина недоступности, плюс признаки кэша."""

    def __init__(self, name, data=None, reason=None, detail="", stale=False, cached=False, fetched_at=None):
        self.name, self.data, self.reason, self.detail = name, data, reason, detail
        self.stale, self.cached, self.fetched_at = stale, cached, fetched_at

    @property
    def ok(self):
        return self.data is not None


def cached_call(deps: Deps, name: str, inn: str, fetch, force: bool = False) -> Res:
    """Свежий кэш -> он; иначе запрос; при недоступности источника -> просроченный кэш (stale) или причина."""
    key = inn_key(inn)
    hit = deps.store.cache_get(name, key)
    ttl = config.cache_ttl()
    now = time.time()
    if hit and not force and now - hit[1] < ttl:
        return Res(name, hit[0], cached=True, fetched_at=hit[1])
    try:
        data = fetch()
    except SourceUnavailable as e:
        log.warning("source=%s unavailable reason=%s", name, e.reason)
        if hit:
            return Res(name, hit[0], stale=True, cached=True, fetched_at=hit[1], reason=e.reason, detail=e.detail)
        return Res(name, reason=e.reason, detail=e.detail)
    deps.store.cache_put(name, key, data, now)
    return Res(name, data, fetched_at=now)


# Настройки компании (что проверять и что считать риском) приходят от api в теле запроса; без них — значения по умолчанию.
DEFAULT_SETTINGS = {
    "sources": {"egrul": True, "data_nalog": True, "fedresurs": True},
    "risk": {"bankruptcy": True, "liquidation": True, "not_in_registry": False, "unreliable": True, "disqualified": True,
             "tax_debt": True, "mass_address": True, "large_debt_kop": HIGH_DEBT_KOP},
}


def settings_of(raw: dict | None) -> dict:
    """Слияние с умолчаниями: неизвестные ключи отбрасываются, типы приводятся."""
    raw = raw or {}
    out = {"sources": dict(DEFAULT_SETTINGS["sources"]), "risk": dict(DEFAULT_SETTINGS["risk"])}
    for group in ("sources", "risk"):
        for k, v in (raw.get(group) or {}).items():
            if k not in out[group]:
                continue
            if k == "large_debt_kop":
                try:
                    out[group][k] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
            else:
                out[group][k] = bool(v)
    return out


def risk(rec: dict, settings: dict | None = None) -> tuple[str, list]:
    """Простая прозрачная оценка по известным признакам; неизвестные (null) и выключенные в настройках не учитываются."""
    on = settings_of(settings)["risk"]
    reasons, score = [], 0

    def add(code, w):
        nonlocal score
        reasons.append(code)
        score += w
    if on["bankruptcy"] and (rec["bankruptcy"] or {}).get("flag"):
        add("bankruptcy", 3)
    if on["liquidation"] and rec["status"] in ("liquidating", "liquidated"):
        add(rec["status"], 3)
    if on["not_in_registry"] and rec["status"] == "not_found":
        add("not_in_registry", 2)
    if on["unreliable"] and rec["unreliable"]:
        add("unreliable_data", 2)
    if on["disqualified"] and rec["disqualified"]:
        add("disqualified_head", 2)
    td = rec["tax_debt"]
    if on["tax_debt"] and td.get("has"):
        large = (td.get("amount_kop") or 0) >= on["large_debt_kop"]
        add("tax_debt_large" if large else "tax_debt", 2 if large else 1)
    if on["mass_address"] and rec["mass_address"]:
        add("mass_address", 1)
    return ("high" if score >= 3 else "medium" if score >= 1 else "low"), reasons


def _empty(inn: str) -> dict:
    return {"inn": inn, "status": "unknown", "registered_at": None, "mass_address": None, "unreliable": None,
            "tax_debt": {"has": None, "amount_kop": None, "as_of": None}, "disqualified": None,
            "bankruptcy": {"flag": None, "source": None}}


def _finish(rec: dict, results: list[Res], primary: str, settings: dict | None = None) -> dict:
    used = [r for r in results if r.ok]
    rec["risk_level"], rec["risk_reasons"] = risk(rec, settings) if used else ("unknown", [])
    rec["source"] = primary if used else "unavailable"
    rec["sources"] = {r.name: ("ok" if r.ok else "off" if r.reason == "disabled" else f"unavailable:{r.reason}") for r in results}
    rec["unavailable_fields"] = sorted(k for k, v in (
        ("status", rec["status"] == "unknown"), ("registered_at", rec["registered_at"] is None),
        ("mass_address", rec["mass_address"] is None), ("unreliable", rec["unreliable"] is None),
        ("tax_debt", rec["tax_debt"]["has"] is None), ("disqualified", rec["disqualified"] is None),
        ("bankruptcy", rec["bankruptcy"]["flag"] is None)) if v)
    stamps = [r.fetched_at for r in used if r.fetched_at]
    rec["checked_at"] = iso(min(stamps)) if stamps else iso(time.time())
    rec["stale"] = any(r.stale for r in used)
    rec["cached"] = bool(used) and all(r.cached for r in used)
    unavailable = [r for r in results if not r.ok and r.reason != "disabled"]
    if unavailable and not used:
        rec["reason"] = unavailable[0].reason
        rec["message"] = unavailable[0].detail
    return rec


def check(deps: Deps, inn: str, force=False, settings: dict | None = None) -> dict:
    """Проверка по открытым реестрам ФНС и ЕФРСБ; источники, выключенные в настройках компании, не запрашиваются."""
    cfg = settings_of(settings)
    use = cfg["sources"]
    rec = _empty(inn)
    if not valid_inn_checksum(inn):
        rec["status"] = "invalid_inn"
        out = _finish(rec, [], "unavailable", cfg)
        out.update(reason="inn_checksum_invalid", message="контрольное число ИНН неверно: в реестры не обращаемся", sources={})
        return out
    off = lambda name: Res(name, reason="disabled", detail="выключено в настройках")  # noqa: E731
    results = []
    eg = cached_call(deps, "egrul", inn, lambda: egrul.fetch(deps.http, inn), force) if use["egrul"] else off("egrul")
    results.append(eg)
    if eg.ok:
        d = eg.data
        if not d["found"]:
            rec["status"] = "not_found"
        else:
            rec["status"] = "liquidated" if d["terminated"] else "active"
            rec["registered_at"] = d["registered_at"]
    dt = off("data_nalog")
    if use["data_nalog"]:
        try:
            deps.debt.ensure()
            v = deps.debt.lookup(inn)
            dt = Res("data_nalog", v, fetched_at=time.time())
            rec["tax_debt"] = {"has": v["has"], "amount_kop": v["amount_kop"], "as_of": v["as_of"]}
        except SourceUnavailable as e:
            dt = Res("data_nalog", reason=e.reason, detail=e.detail)
    results.append(dt)
    fr = cached_call(deps, "fedresurs", inn, lambda: fedresurs.fetch(deps.http, inn), force) if use["fedresurs"] else off("fedresurs")
    results.append(fr)
    if fr.ok:
        rec["bankruptcy"] = {"flag": fr.data["flag"], "source": "fedresurs"}
    results.append(Res("pb_nalog", reason="captcha_required", detail="автоматический доступ не используется"))
    primary = "egrul" if eg.ok else "data_nalog"
    out = _finish(rec, results, primary, cfg)
    out["exported_from"] = "fns"  # сведения выгружены из открытых источников ФНС (и ЕФРСБ), а не введены вручную
    return out


def batch(deps: Deps, inns: list[str], force=False, workers: int = 4, timeout: float | None = None,
          settings: dict | None = None) -> dict:
    timeout = config.float_env("FNS_BATCH_TIMEOUT", 25.0) if timeout is None else timeout
    uniq = list(dict.fromkeys(inns))
    ex = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fns-batch")
    futs = {i: ex.submit(check, deps, i, force, settings) for i in uniq}
    done, pending = cf.wait(list(futs.values()), timeout=timeout)
    ex.shutdown(wait=False, cancel_futures=True)  # начатые запросы дойдут и попадут в кэш
    items, errors = {}, []
    for i, f in futs.items():
        if f in done:
            try:
                items[i] = f.result()
            except Exception as e:  # noqa: BLE001
                log.warning("batch item failed error=%s", type(e).__name__)
                errors.append({"inn": i, "error": "internal_error"})
        else:
            errors.append({"inn": i, "error": "timeout"})
    return {"requested": len(uniq), "items": [items[i] for i in uniq if i in items], "errors": errors,
            "partial": bool(errors), "settings": settings_of(settings)}
