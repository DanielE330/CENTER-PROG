"""Инструменты консультанта: функции, которые читают внутренние сервисы по HTTP (forecast, ledger, planfact).
Суммы в ответах инструментов — в рублях (поля *_rub). Записи нет: propose_* только готовят предложение."""
import datetime as dt
import json
import os
import uuid
from collections import defaultdict
from typing import Any

import httpx

MAX_PROPOSALS = 3
MAX_TX_PAGES = 10


def today() -> dt.date:
    return dt.date.today()


def rub(kop: Any) -> int | None:
    try:
        return round(int(kop) / 100)
    except (TypeError, ValueError):
        return None


class ToolError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class Services:
    """HTTP-клиент к внутренним сервисам; адреса читаются из env при создании."""

    def __init__(self, http: httpx.AsyncClient, cid: str, rid: str = "", actor: str = ""):
        self.http, self.cid = http, cid
        self.headers = {k: v for k, v in (("x-request-id", rid), ("x-actor", actor)) if v}
        env = os.environ.get
        self.urls = {
            "forecast": env("FORECAST_URL", "http://127.0.0.1:3003").rstrip("/"),
            "ledger": env("LEDGER_URL", "http://127.0.0.1:3002").rstrip("/"),
            "planfact": env("PLANFACT_URL", "http://127.0.0.1:3004").rstrip("/"),
        }

    async def call(self, service: str, method: str, path: str, **kw) -> Any:
        url = f"{self.urls[service]}/internal/v1/companies/{self.cid}/{path}"
        try:
            r = await self.http.request(method, url, headers=self.headers, **kw)
        except httpx.HTTPError:
            raise ToolError(f"{service}_unavailable", f"Сервис {service} недоступен") from None
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                raise ToolError(err.get("code", "upstream_error"), err.get("message", ""))
            except (ValueError, KeyError, TypeError):
                raise ToolError("upstream_error", f"{service} вернул HTTP {r.status_code}") from None
        if not r.content:  # 201/204 без тела (например, запись в журнал)
            return {}
        try:
            return r.json()
        except ValueError:
            raise ToolError("upstream_error", f"{service} вернул не JSON") from None

    async def latest_forecast(self) -> dict:
        return await self.call("forecast", "GET", "forecasts/latest")


def _items(xs: list[dict]) -> list[dict]:
    return [{"date": x.get("date"), "title": x.get("title"), "amount_rub": rub(x.get("amount")), "overdue": bool(x.get("overdue"))} for x in xs]


def summarize_forecast(run: dict) -> dict:
    r = run["result"]
    risk = r["risk"]
    days = r.get("days") or []
    # уровень none — разрыва в горизонте не ждём: дату пересечения нуля и нехватку не показываем, иначе модель видит противоречие
    calm = risk.get("level") == "none"
    out: dict = {
        "as_of": r.get("as_of"),
        "horizon_days": r.get("horizon"),
        "opening_balance_rub": rub(r.get("opening_balance")),
        "data_stale": bool(run.get("stale")),
        "risk": {
            "level": risk.get("level"),
            "shortfall_probability_pct": round((risk.get("shortfall_probability") or 0) * 100),
            "first_break_date": None if calm else risk.get("first_break_date"),
            "possible_break_date": None if calm else risk.get("possible_break_date"),
            "typical_deficit_rub": None if calm else rub(risk.get("deficit_given_break_median")),
            "max_deficit_p90_rub": rub(risk.get("max_deficit_p90")),
        },
        "warnings": [
            {"code": w.get("code"), "severity": w.get("severity"), "date": w.get("date"), "message": w.get("message"),
             "reasons": _items((w.get("reasons") or [])[:5])}
            for w in (risk.get("warnings") or [])[:6]
        ],
    }
    if days:
        low = min(days, key=lambda d: int(d["p50"]))
        out["lowest_expected_balance"] = {"date": low["date"], "rub": rub(low["p50"])}
        out["end_expected_balance_rub"] = rub(days[-1]["p50"])
        out["weekly_balance_rub"] = [
            {"date": d["date"], "p10": rub(d["p10"]), "p50": rub(d["p50"]), "p90": rub(d["p90"])} for d in days[::7][:14]
        ]
    return out


TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_forecast_risk",
        "description": "Последний прогноз остатка и риск кассового разрыва: остаток на сегодня, самый низкий ожидаемый остаток, "
                       "вероятность разрыва, ближайшая дата разрыва, типичная и максимальная (P90) нехватка, причины (платежи в эти дни).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_upcoming_payments",
        "description": "Ближайшие платежи и поступления (из прогноза) и просроченные платежи. amount_rub: минус — списание, плюс — поступление.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "Горизонт в днях, по умолчанию 14"}}}}},
    {"type": "function", "function": {
        "name": "get_plan_fact",
        "description": "План-факт: сколько платежей оплачено в срок, с отклонением, пропущено; крупнейшие отклонения и пропуски.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 14, "maximum": 365, "description": "Период в днях, по умолчанию 90"}}}}},
    {"type": "function", "function": {
        "name": "run_scenario",
        "description": "Сценарий «что если» без сохранения решения: задержка оплат клиента, падение выручки, рост постоянных расходов, "
                       "разовый расход или поступление. Возвращает риск до и после. Укажите хотя бы одно изменение.",
        "parameters": {"type": "object", "properties": {
            "delay_days": {"type": "integer", "minimum": 0, "maximum": 90, "description": "На сколько дней клиент задерживает оплаты"},
            "delay_counterparty": {"type": "string", "description": "Клиент (по умолчанию самый крупный по поступлениям)"},
            "revenue_drop_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "fixed_cost_growth_pct": {"type": "number", "minimum": 0, "maximum": 300},
            "extra_expense_rub": {"type": "number", "exclusiveMinimum": 0}, "extra_expense_date": {"type": "string", "description": "YYYY-MM-DD"},
            "extra_income_rub": {"type": "number", "exclusiveMinimum": 0}, "extra_income_date": {"type": "string", "description": "YYYY-MM-DD"}}}}},
    {"type": "function", "function": {
        "name": "get_transactions_by_category",
        "description": "Операции из выписки по категориям за период: поступления, списания, число операций, крупнейшие контрагенты. "
                       "Категории: revenue, other_income, suppliers, payroll, rent, taxes, loans, utilities, bank_fees, marketing, transfers, refunds, other_expense.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 7, "maximum": 365, "description": "Период в днях от последней операции, по умолчанию 90"},
            "category": {"type": "string"}, "direction": {"type": "string", "enum": ["in", "out"]}}}}},
    {"type": "function", "function": {
        "name": "get_counterparty_risks",
        "description": "Крупнейшие клиенты и поставщики (выписка и счета 1С) с флагами риска, выгруженными из ФНС: статус в ЕГРЮЛ, "
                       "долг перед бюджетом, уровень риска. Суммы в рублях.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Сколько контрагентов, по умолчанию 10"},
            "direction": {"type": "string", "enum": ["in", "out"], "description": "in — клиенты, out — поставщики"}}}}},
    {"type": "function", "function": {
        "name": "add_expected_payment",
        "description": "Добавить ожидаемый платёж или поступление в план компании сразу (право на запись выдано). "
                       "Используй, только если пользователь прямо попросил добавить; иначе propose_expected_payment.",
        "parameters": {"type": "object", "required": ["direction", "amount_rub", "date", "title"], "properties": {
            "direction": {"type": "string", "enum": ["in", "out"]},
            "amount_rub": {"type": "number", "exclusiveMinimum": 0},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "title": {"type": "string", "maxLength": 200},
            "counterparty": {"type": "string", "maxLength": 200}}}}},
    {"type": "function", "function": {
        "name": "recalc_forecast",
        "description": "Пересчитать прогноз по текущим данным (после добавления платежей). Возвращает новый риск разрыва.",
        "parameters": {"type": "object", "properties": {
            "horizon_days": {"type": "integer", "minimum": 7, "maximum": 90}}}}},
    {"type": "function", "function": {
        "name": "suggest_followups",
        "description": "В конце ответа: 2–3 коротких следующих вопроса, которые пользователь может задать (кнопки под чатом). "
                       "Только вопросы, на которые ты сможешь ответить своими инструментами.",
        "parameters": {"type": "object", "required": ["questions"], "properties": {
            "questions": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 120}}}}}},
    {"type": "function", "function": {
        "name": "get_app_guide",
        "description": "Гид по приложению: экраны (как подписаны в меню), что на них и как сделать типичные действия. "
                       "Вызывай, когда спрашивают «где/как в приложении…».",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Экран или действие: прогноз, выписка, 1С, ФНС, план и факт… Пусто — все экраны"}}}}},
    {"type": "function", "function": {
        "name": "save_strategy_plan",
        "description": "Сохранить стратегию для экрана «План и факт» (режим «План»): цель, шаги с датами и суммами, риски. "
                       "Сохраняется одна активная стратегия компании; данные компании (платежи) не меняет.",
        "parameters": {"type": "object", "required": ["title", "summary", "steps"], "properties": {
            "title": {"type": "string", "maxLength": 120},
            "summary": {"type": "string", "maxLength": 1200, "description": "Суть плана простыми словами: цель и почему так"},
            "goal": {"type": "string", "maxLength": 300, "description": "Измеримая цель: например, не уйти в минус до 30.11 и держать резерв 300 000 ₽"},
            "horizon_days": {"type": "integer", "minimum": 7, "maximum": 365},
            "steps": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "object", "required": ["title"], "properties": {
                "title": {"type": "string", "maxLength": 160},
                "detail": {"type": "string", "maxLength": 600, "description": "Что сделать и как это влияет на разрыв"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "amount_rub": {"type": "number", "description": "Сумма эффекта: плюс — деньги приходят/остаются, минус — уходят"},
                "kind": {"type": "string", "enum": ["income", "expense", "reserve", "negotiation", "tax", "financing", "control"]}}}},
            "risks": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 300}},
            "checkpoints": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 200},
                            "description": "Что смотреть на «Плане и факте», чтобы понять, что план работает"}}}}},
    {"type": "function", "function": {
        "name": "propose_expected_payment",
        "description": "Предложить пользователю добавить ожидаемый платёж или поступление. Ничего не записывает: пользователь увидит карточку и решит сам.",
        "parameters": {"type": "object", "required": ["direction", "amount_rub", "date", "title"], "properties": {
            "direction": {"type": "string", "enum": ["in", "out"], "description": "in — поступление, out — платёж"},
            "amount_rub": {"type": "number", "exclusiveMinimum": 0},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "title": {"type": "string", "maxLength": 200},
            "counterparty": {"type": "string", "maxLength": 200}}}}},
]


