# ML-лаборатория

Единый бэктест и challenger-модели для прогноза остатка и разрывов. Описание, результаты и ограничения: `docs/ml-architecture.md`.

    cd backend/services && uv sync --group ml && cd ../..
    export MOCK_DATASET_DIR="<каталог cashflow_dataset_participants>"
    python -m ml.backtest.run --companies 30 --cutoffs 8
    python -m ml.backtest.gate ml/reports
    pytest ml/tests

- `lab/data.py` — загрузка датасета (только чтение) и разметка тем же кодом, что в боевом парсере (`backend/services/parser/taxonomy.py`).
- `lab/engine.py`, `delays.py`, `workdays.py`, `models.py`, `backtest.py` — слои + Монте-Карло, задержки оплат (Каплан–Мейер), модели остаточного слоя, rolling-origin с разметкой разрывов `raw` и `adjusted`. Основаны на проекте «ЦИ1» (ветка `gleb`, swimshadow), адаптированы под общий бэктест.
- `reports/` — отчёты бэктеста (JSON и Markdown, внутри sha256 данных).
