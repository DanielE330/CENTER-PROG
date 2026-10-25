# Передача проекта: с чего продолжить

Дата: 25.10.2026. Проект: «Центр Прогнозирования» (сервис прогноза денежного потока и кассовых разрывов для малого бизнеса, кейс «Центр-инвест»). Репозиторий `DanielE330/CI-hack`, рабочая ветка **`testing`**. Секретов в этом файле нет: пароли и ключи лежат только на сервере в `infra/.env` и у владельца.

## 1. Где что лежит
| Что | Где |
|---|---|
| Запуск одной командой | `./run.sh` (только Docker) или `./install.sh` (создаёт `infra/.env`, ключи, оператора) |
| Запуск без Docker | `infra/scripts/dev.sh` (нужны Rust, Node, uv) |
| Тесты | `cd backend && cargo test --workspace`; `cd backend/services && uv run pytest -q`; `cd frontend && npx tsc --noEmit && npm run build`; сквозной `infra/scripts/smoke.sh` |
| Архитектура и сервисы | `docs/microservices.md`, `README.md` |
| Роли и права | `docs/roles.md` (таблица маршрут → право; тест `roles_tests` проверяет число маршрутов, при добавлении маршрута обновляйте и число, и таблицу) |
| Что делает каждая кнопка и график | `docs/ui-guide.md` (раздел 17 — что не подключено и что подозрительно) |
| Интеграции | 1С: `docs/integrator.md`; ФНС: `docs/fns.md`, план `docs/fns-integration-plan.md`; почта: `docs/mail.md`; Telegram: `docs/notifications.md`; консультант (Hermes): `docs/advisor-hermes.md`; оператор: `docs/backoffice.md`; ЭЦП: `docs/ecp.md` |
| Прежние списки дел | `docs/TODO.md` (частично устарел), `docs/gleb-port.md` |

