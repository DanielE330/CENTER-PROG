# Имитаторы внешних интеграций (1С, ЭДО, банк)

Три независимых FastAPI-сервиса в `backend/services/`, каждый на своём порту. Общая детерминированная
генерация данных вынесена в пакет `mockdata/` (только данные, без HTTP). Данные строятся из реальной выписки
компании (`data/dataset/statements/<company>.csv`): реальные контрагенты и типичные (медианные) суммы.

| Сервис | Порт | Запуск (из `backend/services`) | Имитирует |
|---|---|---|---|
| `onec` | 8004 | `uv run uvicorn onec.app:app --port 8004` | 1С:Бухгалтерия, OData v3 |
| `edo` | 8005 | `uv run uvicorn edo.app:app --port 8005` | ЭДО (по духу Диадок/СБИС) |
| `bank` | 8006 | `uv run uvicorn bank.app:app --port 8006` | Банковский API выписок, OAuth2 |

Общее: `GET /live` без авторизации; параметр `?company=C001` (по умолчанию C001, неизвестная компания даёт 404);
ответы детерминированы.

Переменные окружения: `MOCK_TODAY` (ГГГГ-ММ-ДД, «сегодня»; по умолчанию последняя дата выписки),
`MOCK_FAIL_RATE` (0..1, доля ответов 503), `MOCK_DELAY_MS` (задержка каждого запроса), `MOCK_DATASET_DIR`
(по умолчанию `data/dataset`), `MOCK_1C_USER`/`MOCK_1C_PASSWORD` (demo/demo), `MOCK_EDO_TOKEN`
(`edo-demo-token`), `MOCK_BANK_CLIENT_ID`/`MOCK_BANK_CLIENT_SECRET` (`demo-client`/`demo-secret`),
`MOCK_BANK_SIGN_KEY`.

## Сценарии в данных (общие для 1С и ЭДО)

Сроки оплаты будущих документов лежат в пределах 60 дней от «сегодня».
- обычные счета клиентам (ожидаемые поступления) и поставщиков (будущие платежи);
- `large_supplier`: крупный счёт поставщику (в 3-5 раз выше типичного), и в 1С, и в ЭДО;
- `overdue`: просроченный счёт клиента (дата оплаты в прошлом) и просроченный счёт поставщика (только 1С);
- `duplicate`: один и тот же счёт поставщика в 1С и в ЭДО (совпадают номер, ИНН, сумма, дата);
- `rejected`: исходящий документ со статусом «Отклонён», в 1С его нет;
- `new_contract`: новый контрагент и договор (комментарий «Новый договор»), счёт по нему только в 1С;
- `draft` (не проведён) и `deleted` (ПометкаУдаления=true) в 1С: интегратор должен их отбрасывать;
- `edo_only_act`: акт поставщика только в ЭДО.
Дедупликация на стороне интегратора: ключ (Номер, ИНН контрагента, сумма). В ЭДО для отладки есть `?debug=true`
(добавляет поле `x_scenario`, в реальных API его нет).

## 1. onec — OData v3 (порт 8004)

Базовый путь `/odata/standard.odata/`. Basic-авторизация, без неё 401 + `WWW-Authenticate: Basic`.
Ответ списка: `{"odata.metadata": ".../$metadata#<Сущность>", "value": [...]}`; ошибки в виде
`{"odata.error": {"code","message":{"lang","value"}}}`.

Ресурсы: `Document_СчетНаОплатуПокупателю` (нам должны), `Document_СчетНаОплатуПоставщика` (мы должны),
`Catalog_Контрагенты`, `Catalog_ДоговорыКонтрагентов`; также корень сервиса и `$metadata`, выборка
по ключу `Сущность(guid'...')`.

Параметры: `$top`, `$skip`, `$filter` (операторы eq ne ge gt le lt, соединение `and`, литералы
`datetime'2026-10-01T00:00:00'`, `guid'..'`, `true/false`, числа, строки), `$orderby` (`Дата desc`),
`$inlinecount=allpages` (добавляет `odata.count`), `$format=json` (иное даёт 406). В фильтре допустимы
псевдонимы Date/Number/Posted/DeletionMark.

Поля счёта: `Ref_Key, DataVersion, ПометкаУдаления, Номер, Дата, Проведен, Организация_Key, Контрагент_Key,
ДоговорКонтрагента_Key, СуммаДокумента, ВалютаДокумента_Key, СуммаВключаетНДС, ДатаОплаты, СрокОплаты
(то же значение), Комментарий`. Даты вида `2026-10-30T00:00:00`.
Контрагент: `Ref_Key, Code, Description, НаименованиеПолное, ИНН, КПП, ЮридическоеФизическоеЛицо, ПометкаУдаления`.
Договор: `Ref_Key, Owner_Key (=Ref_Key контрагента), Номер, Дата, ВидДоговора (СПокупателем|СПоставщиком),
СрокДействия, Комментарий, ...`.