def validate_payment(args: dict) -> dict:
    """Проверка параметров предложения; возвращает params (amount_kop строкой). Те же правила, что в ledger и api."""
    direction = args.get("direction")
    if direction not in ("in", "out"):
        raise ToolError("invalid_direction", "direction: in | out")
    try:
        amount = round(float(args.get("amount_rub")) * 100)
    except (TypeError, ValueError):
        raise ToolError("invalid_amount", "amount_rub должна быть положительным числом") from None
    if not 0 < amount < 10**15:
        raise ToolError("invalid_amount", "amount_rub должна быть положительным числом")
    try:
        d = dt.date.fromisoformat(str(args.get("date")))
    except ValueError:
        raise ToolError("invalid_date", "date в формате YYYY-MM-DD") from None
    if not today() - dt.timedelta(days=120) <= d <= today() + dt.timedelta(days=730):
        raise ToolError("invalid_date", "Дата вне допустимого диапазона")
    title = str(args.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ToolError("invalid_title", "Название от 1 до 200 символов")
    cp = str(args.get("counterparty") or "").strip()[:200]
    return {"direction": direction, "amount_kop": str(amount), "date": d.isoformat(), "title": title, "counterparty": cp}


def suggestion_title(params: dict, lang: str = "ru", done: bool = False) -> str:
    """Заголовок карточки: предложение («Добавить …») или сделанное консультантом действие («Добавлен … в план»)."""
    sign = "+" if params["direction"] == "in" else "−"
    amt = f"{round(int(params['amount_kop']) / 100):,}".replace(",", " ")
    d = dt.date.fromisoformat(params["date"])
    if lang == "en":
        return f"{'Added' if done else 'Add'} expected {'income' if params['direction'] == 'in' else 'payment'}: {params['title']}, {sign}{amt} RUB, {d.isoformat()}"
    return f"{'Добавлен' if done else 'Добавить'} ожидаемый {'приход' if params['direction'] == 'in' else 'платёж'}{' в план' if done else ''}: {params['title']}, {sign}{amt} ₽, {d.strftime('%d.%m.%Y')}"


class Toolbox:
    """Инструменты одной компании. `allowed` — инструменты по правам консультанта (None — все, кроме записи: старый
    агент через llm_gateway). Состояние ответа (предложения, сделанные действия, подсказки) можно разделить с пропуском Hermes."""

    def __init__(self, svc: Services, lang: str = "ru", allowed: set[str] | None = None, state=None):
        self.svc, self.lang, self.allowed = svc, lang, allowed
        st = state if state is not None else _State()
        self.proposals: list[dict] = st.proposals
        self.counterparties: set[str] = st.counterparties
        self.used: list[str] = st.used
        self.actions: list[dict] = st.actions
        self.followups: list[str] = st.followups
        self.plan: dict = st.plan

    def specs(self) -> list[dict]:
        """Спецификации инструментов, доступных по правам."""
        return [s for s in TOOL_SPECS if self._ok(s["function"]["name"])]

    def _ok(self, name: str) -> bool:
        if self.allowed is None:
            return name in LEGACY_TOOLS
        return name in self.allowed

    def _remember(self, *names: Any) -> None:
        for n in names:
            if isinstance(n, str) and len(n.strip()) >= 3:
                self.counterparties.add(n.strip())

    async def call(self, name: str, args: dict) -> dict:
        """Результат — JSON-словарь; ошибка сервиса возвращается модели как {"error": ...}, а не бросается."""
        self.used.append(name)
        fn = getattr(self, f"_t_{name}", None)
        if fn is None or name not in {s["function"]["name"] for s in TOOL_SPECS}:
            return {"error": "unknown_tool"}
        if not self._ok(name):
            return {"error": "permission_denied", "message": "Это действие консультанту не разрешено в настройках компании"}
        try:
            return await fn(**{k: v for k, v in (args or {}).items() if v is not None}) if isinstance(args, dict) else {"error": "invalid_arguments"}
        except ToolError as e:
            return {"error": e.code, "message": str(e)}
        except TypeError:
            return {"error": "invalid_arguments"}

    async def _t_get_forecast_risk(self) -> dict:
        out = summarize_forecast(await self.svc.latest_forecast())
        return out

    async def _t_get_upcoming_payments(self, days: int = 14) -> dict:
        run = await self.svc.latest_forecast()
        r = run["result"]
        days = max(1, min(int(days), 90))
        as_of = dt.date.fromisoformat(r["as_of"])
        end = (as_of + dt.timedelta(days=days)).isoformat()
        planned = r.get("planned") or []
        upcoming = sorted((p for p in planned if not p.get("overdue") and p["date"] <= end), key=lambda p: p["date"])
        overdue = [p for p in planned if p.get("overdue")]
        return {"as_of": r["as_of"], "until": end, "upcoming": _items(upcoming[:20]), "overdue": _items(overdue[:20]),
                "overdue_total_rub": sum(rub(p["amount"]) or 0 for p in overdue)}

    async def _t_get_plan_fact(self, days: int = 90) -> dict:
        pf = await self.svc.call("planfact", "GET", "plan-fact", params={"days": max(14, min(int(days), 365))})
        items = pf.get("items") or []
        counts: dict[str, int] = defaultdict(int)
        for i in items:
            counts[i.get("status", "")] += 1

        def row(i):
            return {"date": i.get("date"), "title": i.get("title"), "plan_rub": rub(i.get("plan")), "fact_rub": rub(i.get("fact")),
                    "diff_rub": rub(i.get("diff")), "status": i.get("status")}

        missed = [row(i) for i in items if i.get("status") == "missed"][-8:]
        dev = sorted((i for i in items if i.get("status") == "deviation" and i.get("diff") is not None), key=lambda i: -abs(int(i["diff"])))[:8]
        weeks = [{"week_start": w.get("week_start"), **{k: rub(w.get(k)) for k in ("plan_in", "plan_out", "fact_in", "fact_out")}} for w in (pf.get("weeks") or [])[-6:]]
        return {"as_of": pf.get("as_of"), "days": pf.get("days"), "counts": dict(counts), "missed": missed,
                "largest_deviations": [row(i) for i in dev], "recent_weeks": weeks, "note": pf.get("note")}

    async def _t_run_scenario(self, **a) -> dict:
        sc: dict[str, Any] = {}
        for k, lo, hi, conv in (("delay_days", 0, 90, int), ("revenue_drop_pct", 0, 100, float), ("fixed_cost_growth_pct", 0, 300, float)):
            if k in a:
                try:
                    v = conv(a[k])
                except (TypeError, ValueError):
                    raise ToolError("invalid_arguments", k) from None
                if not lo <= v <= hi:
                    raise ToolError("invalid_arguments", f"{k} вне диапазона {lo}..{hi}")
                sc[k] = v
        if a.get("delay_counterparty"):
            sc["delay_counterparty"] = str(a["delay_counterparty"])[:200]
        for key, kop_key, date_key in (("extra_expense", "extra_expense_kop", "extra_expense_date"), ("extra_income", "extra_income_kop", "extra_income_date")):
            if f"{key}_rub" in a:
                try:
                    kop = round(float(a[f"{key}_rub"]) * 100)
                    d = dt.date.fromisoformat(str(a[date_key]))
                except (TypeError, ValueError, KeyError):
                    raise ToolError("invalid_arguments", f"{key}_rub и {date_key}") from None
                if not 0 < kop < 10**15:
                    raise ToolError("invalid_arguments", f"{key}_rub")
                sc[kop_key], sc[date_key] = kop, d.isoformat()
        if not any(sc.get(k) for k in ("delay_days", "revenue_drop_pct", "fixed_cost_growth_pct", "extra_expense_kop", "extra_income_kop")):
            raise ToolError("empty_scenario", "Задайте хотя бы одно изменение")
        try:
            horizon = int((await self.svc.latest_forecast())["result"]["horizon"])
        except (ToolError, KeyError, TypeError, ValueError):
            horizon = 30
        horizon = max(7, min(horizon, 90))
        res = await self.svc.call("forecast", "POST", "scenarios", json={"horizon_days": horizon, "scenario": sc})
        base, scen = summarize_forecast(res["base"]), summarize_forecast(res["scenario"])
        return {"scenario": sc, "horizon_days": horizon, "base": base["risk"] | {"lowest_expected_balance": base.get("lowest_expected_balance")},
                "with_scenario": scen["risk"] | {"lowest_expected_balance": scen.get("lowest_expected_balance")},
                "delta": {"shortfall_probability_pct_points": round((res.get("delta") or {}).get("shortfall_probability", 0) * 100),
                          "max_deficit_p90_rub_change": rub((res.get("delta") or {}).get("max_deficit_p90"))}}

    async def _t_get_transactions_by_category(self, days: int = 90, category: str = "", direction: str = "") -> dict:
        days = max(7, min(int(days), 365))
        params: dict[str, Any] = {"limit": 200, "sort": "date", "order": "desc"}
        if category:
            params["category"] = str(category)
        if direction in ("in", "out"):
            params["direction"] = direction
        rows: list[dict] = []
        last: dt.date | None = None
        done = False
        for page in range(MAX_TX_PAGES):
            data = await self.svc.call("ledger", "GET", "transactions", params={**params, "offset": page * 200})
            items = data.get("items") or []
            if not items:
                break
            if last is None:
                last = dt.date.fromisoformat(items[0]["date"])
            since = (last - dt.timedelta(days=days)).isoformat()
            for t in items:
                if t["date"] < since:
                    done = True
                    break
                rows.append(t)
            if done or len(items) < 200:
                break
        if last is None:
            return {"days": days, "categories": [], "note": "Операций нет"}
        agg: dict[str, dict] = {}
        for t in rows:
            amt = int(t["amount_kop"])
            c = agg.setdefault(t["category"], {"category": t["category"], "inflow_kop": 0, "outflow_kop": 0, "count": 0, "cp": defaultdict(int)})
            c["inflow_kop" if amt > 0 else "outflow_kop"] += amt
            c["count"] += 1
            c["cp"][t.get("counterparty") or ""] += abs(amt)
        cats = []
        for c in sorted(agg.values(), key=lambda c: -(c["inflow_kop"] - c["outflow_kop"])):
            top = sorted(c["cp"].items(), key=lambda kv: -kv[1])[:3]
            self._remember(*[n for n, _ in top])
            cats.append({"category": c["category"], "inflow_rub": rub(c["inflow_kop"]), "outflow_rub": rub(c["outflow_kop"]),
                         "count": c["count"], "top_counterparties": [{"name": n, "turnover_rub": rub(v)} for n, v in top if n]})
        return {"period_to": last.isoformat(), "days": days, "operations": len(rows), "categories": cats}

    async def _t_get_counterparty_risks(self, limit: int = 10, direction: str = "") -> dict:
        limit = max(1, min(int(limit), 30))
        params: dict[str, Any] = {"limit": limit}
        if direction in ("in", "out"):
            params["direction"] = direction
        data = await self.svc.call("ledger", "GET", "counterparties/top", params=params)
        out = []
        for r in data.get("items") or []:
            risk = r.get("risk") or {}
            self._remember(r.get("name"))
            out.append({"name": r.get("name"), "inn": r.get("inn"), "income_rub": rub(r.get("income_kop")), "expense_rub": rub(r.get("expense_kop")),
                        "expected_in_rub": rub(r.get("expected_in_kop")), "expected_out_rub": rub(r.get("expected_out_kop")),
                        "income_share_pct": round((r.get("income_share") or 0) * 100), "from": r.get("origin") or ["statement"],
                        "fns": {"risk_level": risk.get("risk_level", "not_checked"), "status": risk.get("status"),
                                "tax_debt_rub": rub(risk.get("tax_debt_kop")), "bankruptcy": risk.get("bankruptcy_flag"),
                                "checked_at": risk.get("checked_at")} if risk else {"risk_level": "not_checked"}})
        return {"counterparties": out, "source": "Флаги выгружены из открытых реестров ФНС (ЕГРЮЛ, открытые данные ФНС)"}

    async def _t_add_expected_payment(self, **a) -> dict:
        if len(self.actions) >= MAX_PROPOSALS:
            raise ToolError("too_many_actions", f"Не больше {MAX_PROPOSALS} записей за ответ")
        params = validate_payment(a)
        title = ("ИИ-консультант: " + params["title"])[:200]
        ev = await self.svc.call("ledger", "POST", "expected-events", json={
            "date": params["date"], "amount_kop": params["amount_kop"], "direction": params["direction"], "title": title,
            "counterparty": params["counterparty"], "source": "manual", "confidence": 0.8})
        eid = ev.get("id", "")
        try:  # след в журнале компании: кто и что записал от имени консультанта
            await self.svc.call("ledger", "POST", "audit", json={"action": "advisor.add_payment", "object_id": eid, "outcome": "ok"})
        except ToolError:
            pass
        self.actions.append({"type": "add_expected_payment", "id": eid, "params": params | {"title": title},
                             "title": suggestion_title(params, self.lang, done=True)})
        self._remember(params["counterparty"])
        return {"status": "added", "id": eid, "note": "Платёж добавлен в план. Прогноз пересчитается при следующем расчёте (recalc_forecast)."}

    async def _t_recalc_forecast(self, horizon_days: int | None = None) -> dict:
        if horizon_days is None:
            try:
                horizon_days = int((await self.svc.latest_forecast())["result"]["horizon"])
            except (ToolError, KeyError, TypeError, ValueError):
                horizon_days = 30
        run = await self.svc.call("forecast", "POST", "forecasts", json={"horizon_days": max(7, min(int(horizon_days), 90))})
        self.actions.append({"type": "recalc_forecast", "id": run.get("run_id") or run.get("id", ""), "title": "Прогноз пересчитан" if self.lang == "ru" else "Forecast recalculated"})
        return summarize_forecast(run)

    async def _t_get_app_guide(self, topic: str = "") -> dict:
        from .guide import guide
        return guide(str(topic)[:80])

    async def _t_save_strategy_plan(self, title: str, summary: str, steps: list, goal: str = "", horizon_days: int | None = None,
                                    risks: list | None = None, checkpoints: list | None = None) -> dict:
        def s(v, n):
            return str(v or "").strip()[:n]
        clean = []
        for st in (steps or [])[:10]:
            if not isinstance(st, dict) or not s(st.get("title"), 160):
                continue
            row = {"title": s(st.get("title"), 160), "detail": s(st.get("detail"), 600), "kind": st.get("kind") if st.get("kind") in
                   ("income", "expense", "reserve", "negotiation", "tax", "financing", "control") else "control"}
            try:
                row["due_date"] = dt.date.fromisoformat(str(st["due_date"])).isoformat() if st.get("due_date") else None
            except ValueError:
                row["due_date"] = None
            try:
                row["amount_rub"] = round(float(st["amount_rub"])) if st.get("amount_rub") not in (None, "") else None
            except (TypeError, ValueError):
                row["amount_rub"] = None
            clean.append(row)
        if not s(title, 120) or not clean:
            raise ToolError("invalid_plan", "Нужны название и хотя бы один шаг")
        self.plan.clear()
        self.plan.update({"title": s(title, 120), "summary": s(summary, 1200), "goal": s(goal, 300),
                          "horizon_days": max(7, min(int(horizon_days), 365)) if horizon_days else None, "steps": clean,
                          "risks": [s(r, 300) for r in (risks or [])[:6] if s(r, 300)],
                          "checkpoints": [s(c, 200) for c in (checkpoints or [])[:6] if s(c, 200)]})
        return {"status": "saved", "note": "Стратегия появится на экране «План и факт»."}

    async def _t_suggest_followups(self, questions: list | None = None) -> dict:
        qs = [str(q).strip()[:120] for q in (questions or []) if str(q).strip()][:3]
        self.followups[:] = qs
        return {"status": "ok"}

    async def _t_propose_expected_payment(self, **a) -> dict:
        if len(self.proposals) >= MAX_PROPOSALS:
            raise ToolError("too_many_proposals", f"Не больше {MAX_PROPOSALS} предложений за ответ")
        params = validate_payment(a)
        for p in self.proposals:
            if p["params"] == params:
                return {"status": "already_proposed"}
        self.proposals.append({"id": uuid.uuid4().hex[:12], "type": "add_expected_payment", "params": params,
                               "title": suggestion_title(params, self.lang)})
        self._remember(params["counterparty"])
        return {"status": "proposed", "note": "Пользователь увидит карточку и решит сам; в план ничего не записано."}


class _State:
    def __init__(self):
        self.proposals: list[dict] = []
        self.counterparties: set[str] = set()
        self.used: list[str] = []
        self.actions: list[dict] = []
        self.followups: list[str] = []
        self.plan: dict = {}


# инструменты старого агента (llm_gateway): только чтение, сценарии и предложения
LEGACY_TOOLS = {"get_forecast_risk", "get_upcoming_payments", "get_plan_fact", "run_scenario", "get_transactions_by_category",
                "propose_expected_payment", "get_counterparty_risks", "suggest_followups", "get_app_guide"}


def dumps(data: Any, limit: int = 8000) -> str:
    s = json.dumps(data, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + '…[обрезано]'
