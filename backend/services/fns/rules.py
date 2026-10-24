"""Налоговые события на год по правилам (сроки, без расчёта сумм) и сверка с окном календаря ФНС."""
from datetime import date

from .calendar_src import Workdays, site_window
from .deps import Deps
from .rules_data import MAX_YEAR, MIN_YEAR, REGIMES, rules_for

TITLES = {
    "enp": "ЕНП: НДФЛ и страховые взносы за работников",
    "usn_advance": "Аванс по УСН",
    "usn_annual": "Налог по УСН за год",
    "nds": "НДС (1/3 за квартал)",
    "profit_advance": "Авансовый платёж по налогу на прибыль",
    "profit_annual": "Налог на прибыль за год",
    "ndfl_ip_advance": "Авансовый платёж по НДФЛ (ИП на ОСНО)",
    "ndfl_ip_annual": "НДФЛ ИП за год",
    "eshn_advance": "Аванс по ЕСХН за полугодие",
    "eshn_annual": "ЕСХН за год",
    "ausn_monthly": "Налог по АУСН за прошлый месяц",
    "npd_monthly": "Налог на профессиональный доход",
    "ip_fixed_contributions": "Фиксированные страховые взносы ИП",
    "ip_contributions_1pct": "Страховые взносы ИП: 1% с дохода свыше 300 тыс. руб.",
}
PERIOD_RU = {"Q1": "I квартал", "H1": "полугодие", "9M": "9 месяцев"}
# вид события -> категория в календаре на сайте ФНС (gov.parsers.nalog.CATEGORIES)
SITE_CATEGORY = {"usn_advance": "usn", "usn_annual": "usn", "nds": "nds", "profit_advance": "profit",
                 "profit_annual": "profit", "ausn_monthly": "ausn"}


class BadParams(ValueError):
    pass


def normalize(inn: str | None, regime: str, year: int, entity: str | None, employees: bool | None):
    if regime not in REGIMES:
        raise BadParams(f"regime: одно из {', '.join(REGIMES)}")
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise BadParams(f"year: от {MIN_YEAR} до {MAX_YEAR}")
    if entity is None:
        entity = "ip" if inn and len(inn) == 12 else "org"
    if entity not in ("org", "ip"):
        raise BadParams("entity: org или ip")
    if regime in ("npd", "patent"):
        entity = "ip"  # патент и НПД доступны только ИП
    return entity, True if employees is None else employees


def build_events(wd: Workdays, regime: str, year: int, entity: str, employees: bool) -> tuple[list, dict]:
    rules, rules_year, exact = rules_for(year)
    events = []

    def add(kind, month, day, y=None, period=None, amount_kop=None, basis="unknown", extra=None):
        y = y or year
        nominal = date(y, month, day)
        due = wd.shift(nominal)
        title = TITLES[kind]
        if period and kind in ("usn_advance", "profit_advance", "ndfl_ip_advance"):
            title += f" за {PERIOD_RU.get(period.split('-')[-1], period)}"
        e = {"id": f"{kind}:{nominal.isoformat()}" + (f":{period}" if period else ""), "kind": kind, "title": title,
             "date": due.isoformat(), "nominal_date": nominal.isoformat(), "shifted": due != nominal,
             "period": period, "amount_kop": amount_kop, "amount_basis": basis, "direction": "out"}
        if extra:
            e.update(extra)
        events.append(e)

    if employees:
        for m in range(1, 13):
            add("enp", m, rules["enp"]["day"], period=f"{year}-{m:02d}")
    if regime in ("usn_income", "usn_income_expense"):
        u = rules["usn"]
        for m, p in u["advance"].items():
            add("usn_advance", m, u["advance_day"], period=f"{year}-{p}")
        am, ad = u["annual"][entity]
        add("usn_annual", am, ad, period=str(year - 1))
    elif regime == "osno":
        n = rules["nds"]
        # I квартал -> апрель, май, июнь; II -> июль-сентябрь; III -> октябрь-декабрь; IV прошлого года -> январь-март
        for q in range(3):
            for i in range(n["installments"]):
                add("nds", 3 * q + 4 + i, n["day"], period=f"{year}-Q{q + 1}", basis="one_third_of_quarter",
                    extra={"installment": i + 1, "of": n["installments"]})
        for i in range(n["installments"]):
            add("nds", 1 + i, n["day"], period=f"{year - 1}-Q4", basis="one_third_of_quarter",
                extra={"installment": i + 1, "of": n["installments"]})
        if entity == "org":
            p = rules["profit"]
            for m, lab in p["advance"].items():
                add("profit_advance", m, p["advance_day"], period=f"{year}-{lab}")
            add("profit_annual", p["annual"][0], p["annual"][1], period=str(year - 1))
        else:
            p = rules["ndfl_ip"]
            for m, lab in p["advance"].items():
                add("ndfl_ip_advance", m, p["advance_day"], period=f"{year}-{lab}")
            add("ndfl_ip_annual", p["annual"][0], p["annual"][1], period=str(year - 1))
    elif regime == "eshn":
        e = rules["eshn"]
        add("eshn_advance", e["advance"][0], e["advance"][1], period=f"{year}-H1")
        add("eshn_annual", *e["annual"][entity], period=str(year - 1))
    elif regime == "ausn":
        for m in range(1, 13):
            add("ausn_monthly", m, rules["ausn"]["day"], period=f"{year - 1}-12" if m == 1 else f"{year}-{m - 1:02d}")
    elif regime == "npd":
        for m in range(1, 13):
            add("npd_monthly", m, rules["npd"]["day"], period=f"{year - 1}-12" if m == 1 else f"{year}-{m - 1:02d}")
    # патент: сроки оплаты зависят от срока действия патента, которого у нас нет; в календарь идут только взносы
    if entity == "ip" and regime != "npd":
        ins = rules["insurance_ip"]
        add("ip_fixed_contributions", *ins["fixed_due"], period=str(year), amount_kop=ins["fixed_kop"],
            basis="statutory_fixed")
        add("ip_contributions_1pct", *ins["one_percent_due"], period=str(year - 1), basis="one_percent_over_threshold",
            extra={"threshold_kop": ins["one_percent_threshold_kop"]})
    events.sort(key=lambda e: (e["date"], e["kind"]))
    meta = {"rules_year": rules_year, "rules_exact": exact, "rules_status": rules["status"],
            "rules_checked_at": rules["checked_at"], "rules_note": rules["note"]}
    return events, meta


