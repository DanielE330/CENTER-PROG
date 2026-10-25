# ИИ-консультант на Hermes Agent

Консультант в чате («AI-ассистент») работает на [Hermes Agent](https://github.com/NousResearch/hermes-agent) 0.19.0. Модель берётся по подписке ChatGPT/Codex: вход по OAuth, провайдер `openai-codex`, модель `HERMES_MODEL`, по умолчанию `gpt-5.5`. Ключ API OpenAI не нужен.

Hermes выбран вместо OpenClaw по трём причинам:
- у него есть OpenAI-совместимый API-сервер;
- MCP-клиент умеет фильтровать инструменты;
- можно оставить агенту только наши инструменты, без терминала и файлов.

## Как устроено

```
SPA ── api (права, роль) ── advisor ──/v1/chat/completions──> hermes ──> ChatGPT (подписка Codex)
                               ▲                                │
                               └────────── MCP /mcp ────────────┘  (инструменты компании, по пропуску вопроса)
```

1. **Права.** api берёт права консультанта из настроек компании (`advisor/settings`, раздел `advisor` в ledger) и убирает право записи, если у пользователя нет права edit. Получившиеся права api передаёт в advisor.
2. **Пропуск.** advisor выдаёт пропуск на вопрос (`tickets.py`): компания, пользователь, права, срок жизни 5 минут. Затем отправляет вопрос в Hermes с системным сообщением: что разрешено, что запрещено и пропуск.
   - Каждый вопрос идёт отдельной сессией Hermes (`X-Hermes-Session-Id` = пропуск), поэтому контекст разных компаний не смешивается.
3. **Вызов инструментов.** Hermes вызывает инструменты MCP-сервера `ci` (advisor `/mcp`, ключ `ADVISOR_MCP_TOKEN`) и в каждом вызове передаёт пропуск. Hermes не сообщает MCP-серверу, кто спрашивает, поэтому без пропуска не обойтись.
   - Инструмент без выданного права, с чужим или просроченным пропуском не выполняется. Модель получает `permission_denied` или `invalid_ticket`.
   - После ответа пропуск закрывается.
4. **Ответ.** В пропуске собираются предложения, сделанные действия и подсказки следующих вопросов (`suggest_followups`). Ответ api: `{answer, suggestions, actions, followups, provider:"hermes", model}`.
5. **Если Hermes недоступен**, отвечает агент через llm-gateway, а если и он недоступен — шаблон из прогноза (`notice`).
   - Если Hermes уже успел что-то записать, второй движок не запускается: пользователь видит сделанные действия.

**Что доступно Hermes.** У Hermes включён только MCP-сервер `ci`: `platform_toolsets.api_server: [ci]` (`infra/hermes/config.yaml`). Память выключена. Проверка в контейнере: `get_tool_definitions(enabled_toolsets=['ci'])`. Результат — 10 инструментов `mcp__ci__*` и ничего больше: нет терминала, файлов, браузера и веба.

## Права консультанта

Права выдают владелец или администратор: «Настройки → ИИ-консультант» (`PUT /advisor/settings`).

| Право | Инструменты | Запись |
|---|---|---|
| `read_forecast` | `get_forecast_risk`, `get_upcoming_payments` | нет |
| `read_operations` | `get_transactions_by_category`, `get_plan_fact` | нет |
| `read_counterparties` | `get_counterparty_risks` (флаги выгружены из ФНС) | нет |
| `run_scenarios` | `run_scenario` (без сохранения) | нет |
| `propose_payments` | `propose_expected_payment` (карточка, применяет пользователь) | нет |
| `add_payments` | `add_expected_payment`: событие `ИИ-консультант: …`, запись в журнале `advisor.add_payment` | да, только для ролей с edit |
| `recalc_forecast` | `recalc_forecast` | да, только для ролей с edit |

По умолчанию выданы пять прав на чтение, сценарии и предложения. Дополнительно в настройках:
- `enabled=false`: консультант выключен, `ask` и `prompts` отвечают 403 `advisor_disabled`, чат отвечает по правилам;
- `show_prompts`: показывать ли подсказки под полем ввода.

## Подсказки «что написать»

`GET /advisor/prompts` строит до четырёх вопросов по данным компании (`advisor/prompts.py`, без модели) и только в пределах выданных прав:
- ближайший разрыв с датой и суммой;
- просрочки;
- контрагент с риском по ФНС;
- сценарий;
- действие.

После каждого ответа под полем ввода показываются следующие вопросы, которые предложил сам консультант.

## Запуск

```sh
# infra/.env: HERMES_AGENT_KEY и ADVISOR_MCP_TOKEN (openssl rand -hex 24), HERMES_MODEL
docker compose -f infra/docker-compose.yml --profile hermes run --rm hermes-login   # код устройства, вход в ChatGPT
docker compose -f infra/docker-compose.yml --profile hermes up -d hermes advisor
```

Вход хранится в томе `hermes-data` (`auth.json`) или в каталоге `HERMES_DATA`. Конфиг Hermes при каждом старте копируется из образа, поэтому вручную в томе его не правят.
