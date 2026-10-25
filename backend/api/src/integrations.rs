//! Подключение интеграций пользователем («Настройки → Интеграции»). Сейчас настраивается 1С (OData); ЭДО и банк — имитаторы.
//! Реквизиты хранит ledger по компаниям (пароль зашифрован ключом `INTEGRATIONS_KEY`), адрес проверяет и к 1С ходит
//! integrator (защита от SSRF, лимит проверок: docs/integrator.md). Здесь права, оркестрация и вид ответов.
//!
//! Маршруты и права (таблица в docs/roles.md):
//! - `GET    /api/v1/companies/{cid}/integrations` — read: статусы; адрес, логин и флаг «пароль задан» видит только manage
//! - `PUT    /api/v1/companies/{cid}/integrations/onec` — manage: `{url, username, password?}`; сохраняет и сразу проверяет
//! - `POST   /api/v1/companies/{cid}/integrations/onec/test` — manage: проверка (тело необязательно: `{url?, username?, password?}`)
//! - `DELETE /api/v1/companies/{cid}/integrations/onec` — manage: отключить, реквизиты удаляются
//! - `PUT    /api/v1/companies/{cid}/integrations/telegram` — manage: `{token}` токен СВОЕГО бота (@BotFather); сначала getMe через
//!   notifier, потом сохранение (ledger шифрует токен); не больше 10 проверок в минуту на компанию (`429 rate_limited`)
//! - `POST   /api/v1/companies/{cid}/integrations/telegram/test` — manage: повторная проверка сохранённого токена
//! - `DELETE /api/v1/companies/{cid}/integrations/telegram` — manage: отключить, токен удаляется, компания снова на общем боте
//!
//! Токен бота принимается только в теле PUT и дальше идёт notifier (getMe) и ledger (шифрование) по внутренней сети; ни один
//! ответ, журнал или запись аудита его не содержит (в аудите ledger только действие и маска).
//!
//! Пароль принимается только в теле PUT/POST и дальше уходит ledger/integrator по внутренней сети; ни один ответ
//! (и ни одна запись журнала) его не содержит. Без `INTEGRATIONS_KEY` пароль не принимается: `503 integrations_key_missing`.

use crate::AppState;
use crate::http::{Manager, Principal, Reader, audit, with_actor};
use axum::{
    Json,
    extract::{Path, State},
    http::{Method, StatusCode},
};
use common::{Ctx, R, err};
use serde_json::{Value, json};

