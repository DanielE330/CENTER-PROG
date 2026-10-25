//! Обработчики внутреннего API ledger. Тела ответов и коды ошибок совпадают с прежним монолитом.

use crate::AppState;
use axum::{
    Json,
    extract::{Multipart, Path, Query, State},
    http::StatusCode,
};
use chrono::{Duration, NaiveDate};
use common::{ApiError, Ctx, Event, HistoryRow, R, Snapshot, Tx, err, internal, money, stringify_money};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};

pub(crate) fn lock(st: &AppState) -> std::sync::MutexGuard<'_, rusqlite::Connection> {
    st.db.lock().unwrap_or_else(|e| e.into_inner())
}

pub(crate) fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

pub(crate) fn audit(st: &AppState, ctx: &Ctx, cid: &str, action: &str, object: &str, outcome: &str) {
    let c = lock(st);
    let _ = c.execute(
        "INSERT INTO audit_events(at, actor, company_id, action, object_id, outcome, request_id) VALUES(?,?,?,?,?,?,?)",
        params![now(), ctx.actor, cid, action, object, outcome, ctx.rid],
    );
}

pub(crate) fn not_found() -> ApiError {
    err(StatusCode::NOT_FOUND, "not_found", "Не найдено")
}

fn bump(c: &rusqlite::Connection, cid: &str) -> R<()> {
    c.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [cid]).map_err(internal)?;
    Ok(())
}

/// Новая ревизия, в которой уже учтённые операции изменились (не дописаны): прогноз по дельте здесь невозможен.
fn bump_reset(c: &rusqlite::Connection, cid: &str) -> R<()> {
    c.execute("UPDATE companies SET data_revision=data_revision+1, tx_reset_rev=data_revision+1 WHERE id=?", [cid]).map_err(internal)?;
    Ok(())
}

// ---------- компании ----------
#[derive(Deserialize)]
pub struct EnsureBody {
    owner_id: String,
    #[serde(default)]
    email: String,
    #[serde(default)]
    name: Option<String>,
    /// реквизиты из сертификата ЭЦП: только цифры, иначе игнорируются
    #[serde(default)]
    inn: Option<String>,
    #[serde(default)]
    ogrn: Option<String>,
}

fn digits(v: &Option<String>, lens: &[usize]) -> String {
    let d: String = v.as_deref().unwrap_or("").chars().filter(|c| c.is_ascii_digit()).collect();
    if lens.contains(&d.len()) { d } else { String::new() }
}

