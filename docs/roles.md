# Роли, сотрудники, права и блокировка компании

Решения владельца проекта. Реализация: `auth` (пароли), `ledger` (членство и блокировка), `api` (проверка прав), `ecp` (директор по ЭЦП). Оператор платформы и его панель — отдельно, см. [backoffice.md](backoffice.md).

## Модель

- Компания и её **директор** создаются при регистрации (почта+пароль или ЭЦП). Директор — роль `director`.
- Директор добавляет **сотрудников**: `editor` (чтение + редактирование данных) и `viewer` (только чтение). Сотрудник состоит ровно в одной компании и входит по email+паролю.
- Роль определяется **членством** в `company_members` (ledger), а не владельцем компании (`owner_id`). Вход сотрудника не создаёт компанию: `companies/ensure` возвращает компанию директора. Компанию создаёт только вход нового пользователя без членства (он становится директором).
- Права:

| Право | Что даёт | director | editor | viewer |
|---|---|:-:|:-:|:-:|
| `read` | все GET и вычисляющие POST (создание прогноза и сценария, backtest, вопрос консультанту) | да | да | да |
| `edit` | любые изменения данных компании: импорт и подтверждение, ожидаемые платежи, смена категории, налоговый календарь, sync интеграций, применение предложений консультанта, `demo/advance-week` | да | да | нет |
| `manage` | сотрудники, название и реквизиты компании, способы входа директора (почта, ЭЦП), токены других сотрудников | да | нет | нет |

Нарушение: `403 forbidden` (`{"error":{"code":"forbidden","message":"Недостаточно прав для этого действия"}}`). Проверка выполняется до разбора тела и до обращения к данным. Чужая компания в пути остаётся `404 not_found` (проверяется раньше прав). Роль читается при **каждом** запросе, поэтому смена роли, удаление сотрудника и блокировка действуют сразу.

## Пароль директора

У директора всегда есть email и пароль (даже при входе по ЭЦП). Регистрация по ЭЦП создаёт директора без пароля; до установки пароля api отвечает `403 password_required` на всё, кроме:

- `GET /api/v1/auth/me` (в ответе `password_required: true`),
- `POST /api/v1/auth/logout`,
- `POST /api/v1/account/set-credentials` `{email, password, password_confirm?}` → `{email, password_required:false}`; повторно — `409 credentials_exist`; ошибки `422 invalid_email | invalid_password | password_mismatch`, `409 email_taken`. Сертификаты остаются способом входа.

Пароль директора удалить нельзя: `DELETE /api/v1/account/email` всегда `409 password_required`. Подписи добавляются и удаляются (`/account/signatures`, manage).

## Сотрудники и пароли

- `POST /api/v1/members` `{email, role: editor|viewer, password? | generate:true}` (manage) → `201 {user_id, email, role, must_change_password:true, password_generated, password?}`. Нужно ровно одно из `password` / `generate`. Сгенерированный пароль: 14 символов (`secrets`, без `0O1lI`), по классам символов; **возвращается один раз** только в этом ответе, не пишется в логи и не хранится открытым (в БД scrypt). Заданный директором пароль в ответе не возвращается.
- Новый пользователь входит с `must_change_password=true`. Интерфейс предлагает сменить пароль; для сгенерированного (`password_generated=true`) смена обязательна, для заданного директором — можно пропустить (серверный запрет работы до смены не вводится, это решение интерфейса).
- `POST /api/v1/account/change-password` `{current?, new, new_confirm?}` (любая роль, свой пароль). `current` обязателен, пока пароль не помечен `must_change_password` (после входа с выданным паролем он не нужен). Остальные сессии пользователя закрываются. Ошибки: `422 invalid_current_password | invalid_password | password_mismatch`, `429 too_many_attempts` (5 неверных подряд, блокировка на 5 минут).
- `POST /api/v1/members/{user_id}/reset-password` (manage) → `{password, must_change_password:true}`: новый сгенерированный пароль (один раз), все сессии сотрудника закрыты. Для директора — `409 cannot_reset_director`.
- `PATCH /api/v1/members/{user_id}` `{role: editor|viewer}` (manage). `409 cannot_change_director` для директора, `422 invalid_role`.
- `DELETE /api/v1/members/{user_id}` (manage) → 204: пользователь удаляется из auth вместе с сессиями, API-токенами и подписями, затем из членства. Директора удалить нельзя: `409 cannot_remove_director`.
- `GET /api/v1/members` (любая роль) → `{items:[{user_id, email, role, created_at, is_me}]}`; служебный адрес `@ecp.invalid` отдаётся как `null`. Участников (включая директора) не больше 50 на компанию (`409 too_many_members`).
- Члены чужой компании недоступны (`404`).

## `GET /api/v1/auth/me`

