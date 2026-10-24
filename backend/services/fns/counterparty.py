"""Проверка контрагента: источники по режиму, кэш с TTL (сутки), просроченный кэш с пометкой stale, риск, батч."""
import concurrent.futures as cf
import re
import time
from datetime import datetime, timezone

from . import config
from .deps import Deps
from .errors import SourceUnavailable
from .logsafe import log
from .sources import egrul, fedresurs, sim
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


def risk(rec: dict) -> tuple[str, list]:
    """Простая прозрачная оценка по известным признакам; неизвестные (null) не учитываются."""
    reasons, score = [], 0

    def add(code, w):
        nonlocal score
        reasons.append(code)
        score += w
    if (rec["bankruptcy"] or {}).get("flag"):
        add("bankruptcy", 3)
    if rec["status"] in ("liquidating", "liquidated"):
        add(rec["status"], 3)
    if rec["status"] == "not_found":
        add("not_in_registry", 2)
    if rec["unreliable"]:
        add("unreliable_data", 2)
    if rec["disqualified"]:
        add("disqualified_head", 2)
    td = rec["tax_debt"]
    if td.get("has"):
        add("tax_debt_large" if (td.get("amount_kop") or 0) >= HIGH_DEBT_KOP else "tax_debt", 2 if (td.get("amount_kop") or 0) >= HIGH_DEBT_KOP else 1)
    if rec["mass_address"]:
        add("mass_address", 1)
    return ("high" if score >= 3 else "medium" if score >= 1 else "low"), reasons


def _empty(inn: str) -> dict:
    return {"inn": inn, "status": "unknown", "registered_at": None, "mass_address": None, "unreliable": None,
            "tax_debt": {"has": None, "amount_kop": None, "as_of": None}, "disqualified": None,
            "bankruptcy": {"flag": None, "source": None}}


def _finish(rec: dict, results: list[Res], primary: str) -> dict:
    used = [r for r in results if r.ok]
    rec["risk_level"], rec["risk_reasons"] = risk(rec) if used else ("unknown", [])
    rec["source"] = primary if used else "unavailable"
    rec["sources"] = {r.name: ("ok" if r.ok else f"unavailable:{r.reason}") for r in results}
    rec["unavailable_fields"] = sorted(k for k, v in (
        ("status", rec["status"] == "unknown"), ("registered_at", rec["registered_at"] is None),
        ("mass_address", rec["mass_address"] is None), ("unreliable", rec["unreliable"] is None),
        ("tax_debt", rec["tax_debt"]["has"] is None), ("disqualified", rec["disqualified"] is None),
        ("bankruptcy", rec["bankruptcy"]["flag"] is None)) if v)
    stamps = [r.fetched_at for r in used if r.fetched_at]
    rec["checked_at"] = iso(min(stamps)) if stamps else iso(time.time())
    rec["stale"] = any(r.stale for r in used)
    rec["cached"] = bool(used) and all(r.cached for r in used)
    unavailable = [r for r in results if not r.ok]
    if unavailable and not used:
        rec["reason"] = unavailable[0].reason
        rec["message"] = unavailable[0].detail
    return rec


def check_sim(deps: Deps, inn: str, force=False) -> dict:
    res = cached_call(deps, "fns_sim", inn, lambda: sim.counterparty(deps.sim(), inn), force)
    rec = _empty(inn)
    if res.ok:
        d = res.data
        if not d.get("found"):
            rec["status"] = "not_found"
        else:
            rec.update(status=d["status"], registered_at=d["registered_at"], mass_address=d["mass_address"],
                       unreliable=d["unreliable"], disqualified=d["disqualified"],
                       tax_debt=dict(d["tax_debt"]), bankruptcy=dict(d["bankruptcy"]))
    out = _finish(rec, [res], "fns_sim")
    out["mode"] = "sim"
    return out


def check_live(deps: Deps, inn: str, force=False) -> dict:
    rec = _empty(inn)
    if not valid_inn_checksum(inn):
        rec["status"] = "invalid_inn"
        out = _finish(rec, [], "unavailable")
        out.update(mode="live", reason="inn_checksum_invalid", message="контрольное число ИНН неверно: в реестры не обращаемся",
                   sources={})
        return out
    results = []
    eg = cached_call(deps, "egrul", inn, lambda: egrul.fetch(deps.http, inn), force)
    results.append(eg)
    if eg.ok:
        d = eg.data
        if not d["found"]:
            rec["status"] = "not_found"
        else:
            rec["status"] = "liquidated" if d["terminated"] else "active"
            rec["registered_at"] = d["registered_at"]
    dt = Res("data_nalog")
    try:
        deps.debt.ensure()
        v = deps.debt.lookup(inn)
        dt = Res("data_nalog", v, fetched_at=time.time())
        rec["tax_debt"] = {"has": v["has"], "amount_kop": v["amount_kop"], "as_of": v["as_of"]}
    except SourceUnavailable as e:
        dt = Res("data_nalog", reason=e.reason, detail=e.detail)
    results.append(dt)
    fr = cached_call(deps, "fedresurs", inn, lambda: fedresurs.fetch(deps.http, inn), force)
    results.append(fr)
    if fr.ok:
        rec["bankruptcy"] = {"flag": fr.data["flag"], "source": "fedresurs"}
    pbr = Res("pb_nalog", reason="captcha_required", detail="автоматический доступ не используется")
    results.append(pbr)
    primary = "egrul" if eg.ok else "data_nalog"
    out = _finish(rec, results, primary)
    out["mode"] = "live"
    return out


def check(deps: Deps, inn: str, force=False) -> dict:
    m = config.mode()
    if m == "live":
        return check_live(deps, inn, force)
    rec = check_sim(deps, inn, force)
    # auto: имитатор знает только ИНН датасета; ИНН вне его — настоящий, проверяем по открытым реестрам
    if m == "auto" and rec["status"] == "not_found":
        return check_live(deps, inn, force)
    return rec


def batch(deps: Deps, inns: list[str], force=False, workers: int = 4, timeout: float | None = None) -> dict:
    timeout = config.float_env("FNS_BATCH_TIMEOUT", 25.0) if timeout is None else timeout
    uniq = list(dict.fromkeys(inns))
    ex = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fns-batch")
    futs = {i: ex.submit(check, deps, i, force) for i in uniq}
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
            "partial": bool(errors), "mode": config.mode()}
