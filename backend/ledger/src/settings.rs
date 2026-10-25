//! Настройки компании по разделам (`fns`, `advisor`): JSON-объект целиком. ledger только хранит; что внутри и какие
//! значения допустимы, проверяет api. Изменение раздела `fns` двигает `data_revision`: от него зависит учёт риска в прогнозе.

use crate::AppState;
use crate::routes::{lock, not_found, now};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
};
use common::{R, err, internal};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

pub const KEYS: [&str; 2] = ["fns", "advisor"];
const MAX_BYTES: usize = 16 * 1024;

fn check_key(key: &str) -> R<()> {
    if KEYS.contains(&key) { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "unknown_settings", "Нет такого раздела настроек")) }
}

/// GET settings/{key} -> {key, value: {...} | null, updated_at}
pub async fn get_settings(State(st): State<AppState>, Path((cid, key)): Path<(String, String)>) -> R<Json<Value>> {
    check_key(&key)?;
    let c = lock(&st);
    let row: Option<(String, String)> = c
        .query_row("SELECT value, updated_at FROM company_settings WHERE company_id=? AND key=?", params![cid, key], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?;
    let (value, updated) = match row {
        Some((v, at)) => (serde_json::from_str(&v).unwrap_or(Value::Null), Some(at)),
        None => (Value::Null, None),
    };
    Ok(Json(json!({"key": key, "value": value, "updated_at": updated})))
}

/// PUT settings/{key} {...} -> {key, value, updated_at, changed}
pub async fn put_settings(State(st): State<AppState>, Path((cid, key)): Path<(String, String)>, Json(v): Json<Value>) -> R<Json<Value>> {
    check_key(&key)?;
    if !v.is_object() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_settings", "Настройки: JSON-объект"));
    }
    let text = v.to_string();
    if text.len() > MAX_BYTES {
        return Err(err(StatusCode::PAYLOAD_TOO_LARGE, "settings_too_large", "Настройки больше 16 КБ"));
    }
    let mut c = lock(&st);
    let tx = c.transaction().map_err(internal)?;
    let exists: i64 = tx.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [&cid], |r| r.get(0)).map_err(internal)?;
    if exists == 0 {
        return Err(not_found());
    }
    let prev: Option<String> = tx.query_row("SELECT value FROM company_settings WHERE company_id=? AND key=?", params![cid, key], |r| r.get(0)).optional().map_err(internal)?;
    let changed = prev.as_deref() != Some(text.as_str());
    let at = now();
    tx.execute(
        "INSERT INTO company_settings(company_id, key, value, updated_at) VALUES(?,?,?,?)
         ON CONFLICT(company_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        params![cid, key, text, at],
    )
    .map_err(internal)?;
    if changed && key == "fns" {
        tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    }
    tx.commit().map_err(internal)?;
    Ok(Json(json!({"key": key, "value": v, "updated_at": at, "changed": changed})))
}
