//! Операторский API (`/api/admin/v1/...`): вход оператора платформы, список компаний с метаданными и блокировка.
//! Отдельная cookie `op_session` (Path=/api/admin), не пересекающаяся с тенантской `session`: операторская сессия
//! не даёт доступа к `/api/v1`, а сессия клиента — к `/api/admin`. Логика и данные — в сервисе backoffice;
//! здесь проверка сессии, валидация входа и белый список полей ответа: содержимое данных компаний (операции, платежи)
//! оператору не отдаётся, даже если внутренний сервис по ошибке вернёт лишнее.

use axum::{
    Json, Router,
    extract::{FromRef, FromRequestParts, Path, Query, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use common::{ApiError, Ctx, R, Upstream, err};
use serde::Deserialize;
use serde_json::{Map, Value, json};

const COOKIE: &str = "op_session";
const COOKIE_PATH: &str = "/api/admin";
const REASON_MIN: usize = 3;
const REASON_MAX: usize = 500;
const LIST_LIMIT_MAX: u32 = 100;

/// Всё, что нужно операторским обработчикам от состояния приложения (так модуль не зависит от остальных полей `AppState`).
#[derive(Clone)]
pub struct Bo {
    pub up: Upstream,
    pub secure: bool,
}

impl FromRef<crate::AppState> for Bo {
    fn from_ref(s: &crate::AppState) -> Bo {
        Bo { up: s.backoffice.clone(), secure: s.cfg.cookie_secure }
    }
}

pub fn routes<S>() -> Router<S>
where
    S: Clone + Send + Sync + 'static,
    Bo: FromRef<S>,
{
    Router::new()
        .route("/api/admin/v1/login", post(login))
        // второй фактор оператора (TOTP, обязателен): после верного пароля клиент получает билет и идёт на настройку или ввод кода
        .route("/api/admin/v1/mfa/setup", post(mfa_setup))
        .route("/api/admin/v1/mfa/enable", post(mfa_enable))
        .route("/api/admin/v1/mfa/verify", post(mfa_verify))
        .route("/api/admin/v1/logout", post(logout))
        .route("/api/admin/v1/me", get(me))
        .route("/api/admin/v1/companies", get(companies))
        .route("/api/admin/v1/companies/{id}", get(company))
        .route("/api/admin/v1/companies/{id}/ban", post(ban))
        .route("/api/admin/v1/companies/{id}/unban", post(unban))
        .route("/api/admin/v1/stats", get(stats))
        .route("/api/admin/v1/audit", get(audit))
}

// ---------- сессия оператора ----------
pub struct Operator {
    pub id: String,
    pub email: String,
}

impl Operator {
    /// Актор внутренних вызовов: id оператора (заголовок x-actor выставляет только api).
    fn ctx(&self, base: &Ctx) -> Ctx {
        Ctx { rid: base.rid.clone(), actor: self.id.clone() }
    }
}

/// Только cookie `op_session`; тенантская `session` здесь не рассматривается.
fn op_cookie_token(h: &HeaderMap) -> Option<String> {
    let c = h.get(header::COOKIE)?.to_str().ok()?;
    c.split(';').filter_map(|p| p.trim().strip_prefix("op_session=")).next().filter(|t| !t.is_empty()).map(str::to_string)
}

fn unauthenticated() -> ApiError {
    err(StatusCode::UNAUTHORIZED, "unauthenticated", "Требуется вход оператора")
}

impl<S> FromRequestParts<S> for Operator
where
    S: Send + Sync,
    Bo: FromRef<S>,
{
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, state: &S) -> R<Self> {
        let bo = Bo::from_ref(state);
        let ctx = Ctx::from_request_parts(parts, state).await.unwrap_or_default();
        let token = op_cookie_token(&parts.headers).ok_or_else(unauthenticated)?;
        let r = bo.up.json(Method::POST, "/internal/v1/operators/session", &ctx, Some(&json!({"token": token}))).await?;
        if r.status == StatusCode::UNAUTHORIZED {
            return Err(unauthenticated());
        }
        let v = r.into_result()?;
        let s = |k: &str| v[k].as_str().unwrap_or_default().to_string();
        let op = Operator { id: s("operator_id"), email: s("email") };
        if op.id.is_empty() { Err(unauthenticated()) } else { Ok(op) }
    }
}

fn session_cookie(token: &str, max_age: i64, secure: bool) -> R<HeaderValue> {
    if !token.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(err(StatusCode::BAD_GATEWAY, "backoffice_error", "Ошибка сервиса оператора"));
    }
    let secure = if secure { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{COOKIE}={token}; HttpOnly; SameSite=Strict; Path={COOKIE_PATH}; Max-Age={max_age}{secure}"))
        .map_err(|_| err(StatusCode::BAD_GATEWAY, "backoffice_error", "Ошибка сервиса оператора"))
}

// ---------- белый список полей ответа ----------
const COMPANY_KEYS: [&str; 14] = [
    "id", "name", "inn", "ogrn", "created_at", "banned", "banned_at", "ban_reason", "members_count", "transactions_count",
    "last_transaction_date", "last_import_at", "events_count", "director_email",
];
const FORECAST_KEYS: [&str; 2] = ["risk_level", "shortfall_probability"];
const MEMBER_KEYS: [&str; 3] = ["email", "role", "created_at"];
const STATS_KEYS: [&str; 6] = ["total", "active", "banned", "new_7d", "new_30d", "users_total"];
const AUDIT_KEYS: [&str; 7] = ["id", "at", "operator", "action", "company_id", "detail", "outcome"];
const BAN_KEYS: [&str; 5] = ["company_id", "banned", "ban_reason", "sessions_revoked", "revoke_failed"];

/// Копирует только перечисленные ключи и только скалярные значения (вложенные объекты и массивы отбрасываются).
fn pick(v: &Value, keys: &[&str]) -> Map<String, Value> {
    keys.iter()
        .map(|k| {
            let x = v.get(*k).filter(|x| !x.is_object() && !x.is_array()).cloned().unwrap_or(Value::Null);
            (k.to_string(), x)
        })
        .collect()
}

fn project_company(v: &Value) -> Value {
    let mut o = pick(v, &COMPANY_KEYS);
    o.extend(pick(v, &FORECAST_KEYS));
    if let Some(ms) = v.get("members").and_then(Value::as_array) {
        o.insert("members".into(), Value::Array(ms.iter().map(|m| Value::Object(pick(m, &MEMBER_KEYS))).collect()));
    }
    Value::Object(o)
}

fn project_list(v: &Value) -> Value {
    let items: Vec<Value> = v["items"].as_array().map(|a| a.iter().map(project_company).collect()).unwrap_or_default();
    json!({"total": v["total"].as_u64().unwrap_or(items.len() as u64), "limit": v["limit"], "offset": v["offset"], "items": items})
}

// ---------- валидация ----------
/// Идентификатор компании попадает в путь внутреннего вызова: только безопасные символы.
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

fn check_id(id: &str) -> R<()> {
    if valid_id(id) { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn clean_reason(reason: &str) -> R<String> {
    let r = reason.split_whitespace().collect::<Vec<_>>().join(" ");
    let n = r.chars().count();
    if (REASON_MIN..=REASON_MAX).contains(&n) && !r.chars().any(char::is_control) {
        Ok(r)
    } else {
        Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_reason", format!("Укажите причину блокировки ({REASON_MIN}–{REASON_MAX} символов)")))
    }
}

#[derive(Deserialize)]
pub struct ListQuery {
    query: Option<String>,
    banned: Option<String>,
    limit: Option<u32>,
    offset: Option<u32>,
}

fn invalid_request() -> ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request", "Некорректные параметры запроса")
}

/// Путь внутреннего вызова со списком параметров: значения кодируются, лишние параметры не пробрасываются.
fn list_path(q: &ListQuery) -> R<String> {
    let query = q.query.as_deref().map(str::trim).unwrap_or_default();
    let banned = q.banned.as_deref().unwrap_or_default();
    let limit = q.limit.unwrap_or(50);
    if query.chars().count() > 100 || !["", "true", "false"].contains(&banned) || !(1..=LIST_LIMIT_MAX).contains(&limit) {
        return Err(invalid_request());
    }
    let mut u = reqwest::Url::parse("http://internal/internal/v1/admin/companies").expect("static url");
    {
        let mut p = u.query_pairs_mut();
        if !query.is_empty() {
            p.append_pair("query", query);
        }
        if !banned.is_empty() {
            p.append_pair("banned", banned);
        }
        p.append_pair("limit", &limit.to_string());
        p.append_pair("offset", &q.offset.unwrap_or(0).to_string());
    }
    Ok(format!("{}?{}", u.path(), u.query().unwrap_or_default()))
}

// ---------- обработчики ----------
#[derive(Deserialize)]
pub struct Login {
    email: String,
    password: String,
}

pub async fn login(State(bo): State<Bo>, ctx: Ctx, Json(c): Json<Login>) -> R<Response> {
    if c.email.len() > 254 || c.password.len() > 128 || c.email.trim().is_empty() || c.password.is_empty() {
        return Err(err(StatusCode::UNAUTHORIZED, "invalid_credentials", "Неверный email или пароль"));
    }
    let v = bo.up.post("/internal/v1/operators/login", &ctx, &json!({"email": c.email, "password": c.password})).await?;
    // пароль верен, но нужен второй фактор: cookie ещё нет
    if let Some(step) = v["mfa"].as_str() {
        return Ok(Json(json!({"mfa": step, "mfa_token": v["mfa_token"], "email": v["email"]})).into_response());
    }
    session_response(&bo, &v, None)
}

/// Cookie `op_session` и тело ответа: email и, при настройке 2FA, резервные коды (показываются один раз).
fn session_response(bo: &Bo, v: &Value, extra: Option<Value>) -> R<Response> {
    let cookie = session_cookie(v["token"].as_str().unwrap_or_default(), v["ttl"].as_i64().unwrap_or(3600), bo.secure)?;
    let mut body = json!({"email": v["email"]});
    if let (Some(extra), Some(obj)) = (extra, body.as_object_mut()) {
        if let Some(e) = extra.as_object() {
            obj.extend(e.clone());
        }
    }
    let mut res = Json(body).into_response();
    res.headers_mut().insert(header::SET_COOKIE, cookie);
    Ok(res)
}

#[derive(Deserialize)]
pub struct MfaTicket {
    mfa_token: String,
}

#[derive(Deserialize)]
pub struct MfaCode {
    mfa_token: String,
    code: String,
}

fn mfa_input_ok(token: &str, code: &str) -> R<()> {
    if token.len() > 512 || code.len() > 64 || token.is_empty() || code.trim().is_empty() {
        return Err(err(StatusCode::UNAUTHORIZED, "invalid_mfa_code", "Неверный код"));
    }
    Ok(())
}

/// Секрет, otpauth-ссылка и QR (SVG рисует backoffice).
pub async fn mfa_setup(State(bo): State<Bo>, ctx: Ctx, Json(c): Json<MfaTicket>) -> R<Json<Value>> {
    mfa_input_ok(&c.mfa_token, "0")?;
    let v = bo.up.post("/internal/v1/operators/mfa/setup", &ctx, &json!({"mfa_token": c.mfa_token})).await?;
    Ok(Json(json!({"secret": v["secret"], "otpauth_uri": v["otpauth_uri"], "qr_svg": v["qr_svg"]})))
}

pub async fn mfa_enable(State(bo): State<Bo>, ctx: Ctx, Json(c): Json<MfaCode>) -> R<Response> {
    mfa_input_ok(&c.mfa_token, &c.code)?;
    let v = bo.up.post("/internal/v1/operators/mfa/enable", &ctx, &json!({"mfa_token": c.mfa_token, "code": c.code})).await?;
    session_response(&bo, &v, Some(json!({"backup_codes": v["backup_codes"]})))
}

pub async fn mfa_verify(State(bo): State<Bo>, ctx: Ctx, Json(c): Json<MfaCode>) -> R<Response> {
    mfa_input_ok(&c.mfa_token, &c.code)?;
    let v = bo.up.post("/internal/v1/operators/mfa/verify", &ctx, &json!({"mfa_token": c.mfa_token, "code": c.code})).await?;
    let extra = v["backup_codes_left"].as_i64().map(|n| json!({"backup_codes_left": n}));
    session_response(&bo, &v, extra)
}

pub async fn logout(State(bo): State<Bo>, ctx: Ctx, headers: HeaderMap) -> R<Response> {
    if let Some(t) = op_cookie_token(&headers) {
        let _ = bo.up.post("/internal/v1/operators/logout", &ctx, &json!({"token": t})).await;
    }
    let mut res = StatusCode::NO_CONTENT.into_response();
    res.headers_mut().insert(header::SET_COOKIE, session_cookie("", 0, bo.secure)?);
    Ok(res)
}

pub async fn me(op: Operator) -> Json<Value> {
    Json(json!({"email": op.email}))
}

pub async fn companies(State(bo): State<Bo>, op: Operator, ctx: Ctx, Query(q): Query<ListQuery>) -> R<Json<Value>> {
    let v = bo.up.get(&list_path(&q)?, &op.ctx(&ctx)).await?;
    Ok(Json(project_list(&v)))
}

pub async fn company(State(bo): State<Bo>, op: Operator, ctx: Ctx, Path(id): Path<String>) -> R<Json<Value>> {
    check_id(&id)?;
    let v = bo.up.get(&format!("/internal/v1/admin/companies/{id}"), &op.ctx(&ctx)).await?;
    Ok(Json(project_company(&v)))
}

#[derive(Deserialize)]
pub struct BanBody {
    reason: String,
}

pub async fn ban(State(bo): State<Bo>, op: Operator, ctx: Ctx, Path(id): Path<String>, Json(b): Json<BanBody>) -> R<Json<Value>> {
    check_id(&id)?;
    let reason = clean_reason(&b.reason)?;
    let v = bo.up.post(&format!("/internal/v1/admin/companies/{id}/ban"), &op.ctx(&ctx), &json!({"reason": reason})).await?;
    Ok(Json(Value::Object(pick(&v, &BAN_KEYS))))
}

pub async fn unban(State(bo): State<Bo>, op: Operator, ctx: Ctx, Path(id): Path<String>) -> R<Json<Value>> {
    check_id(&id)?;
    let v = bo.up.post(&format!("/internal/v1/admin/companies/{id}/unban"), &op.ctx(&ctx), &json!({})).await?;
    Ok(Json(Value::Object(pick(&v, &BAN_KEYS))))
}

pub async fn stats(State(bo): State<Bo>, op: Operator, ctx: Ctx) -> R<Json<Value>> {
    let v = bo.up.get("/internal/v1/admin/stats", &op.ctx(&ctx)).await?;
    Ok(Json(Value::Object(pick(&v, &STATS_KEYS))))
}

#[derive(Deserialize)]
pub struct AuditQuery {
    limit: Option<u32>,
    company_id: Option<String>,
}

/// Журнал действий операторов (кто и что делал; без данных компаний).
pub async fn audit(State(bo): State<Bo>, op: Operator, ctx: Ctx, Query(q): Query<AuditQuery>) -> R<Json<Value>> {
    let limit = q.limit.unwrap_or(100);
    let cid = q.company_id.unwrap_or_default();
    if !(1..=500).contains(&limit) || (!cid.is_empty() && !valid_id(&cid)) {
        return Err(invalid_request());
    }
    let path = if cid.is_empty() { format!("/internal/v1/admin/audit?limit={limit}") } else { format!("/internal/v1/admin/audit?limit={limit}&company_id={cid}") };
    let v = bo.up.get(&path, &op.ctx(&ctx)).await?;
    let items: Vec<Value> = v["items"].as_array().map(|a| a.iter().map(|i| Value::Object(pick(i, &AUDIT_KEYS))).collect()).unwrap_or_default();
    Ok(Json(json!({"items": items})))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::extract::Request;
    use std::sync::{Arc, Mutex};

    type Log = Arc<Mutex<Vec<String>>>;

    /// Имитация backoffice: токен `good` — действующая сессия оператора o1.
    async fn mock_backoffice(log: Log) -> String {
        let l = log.clone();
        let record = move |what: &str| l.lock().unwrap().push(what.to_string());
        let r1 = record.clone();
        let r2 = record.clone();
        let r3 = record.clone();
        let r4 = record.clone();
        let r5 = record.clone();
        let app: Router = Router::new()
            .route(
                "/internal/v1/operators/login",
                post(move |Json(b): Json<Value>| async move {
                    r1(&format!("login {}", b["email"].as_str().unwrap_or("")));
                    match (b["email"].as_str(), b["password"].as_str()) {
                        (Some("ops@example.com"), Some("right-password-1")) => (StatusCode::OK, Json(json!({"token": "good", "operator_id": "o1", "email": "ops@example.com", "ttl": 28800}))),
                        (Some("mfa@example.com"), Some("right-password-1")) => (StatusCode::OK, Json(json!({"mfa": "verify", "mfa_token": "ticket", "email": "mfa@example.com"}))),
                        (Some("locked@example.com"), _) => (StatusCode::TOO_MANY_REQUESTS, Json(json!({"error": {"code": "too_many_attempts", "message": "Слишком много попыток входа"}}))),
                        _ => (StatusCode::UNAUTHORIZED, Json(json!({"error": {"code": "invalid_credentials", "message": "Неверный email или пароль"}}))),
                    }
                }),
            )
            .route(
                "/internal/v1/operators/session",
                post(|Json(b): Json<Value>| async move {
                    if b["token"] == "good" {
                        (StatusCode::OK, Json(json!({"operator_id": "o1", "email": "ops@example.com"})))
                    } else {
                        (StatusCode::UNAUTHORIZED, Json(json!({"error": {"code": "invalid_session", "message": "x"}})))
                    }
                }),
            )
            .route(
                "/internal/v1/operators/mfa/verify",
                post(|Json(b): Json<Value>| async move {
                    if b["mfa_token"] == "ticket" && b["code"] == "123456" {
                        (StatusCode::OK, Json(json!({"token": "good", "operator_id": "o1", "email": "mfa@example.com", "ttl": 28800})))
                    } else {
                        (StatusCode::UNAUTHORIZED, Json(json!({"error": {"code": "invalid_mfa_code", "message": "Неверный код"}})))
                    }
                }),
            )
            .route(
                "/internal/v1/operators/mfa/enable",
                post(|Json(b): Json<Value>| async move {
                    if b["code"] == "123456" {
                        (StatusCode::OK, Json(json!({"token": "good", "operator_id": "o1", "email": "mfa@example.com", "ttl": 28800, "backup_codes": ["aaaa-bbbb"]})))
                    } else {
                        (StatusCode::UNAUTHORIZED, Json(json!({"error": {"code": "invalid_mfa_code", "message": "Неверный код"}})))
                    }
                }),
            )
            .route("/internal/v1/operators/logout", post(move |Json(b): Json<Value>| async move {
                r2(&format!("logout {}", b["token"].as_str().unwrap_or("")));
                StatusCode::NO_CONTENT
            }))
            .route(
                "/internal/v1/admin/companies",
                get(move |req: Request| async move {
                    r3(&format!("list actor={} {}", req.headers().get("x-actor").and_then(|v| v.to_str().ok()).unwrap_or(""), req.uri().query().unwrap_or("")));
                    Json(json!({"total": 1, "items": [{
                        "id": "c1", "name": "Ромашка", "inn": "7700000000", "ogrn": "1", "created_at": "2026-10-20T10:00:00Z", "banned": false, "banned_at": null,
                        "ban_reason": null, "director_email": "boss@romashka.ru", "members_count": 2, "transactions_count": 10, "last_transaction_date": "2026-10-01", "last_import_at": null, "events_count": 3,
                        "risk_level": "high", "shortfall_probability": 0.4,
                        "transactions": [{"purpose": "секрет"}], "balance_kop": 123456, "members": [{"email": "a@b.ru", "role": "director", "user_id": "u1", "password_hash": "zz"}]
                    }]}))
                }),
            )
            .route("/internal/v1/admin/companies/{id}/ban", post(move |Path(id): Path<String>, Json(b): Json<Value>| async move {
                r4(&format!("ban {id} {}", b["reason"].as_str().unwrap_or("")));
                Json(json!({"company_id": id, "banned": true, "ban_reason": b["reason"], "sessions_revoked": 2, "revoke_failed": 0, "debug": {"x": 1}}))
            }))
            .route("/internal/v1/admin/stats", get(move || async move {
                r5("stats");
                Json(json!({"total": 3, "active": 2, "banned": 1, "new_7d": 1, "new_30d": 2, "users_total": 5, "extra": 1}))
            }));
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        format!("http://{addr}")
    }

    /// Операторский роутер вместе с настоящим `security_layer` (CSRF, request id).
    async fn serve(backoffice_url: &str, secure: bool) -> String {
        let bo = Bo { up: Upstream::new("backoffice", "Сервис оператора", backoffice_url, 5), secure };
        let app = routes::<Bo>().layer(axum::middleware::from_fn(crate::security_layer)).with_state(bo);
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        format!("http://{addr}")
    }

    async fn setup() -> (String, Log, reqwest::Client) {
        let log: Log = Arc::default();
        let b = mock_backoffice(log.clone()).await;
        (serve(&b, false).await, log, reqwest::Client::new())
    }

    #[test]
    fn cookie_parser_reads_only_op_session() {
        let h = |v: &str| {
            let mut m = HeaderMap::new();
            m.insert(header::COOKIE, HeaderValue::from_str(v).unwrap());
            m
        };
        assert_eq!(op_cookie_token(&h("session=abc; op_session=def")).as_deref(), Some("def"));
        assert_eq!(op_cookie_token(&h("op_session=def")).as_deref(), Some("def"));
        assert_eq!(op_cookie_token(&h("session=abc")), None, "тенантская cookie не даёт операторской сессии");
        assert_eq!(op_cookie_token(&h("xop_session=abc; op_session=")), None);
        assert_eq!(op_cookie_token(&HeaderMap::new()), None);
    }

    #[test]
    fn session_cookie_attributes() {
        let c = session_cookie("tok-_1", 28800, false).unwrap();
        assert_eq!(c, "op_session=tok-_1; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=28800");
        assert!(session_cookie("tok", 1, true).unwrap().to_str().unwrap().ends_with("; Secure"));
        assert!(session_cookie("a;b", 1, false).is_err(), "служебные символы в токене недопустимы");
        assert!(session_cookie("a\r\nSet-Cookie: x", 1, false).is_err());
    }

    #[test]
    fn projection_drops_everything_but_metadata() {
        let p = project_company(&json!({"id": "c1", "name": "N", "transactions": [1], "balance_kop": 5, "members": [{"email": "a@b.ru", "role": "viewer", "user_id": "u", "x": 1}],
            "risk_level": "low", "inn": {"nested": 1}}));
        assert_eq!(p["id"], "c1");
        assert_eq!(p["inn"], Value::Null, "вложенные значения отбрасываются");
        assert_eq!(p["risk_level"], "low");
        assert!(p.get("transactions").is_none() && p.get("balance_kop").is_none());
        assert_eq!(p["members"], json!([{"email": "a@b.ru", "role": "viewer", "created_at": null}]));
        assert!(project_company(&json!({"id": "c1"})).get("members").is_none());
    }

    #[test]
    fn id_and_reason_validation() {
        assert!(valid_id("3f2a-B_9") && valid_id(&"a".repeat(64)));
        for bad in ["", "..", "a/b", "a?b", "a b", "a%2f", &"a".repeat(65)] {
            assert!(!valid_id(bad), "{bad}");
        }
        assert_eq!(clean_reason("  Нарушение \n условий ").unwrap(), "Нарушение условий");
        for bad in ["", "  ", "ab", &"я".repeat(501), "a\u{0}bc"] {
            assert!(clean_reason(bad).is_err(), "{bad:?}");
        }
        assert!(clean_reason(&"я".repeat(500)).is_ok());
    }

    #[test]
    fn list_path_encodes_and_validates() {
        let q = |query: Option<&str>, banned: Option<&str>, limit: Option<u32>| ListQuery { query: query.map(Into::into), banned: banned.map(Into::into), limit, offset: Some(20) };
        assert_eq!(list_path(&q(None, None, None)).unwrap(), "/internal/v1/admin/companies?limit=50&offset=20");
        assert_eq!(list_path(&q(Some(" a&b=c "), Some("true"), Some(10))).unwrap(), "/internal/v1/admin/companies?query=a%26b%3Dc&banned=true&limit=10&offset=20");
        assert!(list_path(&q(None, Some("maybe"), None)).is_err());
        assert!(list_path(&q(None, None, Some(0))).is_err());
        assert!(list_path(&q(None, None, Some(101))).is_err());
        assert!(list_path(&q(Some(&"x".repeat(101)), None, None)).is_err());
    }

    #[tokio::test]
    async fn login_sets_httponly_strict_cookie_scoped_to_admin() {
        let (base, log, c) = setup().await;
        let r = c.post(format!("{base}/api/admin/v1/login")).json(&json!({"email": "ops@example.com", "password": "right-password-1"})).send().await.unwrap();
        assert_eq!(r.status(), 200);
        let cookie = r.headers()[header::SET_COOKIE].to_str().unwrap().to_string();
        assert_eq!(cookie, "op_session=good; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=28800");
        let body: Value = r.json().await.unwrap();
        assert_eq!(body, json!({"email": "ops@example.com"}), "токен в теле ответа не отдаётся");
        assert_eq!(log.lock().unwrap()[0], "login ops@example.com");
    }

    #[tokio::test]
    async fn second_factor_gives_a_ticket_not_a_cookie_and_the_cookie_only_after_the_code() {
        let (base, _, c) = setup().await;
        let r = c.post(format!("{base}/api/admin/v1/login")).json(&json!({"email": "mfa@example.com", "password": "right-password-1"})).send().await.unwrap();
        assert_eq!(r.status(), 200);
        assert!(r.headers().get(header::SET_COOKIE).is_none(), "до кода сессии нет");
        let body: Value = r.json().await.unwrap();
        assert_eq!((body["mfa"].as_str(), body["mfa_token"].as_str()), (Some("verify"), Some("ticket")));
        assert!(body.get("token").is_none());
        let verify = |code: &str| c.post(format!("{base}/api/admin/v1/mfa/verify")).json(&json!({"mfa_token": "ticket", "code": code})).send();
        let bad = verify("000000").await.unwrap();
        assert_eq!(bad.status(), 401);
        assert!(bad.headers().get(header::SET_COOKIE).is_none());
        assert_eq!(bad.json::<Value>().await.unwrap()["error"]["code"], "invalid_mfa_code");
        let ok = verify("123456").await.unwrap();
        assert_eq!(ok.status(), 200);
        assert_eq!(ok.headers()[header::SET_COOKIE], "op_session=good; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=28800");
        assert_eq!(ok.json::<Value>().await.unwrap(), json!({"email": "mfa@example.com"}), "токен в теле не отдаётся");
        // настройка при первом входе отдаёт резервные коды один раз
        let en = c.post(format!("{base}/api/admin/v1/mfa/enable")).json(&json!({"mfa_token": "ticket", "code": "123456"})).send().await.unwrap();
        assert_eq!(en.status(), 200);
        assert!(en.headers().get(header::SET_COOKIE).is_some());
        assert_eq!(en.json::<Value>().await.unwrap()["backup_codes"], json!(["aaaa-bbbb"]));
        // пустые и слишком длинные входные данные отклоняются до обращения к backoffice
        let r = c.post(format!("{base}/api/admin/v1/mfa/verify")).json(&json!({"mfa_token": "ticket", "code": " "})).send().await.unwrap();
        assert_eq!(r.status(), 401);
    }

    #[tokio::test]
    async fn login_errors_pass_through() {
        let (base, _, c) = setup().await;
        let post = |email: &str, pw: &str| c.post(format!("{base}/api/admin/v1/login")).json(&json!({"email": email, "password": pw})).send();
        let r = post("ops@example.com", "wrong").await.unwrap();
        assert_eq!(r.status(), 401);
        assert!(r.headers().get(header::SET_COOKIE).is_none());
        assert_eq!(r.json::<Value>().await.unwrap()["error"]["code"], "invalid_credentials");
        let r = post("locked@example.com", "x").await.unwrap();
        assert_eq!((r.status().as_u16(), r.json::<Value>().await.unwrap()["error"]["code"].clone()), (429, json!("too_many_attempts")));
    }

    #[tokio::test]
    async fn secure_flag_follows_config() {
        let log: Log = Arc::default();
        let b = mock_backoffice(log).await;
        let base = serve(&b, true).await;
        let r = reqwest::Client::new().post(format!("{base}/api/admin/v1/login")).json(&json!({"email": "ops@example.com", "password": "right-password-1"})).send().await.unwrap();
        assert!(r.headers()[header::SET_COOKIE].to_str().unwrap().ends_with("; Secure"));
    }

    #[tokio::test]
    async fn operator_routes_need_operator_cookie_only() {
        let (base, _, c) = setup().await;
        let url = format!("{base}/api/admin/v1/companies");
        for cookie in [None, Some("session=good"), Some("op_session=bad"), Some("session=good; op_session=bad")] {
            let mut rb = c.get(&url);
            if let Some(v) = cookie {
                rb = rb.header(header::COOKIE, v);
            }
            let r = rb.send().await.unwrap();
            assert_eq!(r.status(), 401, "{cookie:?}");
            assert_eq!(r.json::<Value>().await.unwrap()["error"]["code"], "unauthenticated");
        }
        let r = c.get(format!("{base}/api/admin/v1/me")).header(header::COOKIE, "op_session=good").send().await.unwrap();
        assert_eq!((r.status().as_u16(), r.json::<Value>().await.unwrap()), (200, json!({"email": "ops@example.com"})));
    }

    #[tokio::test]
    async fn companies_response_has_no_company_data() {
        let (base, log, c) = setup().await;
        let r = c.get(format!("{base}/api/admin/v1/companies?query=%D1%80%D0%BE%D0%BC&banned=false&limit=5&evil=1")).header(header::COOKIE, "op_session=good").send().await.unwrap();
        assert_eq!(r.status(), 200);
        let text = r.text().await.unwrap();
        for leak in ["секрет", "balance_kop", "password_hash", "user_id", "transactions\":"] {
            assert!(!text.contains(leak), "{leak}");
        }
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["total"], 1);
        assert_eq!(v["items"][0]["name"], "Ромашка");
        assert_eq!(v["items"][0]["risk_level"], "high");
        assert_eq!(v["items"][0]["director_email"], "boss@romashka.ru");
        assert_eq!(v["items"][0]["members"], json!([{"email": "a@b.ru", "role": "director", "created_at": null}]));
        let sent = log.lock().unwrap().iter().find(|l| l.starts_with("list")).cloned().unwrap();
        assert_eq!(sent, "list actor=o1 query=%D1%80%D0%BE%D0%BC&banned=false&limit=5&offset=0", "актор — оператор; лишние параметры не пробрасываются");
    }

    #[tokio::test]
    async fn ban_validates_reason_and_id_before_calling_backoffice() {
        let (base, log, c) = setup().await;
        let ck = (header::COOKIE, "op_session=good");
        let ban = |id: &str, reason: &str| c.post(format!("{base}/api/admin/v1/companies/{id}/ban")).header(ck.0.clone(), ck.1).json(&json!({"reason": reason})).send();
        assert_eq!(ban("c1", "  ").await.unwrap().status(), 422);
        assert_eq!(ban("c1", "ab").await.unwrap().status(), 422);
        assert_eq!(ban("a.b", "причина").await.unwrap().status(), 404);
        assert!(!log.lock().unwrap().iter().any(|l| l.starts_with("ban")));
        let r = ban("c1", " Нарушение  условий ").await.unwrap();
        assert_eq!(r.status(), 200);
        assert_eq!(r.json::<Value>().await.unwrap(), json!({"company_id": "c1", "banned": true, "ban_reason": "Нарушение условий", "sessions_revoked": 2, "revoke_failed": 0}));
        assert!(log.lock().unwrap().contains(&"ban c1 Нарушение условий".to_string()));
        let r = c.post(format!("{base}/api/admin/v1/companies/c1/ban")).json(&json!({"reason": "причина"})).send().await.unwrap();
        assert_eq!(r.status(), 401, "без cookie оператора бан невозможен");
    }

    #[tokio::test]
    async fn stats_whitelist() {
        let (base, _, c) = setup().await;
        let r = c.get(format!("{base}/api/admin/v1/stats")).header(header::COOKIE, "op_session=good").send().await.unwrap();
        assert_eq!(r.json::<Value>().await.unwrap(), json!({"total": 3, "active": 2, "banned": 1, "new_7d": 1, "new_30d": 2, "users_total": 5}));
    }

    #[tokio::test]
    async fn csrf_origin_is_checked() {
        let (base, _, c) = setup().await;
        let host = base.trim_start_matches("http://").to_string();
        let post = |origin: &str| c.post(format!("{base}/api/admin/v1/login")).header(header::ORIGIN, origin).json(&json!({"email": "ops@example.com", "password": "right-password-1"})).send();
        let r = post("http://evil.example").await.unwrap();
        assert_eq!(r.status(), 403);
        assert_eq!(r.json::<Value>().await.unwrap()["error"]["code"], "bad_origin");
        assert_eq!(post(&format!("http://{host}")).await.unwrap().status(), 200);
        let r = c.post(format!("{base}/api/admin/v1/companies/c1/ban")).header(header::ORIGIN, "http://evil.example").header(header::COOKIE, "op_session=good").json(&json!({"reason": "причина"})).send().await.unwrap();
        assert_eq!(r.status(), 403);
    }

    #[tokio::test]
    async fn logout_clears_cookie_and_ends_session() {
        let (base, log, c) = setup().await;
        let r = c.post(format!("{base}/api/admin/v1/logout")).header(header::COOKIE, "session=t; op_session=good").json(&json!({})).send().await.unwrap();
        assert_eq!(r.status(), 204);
        assert_eq!(r.headers()[header::SET_COOKIE], "op_session=; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=0");
        assert!(log.lock().unwrap().contains(&"logout good".to_string()), "завершается только операторская сессия");
    }

    #[tokio::test]
    async fn backoffice_down_is_503() {
        let base = serve("http://127.0.0.1:1", false).await;
        let c = reqwest::Client::new();
        let r = c.post(format!("{base}/api/admin/v1/login")).json(&json!({"email": "a@b.ru", "password": "x"})).send().await.unwrap();
        assert_eq!(r.status(), 503);
        assert_eq!(r.json::<Value>().await.unwrap()["error"]["code"], "backoffice_unavailable");
        let r = c.get(format!("{base}/api/admin/v1/companies")).header(header::COOKIE, "op_session=good").send().await.unwrap();
        assert_eq!(r.status(), 503);
    }
}