/// Компания пользователя: для сотрудника — компания директора (новая не создаётся), для директора — его компания,
/// у нового пользователя она создаётся при первом обращении (он становится директором).
pub async fn ensure_company(State(st): State<AppState>, Json(b): Json<EnsureBody>) -> R<Json<Value>> {
    let c = lock(&st);
    let (inn, ogrn) = (digits(&b.inn, &[10, 12]), digits(&b.ogrn, &[13, 15]));
    let by_member: Option<(String, String)> = c
        .query_row("SELECT company_id, role FROM company_members WHERE user_id=?", [&b.owner_id], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?;
    let found = match by_member {
        Some(x) => Some(x),
        // база без записи участника (не должно быть после миграции): владелец компании — директор, если у компании нет другого
        // (бывший владелец, которого удалили или который вышел, права не возвращает)
        None => c
            .query_row(
                "SELECT id FROM companies WHERE owner_id=? AND NOT EXISTS (SELECT 1 FROM company_members m WHERE m.company_id = companies.id AND m.role='director')",
                [&b.owner_id],
                |r| r.get::<_, String>(0),
            )
            .optional()
            .map_err(internal)?
            .map(|id| (id, "director".to_string())),
    };
    if let Some((id, role)) = found {
        if role == "director" {
            c.execute(
                "INSERT OR IGNORE INTO company_members(company_id, user_id, email, role, created_at, created_by) VALUES(?,?,?, 'director', ?, '')",
                params![id, b.owner_id, b.email, now()],
            )
            .map_err(internal)?;
            if !b.email.is_empty() {
                c.execute("UPDATE company_members SET email=? WHERE user_id=? AND email<>?", params![b.email, b.owner_id, b.email]).map_err(internal)?;
            }
            // реквизиты, которых ещё не было, дополняем из сертификата; введённые раньше не затираем
            let (cur_inn, cur_ogrn): (String, String) = c.query_row("SELECT inn, ogrn FROM companies WHERE id=?", [&id], |r| Ok((r.get(0)?, r.get(1)?))).map_err(internal)?;
            let (inn, ogrn) = (if cur_inn.is_empty() { inn } else { cur_inn.clone() }, if cur_ogrn.is_empty() { ogrn } else { cur_ogrn.clone() });
            if (inn.as_str(), ogrn.as_str()) != (cur_inn.as_str(), cur_ogrn.as_str()) {
                c.execute("UPDATE companies SET inn=?, ogrn=? WHERE id=?", params![inn, ogrn, id]).map_err(internal)?;
            }
        }
        return Ok(Json(member_view(&c, &id, &role)?));
    }
    let id = uuid::Uuid::new_v4().to_string();
    let name = b.name.unwrap_or_else(|| format!("Компания {}", b.email.split('@').next().unwrap_or("")));
    let created = now();
    // owner_id уникален: у вышедшего из команды бывшего владельца он уже занят старой компанией
    let taken: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE owner_id=?", [&b.owner_id], |r| r.get(0)).map_err(internal)?;
    let owner_key = if taken > 0 { format!("{}#{id}", b.owner_id) } else { b.owner_id.clone() };
    c.execute("INSERT INTO companies(id, owner_id, name, created_at, inn, ogrn) VALUES(?,?,?,?,?,?)", params![id, owner_key, name, created, inn, ogrn]).map_err(internal)?;
    c.execute(
        "INSERT INTO company_members(company_id, user_id, email, role, created_at, created_by) VALUES(?,?,?, 'director', ?, '')",
        params![id, b.owner_id, b.email, created],
    )
    .map_err(internal)?;
    Ok(Json(member_view(&c, &id, "director")?))
}

/// Компания глазами участника: реквизиты, роль и состояние блокировки.
pub(crate) fn member_view(c: &rusqlite::Connection, cid: &str, role: &str) -> R<Value> {
    let (name, inn, ogrn, banned_at, reason): (String, String, String, Option<String>, Option<String>) = c
        .query_row("SELECT name, inn, ogrn, banned_at, ban_reason FROM companies WHERE id=?", [cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))
        .map_err(internal)?;
    Ok(json!({"company_id": cid, "company_name": name, "inn": inn, "ogrn": ogrn, "role": role, "banned": banned_at.is_some(), "ban_reason": reason}))
}

pub async fn get_company(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    let row: Option<(String, i64, String, String, Option<String>, Option<String>)> = c
        .query_row("SELECT name, data_revision, inn, ogrn, banned_at, ban_reason FROM companies WHERE id=?", [&cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))
        .optional()
        .map_err(internal)?;
    let (name, rev, inn, ogrn, banned_at, reason) = row.ok_or_else(not_found)?;
    Ok(Json(json!({"company_id": cid, "company_name": name, "data_revision": rev, "inn": inn, "ogrn": ogrn, "banned": banned_at.is_some(), "ban_reason": reason})))
}

#[derive(Deserialize)]
pub struct CompanyPatch {
    #[serde(default)]
    name: Option<String>,
    /// реквизиты: пустая строка очищает, иначе только цифры (ИНН 10 или 12, ОГРН 13 или 15)
    #[serde(default)]
    inn: Option<String>,
    #[serde(default)]
    ogrn: Option<String>,
}

fn requisite(v: &Option<String>, lens: &[usize], code: &str, msg: &str) -> R<Option<String>> {
    let Some(s) = v.as_deref().map(str::trim) else { return Ok(None) };
    if s.is_empty() {
        return Ok(Some(String::new()));
    }
    if s.bytes().all(|b| b.is_ascii_digit()) && lens.contains(&s.len()) {
        Ok(Some(s.to_string()))
    } else {
        Err(err(StatusCode::UNPROCESSABLE_ENTITY, code, msg))
    }
}

pub async fn update_company(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<CompanyPatch>) -> R<Json<Value>> {
    let name = match b.name.as_deref().map(str::trim) {
        Some(n) if n.chars().count() < 2 || n.chars().count() > 200 => {
            return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_company_name", "Название от 2 до 200 символов"));
        }
        Some(n) => Some(n.to_string()),
        None => None,
    };
    let inn = requisite(&b.inn, &[10, 12], "invalid_inn", "ИНН: 10 или 12 цифр")?;
    let ogrn = requisite(&b.ogrn, &[13, 15], "invalid_ogrn", "ОГРН: 13 или 15 цифр")?;
    if name.is_none() && inn.is_none() && ogrn.is_none() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "empty_patch", "Нечего менять"));
    }
    let c = lock(&st);
    if let Some(n) = &name {
        c.execute("UPDATE companies SET name=? WHERE id=?", params![n, cid]).map_err(internal)?;
    }
    if let Some(v) = &inn {
        c.execute("UPDATE companies SET inn=? WHERE id=?", params![v, cid]).map_err(internal)?;
    }
    if let Some(v) = &ogrn {
        c.execute("UPDATE companies SET ogrn=? WHERE id=?", params![v, cid]).map_err(internal)?;
    }
    let (name, inn, ogrn): (String, String, String) = c
        .query_row("SELECT name, inn, ogrn FROM companies WHERE id=?", [&cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .optional()
        .map_err(internal)?
        .ok_or_else(not_found)?;
    drop(c);
    audit(&st, &ctx, &cid, "company.update", &cid, "ok");
    Ok(Json(json!({"company_id": cid, "company_name": name, "inn": inn, "ogrn": ogrn})))
}

// ---------- снимок для расчётных сервисов ----------
/// Операции компании, добавленные после ревизии `since` (0 — все).
fn load_history(c: &rusqlite::Connection, cid: &str, since: i64) -> R<Vec<HistoryRow>> {
    let mut s = c
        .prepare("SELECT date, amount_kop, balance_kop, counterparty, inn, purpose, category FROM transactions WHERE company_id=?1 AND (?2 = 0 OR rev>?2) ORDER BY date, id")
        .map_err(internal)?;
    let v = s
        .query_map(params![cid, since], |r| {
            Ok(HistoryRow {
                tx: Tx {
                    date: NaiveDate::parse_from_str(&r.get::<_, String>(0)?, "%Y-%m-%d").unwrap_or_default(),
                    amount: r.get(1)?,
                    counterparty: r.get(3)?,
                    inn: r.get(4)?,
                    purpose: r.get(5)?,
                    category: r.get(6)?,
                },
                balance: r.get::<_, Option<i64>>(2)?,
            })
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(v)
}

fn load_events(c: &rusqlite::Connection, cid: &str) -> R<Vec<Event>> {
    let mut s = c.prepare("SELECT id, date, amount_kop, title, source, confidence, counterparty FROM expected_events WHERE company_id=? ORDER BY date").map_err(internal)?;
    let v = s
        .query_map([cid], |r| {
            Ok(Event {
                id: r.get(0)?,
                date: NaiveDate::parse_from_str(&r.get::<_, String>(1)?, "%Y-%m-%d").unwrap_or_default(),
                amount: r.get(2)?,
                title: r.get(3)?,
                source: r.get(4)?,
                confidence: r.get(5)?,
                counterparty: r.get(6)?,
            })
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(v)
}

#[derive(Deserialize)]
pub struct SnapshotQuery {
    since_rev: Option<i64>,
}

/// История, события и ревизия одним согласованным чтением. Суммы — числа (не для публикации в SPA).
/// `since_rev`: только операции, добавленные после этой ревизии (дописывание прогноза без полного пересчёта).
pub async fn snapshot(State(st): State<AppState>, Path(cid): Path<String>, Query(q): Query<SnapshotQuery>) -> R<Json<Snapshot>> {
    let c = lock(&st);
    let (rev, reset): (i64, i64) = c
        .query_row("SELECT data_revision, tx_reset_rev FROM companies WHERE id=?", [&cid], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?
        .ok_or_else(not_found)?;
    let since = q.since_rev.unwrap_or(0).max(0);
    Ok(Json(Snapshot { data_revision: rev, tx_reset_rev: reset, transactions: load_history(&c, &cid, since)?, events: load_events(&c, &cid)?, counterparty_risks: crate::counterparties::load_risks(&c, &cid, true)? }))
}

// ---------- импорт ----------
const CATEGORIES: &[&str] = &[
    "revenue", "other_income", "suppliers", "payroll", "rent", "taxes", "loans", "utilities", "bank_fees", "marketing", "transfers", "refunds", "other_expense",
];

fn dedup_keys(txs: &[Value]) -> Vec<String> {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    txs.iter()
        .map(|t| {
            let base = format!("{}|{}|{}|{}", t["date"].as_str().unwrap_or(""), t["amount_kop"].as_str().unwrap_or(""), t["inn"].as_str().unwrap_or(""), t["purpose"].as_str().unwrap_or(""));
            let n = seen.entry(base.clone()).or_insert(0);
            *n += 1;
            format!("{base}|{n}")
        })
        .collect()
}

pub async fn upload_import(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, mut mp: Multipart) -> R<(StatusCode, Json<Value>)> {
    let mut file: Option<(String, Vec<u8>)> = None;
    while let Some(f) = mp.next_field().await.map_err(|_| err(StatusCode::BAD_REQUEST, "bad_upload", "Не удалось прочитать файл"))? {
        if f.name() == Some("file") {
            let name = f.file_name().unwrap_or("upload").to_string();
            let data = f.bytes().await.map_err(|_| err(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large", "Файл больше 25 МБ"))?;
            file = Some((name, data.to_vec()));
        }
    }
    let (name, data) = file.ok_or_else(|| err(StatusCode::BAD_REQUEST, "no_file", "Файл не передан"))?;
    let lname = name.to_lowercase();
    if !(lname.ends_with(".csv") || lname.ends_with(".xlsx") || lname.ends_with(".txt") || lname.ends_with(".1c")) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "unsupported_type", "Поддерживаются CSV, XLSX и выписка 1С (.txt, .1c)"));
    }
    if data.len() > 25 * 1024 * 1024 {
        return Err(err(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large", "Файл больше 25 МБ"));
    }
    let digest = hex::encode(Sha256::digest(&data));
    {
        let c = lock(&st);
        let dup: Option<String> = c
            .query_row("SELECT id FROM imports WHERE company_id=? AND digest=? AND status='confirmed'", params![cid, digest], |r| r.get(0))
            .optional()
            .map_err(internal)?;
        if dup.is_some() {
            return Err(err(StatusCode::CONFLICT, "duplicate_file", "Этот файл уже загружен"));
        }
    }
    let r = st.parser.upload("/internal/v1/parse", &ctx, name.clone(), data).await?;
    let status = r.status;
    let parsed: Value = r.json();
    if !status.is_success() {
        let code = parsed["detail"]["code"].as_str().unwrap_or("unreadable_file");
        let msg = match code {
            "no_date_column" => "Не найдена колонка с датой операции",
            "no_amount_column" => "Не найдена колонка с суммой",
            "empty_file" => "Файл пуст",
            "too_many_rows" => "Слишком много строк (максимум 200 000)",
            "file_too_large" => "Файл больше 25 МБ",
            "bad_encoding" => "Не удалось определить кодировку файла",
            "multiple_companies" => "В файле операции нескольких компаний: выберите компанию при загрузке",
            _ => "Не удалось разобрать файл",
        };
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "parse_failed", format!("{msg} ({code})")));
    }
    let mut txs = parsed["transactions"].as_array().cloned().unwrap_or_default();
    enrich_categories(&st, &ctx, &cid, &mut txs).await;
    if txs.is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "no_valid_rows", "В файле нет ни одной корректной операции"));
    }
    // уже существующие операции (перекрывающиеся выписки)
    let keys = dedup_keys(&txs);
    let existing: HashSet<String> = {
        let c = lock(&st);
        let mut s = c.prepare("SELECT dedup_key FROM transactions WHERE company_id=? AND date>=? AND date<=?").map_err(internal)?;
        let from = parsed["stats"]["date_from"].as_str().unwrap_or("0000-00-00").to_string();
        let to = parsed["stats"]["date_to"].as_str().unwrap_or("9999-99-99").to_string();
        s.query_map(params![cid, from, to], |r| r.get::<_, String>(0)).map_err(internal)?.filter_map(|x| x.ok()).collect()
    };
    let dups = keys.iter().filter(|k| existing.contains(*k)).count();
    let mut cats: BTreeMap<String, usize> = BTreeMap::new();
    for t in &txs {
        *cats.entry(t["category"].as_str().unwrap_or("").to_string()).or_default() += 1;
    }
    let errors: Vec<Value> = parsed["errors"].as_array().cloned().unwrap_or_default();
    let summary = json!({
        "stats": parsed["stats"],
        "rows_total": txs.len() + errors.len(),
        "errors_total": errors.len(),
        "errors": errors.iter().take(100).collect::<Vec<_>>(),
        "duplicates_existing": dups,
        "categories": cats,
        "sample": txs.iter().take(15).collect::<Vec<_>>(),
    });
    let id = uuid::Uuid::new_v4().to_string();
    {
        let c = lock(&st);
        c.execute(
            "INSERT INTO imports(id, company_id, filename, digest, status, summary, payload, created_at) VALUES(?,?,?,?, 'preview', ?,?,?)",
            params![id, cid, name, digest, summary.to_string(), serde_json::to_string(&txs).unwrap(), now()],
        )
        .map_err(internal)?;
    }
    audit(&st, &ctx, &cid, "import.upload", &id, "ok");
    let mut out = json!({"import_id": id, "status": "preview", "summary": summary});
    stringify_money(&mut out);
    Ok((StatusCode::CREATED, Json(out)))
}

pub async fn list_imports(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    let mut s = c
        .prepare("SELECT id, filename, status, created_at, summary FROM imports WHERE company_id=? ORDER BY created_at DESC LIMIT 50")
        .map_err(internal)?;
    let items: Vec<Value> = s
        .query_map([&cid], |r| {
            let summary: Value = serde_json::from_str(&r.get::<_, String>(4)?).unwrap_or(Value::Null);
            Ok(json!({"import_id": r.get::<_, String>(0)?, "filename": r.get::<_, String>(1)?, "status": r.get::<_, String>(2)?,
                "created_at": r.get::<_, String>(3)?, "rows_ok": summary["stats"]["rows_ok"], "errors_total": summary["errors_total"],
                "date_from": summary["stats"]["date_from"], "date_to": summary["stats"]["date_to"]}))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(Json(json!({"items": items})))
}

pub async fn get_import(State(st): State<AppState>, Path((cid, iid)): Path<(String, String)>) -> R<Json<Value>> {
    let c = lock(&st);
    let row: Option<(String, String, String)> = c
        .query_row("SELECT status, summary, filename FROM imports WHERE id=? AND company_id=?", params![iid, cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .optional()
        .map_err(internal)?;
    let (status, summary, filename) = row.ok_or_else(not_found)?;
    Ok(Json(json!({"import_id": iid, "status": status, "filename": filename, "summary": serde_json::from_str::<Value>(&summary).unwrap_or(Value::Null)})))
}

/// Отмена подтверждённого импорта: удаляет его операции и запись импорта, повышает ревизию данных (прогноз пересчитается).
pub async fn delete_import(State(st): State<AppState>, ctx: Ctx, Path((cid, iid)): Path<(String, String)>) -> R<Json<Value>> {
    let mut c = lock(&st);
    let exists: Option<i64> = c
        .query_row("SELECT 1 FROM imports WHERE id=? AND company_id=?", params![iid, cid], |r| r.get(0))
        .optional()
        .map_err(internal)?;
    if exists.is_none() {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено"));
    }
    let tx = c.transaction().map_err(internal)?;
    let removed = tx.execute("DELETE FROM transactions WHERE import_id=? AND company_id=?", params![iid, cid]).map_err(internal)?;
    tx.execute("DELETE FROM imports WHERE id=? AND company_id=?", params![iid, cid]).map_err(internal)?;
    tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    tx.commit().map_err(internal)?;
    drop(c);
    let _ = (&ctx, removed);
    Ok(Json(json!({"import_id": iid, "removed": removed})))
}

pub async fn confirm_import(State(st): State<AppState>, ctx: Ctx, Path((cid, iid)): Path<(String, String)>) -> R<Json<Value>> {
    let mut c = lock(&st);
    let row: Option<(String, Option<String>)> = c
        .query_row("SELECT status, payload FROM imports WHERE id=? AND company_id=?", params![iid, cid], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?;
    let (status, payload) = row.ok_or_else(not_found)?;
    if status != "preview" {
        return Err(err(StatusCode::CONFLICT, "already_confirmed", "Импорт уже подтверждён"));
    }
    let txs: Vec<Value> = serde_json::from_str(&payload.unwrap_or_default()).map_err(internal)?;
    let keys = dedup_keys(&txs);
    let tx = c.transaction().map_err(internal)?;
    let (mut inserted, mut skipped) = (0usize, 0usize);
    {
        let mut ins = tx
            .prepare(
                "INSERT OR IGNORE INTO transactions(company_id, import_id, row_no, date, amount_kop, balance_kop, counterparty, inn, purpose, category, category_conf, dedup_key, subcategory, grp, method, rev)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,(SELECT data_revision+1 FROM companies WHERE id=?1))",
            )
            .map_err(internal)?;
        for (t, k) in txs.iter().zip(keys.iter()) {
            let amount: i64 = t["amount_kop"].as_str().and_then(|s| s.parse().ok()).ok_or_else(|| internal("bad amount"))?;
            let balance: Option<i64> = t["balance_kop"].as_str().and_then(|s| s.parse().ok());
            let n = ins
                .execute(params![
                    cid, iid, t["row_no"].as_i64().unwrap_or(0), t["date"].as_str().unwrap_or(""), amount, balance,
                    t["counterparty"].as_str().unwrap_or(""), t["inn"].as_str().unwrap_or(""), t["purpose"].as_str().unwrap_or(""),
                    t["category"].as_str().unwrap_or("other_expense"), t["category_confidence"].as_f64().unwrap_or(0.0), k,
                    t["subcategory"].as_str().unwrap_or(""), t["group"].as_str().unwrap_or(""), t["method"].as_str().unwrap_or("")
                ])
                .map_err(internal)?;
            if n == 1 { inserted += 1 } else { skipped += 1 }
        }
    }
    tx.execute("UPDATE imports SET status='confirmed', payload=NULL WHERE id=?", [&iid]).map_err(internal)?;
    tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    tx.commit().map_err(internal)?;
    drop(c);
    audit(&st, &ctx, &cid, "import.confirm", &iid, "ok");
    Ok(Json(json!({"import_id": iid, "status": "confirmed", "inserted": inserted, "skipped_duplicates": skipped})))
}

// ---------- операции ----------
#[derive(Deserialize)]
pub struct TxQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    category: Option<String>,
    direction: Option<String>,
    q: Option<String>,
    sort: Option<String>,
    order: Option<String>,
}

pub async fn list_transactions(State(st): State<AppState>, Path(cid): Path<String>, Query(q): Query<TxQuery>) -> R<Json<Value>> {
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let offset = q.offset.unwrap_or(0).max(0);
    let cat = q.category.unwrap_or_default();
    let dir = q.direction.unwrap_or_default();
    let search = q.q.unwrap_or_default().trim().to_lowercase();
    let order_col = match q.sort.as_deref() {
        Some("amount") => "ABS(amount_kop)",
        Some("counterparty") => "counterparty COLLATE NOCASE",
        Some("category") => "category",
        _ => "date",
    };
    let dir_sql = if q.order.as_deref() == Some("asc") { "ASC" } else { "DESC" };
    let c = lock(&st);
    let where_ = "company_id=?1 AND (?2='' OR category=?2) AND (?3='' OR (?3='in' AND amount_kop>0) OR (?3='out' AND amount_kop<0)) AND (?6='' OR instr(lower(counterparty),?6)>0 OR instr(lower(purpose),?6)>0)";
    let total: i64 = c.query_row(&format!("SELECT COUNT(*) FROM transactions WHERE {where_}"), params![cid, cat, dir, "", "", search], |r| r.get(0)).map_err(internal)?;
    let mut s = c
        .prepare(&format!(
            "SELECT id, date, amount_kop, counterparty, purpose, category, category_conf, manual, balance_kop, subcategory, grp, method FROM transactions WHERE {where_} ORDER BY {order_col} {dir_sql}, id {dir_sql} LIMIT ?4 OFFSET ?5"
        ))
        .map_err(internal)?;
    let items: Vec<Value> = s
        .query_map(params![cid, cat, dir, limit, offset, search], |r| {
            Ok(json!({"id": r.get::<_, i64>(0)?, "date": r.get::<_, String>(1)?, "amount_kop": r.get::<_, i64>(2)?, "counterparty": r.get::<_, String>(3)?,
                "purpose": r.get::<_, String>(4)?, "category": r.get::<_, String>(5)?, "category_confidence": r.get::<_, f64>(6)?,
                "manual": r.get::<_, i64>(7)? == 1, "balance_kop": r.get::<_, Option<i64>>(8)?,
                "subcategory": r.get::<_, String>(9)?, "group": r.get::<_, String>(10)?, "method": r.get::<_, String>(11)?}))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(money(json!({"items": items, "total": total, "limit": limit, "offset": offset})))
}

#[derive(Deserialize)]
pub struct CategoryBody {
    #[serde(default)]
    category: Option<String>,
    /// подкатегория таксономии: категория и группа выводятся из неё, правка запоминается как правило компании
    #[serde(default)]
    subcategory: Option<String>,
}

/// Минимум обучающих примеров хвостовой модели компании (совпадает с `parser.tail.MIN_SAMPLES`).
const TAIL_MIN_TRAIN: usize = 200;
const TAIL_MAX_TRAIN: i64 = 30_000;
const FALLBACK_SUBS: [&str; 2] = ["other_in", "other_out"];

fn rule_key(counterparty: &str) -> String {
    counterparty.trim().to_lowercase()
}

/// Подкатегория и группа из словаря парсера: (подкатегория -> (группа, категория ledger)).
async fn taxonomy(st: &AppState, ctx: &Ctx) -> Option<BTreeMap<String, (String, String)>> {
    let v = st.parser.get("/internal/v1/taxonomy", ctx).await.ok()?;
    let subs = v["subcategories"].as_object()?;
    Some(subs.iter().map(|(k, x)| (k.clone(), (x["group"].as_str().unwrap_or("other").to_string(), x["category"].as_str().unwrap_or("").to_string()))).collect())
}

fn apply_sub(t: &mut Value, sub: &str, group: &str, category: &str, conf: f64, method: &str) {
    t["subcategory"] = json!(sub);
    t["group"] = json!(group);
    t["category"] = json!(category);
    t["category_confidence"] = json!(conf);
    t["method"] = json!(method);
}

/// Подкатегории из правил компании (правки пользователя) и из модели на хвост; не критично: при сбое парсера
/// импорт идёт с разметкой по правилам, как раньше.
async fn enrich_categories(st: &AppState, ctx: &Ctx, cid: &str, txs: &mut [Value]) {
    let tax = match taxonomy(st, ctx).await {
        Some(t) => t,
        None => return,
    };
    let rules: BTreeMap<String, String> = {
        let c = lock(st);
        let mut q = match c.prepare("SELECT key, subcategory FROM tenant_rules WHERE company_id=?") {
            Ok(q) => q,
            Err(_) => return,
        };
        q.query_map([cid], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map(|it| it.filter_map(|x| x.ok()).collect()).unwrap_or_default()
    };
    let mut pending: Vec<usize> = vec![];
    for (i, t) in txs.iter_mut().enumerate() {
        let key = rule_key(t["counterparty"].as_str().unwrap_or(""));
        if let Some((group, cat)) = rules.get(&key).filter(|_| !key.is_empty()).and_then(|s| tax.get(s).map(|g| (g.0.clone(), g.1.clone()))) {
            let sub = rules[&key].clone();
            apply_sub(t, &sub, &group, &cat, 1.0, "tenant_rule");
        } else if t["method"].as_str() == Some("fallback") {
            pending.push(i);
        }
    }
    if pending.is_empty() {
        return;
    }
    let train: Vec<Value> = {
        let c = lock(st);
        let mut q = match c.prepare(
            "SELECT purpose, counterparty, amount_kop, subcategory FROM transactions WHERE company_id=?1 AND subcategory<>'' AND subcategory NOT IN ('other_in','other_out') ORDER BY id DESC LIMIT ?2",
        ) {
            Ok(q) => q,
            Err(_) => return,
        };
        q.query_map(params![cid, TAIL_MAX_TRAIN], |r| Ok(json!({"purpose": r.get::<_, String>(0)?, "name": r.get::<_, String>(1)?, "amount_kop": r.get::<_, i64>(2)?, "subcategory": r.get::<_, String>(3)?})))
            .map(|it| it.filter_map(|x| x.ok()).collect())
            .unwrap_or_default()
    };
    if train.len() < TAIL_MIN_TRAIN {
        return;
    }
    let rows: Vec<Value> = pending
        .iter()
        .map(|&i| json!({"purpose": txs[i]["purpose"], "name": txs[i]["counterparty"], "amount_kop": txs[i]["amount_kop"].as_str().and_then(|s| s.parse::<i64>().ok()).unwrap_or(0)}))
        .collect();
    let resp = match st.parser.post("/internal/v1/tail", ctx, &json!({"train": train, "rows": rows})).await {
        Ok(v) => v,
        Err(_) => return,
    };
    let preds = resp["predictions"].as_array().cloned().unwrap_or_default();
    for (&i, p) in pending.iter().zip(preds.iter()) {
        if let (Some(sub), Some(group), Some(cat)) = (p["subcategory"].as_str(), p["group"].as_str(), p["category"].as_str()) {
            apply_sub(&mut txs[i], sub, group, cat, p["confidence"].as_f64().unwrap_or(0.0), "tail");
        }
    }
}

pub async fn get_taxonomy(State(st): State<AppState>, ctx: Ctx) -> R<Json<Value>> {
    st.parser.get("/internal/v1/taxonomy", &ctx).await.map(Json)
}

pub async fn set_category(State(st): State<AppState>, ctx: Ctx, Path((cid, id)): Path<(String, i64)>, Json(b): Json<CategoryBody>) -> R<Json<Value>> {
    if let Some(sub) = b.subcategory.as_deref().filter(|s| !s.is_empty()) {
        let tax = taxonomy(&st, &ctx).await.ok_or_else(|| err(StatusCode::BAD_GATEWAY, "taxonomy_unavailable", "Справочник подкатегорий недоступен"))?;
        let (group, category) = tax.get(sub).cloned().ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_subcategory", "Неизвестная подкатегория"))?;
        let mut c = lock(&st);
        let cp: Option<String> = c
            .query_row("SELECT counterparty FROM transactions WHERE id=? AND company_id=?", params![id, cid], |r| r.get(0))
            .optional()
            .map_err(internal)?;
        let cp = cp.ok_or_else(not_found)?;
        let key = rule_key(&cp);
        let tx = c.transaction().map_err(internal)?;
        tx.execute(
            "UPDATE transactions SET category=?, subcategory=?, grp=?, method='manual', manual=1, category_conf=1.0 WHERE id=? AND company_id=?",
            params![category, sub, group, id, cid],
        )
        .map_err(internal)?;
        let mut same = 0;
        if !key.is_empty() && !FALLBACK_SUBS.contains(&sub) {
            // правило компании действует сразу: на остальные операции этого контрагента, которых не правили вручную
            tx.execute(
                "INSERT INTO tenant_rules(company_id, key, subcategory, created_at) VALUES(?,?,?,?) ON CONFLICT(company_id, key) DO UPDATE SET subcategory=excluded.subcategory",
                params![cid, key, sub, now()],
            )
            .map_err(internal)?;
            // регистр сравниваем в Rust: SQLite lower() не приводит кириллицу
            let income = tx.query_row("SELECT amount_kop FROM transactions WHERE id=?", [id], |r| r.get::<_, i64>(0)).map_err(internal)? > 0;
            let ids: Vec<i64> = {
                let mut q = tx.prepare("SELECT id, counterparty FROM transactions WHERE company_id=? AND manual=0 AND (amount_kop>0)=?").map_err(internal)?;
                let rows = q.query_map(params![cid, income], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))).map_err(internal)?;
                rows.filter_map(|x| x.ok()).filter(|(_, c)| rule_key(c) == key).map(|(i, _)| i).collect()
            };
            for i in &ids {
                tx.execute("UPDATE transactions SET category=?, subcategory=?, grp=?, method='tenant_rule', category_conf=1.0 WHERE id=?", params![category, sub, group, i]).map_err(internal)?;
            }
            same = ids.len();
        }
        tx.commit().map_err(internal)?;
        bump_reset(&c, &cid)?;
        drop(c);
        audit(&st, &ctx, &cid, "transaction.subcategory", &id.to_string(), "ok");
        return Ok(Json(json!({"id": id, "category": category, "subcategory": sub, "group": group, "applied_to_similar": same})));
    }
    let category = b.category.unwrap_or_default();
    if !CATEGORIES.contains(&category.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_category", "Неизвестная категория"));
    }
    let c = lock(&st);
    let n = c.execute("UPDATE transactions SET category=?, manual=1, category_conf=1.0, method='manual' WHERE id=? AND company_id=?", params![category, id, cid]).map_err(internal)?;
    if n == 0 {
        return Err(not_found());
    }
    bump_reset(&c, &cid)?;
    Ok(Json(json!({"id": id, "category": category})))
}

// ---------- ожидаемые события ----------
#[derive(Deserialize)]
pub struct EventBody {
    date: NaiveDate,
    amount_kop: String,
    direction: String,
    title: String,
    #[serde(default)]
    counterparty: String,
    source: Option<String>,
    confidence: Option<f64>,
}

fn parse_kop(s: &str) -> Option<i64> {
    let v: i64 = s.trim().parse().ok()?;
    (v > 0 && v < 1_000_000_000_000_000).then_some(v)
}

pub async fn list_events(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let ev = load_events(&lock(&st), &cid)?;
    Ok(money(json!({"items": ev})))
}

pub async fn create_event(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<EventBody>) -> R<(StatusCode, Json<Value>)> {
    let amount = parse_kop(&b.amount_kop).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_amount", "Сумма должна быть положительным целым числом копеек"))?;
    let signed = match b.direction.as_str() {
        "in" => amount,
        "out" => -amount,
        _ => return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_direction", "direction: in | out")),
    };
    let title = b.title.trim();
    if title.is_empty() || title.chars().count() > 200 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_title", "Название от 1 до 200 символов"));
    }
    let source = b.source.unwrap_or_else(|| "manual".into());
    if !["manual", "invoice", "contract", "tax"].contains(&source.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_source", "Неизвестный источник"));
    }
    let today = chrono::Local::now().date_naive();
    if b.date < today - Duration::days(120) || b.date > today + Duration::days(730) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_date", "Дата вне допустимого диапазона"));
    }
    let conf = b.confidence.unwrap_or(0.9);
    if !(0.0..=1.0).contains(&conf) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_confidence", "confidence от 0 до 1"));
    }
    let id = {
        let c = lock(&st);
        let id = uuid::Uuid::new_v4().to_string();
        c.execute(
            "INSERT INTO expected_events(id, company_id, date, amount_kop, title, counterparty, source, confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            params![id, cid, b.date.to_string(), signed, title, b.counterparty.trim(), source, conf, now()],
        )
        .map_err(internal)?;
        bump(&c, &cid)?;
        id
    };
    audit(&st, &ctx, &cid, "event.create", &id, "ok");
    Ok((StatusCode::CREATED, Json(json!({"id": id}))))
}

pub async fn delete_event(State(st): State<AppState>, Path((cid, eid)): Path<(String, String)>) -> R<StatusCode> {
    let c = lock(&st);
    let n = c.execute("DELETE FROM expected_events WHERE id=? AND company_id=?", params![eid, cid]).map_err(internal)?;
    if n == 0 {
        return Err(not_found());
    }
    bump(&c, &cid)?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Deserialize)]
pub struct BulkItem {
    #[serde(default)]
    external_id: Option<String>,
    date: NaiveDate,
    /// со знаком: + поступление, − списание
    amount_kop: i64,
    title: String,
    #[serde(default)]
    counterparty: String,
    source: String,
    confidence: f64,
    /// ИНН контрагента (10 или 12 цифр); иначе не сохраняется
    #[serde(default)]
    inn: String,
}

fn event_inn(s: &str) -> &str {
    let s = s.trim();
    if (s.len() == 10 || s.len() == 12) && s.bytes().all(|b| b.is_ascii_digit()) { s } else { "" }
}

#[derive(Deserialize)]
pub struct BulkBody {
    events: Vec<BulkItem>,
}

/// Служебное создание событий (налоговый календарь, интеграции). Идемпотентно по (company_id, external_id).
pub async fn bulk_events(State(st): State<AppState>, _ctx: Ctx, Path(cid): Path<String>, Json(b): Json<BulkBody>) -> R<Json<Value>> {
    let mut c = lock(&st);
    let exists: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [&cid], |r| r.get(0)).map_err(internal)?;
    if exists == 0 {
        return Err(not_found());
    }
    let tx = c.transaction().map_err(internal)?;
    let (mut ids, mut skipped) = (vec![], 0usize);
    {
        let mut ins = tx
            .prepare(
                "INSERT OR IGNORE INTO expected_events(id, company_id, date, amount_kop, title, counterparty, source, confidence, created_at, external_id, inn) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            )
            .map_err(internal)?;
        // событие, созданное до появления ИНН, при повторной синхронизации получает ИНН (не считается созданным)
        let mut fill = tx
            .prepare("UPDATE expected_events SET inn=? WHERE company_id=? AND external_id=? AND inn='' AND ?<>''")
            .map_err(internal)?;
        for e in &b.events {
            if e.amount_kop == 0 || !["manual", "invoice", "contract", "tax", "onec"].contains(&e.source.as_str()) || !(0.0..=1.0).contains(&e.confidence) {
                return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_event", "Некорректное событие"));
            }
            let id = uuid::Uuid::new_v4().to_string();
            let inn = event_inn(&e.inn);
            let n = ins
                .execute(params![id, cid, e.date.to_string(), e.amount_kop, e.title, e.counterparty, e.source, e.confidence, now(), e.external_id, inn])
                .map_err(internal)?;
            if n == 1 {
                ids.push(id)
            } else {
                skipped += 1;
                if let Some(ext) = &e.external_id {
                    fill.execute(params![inn, cid, ext, inn]).map_err(internal)?;
                }
            }
        }
    }
    if !ids.is_empty() {
        tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    }
    tx.commit().map_err(internal)?;
    drop(c);
    Ok(Json(json!({"created": ids.len(), "skipped": skipped, "ids": ids})))
}

#[derive(Deserialize)]
pub struct AuditBody {
    action: String,
    #[serde(default)]
    object_id: String,
    #[serde(default = "ok")]
    outcome: String,
}
fn ok() -> String {
    "ok".into()
}

/// Запись аудита от других сервисов (прогноз, налоговый календарь, синхронизация).
pub async fn audit_event(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<AuditBody>) -> StatusCode {
    audit(&st, &ctx, &cid, &b.action, &b.object_id, &b.outcome);
    StatusCode::NO_CONTENT
}

#[derive(Deserialize)]
pub struct AuditQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    /// подстрока в названии действия («team», «goal», «import»…)
    q: Option<String>,
}

/// Журнал действий компании, новые сверху. Фильтр по подстроке действия, постранично.
pub async fn list_audit(State(st): State<AppState>, Path(cid): Path<String>, Query(q): Query<AuditQuery>) -> R<Json<Value>> {
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let offset = q.offset.unwrap_or(0).max(0);
    let like = q.q.unwrap_or_default().trim().to_lowercase();
    let c = lock(&st);
    let total: i64 = c
        .query_row("SELECT COUNT(*) FROM audit_events WHERE company_id=?1 AND (?2='' OR instr(lower(action),?2)>0)", params![cid, like], |r| r.get(0))
        .map_err(internal)?;
    let mut s = c
        .prepare("SELECT id, at, actor, action, object_id, outcome FROM audit_events WHERE company_id=?1 AND (?2='' OR instr(lower(action),?2)>0) ORDER BY id DESC LIMIT ?3 OFFSET ?4")
        .map_err(internal)?;
    let items: Vec<Value> = s
        .query_map(params![cid, like, limit, offset], |r| {
            Ok(json!({"id": r.get::<_, i64>(0)?, "at": r.get::<_, String>(1)?, "actor": r.get::<_, String>(2)?, "action": r.get::<_, String>(3)?,
                "object_id": r.get::<_, Option<String>>(4)?, "outcome": r.get::<_, String>(5)?}))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(Json(json!({"items": items, "total": total, "limit": limit, "offset": offset})))
}
