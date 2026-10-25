"""Маршруты моделей для API-сервера Hermes: псевдоним `модель@уровень` -> модель подписки и уровень размышления.
Список моделей берётся у подписки (зависит от аккаунта ChatGPT); не удалось — только HERMES_MODEL.
Пишет блок platforms.api_server.extra в конфиг Hermes (путь — первый аргумент)."""
import os
import sys

import yaml

EFFORTS = ["low", "medium", "high", "xhigh"]


def subscription_models() -> list[str]:
    try:
        from hermes_cli import auth
        from hermes_cli.codex_models import _fetch_models_from_api

        c = auth.resolve_codex_runtime_credentials()
        return [m for m in _fetch_models_from_api(c.get("api_key") or c.get("access_token")) if isinstance(m, str)]
    except Exception as e:  # noqa: BLE001 — без входа в подписку или без сети работаем с моделью по умолчанию
        print(f"routes: список моделей подписки недоступен ({type(e).__name__})", file=sys.stderr)
        return []


def main(path: str) -> None:
    default = os.environ.get("HERMES_MODEL") or "gpt-6.1-sol"
    models = list(dict.fromkeys([default, *subscription_models()]))
    routes = {}
    for m in models:
        routes[m] = {"model": m, "provider": "openai-codex"}
        for e in EFFORTS:
            routes[f"{m}@{e}"] = {"model": m, "provider": "openai-codex", "reasoning": e}
    cfg = yaml.safe_load(open(path)) or {}
    cfg.setdefault("platforms", {}).setdefault("api_server", {}).setdefault("extra", {})["model_routes"] = routes
    yaml.safe_dump(cfg, open(path, "w"), allow_unicode=True, sort_keys=False)
    print(f"routes: моделей {len(models)}: {', '.join(models)}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
