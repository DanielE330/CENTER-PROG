# Сервис ecp: вход по ЭЦП

Одна задача: аутентифицировать пользователя по электронной подписи и получить для него сессию. Порт 8007, `/live`, БД `ECP_DB` (по умолчанию `data/run/ecp.db`: одноразовые challenge и сведения о сертификатах). Пользователей и сессии ведёт `auth` (`AUTH_URL`, по умолчанию `http://127.0.0.1:8001`).

## Протокол
1. `POST /internal/v1/ecp/challenge` -> `{challenge, ttl}`. Challenge вида `ecp-login:<nonce>`, одноразовый, живёт 300 с.
2. Браузер (КриптоПро ЭЦП Browser plug-in) подписывает challenge, получая откреплённую подпись CAdES (DER, base64).
3. `POST /internal/v1/ecp/login` `{challenge, signature}`: challenge гасится до проверки; разбирается сертификат единственного подписанта (срок действия), подпись проверяется `cryptcp` (`CRYPTCP`, `ECP_REQUIRE_CHAIN=1` включает проверку цепочки). Затем ecp вызывает auth:
   - `POST /internal/v1/users/external` `{external_id: <SHA-256 отпечаток сертификата>, display_name}` -> `{user_id, email, created}` (идемпотентно; пользователь без входа по паролю, email `ecp-<16hex>@ecp.invalid`);
   - `POST /internal/v1/sessions/issue` `{user_id}` -> `{token, ttl, user_id, email}`.

Ответ: `{token, user_id, email, ttl, display_name, new}` (`new` = `created`).

## Ошибки (`{"detail": {"code": ...}}`)
`bad_signature` 422, `no_certificate` 422, `certificate_expired` 401, `signature_invalid` 401, `invalid_challenge` 401, `verifier_unavailable` 503, `too_many_attempts` 429, `auth_unavailable` 503 (auth недоступен или ответил ошибкой).

Auth: `users/external` -> 422 `invalid_external_id`; `sessions/issue` -> 404 `user_not_found`. В `auth.db` у `users` добавлена колонка `external_id` (уникальный индекс, миграция при старте).


## Регистрация и вход (режимы)

`POST /internal/v1/ecp/login` принимает дополнительно `register` (по умолчанию `false`) и `company_name`.

- `register=true` — регистрация: нужно `company_name` (2–200 символов, иначе 422 `invalid_company_name`). Если сертификат уже зарегистрирован, 409 `already_registered`. Создаётся пользователь без пароля и компания с этим названием.
- `register=false` — вход: если сертификат неизвестен, 404 `not_registered` (интерфейс предлагает зарегистрироваться по ЭЦП).

Проверки идут после проверки подписи, поэтому неподписанные запросы не могут узнать, зарегистрирован ли сертификат.
