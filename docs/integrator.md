# integrator (Python, порт 8008)

Одна задача: забрать счета и документы из 1С (OData) и ЭДО и привести к единому списку ожидаемых платежей. Банк используется только для остатка (сверка). Состояния нет.

Запуск (из `backend/services`): `uv run uvicorn integrator.app:app --port 8008`. Тесты: `uv run pytest tests/test_integrator.py`.

Env: `ONEC_URL` (http://localhost:8004), `ONEC_USER`/`ONEC_PASSWORD` (demo/demo), `EDO_URL` (:8005), `EDO_TOKEN` (edo-demo-token), `BANK_URL` (:8006), `BANK_CLIENT_ID`/`BANK_CLIENT_SECRET` (demo-client/demo-secret), `INTEGRATOR_TIMEOUT` (5 c), `INTEGRATOR_RETRY_PAUSE` (0.3 c), `INTEGRATOR_TODAY` (as_of по умолчанию; иначе `MOCK_TODAY`, иначе сегодня).

## GET /internal/v1/obligations?company=C001[&as_of=YYYY-MM-DD]
`{company, as_of, sources:{onec|edo:{status:ok|error, error, ms, count, duplicates, rejected, skipped}}, items:[...]}`.
Элемент: `external_id, source(onec|edo), kind(invoice|contract|act|upd), direction(in|out), date (срок оплаты), amount_kop (строка), title, counterparty, inn, number, confidence 0..1, status, overdue, duplicate_of (в items всегда null: дубли не включаются)`.

Логика: 1С: `Проведен eq true and ПометкаУдаления eq false`, пагинация `$top/$skip`, ИНН из `Catalog_Контрагенты`; «Покупателю» = in, «Поставщика» = out. ЭДО: курсорная пагинация, «Отклонён» отбрасывается; incoming = out, outgoing = in. Дубль по (номер, ИНН, сумма): остаётся запись 1С, дубль из ЭДО учитывается в `sources.edo.duplicates`, у оставшейся confidence +0.03. Просроченные (срок раньше as_of) возвращаются с `overdue=true` и confidence −0.25. Confidence: 1С проведён 0.9, ЭДО подписан 0.95, отправлен 0.7. Непроведённые и удалённые документы 1С отбрасываются (`skipped`). Ретраи 2 раза при 5xx и сетевых ошибках; упавший источник даёт `status=error`, ответ 200 с данными второго. Договоры (`contract`) пока приходят только если ЭДО отдаст тип «Договор».

## GET /internal/v1/bank-balance?company=C001
`{company, status, balance_kop (строка), currency, last_date, accounts:[...]}`; 502 при недоступном банке.
