//! Подключения интеграций компании (сейчас одно: 1С по OData, `kind = onec`). Реквизиты подключает пользователь с
//! правом `manage`; ledger хранит адрес, логин и пароль служебного пользователя 1С.
//!
//! Пароль хранится только зашифрованным: encrypt-then-MAC на HMAC-SHA256 (потоковое шифрование, ключи шифра и
//! имитовставки выводятся из `INTEGRATIONS_KEY`, имитовставка охватывает компанию и вид подключения, поэтому
//! зашифрованное значение нельзя перенести в чужую запись). Без ключа пароль не принимается:
//! `503 integrations_key_missing`. Наружу пароль не отдаётся ни одним ответом, кроме внутреннего
//! `GET .../integrations/onec/credentials` для сервиса integrator (заголовок `x-actor: svc:integrator`);
//! ни в журналы, ни в аудит он не попадает.
//!
//! Проверку адреса (SSRF) и сами обращения к 1С выполняет integrator; здесь только форма значений.

use crate::AppState;
use crate::routes::{audit, lock, not_found, now};
use axum::{
    Json,
    extract::{Path, State},
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use common::{ApiError, Ctx, R, err, internal};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// Служебный «актор» сервиса integrator: только он получает расшифрованные реквизиты.
pub const INTEGRATOR_ACTOR: &str = "svc:integrator";
const KIND: &str = "onec";
const MIN_KEY_LEN: usize = 16;

// ---------- шифрование ----------

fn hmac(key: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut k = [0u8; BLOCK];
    if key.len() > BLOCK {
        k[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(k.map(|b| b ^ 0x36));
    for p in parts {
        inner.update(p);
    }
    let mut outer = Sha256::new();
    outer.update(k.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().into()
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Главный ключ из `INTEGRATIONS_KEY`; слишком короткое или пустое значение считается отсутствием ключа.
pub fn master_key(raw: &str) -> Option<[u8; 32]> {
    let raw = raw.trim();
    (raw.len() >= MIN_KEY_LEN).then(|| Sha256::digest(raw.as_bytes()).into())
}

fn stream(enc_key: &[u8; 32], nonce: &[u8; 16], len: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(len + 32);
    let mut i = 0u32;
    while out.len() < len {
        out.extend_from_slice(&hmac(enc_key, &[b"enc", nonce, &i.to_be_bytes()]));
        i += 1;
    }
    out.truncate(len);
    out
}

fn subkeys(master: &[u8; 32]) -> ([u8; 32], [u8; 32]) {
    (hmac(master, &[b"integrations-enc"]), hmac(master, &[b"integrations-mac"]))
}

/// `v1:` + hex(nonce[16] ‖ шифртекст ‖ имитовставка[32]). `aad` — компания и вид подключения.
pub fn seal(master: &[u8; 32], aad: &str, plain: &str) -> String {
    let (ek, mk) = subkeys(master);
    let nonce: [u8; 16] = *uuid::Uuid::new_v4().as_bytes();
    let ct: Vec<u8> = plain.as_bytes().iter().zip(stream(&ek, &nonce, plain.len())).map(|(a, b)| a ^ b).collect();
    let tag = hmac(&mk, &[b"mac", aad.as_bytes(), &[0], &nonce, &ct]);
    format!("v1:{}", hex::encode([nonce.as_slice(), &ct, &tag].concat()))
}

/// `None`, если значение повреждено, подменено или зашифровано другим ключом.
pub fn open(master: &[u8; 32], aad: &str, blob: &str) -> Option<String> {
    let raw = hex::decode(blob.strip_prefix("v1:")?).ok()?;
    if raw.len() < 16 + 32 {
        return None;
    }
    let (nonce, rest) = raw.split_at(16);
    let (ct, tag) = rest.split_at(rest.len() - 32);
    let (ek, mk) = subkeys(master);
    if !ct_eq(tag, &hmac(&mk, &[b"mac", aad.as_bytes(), &[0], nonce, ct])) {
        return None;
    }
    let nonce: [u8; 16] = nonce.try_into().ok()?;
    String::from_utf8(ct.iter().zip(stream(&ek, &nonce, ct.len())).map(|(a, b)| a ^ b).collect()).ok()
}

fn aad(cid: &str) -> String {
    format!("{cid}/{KIND}")
}

// ---------- значения ----------

fn key_missing() -> ApiError {
    err(
        StatusCode::SERVICE_UNAVAILABLE,
        "integrations_key_missing",
        "На сервере не задан ключ шифрования INTEGRATIONS_KEY: подключения 1С временно недоступны (см. docs/integrator.md)",
    )
}

fn bad(code: &str, msg: &str) -> ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, code, msg)
}

fn clean(s: &str, max: usize) -> bool {
    !s.is_empty() && s.chars().count() <= max && !s.chars().any(char::is_control)
}

/// Схема и хост адреса (нижний регистр): сменился адрес — старый пароль к новому серверу не применяется.
pub fn origin(url: &str) -> String {
    let u = url.trim().to_lowercase();
    match u.split_once("://") {
        Some((scheme, rest)) => format!("{scheme}://{}", rest.split(['/', '?', '#']).next().unwrap_or("")),
        None => u,
    }
}

type Row = (String, String, String, String, Option<String>, Option<String>, Option<String>, String, String);

const COLS: &str = "url, username, password_enc, status, last_check_at, last_error, last_sync_at, created_at, updated_at";

fn row(r: &rusqlite::Row) -> rusqlite::Result<Row> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))
}