fn authorize(p: &Principal, cid: &str) -> R<()> {
    if p.company_id == cid { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn ledger_path(cid: &str, tail: &str) -> String {
    format!("/internal/v1/companies/{cid}/integrations{tail}")
}

/// Непустая строка из тела запроса (обрезанная); не строка или пустая — `None`.
fn field(b: &Value, key: &str, max: usize) -> R<Option<String>> {
    match &b[key] {
        Value::Null => Ok(None),
        Value::String(s) if s.trim().is_empty() && key != "password" => Ok(None),
        Value::String(s) if s.is_empty() => Ok(None),
        Value::String(s) if s.chars().count() <= max && !s.chars().any(char::is_control) => Ok(Some(if key == "password" { s.clone() } else { s.trim().to_string() })),
        _ => Err(err(StatusCode::UNPROCESSABLE_ENTITY, format!("invalid_{key}"), "Некорректное значение в форме подключения")),
    }
}

fn simulator(kind: &str) -> Value {
    json!({"kind": kind, "connected": false, "source": "sim", "status": "sim", "last_check_at": null, "last_error": null, "last_sync_at": null})
}

/// Ответ для интерфейса. Адрес, логин и флаг пароля — только для manage; пароля нет ни у кого.
pub fn build_view(list: &Value, manage: bool) -> Value {
    let saved = list["items"].as_array().and_then(|a| a.iter().find(|i| i["kind"] == "onec"));
    let onec = match saved {
        Some(c) => {
            let mut o = json!({"kind": "onec", "connected": true, "source": "live", "status": c["status"], "last_check_at": c["last_check_at"],
                "last_error": c["last_error"], "last_sync_at": c["last_sync_at"]});
            if manage {
                o["url"] = c["url"].clone();
                o["username"] = c["username"].clone();
                o["password_set"] = c["password_set"].clone();
            }
            o
        }
        None => json!({"kind": "onec", "connected": false, "source": "sim", "status": "not_connected", "last_check_at": null, "last_error": null, "last_sync_at": null}),
    };
    json!({"can_manage": manage, "key_configured": manage.then(|| list["key_configured"].clone()), "items": [onec, simulator("edo"), simulator("bank"), telegram_view(list, manage)]})
}

/// Telegram: `source` = `own` (свой бот компании) или `shared` (общий бот платформы). Имя бота, маска токена и текст ошибки — только manage.
fn telegram_view(list: &Value, manage: bool) -> Value {
    let saved = list["items"].as_array().and_then(|a| a.iter().find(|i| i["kind"] == "telegram"));
    match saved {
        Some(c) => {
            let mut o = json!({"kind": "telegram", "connected": true, "source": "own", "status": c["status"], "last_check_at": c["last_check_at"]});
            if manage {
                o["bot_username"] = c["bot_username"].clone();
                o["token_mask"] = c["token_mask"].clone();
                o["last_error"] = c["last_error"].clone();
            }
            o
        }
        None => json!({"kind": "telegram", "connected": false, "source": "shared", "status": "not_connected", "last_check_at": null}),
    }
}

async fn view_for(st: &AppState, ctx: &Ctx, cid: &str, manage: bool) -> R<Value> {
    let list = st.ledger.get(&ledger_path(cid, ""), ctx).await?;
    Ok(build_view(&list, manage))
}

/// GET /api/v1/companies/{cid}/integrations -> {can_manage, key_configured, items:[{kind, connected, source: sim|live, status, ...}]}
/// `status`: `not_connected | unchecked | ok | error` (1С), `sim` (ЭДО, банк).
pub async fn get(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    Ok(Json(view_for(&st, &with_actor(&ctx, &p), &cid, p.role.can(crate::http::Perm::Manage)).await?))
}

/// Проверка у integrator. Ошибки запроса (429 лимит проверок, 422 нет пароля, 503) возвращаются как есть.
async fn run_check(st: &AppState, ctx: &Ctx, body: &Value) -> R<Value> {
    st.integrator.json(Method::POST, "/internal/v1/onec/test", ctx, Some(body)).await?.into_result()
}

/// Результат проверки в ledger: статус, время и текст причины (без реквизитов).
async fn record_check(st: &AppState, ctx: &Ctx, cid: &str, check: &Value) {
    let ok = check["ok"].as_bool().unwrap_or(false);
    let body = json!({"event": "check", "ok": ok, "error": check["message"]});
    let _ = st.ledger.json(Method::PUT, &ledger_path(cid, "/onec/status"), ctx, Some(&body)).await;
}

/// PUT /api/v1/companies/{cid}/integrations/onec {url, username, password?} (manage) -> {..как GET.., check}
/// Пустой пароль при том же адресе оставляет сохранённый. Адрес проходит проверку SSRF (422 `url_forbidden`,
/// `url_scheme`, `url_userinfo`, `invalid_url`); после сохранения подключение сразу проверяется (`check`: результат или null,
/// если проверки исчерпали лимит).
pub async fn put_onec(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<Value>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    let url = field(&b, "url", 300)?.ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_url", "Укажите адрес базы 1С"))?;
    let username = field(&b, "username", 100)?.ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_username", "Укажите логин служебного пользователя"))?;
    let password = field(&b, "password", 200)?;
    // адрес: форма, https, порт и DNS (SSRF) проверяет integrator; в ledger попадает канонический вид
    let checked = st.integrator.json(Method::POST, "/internal/v1/onec/check-url", &actx, Some(&json!({"url": url}))).await?.into_result()?;
    let url = checked["url"].as_str().unwrap_or(&url).to_string();
    let mut body = json!({"url": url, "username": username});
    if let Some(pw) = password {
        body["password"] = json!(pw);
    }
    st.ledger.json(Method::PUT, &ledger_path(&cid, "/onec"), &actx, Some(&body)).await?.into_result()?;
    let check = run_check(&st, &actx, &json!({"tenant": cid})).await.ok();
    if let Some(c) = &check {
        record_check(&st, &actx, &cid, c).await;
    }
    let mut out = view_for(&st, &actx, &cid, true).await?;
    out["check"] = check.unwrap_or(Value::Null);
    Ok(Json(out))
}

/// POST /api/v1/companies/{cid}/integrations/onec/test {url?, username?, password?} (manage) -> {check, recorded, ..как GET..}
/// Недостающие поля берутся из сохранённого подключения (пароль — только при том же адресе). Результат сохраняется в статусе
/// подключения, если проверялись сохранённые реквизиты. Не больше 10 проверок в минуту на компанию (`429 rate_limited`).
pub async fn test_onec(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>, body: axum::body::Bytes) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    // тело необязательно (пустое = проверить сохранённое подключение)
    let b: Value = if body.iter().all(u8::is_ascii_whitespace) {
        json!({})
    } else {
        serde_json::from_slice(&body).ok().filter(Value::is_object).ok_or_else(|| err(StatusCode::BAD_REQUEST, "invalid_json", "Тело запроса: JSON-объект"))?
    };
    let (url, username, password) = (field(&b, "url", 300)?, field(&b, "username", 100)?, field(&b, "password", 200)?);
    let list = st.ledger.get(&ledger_path(&cid, ""), &actx).await?;
    let saved = list["items"].as_array().and_then(|a| a.iter().find(|i| i["kind"] == "onec")).cloned();
    // проверка сохранённых реквизитов (поля не менялись): результат попадает в статус подключения
    let same = |v: &Option<String>, key: &str| match (v, &saved) {
        (None, _) => true,
        (Some(v), Some(s)) => s[key].as_str().is_some_and(|x| x.trim_end_matches('/') == v.trim_end_matches('/')),
        _ => false,
    };
    let record = saved.is_some() && password.is_none() && same(&url, "url") && same(&username, "username");
    let mut req = json!({"tenant": cid});
    for (k, v) in [("url", &url), ("username", &username), ("password", &password)] {
        if let Some(v) = v {
            req[k] = json!(v);
        }
    }
    let check = run_check(&st, &actx, &req).await?;
    if record {
        record_check(&st, &actx, &cid, &check).await;
    } else {
        audit(&st, &ctx, &p, "integrations.onec.test", check["code"].as_str().unwrap_or("")).await;
    }
    let mut out = view_for(&st, &actx, &cid, true).await?;
    out["check"] = check;
    out["recorded"] = json!(record);
    Ok(Json(out))
}

/// DELETE /api/v1/companies/{cid}/integrations/onec (manage) -> 204; реквизиты, включая пароль, удаляются.
pub async fn delete_onec(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>) -> R<StatusCode> {
    authorize(&p, &cid)?;
    let r = st.ledger.json(Method::DELETE, &ledger_path(&cid, "/onec"), &with_actor(&ctx, &p), None).await?;
    if r.status.is_success() { Ok(StatusCode::NO_CONTENT) } else { r.into_result().map(|_| StatusCode::NO_CONTENT) }
}

// ---------- Telegram: свой бот компании ----------

fn tg_path(cid: &str, tail: &str) -> String {
    format!("/internal/v1/companies/{cid}/telegram{tail}")
}

/// Формат токена BotFather: `^\d{6,12}:[A-Za-z0-9_-]{30,50}$` (то же правило в ledger, notifier и форме).
pub fn valid_token(t: &str) -> bool {
    let Some((id, secret)) = t.split_once(':') else { return false };
    (6..=12).contains(&id.len())
        && id.bytes().all(|b| b.is_ascii_digit())
        && (30..=50).contains(&secret.len())
        && secret.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Сервис notifier сразу подхватывает смену токена (иначе он сделает это сам в течение минуты). Сбой не мешает действию.
async fn notifier_sync(st: &AppState, ctx: &Ctx) {
    let _ = st.notifier.json(Method::POST, "/internal/v1/telegram/sync", ctx, Some(&json!({}))).await;
}

/// PUT /api/v1/companies/{cid}/integrations/telegram {token} (manage) -> {..как GET.., check}
/// Токен проверяется через getMe (notifier: 422 `telegram_token_invalid` / `telegram_unreachable`, 429 `rate_limited`), затем ledger
/// сохраняет его зашифрованным (503 `integrations_key_missing` без `INTEGRATIONS_KEY`). Токен в ответе не возвращается.
pub async fn put_telegram(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<Value>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    let token = b["token"].as_str().map(str::trim).unwrap_or_default().to_string();
    if !valid_token(&token) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_bot_token", "Токен бота: формат 123456789:AA… (его выдаёт @BotFather командой /newbot)"));
    }
    let check = st.notifier.json(Method::POST, &tg_path(&cid, "/verify"), &actx, Some(&json!({"token": token}))).await?.into_result()?;
    let username = check["bot_username"].as_str().unwrap_or_default().to_string();
    st.ledger.json(Method::PUT, &ledger_path(&cid, "/telegram"), &actx, Some(&json!({"token": token, "bot_username": username}))).await?.into_result()?;
    notifier_sync(&st, &actx).await;
    let mut out = view_for(&st, &actx, &cid, true).await?;
    out["check"] = json!({"ok": true, "bot_username": username});
    Ok(Json(out))
}

/// POST /api/v1/companies/{cid}/integrations/telegram/test (manage) -> {check, ..как GET..}
/// Проверяет сохранённый токен (getMe) и записывает результат в статус подключения. 409 `telegram_not_connected`, если токена нет.
pub async fn test_telegram(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    let list = st.ledger.get(&ledger_path(&cid, ""), &actx).await?;
    if !list["items"].as_array().is_some_and(|a| a.iter().any(|i| i["kind"] == "telegram")) {
        return Err(err(StatusCode::CONFLICT, "telegram_not_connected", "Свой бот не подключён: введите токен"));
    }
    let r = st.notifier.json(Method::POST, &tg_path(&cid, "/verify"), &actx, Some(&json!({}))).await?;
    if r.status == StatusCode::TOO_MANY_REQUESTS || r.status.is_server_error() {
        return r.into_result().map(Json);
    }
    let (check, body) = if r.status.is_success() {
        let c = r.json();
        let body = json!({"ok": true, "bot_username": c["bot_username"]});
        (json!({"ok": true, "bot_username": c["bot_username"]}), body)
    } else {
        let e = &r.json()["error"];
        let msg = e["message"].as_str().unwrap_or("Не удалось проверить бота");
        (json!({"ok": false, "code": e["code"], "message": msg}), json!({"ok": false, "error": msg}))
    };
    st.ledger.json(Method::PUT, &ledger_path(&cid, "/telegram/status"), &actx, Some(&body)).await?.into_result()?;
    notifier_sync(&st, &actx).await;
    let mut out = view_for(&st, &actx, &cid, true).await?;
    out["check"] = check;
    Ok(Json(out))
}

/// DELETE /api/v1/companies/{cid}/integrations/telegram (manage) -> 204; токен удаляется, чаты остаются, рассылки идут общим ботом.
pub async fn delete_telegram(State(st): State<AppState>, Manager(p): Manager, ctx: Ctx, Path(cid): Path<String>) -> R<StatusCode> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    st.ledger.json(Method::DELETE, &ledger_path(&cid, "/telegram"), &actx, None).await?.into_result().map(|_| ())?;
    notifier_sync(&st, &actx).await;
    Ok(StatusCode::NO_CONTENT)
}