## 2. Состояние репозитория
- Ветка `testing` содержит работу всей команды (swimshadow, GeoGhost, Dumler Gleb, Michael, DanielE330). `main` и `dan` отстают.
- Локально в `testing` есть коммиты, которые **ещё не отправлены на GitHub** (пуш с этого компьютера требует токен GitHub, не пароль: https://github.com/settings/tokens, право `repo`). Перед пушем: `git fetch && git merge origin/testing`.
- В рабочем дереве есть **незакоммиченные правки** выбора валюты (агент ещё работал): `backend/services/gov/*`, `backend/api/src/{http,main,roles_tests}.rs`, `frontend/src/lib/{currency,formatters,prefs}.ts`, графики. Перед коммитом прогоните тесты (см. выше); если что-то не собирается, это незавершённая работа.
- Резервные ветки на этом компьютере: `backup/dan-before-rebranch`, `backup/dan-before-testing-update`, `backup/testing-local-d206b05`, `backup/testing-local-9a3035e`, `backup/testing-wip`.

## 3. Сервер
- Сайт: https://centr-prog.daniel.crazedns.ru (LXC-контейнер `HLCX`, номер 102 на Proxmox; внутри `/opt/centr`, compose-проект `centr`).
- Доступ: Tailscale `100.64.0.6` (Proxmox), дальше `pct exec 102 -- …`; пароль у владельца (в репозиторий не класть). Edge-Caddy: контейнер 100, блок `centr-prog.daniel.crazedns.ru` проксирует на LAN-адрес контейнера `192.168.1.122:3000` (адрес по DHCP: если изменится, будет 502).
- Выкладка: `git archive --format=tar.gz --prefix=centr/ -o centr.tar.gz testing`, передать в контейнер и распаковать в `/opt` (**`infra/.env` не перезаписывается**, это не отслеживаемый файл); на сервере в `infra/docker/rust.Dockerfile` добавить после `FROM rust:… AS build` строку `ENV CARGO_BUILD_JOBS=2` (4 ГБ памяти не хватает на параллельную сборку); затем `COMPOSE_PROJECT_NAME=centr docker compose -f infra/docker-compose.yml --profile hermes up -d --build` в фоне, логи в `/root/centr-deploy*.log`. Полная пересборка Rust 20–30 минут.
- На сервере остановлены чужие проекты `honeyforge-center` (контейнеры `hf-*`), вернуть: `docker start hf-center-db hf-honeymind hf-center hf-orchestrator hf-proxy`.
- `infra/.env` на сервере содержит: `COOKIE_SECURE=true`, SMTP Gmail (пароль приложения; его нужно **отозвать и заменить**: он попадал в чат), `HERMES_PROXY=http://192.168.1.101:1081` (общий VPN-прокси контейнера `bots`, без него OpenAI из России отвечает 403), `HERMES_AGENT_KEY`, `ADVISOR_MCP_TOKEN`, `INTEGRATIONS_KEY` (**не терять: с другим ключом пароли подключений 1С не расшифровать**), `DEMO_SEED=1`, `DEMO_LOGIN=1`.
- Оператор платформы: `/operator`, логин и пароль в `/opt/centr/infra/.operator-credentials` на сервере; при первом входе подключается 2FA (TOTP). Сброс 2FA: `docker compose … exec backoffice python -m backoffice.create_operator --reset-2fa <email>`.
- Hermes (консультант): вход в подписку Codex сохранён в томе `hermes-data` (`auth.json`). Если слетит: `docker compose -f infra/docker-compose.yml --profile hermes run --rm hermes-login` (код устройства; страницу https://auth.openai.com/codex/device открывает и подтверждает владелец).
- Тестовый вход (кнопки «Директор/Редактор/Наблюдатель») включён: любой посетитель заходит без пароля. После показа выключить (`DEMO_LOGIN=0`, `DEMO_SEED=0`) и перезапустить `auth` и `web-api`.

## 3.1. Демо-данные
- `infra/scripts/seed_demo.sh` загружает выписку C001 до даты отсечки (`DEMO_CUTOFF`), `seed_showcase.sh` добавляет интеграции, налоги, плановые платежи и «зелёные» поступления (идемпотентно по названиям). Кнопка «Прошла неделя» подаёт следующую неделю ленты, «Назад на неделю» откатывает последнюю (`POST …/demo/rewind-week`).
- `data/demo/statements/C001.csv` **совпадает с выпиской из датасета организаторов**, а репозиторий публичный: заменить синтетикой и убрать из истории (`git filter-repo`) либо удалить.

## 4. Что в работе или не доделано
1. **Выбор валюты отображения** (USD, EUR, CNY, GBP, KZT, BYN, TRY, AED по курсу ЦБ): агент начал, часть в рабочем дереве (см. раздел 2). Нужно: довести, прогнать тесты, проверить графики, закоммитить.
2. **Токен Telegram-бота у директора** (Настройки → Интеграции → «Telegram»): агент запущен; делает хранение токена по компаниям (шифрование как у 1С), несколько ботов в `notifier`, прокси `TELEGRAM_PROXY`. На сервере после выкладки прописать `TELEGRAM_PROXY=http://192.168.1.101:1081`.
3. Исправить найденное в `docs/ui-guide.md` (раздел 17.2), в первую очередь: сопоставление плана и факта в «План и факт» не учитывает контрагента; события с датой ≤ даты данных считаются просроченными; ИНН и ОГРН в настройках только для чтения (из-за этого функции ФНС не работают у демо-компании и у зарегистрированных по email); не блокируются для наблюдателя смена подкатегории и ретро целей; кнопка «Удалить» email у директора всегда даёт 409; поиск по операциям не находит кириллицу в другом регистре.
4. Чек-поинт хакатона: загрузка контрольного датасета (`all_operations.csv`) с выбором компании, журнал расчёта и метрики сделаны командой (коммит «журнал расчёта по шагам, метрики и оценка на контрольном датасете»): проверить вручную на реальном файле.
5. ФНС: основная интеграция сделана командой (режим «только реальные данные»); мой черновик остался в `docs/fns-integration-plan.md` как план. Автоматический вход в личный кабинет по ЭЦП сознательно **не делается** (нет официального API, риск блокировки): ЕНС через загрузку справки.
6. Перенос из проекта `gleb` (PostgreSQL, сервис `alerts`, настройки компании и др.): не начат, см. `docs/gleb-port.md`. Keycloak, mTLS и Ansible не переносим.
7. Docker-образы `ecp` не содержат КриптоПро: проверка подписи ЭЦП работает на чистом Python (`ECP_VERIFIER=gost`), цепочка доверия и отзыв сертификата не проверяются.
8. CI-проверки (`.github/workflows`) стоят с `continue-on-error` для замечаний `clippy`, `ruff`, `fmt`: привести код в порядок и сделать их обязательными.

## 5. Как безопасно продолжать
- Не коммитить: `infra/.env`, `*.key`, `.operator-credentials`, выгрузки чата (`docs/ChatExport_*`), реальные токены. Комментарии в `.env.example` держать на **отдельных строках**: Docker Compose читает хвостовой комментарий как значение.
- Новый маршрут API: добавить в `main.rs`, `roles_tests.rs` (таблица и число), `docs/roles.md`, `docs/ui-guide.md`.
- Общие файлы (`ru.json`, `en.json`, `main.rs`, compose) правьте точечно: над ними работают несколько человек.
- Любые обращения к внешним сервисам (ЕГРЮЛ, ФНС, OpenAI, Telegram) держать за прокси и таймаутами, без обхода капч и условий использования.
