# Панель оператора платформы (backoffice)

Отдельный контур для сотрудников платформы: список зарегистрированных компаний, краткая статистика и блокировка/разблокировка компании с причиной. Операторы живут в сервисе `backoffice` (Python, `:8013`, БД `backoffice.db`) и никак не связаны с пользователями компаний.

## Что видит оператор
Только метаданные и агрегаты: название, ИНН, ОГРН, дата регистрации, e-mail директора, участники и их роли, числа (пользователи, загруженные операции, ожидаемые платежи), даты последней операции и последнего импорта, блокировка и её причина, уровень риска и вероятность кассового разрыва из прогноза (если прогноз есть, иначе пусто). Операции, платежи, контрагенты, назначения платежей, остатки и суммы оператору не передаются: сервисы отдают только перечисленные поля, а `api` повторно отсекает всё лишнее белым списком.

## Как создать оператора
1. Через окружение (при старте `backoffice`): задайте `OPERATOR_EMAIL` и `OPERATOR_PASSWORD` в `infra/.env` (файл в `.gitignore`, в репозитории паролей нет). Оператор создаётся, если оператора с таким email ещё нет; существующему пароль не меняется. Пароль не короче 12 символов (иначе в логе `rejected:invalid_password`, сервис продолжает работу).
2. Командой с интерактивным вводом пароля (пароль не передаётся аргументом и не попадает в историю):
   - Docker: `docker compose -f infra/docker-compose.yml exec backoffice python -m backoffice.create_operator operator@example.com`
   - без Docker: `cd backend/services && BACKOFFICE_DB=../../data/run/backoffice.db uv run python -m backoffice.create_operator operator@example.com`

Вход: `http://localhost:3000/operator`. Ссылки на панель в интерфейсе компаний нет.

## Безопасность
- Пароли оператора: scrypt (n=2^15), токены сессий хранятся только как sha256. Сессия оператора живёт 8 часов (`BACKOFFICE_SESSION_TTL_SECONDS`), не более 10 одновременных.
- Лимит попыток входа: 5 неудач на email, затем блокировка на 5 минут (429 `too_many_attempts`), включая правильный пароль; неизвестный email неотличим от неверного пароля.
- Отдельная cookie `op_session` (`HttpOnly; SameSite=Strict; Path=/api/admin`, `Secure` при `COOKIE_SECURE=true`). Она не отправляется на `/api/v1`, а cookie `session` компании не принимается на `/api/admin`: сессии не взаимозаменяемы. Для изменяющих запросов проверяется Origin так же, как в остальном API.
- Журнал действий `operator_audit` (кто, что, над какой компанией, причина блокировки, результат; вход и неудачные попытки): `GET /api/admin/v1/audit?limit=&company_id=`. Данных компаний в нём нет.

## Эндпоинты api (`/api/admin/v1`, cookie `op_session`)
| Метод | Путь | Ответ |
|---|---|---|
| POST | `/login` `{email,password}` | `{email}` + cookie; 401 `invalid_credentials`, 429 `too_many_attempts` |
| POST | `/logout` | 204, cookie сброшена |
| GET | `/me` | `{email}`; без сессии 401 `unauthenticated` |
| GET | `/companies?query=&banned=true\|false&limit=&offset=` | `{total,limit,offset,items:[компания]}`; limit 1..100, по умолчанию 50 |
| GET | `/companies/{id}` | компания + `members:[{email,role,created_at}]` |
| POST | `/companies/{id}/ban` `{reason}` | `{company_id,banned:true,ban_reason,sessions_revoked,revoke_failed}`; причина 3..500 символов, иначе 422 `invalid_reason` |
| POST | `/companies/{id}/unban` | `{company_id,banned:false}` |
| GET | `/stats` | `{total,active,banned,new_7d,new_30d,users_total}` |
| GET | `/audit` | `{items:[{id,at,operator,action,company_id,detail,outcome}]}` |

Компания в списке: `id,name,inn,ogrn,created_at,banned,banned_at,ban_reason,members_count,transactions_count,last_transaction_date,last_import_at,events_count,director_email,risk_level,shortfall_probability`.

## Что делает блокировка
`backoffice` пишет блокировку в ledger (`POST /internal/v1/admin/companies/{cid}/ban`), затем вызывает auth `POST /internal/v1/users/{uid}/revoke-sessions` для каждого участника (`GET /internal/v1/companies/{cid}/members`). Заблокированная компания: вход и все запросы пользователей и API-токенов получают 403 `company_banned` («Компания заблокирована оператором: <причина>»), действующие сессии аннулируются. Данные не удаляются; разблокировка возвращает доступ. Если отзыв сессий части участников не удался, блокировка всё равно действует (её проверяют ledger/api на каждом запросе), число неудач возвращается в `revoke_failed`.

## Внутренний API backoffice (`/internal/v1`, только для api)
`operators/login|session|logout`, `admin/companies`, `admin/companies/{cid}` (+`/ban`, `/unban`), `admin/stats`, `admin/audit`, `/live`. Действия над компаниями требуют `x-actor` с id оператора, прошедшего проверку сессии в api. Риск и вероятность берутся из `forecast` `GET /internal/v1/companies/{cid}/summary` только для компаний с операциями: не более 6 запросов параллельно, таймаут 6 с (`BACKOFFICE_FORECAST_TIMEOUT_SECONDS`), результат кэшируется на 60 с; нет прогноза или forecast недоступен — `null`. Env: `BACKOFFICE_DB`, `LEDGER_URL`, `FORECAST_URL`, `AUTH_URL`, `OPERATOR_EMAIL`, `OPERATOR_PASSWORD`, `BACKOFFICE_TIMEOUT_SECONDS`.

Тесты: `cd backend/services && uv run pytest tests/test_backoffice.py`, `cd backend && cargo test -p api` (модуль `admin`).
