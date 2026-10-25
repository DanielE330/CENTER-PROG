"""Надстройка над Hermes Agent 0.19.0 для CenterInvest: уровень размышления на каждый запрос.

Модель на запрос Hermes выбирает сам: поле `model` запроса — псевдоним из `platforms.api_server.extra.model_routes`.
Уровень размышления API-сервер берёт только из общего конфига, поэтому здесь:
  1) в маршрутах сохраняется ключ `reasoning` (штатный разбор его отбрасывает);
  2) пока создаётся агент для маршрута, уровень маршрута подменяет общий (`GatewayRunner._load_reasoning_config`).
Агент создаётся синхронно внутри `_create_agent`, поэтому хватает contextvar на время вызова.
Версия Hermes закреплена в Dockerfile; если внутренности поменяются, патч сообщит в лог и ничего не сломает."""
import contextvars
import logging
import sys

log = logging.getLogger("ci_patch")
EFFORTS = {"low", "medium", "high", "xhigh"}
_effort: contextvars.ContextVar[str | None] = contextvars.ContextVar("ci_effort", default=None)


def apply() -> bool:
    try:
        from gateway.platforms import api_server as a
        from gateway.run import GatewayRunner
        from hermes_constants import parse_reasoning_effort
    except ImportError as e:  # pragma: no cover
        log.warning("ci_patch: Hermes не найден (%s), уровень размышления — из конфига", e)
        return False
    cls = a.APIServerAdapter
    parse = cls.__dict__.get("_parse_model_routes")
    create = cls.__dict__.get("_create_agent")
    load = GatewayRunner.__dict__.get("_load_reasoning_config")
    if not isinstance(parse, staticmethod) or create is None or not isinstance(load, staticmethod):
        log.warning("ci_patch: внутренности Hermes изменились, уровень размышления — из конфига")
        return False
    parse_f, load_f = parse.__func__, load.__func__

    def parse_routes(raw):
        routes = parse_f(raw)
        for alias, cfg in (raw or {}).items() if isinstance(raw, dict) else ():
            eff = str((cfg or {}).get("reasoning") or "").strip() if isinstance(cfg, dict) else ""
            if str(alias).strip() in routes and eff in EFFORTS:
                routes[str(alias).strip()]["reasoning"] = eff
        return routes

    def create_agent(self, *args, route=None, **kw):
        token = _effort.set((route or {}).get("reasoning"))
        try:
            agent = create(self, *args, route=route, **kw)
        finally:
            _effort.reset(token)
        # в лог: какая модель и уровень размышления реально достались агенту
        print(f"ci_patch: agent model={getattr(agent, 'model', '?')} reasoning={getattr(agent, 'reasoning_config', None)}", file=sys.stderr, flush=True)
        return agent

    def load_reasoning(model: str = ""):
        eff = _effort.get()
        return parse_reasoning_effort(eff) if eff else load_f(model)

    cls._parse_model_routes = staticmethod(parse_routes)
    cls._create_agent = create_agent
    GatewayRunner._load_reasoning_config = staticmethod(load_reasoning)
    print("ci_patch: уровень размышления по маршруту включён", file=sys.stderr, flush=True)
    return True
