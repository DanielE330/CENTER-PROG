//! Данные для оператора платформы: ТОЛЬКО метаданные и агрегаты (названия, реквизиты, счётчики, даты, участники и роли).
//! Содержимого данных компании (операции, платежи, контрагенты, назначения) здесь нет и быть не должно.
//! Блокировка компании: ledger хранит факт и причину, закрытие доступа выполняет api (403 company_banned).

use crate::AppState;
use crate::members::list_of;
use crate::routes::{audit, lock, not_found, now};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::StatusCode,
};
use common::{Ctx, R, err, internal};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

const SELECT: &str = "SELECT c.id, c.name, c.inn, c.ogrn, c.created_at, c.banned_at, c.ban_reason,
    (SELECT COUNT(*) FROM company_members m WHERE m.company_id=c.id),
    (SELECT COUNT(*) FROM transactions t WHERE t.company_id=c.id),
    (SELECT MAX(date) FROM transactions t WHERE t.company_id=c.id),
    (SELECT MAX(created_at) FROM imports i WHERE i.company_id=c.id AND i.status='confirmed'),
    (SELECT COUNT(*) FROM expected_events e WHERE e.company_id=c.id),
    (SELECT email FROM company_members m WHERE m.company_id=c.id AND m.role='director')
    FROM companies c";

fn row_json(r: &rusqlite::Row) -> rusqlite::Result<Value> {
    let banned_at: Option<String> = r.get(5)?;
    Ok(json!({
        "id": r.get::<_, String>(0)?, "name": r.get::<_, String>(1)?, "inn": r.get::<_, String>(2)?, "ogrn": r.get::<_, String>(3)?,
        "created_at": r.get::<_, String>(4)?, "banned": banned_at.is_some(), "banned_at": banned_at, "ban_reason": r.get::<_, Option<String>>(6)?,
        "members_count": r.get::<_, i64>(7)?, "transactions_count": r.get::<_, i64>(8)?, "last_transaction_date": r.get::<_, Option<String>>(9)?,
        "last_import_at": r.get::<_, Option<String>>(10)?, "events_count": r.get::<_, i64>(11)?, "director_email": r.get::<_, Option<String>>(12)?,
    }))
}

#[derive(Deserialize)]
pub struct ListQuery {
    query: Option<String>,
    banned: Option<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}

/// GET /internal/v1/admin/companies?query=&banned=true|false&limit=&offset=
/// `query` ищет по названию, ИНН, ОГРН, email директора (без учёта регистра, в том числе кириллицы).
pub async fn list_companies(State(st): State<AppState>, Query(q): Query<ListQuery>) -> R<Json<Value>> {
    let banned = match q.banned.as_deref() {
        None | Some("") => None,
        Some("true") => Some(true),
        Some("false") => Some(false),
        _ => return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_filter", "banned: true | false")),
    };
    let limit = q.limit.unwrap_or(50).clamp(1, 200) as usize;
    let offset = q.offset.unwrap_or(0).max(0) as usize;
    let needle = q.query.unwrap_or_default().trim().to_lowercase();
    let mut items: Vec<Value> = {
        let c = lock(&st);
        let mut s = c.prepare(&format!("{SELECT} ORDER BY c.created_at DESC, c.id")).map_err(internal)?;
        s.query_map([], row_json).map_err(internal)?.filter_map(|x| x.ok()).collect()
    };
    items.retain(|it| {
        banned.is_none_or(|b| it["banned"].as_bool() == Some(b))
            && (needle.is_empty() || ["name", "inn", "ogrn", "director_email"].iter().any(|k| it[*k].as_str().is_some_and(|v| v.to_lowercase().contains(&needle))))
    });
    let total = items.len();
    let items: Vec<Value> = items.into_iter().skip(offset).take(limit).collect();
    Ok(Json(json!({"total": total, "items": items})))
}

fn company_json(c: &rusqlite::Connection, cid: &str) -> R<Value> {
    let mut v = c.query_row(&format!("{SELECT} WHERE c.id=?"), [cid], row_json).optional().map_err(internal)?.ok_or_else(not_found)?;
    v["members"] = Value::Array(list_of(c, cid)?);
    Ok(v)
}

/// GET /internal/v1/admin/companies/{cid}: то же + участники (user_id, email, роль, дата).
pub async fn get_company(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    Ok(Json(company_json(&c, &cid)?))
}

#[derive(Deserialize)]
pub struct BanBody {
    reason: String,
}

/// POST .../ban {reason}: идемпотентно (повторный вызов обновляет причину, момент блокировки сохраняется).
pub async fn ban(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<BanBody>) -> R<Json<Value>> {
    let reason = b.reason.trim();
    if reason.is_empty() || reason.chars().count() > 500 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_reason", "Причина блокировки от 1 до 500 символов"));
    }
    let v = {
        let c = lock(&st);
        let n = c.execute("UPDATE companies SET banned_at=COALESCE(banned_at, ?), ban_reason=? WHERE id=?", params![now(), reason, cid]).map_err(internal)?;
        if n == 0 {
            return Err(not_found());
        }
        company_json(&c, &cid)?
    };
    audit(&st, &ctx, &cid, "company.ban", &cid, "ok");
    Ok(Json(v))
}

/// POST .../unban: идемпотентно.
pub async fn unban(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    let v = {
        let c = lock(&st);
        let n = c.execute("UPDATE companies SET banned_at=NULL, ban_reason=NULL WHERE id=?", [&cid]).map_err(internal)?;
        if n == 0 {
            return Err(not_found());
        }
        company_json(&c, &cid)?
    };
    audit(&st, &ctx, &cid, "company.unban", &cid, "ok");
    Ok(Json(v))
}

/// GET /internal/v1/admin/stats: счётчики платформы (для панели оператора).
pub async fn stats(State(st): State<AppState>) -> R<Json<Value>> {
    let c = lock(&st);
    let cnt = |sql: &str| -> R<i64> { c.query_row(sql, [], |r| r.get(0)).map_err(internal) };
    let total = cnt("SELECT COUNT(*) FROM companies")?;
    let banned = cnt("SELECT COUNT(*) FROM companies WHERE banned_at IS NOT NULL")?;
    // created_at хранится в RFC 3339 (UTC), поэтому сравнение строк совпадает с хронологическим
    let since = |days: i64| (chrono::Utc::now() - chrono::Duration::days(days)).to_rfc3339();
    let new = |days: i64| -> R<i64> { c.query_row("SELECT COUNT(*) FROM companies WHERE created_at >= ?", [since(days)], |r| r.get(0)).map_err(internal) };
    Ok(Json(json!({
        "companies_total": total, "companies_banned": banned, "companies_active": total - banned,
        "new_7d": new(7)?, "new_30d": new(30)?, "members_total": cnt("SELECT COUNT(*) FROM company_members")?,
    })))
}