Будущие обязательства: запросить оба документа с фильтром
`Проведен eq true and ПометкаУдаления eq false` (или отфильтровать на клиенте), пейджить по `$top/$skip` до
пустого `value`; обязательство = `СуммаДокумента` на `ДатаОплаты`; `Покупателю` — поступление,
`Поставщика` — списание; ИНН получать через `Контрагент_Key` -> `Catalog_Контрагенты`.

    curl -u demo:demo "localhost:8004/odata/standard.odata/Document_СчетНаОплатуПокупателю?\$top=20&\$skip=0&\$format=json"
    curl -u demo:demo -G "localhost:8004/odata/standard.odata/Document_СчетНаОплатуПоставщика" \
      --data-urlencode "\$filter=ДатаОплаты ge datetime'2026-10-01T00:00:00' and Проведен eq true"
    curl -u demo:demo "localhost:8004/odata/standard.odata/Catalog_Контрагенты"

## 2. edo — REST (порт 8005)

Авторизация: `Authorization: Bearer <token>` или `X-Api-Token: <token>`; иначе 401 `{"detail":{"error":"unauthorized",...}}`.

`GET /edo/v1/documents` — параметры `direction=incoming|outgoing` (incoming: от поставщика, мы должны;
outgoing: мы выставили клиенту), `from`, `to` (по дате документа), `status`, `type`, `limit` (1-100, по умолчанию 50),
`cursor`, `company`. Ответ: `{"documents":[...], "next_cursor": "<строка>|null", "total": N}`; пейджить, пока
`next_cursor` не null. Неверный курсор даёт 400. `GET /edo/v1/documents/{id}` — один документ.

Документ: `id, direction, type (УПД|Счёт|Акт), type_code, number, date, status (Отправлен|Подписан|Отклонён),
status_updated_at, counterparty{inn,name}, own_inn, amount, vat_amount, currency, payment_due_date,
rejection_reason`.

Будущие обязательства: все документы с `payment_due_date >= сегодня` (и просроченные), кроме `status = Отклонён`;
`incoming` — списание, `outgoing` — поступление; дубли с 1С убирать по (number, inn, amount).

    curl -H "Authorization: Bearer edo-demo-token" "localhost:8005/edo/v1/documents?direction=incoming&from=2026-09-01&limit=50"
    curl -H "Authorization: Bearer edo-demo-token" "localhost:8005/edo/v1/documents?cursor=bzox"

## 3. bank — OAuth2 (порт 8006)

`POST /bank/oauth/token` (form: `grant_type=client_credentials`, `client_id`, `client_secret`, либо HTTP Basic) ->
`{"access_token","token_type":"Bearer","expires_in":3600,"scope"}`; неверные данные 401 `invalid_client`.
Остальные запросы с `Authorization: Bearer <access_token>`, иначе 401 `invalid_token`.

- `GET /bank/v1/accounts?company=` -> `{"accounts":[{id, number, currency, type, status, owner{inn,name}, bank{bic,name},
  opened_at, balance{amount,currency,as_of}}]}`; `id` = `acc-<company>-1`.
- `GET /bank/v1/accounts/{id}/balance?date=` -> `{account_id, amount, currency, as_of}` (остаток на конец дня).
- `GET /bank/v1/accounts/{id}/transactions?from=&to=&page=&per_page=` (page с 1, per_page до 500, по умолчанию 100;
  без дат — последние 30 дней до «сегодня», будущее не отдаётся) -> `{account_id, from, to, page, per_page, total, pages,
  items:[{id, booking_date, amount (>0), currency, direction CREDIT|DEBIT, counterparty{inn,name}, purpose, balance_after}]}`.
Операции — реальные строки выписки датасета.

Будущих обязательств банк не отдаёт (только факт: операции и остаток); они берутся из 1С и ЭДО.

    T=$(curl -s -d grant_type=client_credentials -d client_id=demo-client -d client_secret=demo-secret \
        localhost:8006/bank/oauth/token | jq -r .access_token)
    curl -H "Authorization: Bearer $T" localhost:8006/bank/v1/accounts
    curl -H "Authorization: Bearer $T" "localhost:8006/bank/v1/accounts/acc-C001-1/transactions?from=2026-09-01&to=2026-09-30&page=1"

## Устойчивость

`MOCK_FAIL_RATE=0.3 MOCK_DELAY_MS=200 uv run uvicorn ...` — 30% запросов вернут 503, остальные с задержкой.
Клиент должен ретраить 503. Тесты: `uv run pytest tests/test_onec.py tests/test_edo.py tests/test_bank.py`
(без сети; пропускаются, если нет `data/dataset`).