`{user_id, email, company_id, company_name, company_inn, company_ogrn, role, permissions:{read, edit, manage}, must_change_password, password_generated, password_required, company_banned, ban_reason}`. Доступен для заблокированной компании и при `password_required`. `POST /auth/login` и `/auth/ecp/login` возвращают `role`, `permissions`, `must_change_password`, `password_generated`, `password_required`.

## API-токены внешнего API

- Токен принадлежит пользователю и имеет права его роли: scope `read` у viewer, `write` у editor и director. Права проверяются при каждом запросе, поэтому понижение роли действует на токен сразу; удаление сотрудника аннулирует его токены (`401 invalid_token`).
- Внешний API: все GET и `POST /api/ext/v1/forecast` (вычисление) — `read`; `POST /api/ext/v1/expected-events` — `write` (токен viewer: `403 forbidden`). `GET /api/ext/v1/company` возвращает `scope`.
- Выпускать токены для себя может любой сотрудник (`POST /account/tokens`, до 10 на пользователя), удалять свои — тоже.
- Директор: `GET /api/v1/account/tokens?scope=company` → `{items:[{id, name, prefix, created_at, last_used, owner_id, owner_email, owner_role, mine}]}` (токены всех сотрудников, сами токены не отдаются); `DELETE /api/v1/account/tokens/{id}` отзывает любой токен компании. `scope=own` (по умолчанию) — только свои; для не-директора `scope=company` → `403 forbidden`.

## Блокировка компании

Оператор платформы блокирует компанию с причиной (`POST /internal/v1/admin/companies/{cid}/ban {reason}` в ledger; панель оператора и закрытие сессий — [backoffice.md](backoffice.md)). Ledger хранит `companies.banned_at`, `ban_reason`; отдаёт их в `members/by-user`. Дальше всё делает api:

- каждый запрос участника (сессия или API-токен) проверяет блокировку → `403 company_banned`, сообщение «Компания заблокирована оператором: <причина>». Это действует сразу, без перезапуска и кэша, даже если сессии не были закрыты;
- вход (`/auth/login`, `/auth/ecp/login`) в заблокированную компанию отклоняется тем же `403 company_banned`, созданная сессия тут же закрывается;
- `GET /auth/me` и `POST /auth/logout` остаются доступны, чтобы интерфейс показал причину (`company_banned`, `ban_reason`);
- backoffice дополнительно закрывает сессии всех участников (`POST /internal/v1/users/{uid}/revoke-sessions` в auth); API-токены не удаляются, но блокируются проверкой выше;
- данные компании не удаляются; разблокировка (`.../unban`) возвращает доступ сразу. Повторный `ban` обновляет причину и не меняет момент блокировки; `unban` идемпотентен.
- оператор видит только метаданные и агрегаты (`admin/*` ledger): название, реквизиты, даты, число участников, операций и событий, дату последней операции и импорта, email директора и список участников с ролями. Содержимого операций, платежей, контрагентов и назначений в этих ответах нет (проверено тестом).

## Таблица «маршрут → право»

Каждый тенантский маршрут api проходит проверку: сессия → блокировка → пароль директора → принадлежность компании → право. Тест `every_route_enforces_its_permission_for_each_role` (`backend/api/src/roles_tests.rs`) перебирает эту таблицу для всех трёх ролей и убеждается, что запрещённое даёт `403 forbidden` ещё до обращения к данным, а без сессии — `401`.

| Маршрут | Право | Заметка |
|---|---|---|
| `POST /api/v1/auth/register`, `/auth/login`, `/auth/ecp/challenge`, `/auth/ecp/login` | публичные | login в заблокированную компанию → 403 `company_banned` |
| `POST /api/v1/auth/logout`, `GET /api/v1/auth/me` | любая сессия | работают при `password_required` и бане |
| `POST /api/v1/account/set-credentials` | сессия директора без пароля | только пока нет email+пароля |
| `POST /api/v1/account/change-password` | read | свой пароль |
| `GET /api/v1/account/methods` | read | |
| `POST\|DELETE /api/v1/account/email` | manage | DELETE всегда 409 `password_required` |
| `POST /api/v1/account/signatures`, `DELETE /api/v1/account/signatures/{id}` | manage | |
| `GET\|POST /api/v1/account/tokens`, `DELETE /api/v1/account/tokens/{id}` | read | свои; директор: `?scope=company` и отзыв чужих (manage) |
| `GET /api/v1/members` | read | |
| `POST /api/v1/members`, `PATCH\|DELETE /api/v1/members/{uid}`, `POST /api/v1/members/{uid}/reset-password` | manage | |
| `PATCH /api/v1/companies/{cid}` | manage | название |
| `GET /api/v1/companies/{cid}/imports`, `/imports/{iid}`, `/transactions`, `/expected-events` | read | |
| `POST /api/v1/companies/{cid}/imports`, `/imports/{iid}/confirm` | edit | |
| `PATCH /api/v1/companies/{cid}/transactions/{id}/category` | edit | |
| `POST /api/v1/companies/{cid}/expected-events`, `DELETE …/expected-events/{eid}` | edit | |
| `POST /api/v1/companies/{cid}/expected-events/tax-calendar` | edit | |
| `POST /api/v1/companies/{cid}/integrations/sync` | edit | |
| `POST /api/v1/companies/{cid}/advisor/ask` | read | вычисляющий POST |
| `POST /api/v1/companies/{cid}/advisor/suggestions/apply` | edit | |
| `GET /api/v1/companies/{cid}/demo/status` | read | |
| `POST /api/v1/companies/{cid}/demo/advance-week` | edit | плюс условия демо |
| `GET /api/v1/gov/key-rate` | read | |
| `POST /api/v1/companies/{cid}/forecasts`, `/scenarios` | read | вычисляющие POST |
| `GET /api/v1/companies/{cid}/forecasts/latest`, `/forecasts/{rid}`, `/backtest`, `/plan-fact` | read | |
| `GET /api/ext/v1/company`, `/forecast`, `/transactions`, `/expected-events`, `/plan-fact`; `POST /api/ext/v1/forecast` | токен, read | права по роли владельца |
| `POST /api/ext/v1/expected-events` | токен, edit (scope `write`) | |
| `/api/admin/v1/*` | оператор (`op_session`) | не пересекается с тенантскими маршрутами |
| `GET /internal/v1/companies/{cid}/summary` (порт 3100) | служебная сеть | без cookie, не публикуется |

