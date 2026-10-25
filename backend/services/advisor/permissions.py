"""Права консультанта: что он может делать в компании. Набор выдаёт владелец или администратор в настройках
(раздел `advisor` в ledger); api пересекает его с ролью пользователя (viewer не получает прав на запись) и передаёт сюда.
Инструмент без выданного права не вызывается: модель получает {"error": "permission_denied"}."""

# право -> инструменты; needs_edit — право записи, доступно только ролям с правом edit
PERMISSIONS: dict[str, dict] = {
    "read_forecast": {"tools": ["get_forecast_risk", "get_upcoming_payments"], "needs_edit": False},
    "read_operations": {"tools": ["get_transactions_by_category", "get_plan_fact"], "needs_edit": False},
    "read_counterparties": {"tools": ["get_counterparty_risks"], "needs_edit": False},
    "run_scenarios": {"tools": ["run_scenario"], "needs_edit": False},
    "propose_payments": {"tools": ["propose_expected_payment"], "needs_edit": False},
    "make_plans": {"tools": ["save_strategy_plan"], "needs_edit": False},
    "add_payments": {"tools": ["add_expected_payment"], "needs_edit": True},
    "recalc_forecast": {"tools": ["recalc_forecast"], "needs_edit": True},
}
# служебные инструменты без права: подсказки следующих вопросов и гид по приложению
ALWAYS = {"suggest_followups", "get_app_guide"}
DEFAULT = ["read_forecast", "read_operations", "read_counterparties", "run_scenarios", "propose_payments", "make_plans"]


def clean(perms: list | None) -> list[str]:
    """Известные права в порядке каталога."""
    got = set(perms or [])
    return [p for p in PERMISSIONS if p in got]


def allowed_tools(perms: list[str]) -> set[str]:
    out = set(ALWAYS)
    for p in clean(perms):
        out.update(PERMISSIONS[p]["tools"])
    return out


def permission_of(tool: str) -> str | None:
    for p, d in PERMISSIONS.items():
        if tool in d["tools"]:
            return p
    return None