/// Описание подключения без пароля: есть ли он — только флагом.
fn view(r: &Row) -> Value {
    json!({
        "kind": KIND, "url": r.0, "username": r.1, "password_set": !r.2.is_empty(), "status": r.3,
        "last_check_at": r.4, "last_error": r.5, "last_sync_at": r.6, "created_at": r.7, "updated_at": r.8,
    })
}

fn load(c: &rusqlite::Connection, cid: &str) -> R<Option<Row>> {
    c.query_row(&format!("SELECT {COLS} FROM integration_connections WHERE company_id=? AND kind=?"), params![cid, KIND], row).optional().map_err(internal)
}

fn company_exists(c: &rusqlite::Connection, cid: &str) -> R<()> {
    let n: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [cid], |r| r.get(0)).map_err(internal)?;
    if n == 0 { Err(not_found()) } else { Ok(()) }
}

// ---------- обработчики ----------

/// GET integrations -> {key_configured, items:[{kind, url, username, password_set, status, ...}]}; пароля нет.
pub async fn list(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    company_exists(&c, &cid)?;
    let items: Vec<Value> = load(&c, &cid)?.iter().map(view).collect();
    Ok(Json(json!({"key_configured": st.integrations_key.is_some(), "items": items})))
}

#[derive(Deserialize)]
pub struct PutBody {
    url: String,
    username: String,
    /// не передан или пуст — оставить сохранённый (только при том же адресе)
    #[serde(default)]
    password: Option<String>,
}

/// PUT integrations/onec {url, username, password?} -> описание подключения. Статус сбрасывается в `unchecked`.
pub async fn put_onec(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<PutBody>) -> R<Json<Value>> {
    let url = b.url.trim().to_string();
    let user = b.username.trim().to_string();
    let lower = url.to_lowercase();
    if !clean(&url, 300) || !(lower.starts_with("https://") || lower.starts_with("http://")) || url.contains(char::is_whitespace) {
        return Err(bad("invalid_url", "Адрес базы 1С: ссылка вида https://… не длиннее 300 символов"));
    }
    if !clean(&user, 100) {
        return Err(bad("invalid_username", "Логин служебного пользователя: от 1 до 100 символов"));
    }
    let password = b.password.filter(|p| !p.is_empty());
    if password.as_ref().is_some_and(|p| p.chars().count() > 200 || p.chars().any(char::is_control)) {
        return Err(bad("invalid_password", "Пароль: не длиннее 200 символов, без управляющих символов"));
    }
    let mut c = lock(&st);
    company_exists(&c, &cid)?;
    let tx = c.transaction().map_err(internal)?;
    let prev = load(&tx, &cid)?;
    let (enc, same_origin) = match (&password, &prev) {
        (Some(p), _) => {
            let key = st.integrations_key.as_ref().ok_or_else(key_missing)?;
            (seal(key, &aad(&cid), p), prev.as_ref().is_some_and(|r| origin(&r.0) == origin(&url)))
        }
        (None, Some(r)) if origin(&r.0) == origin(&url) && !r.2.is_empty() => (r.2.clone(), true),
        _ => return Err(bad("onec_password_required", "Введите пароль служебного пользователя 1С (при смене адреса сохранённый пароль не используется)")),
    };
    let at = now();
    let keep_sync = if same_origin { prev.as_ref().and_then(|r| r.6.clone()) } else { None };
    tx.execute(
        "INSERT INTO integration_connections(company_id, kind, url, username, password_enc, status, last_check_at, last_error, last_sync_at, created_at, updated_at, updated_by)
         VALUES(?1,?2,?3,?4,?5,'unchecked',NULL,NULL,?6,?7,?7,?8)
         ON CONFLICT(company_id, kind) DO UPDATE SET url=excluded.url, username=excluded.username, password_enc=excluded.password_enc,
           status='unchecked', last_check_at=NULL, last_error=NULL, last_sync_at=excluded.last_sync_at, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        params![cid, KIND, url, user, enc, keep_sync, at, ctx.actor],
    )
    .map_err(internal)?;
    let out = load(&tx, &cid)?.map(|r| view(&r)).unwrap_or(Value::Null);
    tx.commit().map_err(internal)?;
    drop(c);
    audit(&st, &ctx, &cid, "integrations.onec.connect", KIND, "ok");
    Ok(Json(out))
}

#[derive(Deserialize)]
pub struct StatusBody {
    /// `check` (проверка подключения) или `sync` (синхронизация)
    event: String,
    ok: bool,
    #[serde(default)]
    error: Option<String>,
}