Правило для нового маршрута: тенантский обработчик обязан брать `Reader`/`Editor`/`Manager` (`backend/api/src/http.rs`) либо вызывать `relay` с правом; голый `Principal` означает «любая роль» и допустим только для проксирования с проверкой метода (`to_ledger`).

## Внутренние контракты

ledger (владеет членством и блокировкой):
- `companies/ensure` `{owner_id, email, name?, inn?, ogrn?}` → `{company_id, company_name, inn, ogrn, role, banned, ban_reason}`: сотрудник получает компанию директора, новый пользователь создаёт компанию и становится директором (запись в `company_members`).
- `GET members/by-user/{user_id}` → `{company_id, company_name, inn, ogrn, role, banned, ban_reason}` (404 нет членства).
- `GET|POST companies/{cid}/members`, `PATCH|DELETE companies/{cid}/members/{user_id}`; `PATCH` принимает `{role}` и служебное `{email}` (для директора после установки пароля).
- `GET admin/companies?query=&banned=true|false&limit=&offset=` → `{total, items:[{id, name, inn, ogrn, created_at, banned, banned_at, ban_reason, members_count, transactions_count, last_transaction_date, last_import_at, events_count, director_email}]}`; `query` ищет по названию, ИНН, ОГРН, email директора без учёта регистра; `GET admin/companies/{cid}` (то же + `members:[{user_id, email, role, created_at}]`); `POST admin/companies/{cid}/ban {reason}` (1–500 символов, `422 invalid_reason`), `POST …/unban`; `GET admin/stats` → `{companies_total, companies_banned, companies_active, new_7d, new_30d, members_total}`.
- Аудит блокировок и членства пишется в `audit_events` с актором из `x-actor` (backoffice передаёт id оператора).
- Миграция существующей БД при старте: колонки `banned_at`, `ban_reason`; таблица `company_members`; владелец каждой компании (`owner_id`) получает запись директора автоматически (email заполняется при следующем входе).

auth:
- `users/create` `{email, password? | generate:true}` → 201 `{user_id, email, must_change_password, password_generated, password?}`; `409 email_taken`, `422 invalid_email | invalid_password`.
- `users/{uid}/password` `{current?, new, keep_token?}`; `users/{uid}/reset-password` → `{password}`; `users/{uid}/revoke-sessions` → `{revoked}` (токены не затрагивает); `DELETE users/{uid}` → 204; `users/{uid}/email-password` `{email, password}` (директор из ЭЦП, `409 email_exists`); `GET tokens?user_ids=a,b` (токены нескольких пользователей без значений).
- `login`, `session`, `sessions/issue` отдают `must_change_password` и `password_generated`; миграция добавляет колонки `users.must_change_password`, `users.password_generated`.

ecp: `ecp/login` дополнительно возвращает `password_required` (= нет email), `must_change_password`, `password_generated`.

## Не сделано

- Серверный запрет работы до смены сгенерированного пароля (сейчас обязательность обеспечивает интерфейс по флагу `password_generated`).
- Перенос сотрудника в другую компанию и несколько компаний у пользователя.
- Приглашение сотрудника по ссылке (пароль выдаёт директор).
- Ограничение частоты запросов по API-токену, срок действия токена.
- Ролевая проверка внутри внутренних сервисов (forecast, planfact, advisor): права проверяет только api.