/// После синхронизации с подключённой 1С: результат попадает в статус подключения (`last_sync_at`, ошибка).
pub async fn record_sync(st: &AppState, ctx: &Ctx, cid: &str, obligations: &Value) {
    if obligations["onec"] != "live" {
        return;
    }
    let src = &obligations["sources"]["onec"];
    let ok = src["status"] == "ok";
    let body = json!({"event": "sync", "ok": ok, "error": src["error"]});
    let _ = st.ledger.json(Method::PUT, &ledger_path(cid, "/onec/status"), ctx, Some(&body)).await;
}

#[cfg(test)]
mod unit {
    use super::*;

    #[test]
    fn view_hides_requisites_from_non_managers() {
        let list = json!({"key_configured": true, "items": [{"kind": "onec", "url": "https://x.ru/a", "username": "u", "password_set": true, "status": "ok",
            "last_check_at": "t", "last_error": null, "last_sync_at": null}]});
        let m = build_view(&list, true);
        assert_eq!((m["items"][0]["url"].as_str(), m["items"][0]["username"].as_str(), m["items"][0]["password_set"].as_bool()), (Some("https://x.ru/a"), Some("u"), Some(true)));
        let r = build_view(&list, false);
        let s = r.to_string();
        assert!(!s.contains("x.ru") && !s.contains("\"u\"") && r["items"][0].get("url").is_none() && r["key_configured"].is_null());
        assert_eq!(r["items"][0]["status"], "ok");
        let none = build_view(&json!({"key_configured": false, "items": []}), true);
        assert_eq!((none["items"][0]["status"].as_str(), none["items"][0]["source"].as_str(), none["items"][1]["source"].as_str()), (Some("not_connected"), Some("sim"), Some("sim")));
    }

    #[test]
    fn form_fields_are_trimmed_and_checked() {
        let b = json!({"url": "  https://x.ru ", "username": " ", "password": " pw with spaces ", "bad": "a\nb", "num": 5});
        assert_eq!(field(&b, "url", 300).unwrap().as_deref(), Some("https://x.ru"));
        assert_eq!(field(&b, "username", 100).unwrap(), None);
        assert_eq!(field(&b, "password", 200).unwrap().as_deref(), Some(" pw with spaces "), "пароль не обрезается");
        assert_eq!(field(&b, "missing", 10).unwrap(), None);
        assert!(field(&b, "bad", 10).is_err() && field(&b, "num", 10).is_err() && field(&b, "url", 5).is_err());
    }
}