def calendar_check(deps: Deps, events: list, year: int) -> dict:
    """Сверка с окном календаря nalog.gov.ru (через gov): matched | diverged | window_unavailable."""
    win = site_window(deps, year)
    if not win or not win[1]:
        return {"status": "window_unavailable", "reason": "gov или сайт ФНС недоступны"}
    site, cov = win
    lo, hi = cov["from"], cov["to"]
    by_date: dict[str, set] = {}
    enp_months = set()
    for s in site:
        by_date.setdefault(s["date"], set()).update(s.get("categories", []))
        if "enp" in s.get("categories", []):
            enp_months.add(s["date"][:7])
    compared = matched = skipped = 0
    diffs = []
    for e in events:
        if not lo <= e["date"] <= hi:
            continue
        cat = SITE_CATEGORY.get(e["kind"])
        if e["kind"] == "enp":
            # платёж 28-го; на сайте есть уведомление об исчисленных суммах (25-е) в том же месяце
            compared += 1
            if e["date"][:7] in enp_months:
                matched += 1
            else:
                diffs.append({"id": e["id"], "date": e["date"], "reason": "в окне нет записи ЕНП за этот месяц"})
            continue
        if not cat:
            skipped += 1
            continue
        compared += 1
        if cat in by_date.get(e["date"], set()):
            matched += 1
        else:
            diffs.append({"id": e["id"], "date": e["date"], "reason": f"на сайте нет записи «{cat}» на эту дату"})
    out = {"window": {"from": lo, "to": hi}, "compared": compared, "matched": matched, "skipped": skipped,
           "diffs": diffs}
    if compared == 0:
        out["status"] = "window_unavailable"
        out["reason"] = "в окне календаря нет сопоставимых событий"
    else:
        out["status"] = "matched" if not diffs else "diverged"
    return out


def obligations(deps: Deps, inn: str | None, regime: str, year: int, entity=None, employees=None, check=True) -> dict:
    entity, employees = normalize(inn, regime, year, entity, employees)
    wd = Workdays(deps)
    events, meta = build_events(wd, regime, year, entity, employees)
    warnings = []
    if wd.source != "gov":
        warnings.append({"code": "calendar_weekends_only",
                         "message": "производственный календарь недоступен: учтены только выходные, праздники не учтены"})
    if not meta["rules_exact"]:
        warnings.append({"code": "rules_not_curated",
                         "message": f"правила для {year} года не заведены, взяты за {meta['rules_year']}: проверить"})
    elif meta["rules_status"] == "verify":
        warnings.append({"code": "rules_verify", "message": "правила года не сверены с сайтом ФНС: проверить на актуальный год"})
    cc = calendar_check(deps, events, year) if check else {"status": "window_unavailable", "reason": "сверка отключена"}
    if cc["status"] == "diverged":
        warnings.append({"code": "calendar_diverged",
                         "message": f"{len(cc['diffs'])} событий не совпали с календарём nalog.gov.ru"})
    return {"inn": inn, "regime": regime, "year": year, "entity": entity, "employees": employees,
            "events": events, "calendar_check": cc["status"], "calendar_details": cc,
            "calendar_source": wd.source, "rules": meta, "warnings": warnings, "source": "rules"}