/// PUT integrations/onec/status {event, ok, error?} -> описание подключения. Текст ошибки обрезается и очищается от управляющих символов.
pub async fn put_status(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<StatusBody>) -> R<Json<Value>> {
    if b.event != "check" && b.event != "sync" {
        return Err(bad("invalid_event", "event: check или sync"));
    }
    let error: Option<String> = if b.ok { None } else { Some(b.error.unwrap_or_default().chars().filter(|c| !c.is_control()).take(300).collect::<String>()).filter(|s| !s.is_empty()).or(Some("error".into())) };
    let at = now();
    let c = lock(&st);
    let status = if b.ok { "ok" } else { "error" };
    let n = if b.event == "check" {
        c.execute(
            "UPDATE integration_connections SET status=?3, last_error=?4, last_check_at=?5 WHERE company_id=?1 AND kind=?2",
            params![cid, KIND, status, error, at],
        )
    } else if b.ok {
        c.execute(
            "UPDATE integration_connections SET status='ok', last_error=NULL, last_sync_at=?3 WHERE company_id=?1 AND kind=?2",
            params![cid, KIND, at],
        )
    } else {
        c.execute("UPDATE integration_connections SET status='error', last_error=?3 WHERE company_id=?1 AND kind=?2", params![cid, KIND, error])
    }
    .map_err(internal)?;
    if n == 0 {
        return Err(not_found());
    }
    let out = load(&c, &cid)?.map(|r| view(&r)).unwrap_or(Value::Null);
    drop(c);
    if b.event == "check" {
        audit(&st, &ctx, &cid, "integrations.onec.check", status, status);
    }
    Ok(Json(out))
}

/// DELETE integrations/onec -> 204 (идемпотентно): реквизиты, включая зашифрованный пароль, удаляются.
pub async fn delete_onec(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<StatusCode> {
    let n = {
        let c = lock(&st);
        company_exists(&c, &cid)?;
        c.execute("DELETE FROM integration_connections WHERE company_id=? AND kind=?", params![cid, KIND]).map_err(internal)?
    };
    if n > 0 {
        audit(&st, &ctx, &cid, "integrations.onec.disconnect", KIND, "ok");
    }
    Ok(StatusCode::NO_CONTENT)
}

/// GET integrations/onec/credentials -> {url, username, password}. Только для integrator (`x-actor: svc:integrator`):
/// единственное место, где пароль расшифровывается и отдаётся. Ответ не кэшируется.
pub async fn credentials(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Response> {
    if ctx.actor != INTEGRATOR_ACTOR {
        return Err(err(StatusCode::FORBIDDEN, "forbidden", "Реквизиты подключения отдаются только сервису integrator"));
    }
    let r = {
        let c = lock(&st);
        load(&c, &cid)?.ok_or_else(not_found)?
    };
    let key = st.integrations_key.as_ref().ok_or_else(key_missing)?;
    let Some(password) = open(key, &aad(&cid), &r.2) else {
        return Err(err(StatusCode::CONFLICT, "integrations_key_invalid", "Пароль подключения не расшифровывается (сменился ключ INTEGRATIONS_KEY): введите его заново"));
    };
    let mut res = Json(json!({"url": r.0, "username": r.1, "password": password})).into_response();
    res.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(res)
}

#[cfg(test)]
mod unit {
    use super::*;

    #[test]
    fn hmac_matches_rfc4231_case_2() {
        // HMAC-SHA256("Jefe", "what do ya want for nothing?")
        assert_eq!(hex::encode(hmac(b"Jefe", &[b"what do ya want for nothing?"])), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    }

    #[test]
    fn seal_open_roundtrip_and_tamper_detection() {
        let k = master_key("0123456789abcdef-test-key").unwrap();
        let blob = seal(&k, "c1/onec", "pa$$ w0rd пароль");
        assert!(!blob.contains("pa$$") && blob.starts_with("v1:"));
        assert_eq!(open(&k, "c1/onec", &blob).as_deref(), Some("pa$$ w0rd пароль"));
        assert_ne!(blob, seal(&k, "c1/onec", "pa$$ w0rd пароль"), "случайный nonce");
        assert_eq!(open(&k, "c2/onec", &blob), None, "другая компания");
        assert_eq!(open(&master_key("another-key-0123456789").unwrap(), "c1/onec", &blob), None, "другой ключ");
        let mut t = blob.clone().into_bytes();
        let i = t.len() - 3;
        t[i] = if t[i] == b'0' { b'1' } else { b'0' };
        assert_eq!(open(&k, "c1/onec", &String::from_utf8(t).unwrap()), None, "подмена тега");
        assert_eq!(open(&k, "c1/onec", "v1:00"), None);
        assert_eq!(open(&k, "c1/onec", "garbage"), None);
    }

    #[test]
    fn short_key_is_no_key() {
        assert!(master_key("").is_none() && master_key("short").is_none() && master_key("  0123456789abcde ").is_none());
        assert!(master_key("0123456789abcdef").is_some());
    }

    #[test]
    fn origin_ignores_path_and_case() {
        assert_eq!(origin("HTTPS://Msk1.1cfresh.com/a/ea/42"), "https://msk1.1cfresh.com");
        assert_eq!(origin("https://x.ru:8443/a"), "https://x.ru:8443");
        assert_ne!(origin("https://x.ru"), origin("http://x.ru"));
    }
}
