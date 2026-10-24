//! HTTP-слой BFF: сессия, вход, проксирование к внутренним сервисам и оркестрация (налоговый календарь, интеграции).
//! Бизнес-логики данных и расчётов здесь нет: ledger, forecast и planfact отвечают готовым публичным JSON.

use crate::AppState;
use axum::{
    Json,
    extract::{FromRequestParts, Path, Request},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
};
use chrono::{Datelike, Duration, NaiveDate};
use common::{ApiError, Ctx, R, Reply, Snapshot, Tx, Upstream, err, internal};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

// ---------- аутентификация ----------
pub struct Principal {
    pub user_id: String,
    /// у пользователя только с ЭЦП это служебный адрес `@ecp.invalid`; наружу отдаётся, только если `has_email`
    pub email: String,
    pub has_email: bool,
    pub company_id: String,
    pub company_name: String,
    pub company_inn: String,
    pub company_ogrn: String,
}

/// Реквизиты компании, которые ledger знает со слов сертификата ЭЦП.
#[derive(Default)]
pub struct Requisites<'a> {
    pub name: Option<&'a str>,
    pub inn: Option<&'a str>,
    pub ogrn: Option<&'a str>,
}

pub struct Company {
    pub id: String,
    pub name: String,
    pub inn: String,
    pub ogrn: String,
}

fn cookie_token(h: &HeaderMap) -> Option<String> {
    let c = h.get(header::COOKIE)?.to_str().ok()?;
    c.split(';').filter_map(|p| p.trim().strip_prefix("session=")).next().map(|s| s.to_string())
}

/// Компания пользователя в ledger (создаётся при первом обращении).
async fn ensure_company(st: &AppState, ctx: &Ctx, user_id: &str, email: &str, req: Requisites<'_>) -> R<Company> {
    let v = st.ledger.post("/internal/v1/companies/ensure", ctx, &json!({"owner_id": user_id, "email": email, "name": req.name, "inn": req.inn, "ogrn": req.ogrn})).await?;
    let s = |k: &str| v[k].as_str().unwrap_or_default().to_string();
    Ok(Company { id: s("company_id"), name: s("company_name"), inn: s("inn"), ogrn: s("ogrn") })
}

async fn principal_of(st: &AppState, ctx: &Ctx, user_id: String, email: String, has_email: bool) -> R<Principal> {
    let c = ensure_company(st, ctx, &user_id, &email, Requisites::default()).await?;
    Ok(Principal { user_id, email, has_email, company_id: c.id, company_name: c.name, company_inn: c.inn, company_ogrn: c.ogrn })
}

impl FromRequestParts<AppState> for Principal {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
        let ctx = Ctx::from_request_parts(parts, st).await.unwrap_or_default();
        let token = cookie_token(&parts.headers).ok_or_else(|| err(StatusCode::UNAUTHORIZED, "unauthenticated", "Требуется вход"))?;
        let r = st.auth.json(Method::POST, "/internal/v1/session", &ctx, Some(&json!({"token": token}))).await?;
        if !r.status.is_success() {
            return Err(err(StatusCode::UNAUTHORIZED, "unauthenticated", "Сессия недействительна"));
        }
        let v = r.json();
        principal_of(st, &ctx, v["user_id"].as_str().unwrap_or_default().to_string(), v["email"].as_str().unwrap_or_default().to_string(), v["has_email"].as_bool().unwrap_or(true)).await
    }
}

/// Доступ внешнего клиента: `Authorization: Bearer ckt_…`. Токен привязан к пользователю, а значит и к его единственной
/// компании, поэтому идентификатор компании в путях внешнего API не нужен.
pub struct TokenPrincipal(pub Principal);

impl FromRequestParts<AppState> for TokenPrincipal {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
        let ctx = Ctx::from_request_parts(parts, st).await.unwrap_or_default();
        let bad = || err(StatusCode::UNAUTHORIZED, "invalid_token", "Нужен заголовок Authorization: Bearer <токен>");
        let token = parts.headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).and_then(|v| v.strip_prefix("Bearer ")).map(str::trim).ok_or_else(bad)?;
        let r = st.auth.json(Method::POST, "/internal/v1/tokens/verify", &ctx, Some(&json!({"token": token}))).await?;
        if !r.status.is_success() {
            return Err(err(StatusCode::UNAUTHORIZED, "invalid_token", "Токен недействителен или отозван"));
        }
        let v = r.json();
        Ok(TokenPrincipal(principal_of(st, &ctx, v["user_id"].as_str().unwrap_or_default().to_string(), v["email"].as_str().unwrap_or_default().to_string(), true).await?))
    }
}

/// Нельзя доверять company_id из пути: сверяем с principal. Чужая компания выглядит как несуществующая.
fn authorize(p: &Principal, cid: &str) -> R<()> {
    if p.company_id == cid { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn with_actor(ctx: &Ctx, p: &Principal) -> Ctx {
    Ctx { rid: ctx.rid.clone(), actor: p.user_id.clone() }
}

async fn audit(st: &AppState, ctx: &Ctx, p: &Principal, action: &str, object: &str) {
    let _ = st
        .ledger
        .post(&format!("/internal/v1/companies/{}/audit", p.company_id), &with_actor(ctx, p), &json!({"action": action, "object_id": object, "outcome": "ok"}))
        .await;
}

#[derive(Deserialize)]
pub struct Creds {
    email: String,
    password: String,
    #[serde(default)]
    password_confirm: Option<String>,
    #[serde(default)]
    company_name: Option<String>,
}

async fn auth_call(st: &AppState, ctx: &Ctx, path: &str, body: Value) -> R<(StatusCode, Value)> {
    service_call(&st.auth, ctx, path, body).await
}

async fn service_call(up: &Upstream, ctx: &Ctx, path: &str, body: Value) -> R<(StatusCode, Value)> {
    let r = up.json(Method::POST, &format!("/internal/v1/{path}"), ctx, Some(&body)).await?;
    Ok((r.status, r.json()))
}

fn auth_err(s: StatusCode, v: &Value) -> ApiError {
    let code = v["detail"]["code"].as_str().unwrap_or("");
    match (s.as_u16(), code) {
        (401, _) => err(s, "invalid_credentials", "Неверный email или пароль"),
        (429, _) => err(s, "too_many_attempts", "Слишком много попыток входа, подождите несколько минут"),
        (409, _) => err(s, "email_taken", "Этот email уже зарегистрирован"),
        (422, "invalid_email") => err(s, "invalid_email", "Некорректный email"),
        (422, _) => err(s, "invalid_password", "Пароль должен быть от 8 до 128 символов"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

fn session_response(st: &AppState, token: &str, ttl: i64, body: Value) -> Response {
    let secure = if st.cfg.cookie_secure { "; Secure" } else { "" };
    let mut res = Json(body).into_response();
    res.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_str(&format!("session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl}{secure}")).unwrap(),
    );
    res
}

pub async fn login(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<Creds>) -> R<Response> {
    let (s, v) = auth_call(&st, &ctx, "login", json!({"email": c.email, "password": c.password})).await?;
    if !s.is_success() {
        return Err(auth_err(s, &v));
    }
    let uid = v["user_id"].as_str().unwrap_or_default();
    let email = v["email"].as_str().unwrap_or_default();
    let co = ensure_company(&st, &ctx, uid, email, Requisites { name: c.company_name.as_deref(), ..Default::default() }).await?;
    Ok(session_response(&st, v["token"].as_str().unwrap_or_default(), v["ttl"].as_i64().unwrap_or(3600), json!({"email": email, "company_id": co.id, "company_name": co.name})))
}

#[derive(Deserialize)]
pub struct EcpLogin {
    challenge: String,
    signature: String,
}

pub async fn ecp_challenge(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx) -> R<Json<Value>> {
    let (s, v) = service_call(&st.ecp, &ctx, "ecp/challenge", json!({})).await?;
    if !s.is_success() {
        return Err(ecp_err(s, &v));
    }
    Ok(Json(json!({"challenge": v["challenge"]})))
}

fn ecp_err(s: StatusCode, v: &Value) -> ApiError {
    match v["detail"]["code"].as_str().unwrap_or("") {
        "invalid_challenge" => err(s, "invalid_challenge", "Запрос на подпись устарел, повторите вход"),
        "signature_invalid" => err(s, "signature_invalid", "Подпись не прошла проверку"),
        "certificate_expired" => err(s, "certificate_expired", "Срок действия сертификата истёк"),
        "bad_signature" | "no_certificate" => err(s, "bad_signature", "Не удалось разобрать подпись"),
        "too_many_attempts" => err(s, "too_many_attempts", "Слишком много попыток, подождите"),
        "auth_unavailable" => err(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable", "Сервис входа недоступен"),
        "verifier_unavailable" => err(StatusCode::SERVICE_UNAVAILABLE, "verifier_unavailable", "Проверка ЭЦП на сервере недоступна"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

/// Вход по ЭЦП: личность определяется сертификатом, email и пароль не нужны.
pub async fn ecp_login(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<EcpLogin>) -> R<Response> {
    let (s, v) = service_call(&st.ecp, &ctx, "ecp/login", json!({"challenge": c.challenge, "signature": c.signature})).await?;
    if !s.is_success() {
        return Err(ecp_err(s, &v));
    }
    let uid = v["user_id"].as_str().unwrap_or_default();
    let email = v["email"].as_str().unwrap_or_default();
    // данные компании берём из сертификата: название (организация или ФИО), ИНН, ОГРН
    let cert = &v["company"];
    let requisites = Requisites {
        name: cert["name"].as_str().map(str::trim).filter(|n| n.chars().count() >= 2),
        inn: cert["inn"].as_str(),
        ogrn: cert["ogrn"].as_str(),
    };
    let co = ensure_company(&st, &ctx, uid, email, requisites).await?;
    Ok(session_response(&st, v["token"].as_str().unwrap_or_default(), v["ttl"].as_i64().unwrap_or(3600), json!({"email": v["has_email"].as_bool().unwrap_or(false).then_some(email), "company_id": co.id, "company_name": co.name, "company_inn": co.inn, "company_ogrn": co.ogrn, "ecp": true})))
}

pub async fn register(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(mut c): Json<Creds>) -> R<Response> {
    let name = c.company_name.take().map(|n| n.trim().to_string()).unwrap_or_default();
    if name.chars().count() < 2 || name.chars().count() > 200 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_company_name", "Укажите название юридического лица (2–200 символов)"));
    }
    if c.password_confirm.as_deref() != Some(c.password.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    c.company_name = Some(name);
    let (s, v) = auth_call(&st, &ctx, "register", json!({"email": c.email, "password": c.password})).await?;
    if !s.is_success() {
        return Err(auth_err(s, &v));
    }
    login(axum::extract::State(st), ctx, Json(c)).await
}

pub async fn logout(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, headers: HeaderMap) -> R<Response> {
    if let Some(t) = cookie_token(&headers) {
        let _ = auth_call(&st, &ctx, "logout", json!({"token": t})).await;
    }
    let mut res = StatusCode::NO_CONTENT.into_response();
    res.headers_mut().insert(header::SET_COOKIE, HeaderValue::from_static("session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"));
    Ok(res)
}

pub async fn me(p: Principal) -> Json<Value> {
    Json(json!({"email": p.has_email.then_some(&p.email), "company_id": p.company_id, "company_name": p.company_name, "company_inn": p.company_inn, "company_ogrn": p.company_ogrn}))
}

// ---------- аккаунт: способы входа и токены внешнего API ----------
fn account_err(s: StatusCode, v: &Value) -> ApiError {
    match v["detail"]["code"].as_str().unwrap_or("") {
        "email_exists" => err(s, "email_exists", "К аккаунту уже привязан email"),
        "email_taken" => err(s, "email_taken", "Этот email уже используется"),
        "invalid_email" => err(s, "invalid_email", "Некорректный email"),
        "invalid_password" => err(s, "invalid_password", "Пароль должен быть от 8 до 128 символов"),
        "last_method" => err(s, "last_method", "Нельзя удалить единственный способ входа: сначала добавьте другой"),
        "signature_taken" => err(s, "signature_taken", "Эта подпись уже привязана к другому аккаунту"),
        "too_many_signatures" => err(s, "too_many_signatures", "Достигнут предел числа подписей"),
        "signature_not_found" | "no_email" | "token_not_found" => err(s, "not_found", "Не найдено"),
        "too_many_tokens" => err(s, "too_many_tokens", "Достигнут предел числа токенов, удалите ненужные"),
        "invalid_token_name" => err(s, "invalid_token_name", "Название токена от 1 до 80 символов"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

async fn account_call(st: &AppState, ctx: &Ctx, p: &Principal, method: Method, path: &str, body: Option<Value>) -> R<Value> {
    let r = st.auth.json(method, &format!("/internal/v1/users/{}{path}", p.user_id), &with_actor(ctx, p), body.as_ref()).await?;
    if r.status.is_success() { Ok(r.json()) } else { Err(account_err(r.status, &r.json())) }
}

pub async fn account_methods(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx) -> R<Json<Value>> {
    Ok(Json(account_call(&st, &ctx, &p, Method::GET, "/methods", None).await?))
}

#[derive(Deserialize)]
pub struct EmailBody {
    email: String,
    password: String,
    #[serde(default)]
    password_confirm: Option<String>,
}

pub async fn account_add_email(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Json(b): Json<EmailBody>) -> R<Json<Value>> {
    if b.password_confirm.as_deref() != Some(b.password.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    let v = account_call(&st, &ctx, &p, Method::POST, "/email", Some(json!({"email": b.email, "password": b.password}))).await?;
    audit(&st, &ctx, &p, "account.email_add", "").await;
    Ok(Json(v))
}

pub async fn account_remove_email(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx) -> R<Json<Value>> {
    let v = account_call(&st, &ctx, &p, Method::DELETE, "/email", None).await?;
    audit(&st, &ctx, &p, "account.email_remove", "").await;
    Ok(Json(v))
}

/// Привязка сертификата: подпись проверяется так же, как при входе, затем отпечаток добавляется к аккаунту.
pub async fn account_add_signature(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Json(c): Json<EcpLogin>) -> R<Json<Value>> {
    let (s, v) = service_call(&st.ecp, &ctx, "ecp/verify", json!({"challenge": c.challenge, "signature": c.signature})).await?;
    if !s.is_success() {
        return Err(ecp_err(s, &v));
    }
    let label = v["display_name"].as_str().unwrap_or_default();
    let out = account_call(&st, &ctx, &p, Method::POST, "/signatures", Some(json!({"external_id": v["external_id"], "label": label}))).await?;
    // реквизиты, которых у компании ещё нет, берём из сертификата (введённые вручную не затираются)
    let cert = &v["company"];
    let _ = ensure_company(&st, &ctx, &p.user_id, &p.email, Requisites { name: None, inn: cert["inn"].as_str(), ogrn: cert["ogrn"].as_str() }).await;
    audit(&st, &ctx, &p, "account.signature_add", "").await;
    Ok(Json(out))
}

pub async fn account_remove_signature(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Path(id): Path<String>) -> R<Json<Value>> {
    let v = account_call(&st, &ctx, &p, Method::DELETE, &format!("/signatures/{id}"), None).await?;
    audit(&st, &ctx, &p, "account.signature_remove", "").await;
    Ok(Json(v))
}

pub async fn tokens_list(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx) -> R<Json<Value>> {
    Ok(Json(account_call(&st, &ctx, &p, Method::GET, "/tokens", None).await?))
}

#[derive(Deserialize)]
pub struct TokenBody {
    name: String,
}

/// Новый токен возвращается один раз; хранится только его хэш.
pub async fn tokens_create(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Json(b): Json<TokenBody>) -> R<(StatusCode, Json<Value>)> {
    let v = account_call(&st, &ctx, &p, Method::POST, "/tokens", Some(json!({"name": b.name}))).await?;
    audit(&st, &ctx, &p, "account.token_create", v["id"].as_str().unwrap_or("")).await;
    Ok((StatusCode::CREATED, Json(v)))
}

pub async fn tokens_delete(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Path(id): Path<String>) -> R<StatusCode> {
    account_call(&st, &ctx, &p, Method::DELETE, &format!("/tokens/{id}"), None).await?;
    audit(&st, &ctx, &p, "account.token_delete", &id).await;
    Ok(StatusCode::NO_CONTENT)
}

// ---------- внешний API по токену: /api/ext/v1/* ----------
async fn ext_forward(up: &Upstream, p: &Principal, ctx: &Ctx, req: Request, tail: &str) -> R<Response> {
    let q = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
    let path = format!("/internal/v1/companies/{}/{tail}{q}", p.company_id);
    Ok(forward(up, p, ctx, req, path).await?.into_response())
}

pub async fn ext_company(TokenPrincipal(p): TokenPrincipal) -> Json<Value> {
    Json(json!({"company_id": p.company_id, "company_name": p.company_name, "inn": p.company_inn, "ogrn": p.company_ogrn}))
}

pub async fn ext_forecast_latest(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.forecast, &p, &ctx, req, "forecasts/latest").await
}

pub async fn ext_forecast_create(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.forecast, &p, &ctx, req, "forecasts").await
}

pub async fn ext_transactions(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.ledger, &p, &ctx, req, "transactions").await
}

pub async fn ext_events(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.ledger, &p, &ctx, req, "expected-events").await
}

pub async fn ext_plan_fact(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.planfact, &p, &ctx, req, "plan-fact").await
}

// ---------- проксирование к внутренним сервисам ----------
const MAX_BODY: usize = 26 * 1024 * 1024;

/// Переписывает публичный путь /api/v1/companies/{cid}/... в /internal/v1/companies/{cid}/... и пересылает запрос
/// как есть (метод, query, тело, Content-Type). Ответ сервиса возвращается без изменений.
async fn relay(up: &Upstream, p: &Principal, ctx: &Ctx, req: Request) -> R<Reply> {
    let uri = req.uri().clone();
    let cid = uri.path().split('/').nth(4).unwrap_or_default().to_string();
    authorize(p, &cid)?;
    let tail = uri.path().strip_prefix("/api/v1").unwrap_or(uri.path());
    let path = match uri.query() {
        Some(q) => format!("/internal/v1{tail}?{q}"),
        None => format!("/internal/v1{tail}"),
    };
    forward(up, p, ctx, req, path).await
}

async fn forward(up: &Upstream, p: &Principal, ctx: &Ctx, req: Request, path: String) -> R<Reply> {
    let method = req.method().clone();
    let ct = req.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).map(str::to_string);
    let body = axum::body::to_bytes(req.into_body(), MAX_BODY)
        .await
        .map_err(|_| err(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large", "Файл больше 25 МБ"))?;
    let body = (!body.is_empty() || method == Method::POST || method == Method::PATCH).then_some(body);
    up.send(method, &path, &with_actor(ctx, p), ct.as_deref(), body).await
}

pub async fn to_ledger(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    Ok(relay(&st.ledger, &p, &ctx, req).await?.into_response())
}

pub async fn to_forecast(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    Ok(relay(&st.forecast, &p, &ctx, req).await?.into_response())
}

pub async fn to_planfact(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    Ok(relay(&st.planfact, &p, &ctx, req).await?.into_response())
}

pub async fn create_forecast(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    let r = relay(&st.forecast, &p, &ctx, req).await?;
    if r.status.is_success() {
        audit(&st, &ctx, &p, "forecast.create", r.json()["run_id"].as_str().unwrap_or("")).await;
    }
    Ok(r.into_response())
}

pub async fn create_scenario(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    let r = relay(&st.forecast, &p, &ctx, req).await?;
    if r.status.is_success() {
        audit(&st, &ctx, &p, "scenario.create", r.json()["scenario"]["run_id"].as_str().unwrap_or("")).await;
    }
    Ok(r.into_response())
}

/// Внутренняя сводка риска (порт 3100, без cookie): отдаёт forecast.
pub async fn internal_summary(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Response> {
    Ok(st.forecast.json(Method::GET, &format!("/internal/v1/companies/{cid}/summary"), &ctx, None).await?.into_response())
}

// ---------- налоговый календарь ----------
fn parse_kop(s: &str) -> Option<i64> {
    let v: i64 = s.trim().parse().ok()?;
    (v > 0 && v < 1_000_000_000_000_000).then_some(v)
}

#[derive(Deserialize, Default)]
pub struct TaxBody {
    #[serde(default)]
    amount_kop: Option<String>,
    /// "enp" (по умолчанию), "usn" (аванс УСН по кварталам) или "vat" (НДС тремя равными платежами)
    #[serde(default)]
    kind: Option<String>,
    /// ставка УСН, %: по умолчанию 6 («доходы») или 15 («доходы минус расходы»)
    #[serde(default)]
    rate_pct: Option<f64>,
    /// объект УСН: "income" (по умолчанию) или "income_expenses"
    #[serde(default)]
    usn_object: Option<String>,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum TaxKind {
    Enp,
    Usn,
    Vat,
}

impl TaxKind {
    fn parse(s: Option<&str>) -> Option<Self> {
        match s.unwrap_or("enp") {
            "enp" => Some(Self::Enp),
            "usn" => Some(Self::Usn),
            "vat" => Some(Self::Vat),
            _ => None,
        }
    }
    fn title(self) -> &'static str {
        match self {
            Self::Enp => "ЕНП (налоговый календарь)",
            Self::Usn => "Аванс по УСН (налоговый календарь)",
            Self::Vat => "НДС, 1/3 за квартал (налоговый календарь)",
        }
    }
}

const REVENUE_CATS: [&str; 2] = ["revenue", "other_income"];
/// Расходы, учитываемые при расчёте НДС к уплате и УСН «доходы минус расходы» (без налогов, зарплаты и кредитов).
const PURCHASE_CATS: [&str; 5] = ["suppliers", "rent", "utilities", "marketing", "other_expense"];

/// Сумма операций заданных категорий за последние ~квартал (90 дней) истории; расходы возвращаются положительным числом.
fn quarter_sum(txs: &[Tx], base: NaiveDate, cats: &[&str], income: bool) -> i64 {
    let from = base - Duration::days(90);
    txs.iter()
        .filter(|t| t.date > from && cats.contains(&t.category.as_str()) && (t.amount > 0) == income)
        .map(|t| t.amount.abs())
        .sum()
}

/// Аванс УСН за квартал: ставка × доходы за последние 90 дней; для «доходы минус расходы» — ставка × (доходы − расходы),
/// но не менее 1 % доходов (минимальный налог). Это оценка по выписке, а не расчёт налоговой базы.
pub fn estimate_usn(txs: &[Tx], base: NaiveDate, rate_pct: f64, income_minus_expenses: bool) -> Option<i64> {
    let income = quarter_sum(txs, base, &REVENUE_CATS, true);
    if income <= 0 {
        return None;
    }
    let tax = if income_minus_expenses {
        let expenses = quarter_sum(txs, base, &PURCHASE_CATS, false);
        let by_base = ((income - expenses).max(0) as f64 * rate_pct / 100.0).round() as i64;
        by_base.max((income as f64 * 0.01).round() as i64)
    } else {
        (income as f64 * rate_pct / 100.0).round() as i64
    };
    (tax > 0).then_some(tax)
}

/// Один из трёх равных платежей НДС за квартал: (поступления − закупки) × 20/120 / 3. Выручка в выписке считается с НДС.
pub fn estimate_vat_installment(txs: &[Tx], base: NaiveDate) -> Option<i64> {
    let net = quarter_sum(txs, base, &REVENUE_CATS, true) - quarter_sum(txs, base, &PURCHASE_CATS, false);
    let vat = net as f64 * 20.0 / 120.0 / 3.0;
    (vat >= 1.0).then(|| vat.round() as i64)
}

fn day28(y: i32, m: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, 28).expect("28-е число есть в каждом месяце")
}

/// Ближайшие `n` сроков после `base`: 28-е в месяцах из списка (апр/июл/окт для аванса УСН; все месяцы для НДС).
fn next_28ths(base: NaiveDate, months: &[u32], n: usize) -> Vec<NaiveDate> {
    let mut out = vec![];
    let mut y = base.year();
    while out.len() < n && y <= base.year() + 2 {
        for &m in months {
            let d = day28(y, m);
            if d > base && out.len() < n {
                out.push(d);
            }
        }
        y += 1;
    }
    out
}

/// Оценка месячного налогового платежа: медиана сумм списаний «taxes» по месяцам за последние полгода истории.
pub fn estimate_tax(txs: &[Tx], base: NaiveDate) -> Option<i64> {
    let from = base - Duration::days(183);
    let mut by_month: BTreeMap<(i32, u32), i64> = BTreeMap::new();
    for t in txs {
        if t.category == "taxes" && t.amount < 0 && t.date > from {
            *by_month.entry((t.date.year(), t.date.month())).or_insert(0) += -t.amount;
        }
    }
    let mut v: Vec<i64> = by_month.into_values().collect();
    if v.is_empty() {
        return None;
    }
    v.sort();
    Some(v[v.len() / 2])
}

/// Сроки уплаты ЕНП из налогового календаря ФНС (через сервис gov): даты уплаты взносов/налогов с учётом переноса на рабочий день.
async fn fns_payment_dates(st: &AppState, ctx: &Ctx, base: NaiveDate) -> Option<Vec<NaiveDate>> {
    let path = format!("/internal/v1/gov/tax-calendar?year={}", base.year());
    let v = tokio::time::timeout(std::time::Duration::from_secs(25), st.gov.get(&path, ctx)).await.ok()?.ok()?;
    let mut dates: Vec<NaiveDate> = v["events"]
        .as_array()?
        .iter()
        .filter(|e| e["categories"].as_array().is_some_and(|c| c.iter().any(|x| x == "insurance")) && e["text"].as_str().is_some_and(|t| t.contains("взносы в ФНС")))
        .filter_map(|e| NaiveDate::parse_from_str(e["date"].as_str()?, "%Y-%m-%d").ok())
        .filter(|d| *d > base)
        .collect();
    dates.sort();
    dates.dedup();
    (!dates.is_empty()).then_some(dates)
}

async fn load_snapshot(st: &AppState, ctx: &Ctx, cid: &str) -> R<Snapshot> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/snapshot"), ctx).await?;
    serde_json::from_value(v).map_err(internal)
}

/// Налоговые события: ЕНП (два срока из календаря ФНС), аванс УСН (апр/июл/окт) или НДС (три ближайших 28-х). Даты берутся с nalog.gov.ru (сервис gov), при недоступности — 28-е число.
/// Сумма: из запроса или оценка по налоговым платежам из выписки. События создаются в ledger.
pub async fn tax_calendar(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<TaxBody>>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    let snap = load_snapshot(&st, &actx, &cid).await?;
    let txs: Vec<Tx> = snap.transactions.iter().map(|r| r.tx.clone()).collect();
    let base = txs.iter().map(|t| t.date).max().unwrap_or_else(|| chrono::Local::now().date_naive());
    let body = body.map(|b| b.0).unwrap_or_default();
    let kind = TaxKind::parse(body.kind.as_deref()).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_kind", "Вид налога: enp, usn или vat"))?;
    let (amount, estimated) = match body.amount_kop.as_deref() {
        Some(a) => (parse_kop(a).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_amount", "Некорректная сумма"))?, false),
        None => {
            let est = match kind {
                TaxKind::Enp => estimate_tax(&txs, base),
                TaxKind::Usn => {
                    let ie = match body.usn_object.as_deref() {
                        None | Some("income") => false,
                        Some("income_expenses") => true,
                        _ => return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_usn_object", "Объект УСН: income или income_expenses")),
                    };
                    let rate = body.rate_pct.unwrap_or(if ie { 15.0 } else { 6.0 });
                    if !(0.0..=100.0).contains(&rate) {
                        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_rate", "Ставка от 0 до 100 %"));
                    }
                    estimate_usn(&txs, base, rate, ie)
                }
                TaxKind::Vat => estimate_vat_installment(&txs, base),
            };
            (est.ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "amount_required", "Не удалось оценить сумму по выписке, укажите её вручную"))?, true)
        }
    };
    let (dates, source) = match kind {
        TaxKind::Usn => (next_28ths(base, &[4, 7, 10], 2), "statutory-28"),
        TaxKind::Vat => (next_28ths(base, &(1..=12).collect::<Vec<u32>>(), 3), "statutory-28"),
        TaxKind::Enp => match fns_payment_dates(&st, &actx, base).await {
            Some(d) => (d.into_iter().take(2).collect(), "nalog.gov.ru"),
            None => {
                let mut d = vec![];
                let (mut y, mut m) = (base.year(), base.month());
                for _ in 0..4 {
                    d.push(day28(y, m));
                    if m == 12 { y += 1; m = 1 } else { m += 1 }
                }
                (d.into_iter().filter(|x| *x > base).take(2).collect(), "fallback-28")
            }
        },
    };
    let mut created = vec![];
    let mut new_events = vec![];
    for d in dates {
        if !snap.events.iter().any(|e| e.date == d && e.source == "tax" && e.title == kind.title()) {
            new_events.push(json!({"date": d, "amount_kop": -amount, "title": kind.title(), "counterparty": "ФНС России", "source": "tax", "confidence": 1.0}));
            created.push(d.to_string());
        }
    }
    if !new_events.is_empty() {
        st.ledger.post(&format!("/internal/v1/companies/{cid}/expected-events/bulk"), &actx, &json!({"events": new_events})).await?;
    }
    audit(&st, &ctx, &p, "event.tax_calendar", source).await;
    Ok((StatusCode::CREATED, Json(json!({"created": created, "source": source, "amount_kop": amount.to_string(), "amount_estimated": estimated, "kind": body.kind.as_deref().unwrap_or("enp")}))))
}

/// Ключевая ставка ЦБ РФ (сервис gov, данные cbr.ru).
pub async fn gov_key_rate(axum::extract::State(st): axum::extract::State<AppState>, _p: Principal, ctx: Ctx) -> R<Json<Value>> {
    let r = st.gov.json(Method::GET, "/internal/v1/gov/key-rate", &ctx, None).await?;
    if !r.status.is_success() {
        return Err(err(StatusCode::BAD_GATEWAY, "gov_error", "Не удалось получить ключевую ставку"));
    }
    Ok(Json(r.json()))
}

// ---------- синхронизация обязательств из интеграций ----------
#[derive(Deserialize, Default)]
pub struct SyncBody {
    #[serde(default)]
    external_company: Option<String>,
}

fn kop_of(v: &Value) -> Option<i64> {
    match v {
        Value::String(s) => s.trim().parse().ok(),
        Value::Number(n) => n.as_i64(),
        _ => None,
    }
}

/// Превращает обязательство из integrator в событие для ledger. None — запись некорректна.
pub fn obligation_to_event(it: &Value) -> Option<Value> {
    let external_id = it["external_id"].as_str().filter(|s| !s.is_empty())?;
    let amount = kop_of(&it["amount_kop"]).filter(|a| *a > 0)?;
    let signed = match it["direction"].as_str()? {
        "in" => amount,
        "out" => -amount,
        _ => return None,
    };
    let date = NaiveDate::parse_from_str(it["date"].as_str()?, "%Y-%m-%d").ok()?;
    let source = if it["kind"].as_str() == Some("contract") { "contract" } else { "invoice" };
    let title: String = it["title"].as_str().filter(|t| !t.trim().is_empty()).unwrap_or("Обязательство из интеграции").chars().take(200).collect();
    let confidence = it["confidence"].as_f64().unwrap_or(0.8).clamp(0.0, 1.0);
    Some(json!({"external_id": external_id, "date": date, "amount_kop": signed, "title": title.trim(),
        "counterparty": it["counterparty"].as_str().unwrap_or("").trim(), "source": source, "confidence": confidence}))
}

/// Забирает счета и договоры из integrator (1С, ЭДО, банк) и идемпотентно создаёт ожидаемые события в ledger.
pub async fn integrations_sync(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<SyncBody>>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let actx = with_actor(&ctx, &p);
    let ext = body.and_then(|b| b.0.external_company).unwrap_or_else(|| "C001".into());
    if ext.is_empty() || ext.len() > 32 || !ext.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_company", "Некорректный код компании в источнике"));
    }
    let v = st.integrator.get(&format!("/internal/v1/obligations?company={ext}"), &actx).await?;
    let items = v["items"].as_array().cloned().unwrap_or_default();
    let events: Vec<Value> = items.iter().filter_map(obligation_to_event).collect();
    let invalid = items.len() - events.len();
    let (mut created, mut skipped) = (0, invalid);
    if !events.is_empty() {
        let r = st.ledger.post(&format!("/internal/v1/companies/{cid}/expected-events/bulk"), &actx, &json!({"events": events})).await?;
        created = r["created"].as_u64().unwrap_or(0) as usize;
        skipped += r["skipped"].as_u64().unwrap_or(0) as usize;
    }
    audit(&st, &ctx, &p, "integrations.sync", &ext).await;
    Ok(Json(json!({"created": created, "skipped": skipped, "sources": v["sources"]})))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).unwrap()
    }
    fn tax(date: NaiveDate, amount: i64) -> Tx {
        Tx { date, amount, counterparty: "ФНС".into(), inn: String::new(), purpose: String::new(), category: "taxes".into() }
    }

    fn cat(date: NaiveDate, amount: i64, category: &str) -> Tx {
        Tx { date, amount, counterparty: "X".into(), inn: String::new(), purpose: String::new(), category: category.into() }
    }

    #[test]
    fn usn_and_vat_estimates_use_last_quarter() {
        let base = d(2026, 9, 30);
        let txs = vec![
            cat(d(2026, 8, 1), 1_200_000, "revenue"),
            cat(d(2026, 9, 1), 600_000, "revenue"),
            cat(d(2026, 9, 2), -300_000, "suppliers"),
            cat(d(2026, 9, 3), -500_000, "payroll"),
            cat(d(2025, 1, 1), 9_999_999, "revenue"),
        ];
        assert_eq!(estimate_usn(&txs, base, 6.0, false), Some(108_000));
        // (1_800_000 − 300_000) × 15 % = 225_000
        assert_eq!(estimate_usn(&txs, base, 15.0, true), Some(225_000));
        // (1_800_000 − 300_000) × 20/120 / 3 = 83_333
        assert_eq!(estimate_vat_installment(&txs, base), Some(83_333));
        assert_eq!(estimate_usn(&[], base, 6.0, false), None);
        assert_eq!(estimate_vat_installment(&[cat(d(2026, 9, 1), -100, "suppliers")], base), None);
    }

    #[test]
    fn usn_income_minus_expenses_has_minimum_tax() {
        let txs = vec![cat(d(2026, 9, 1), 1_000_000, "revenue"), cat(d(2026, 9, 2), -1_500_000, "suppliers")];
        assert_eq!(estimate_usn(&txs, d(2026, 9, 30), 15.0, true), Some(10_000));
    }

    #[test]
    fn statutory_dates_follow_quarters() {
        assert_eq!(next_28ths(d(2026, 9, 30), &[4, 7, 10], 2), vec![d(2026, 10, 28), d(2027, 4, 28)]);
        assert_eq!(next_28ths(d(2026, 10, 28), &[4, 7, 10], 1), vec![d(2027, 4, 28)]);
        assert_eq!(next_28ths(d(2026, 11, 30), &(1..=12).collect::<Vec<u32>>(), 3), vec![d(2026, 12, 28), d(2027, 1, 28), d(2027, 2, 28)]);
    }

    #[test]
    fn estimate_tax_takes_median_of_monthly_sums() {
        let txs = vec![tax(d(2026, 5, 10), -100), tax(d(2026, 6, 10), -300), tax(d(2026, 6, 20), -100), tax(d(2026, 7, 10), -200), tax(d(2025, 1, 1), -999)];
        // месяцы: май 100, июнь 400, июль 200 -> медиана 200
        assert_eq!(estimate_tax(&txs, d(2026, 7, 31)), Some(200));
        assert_eq!(estimate_tax(&[], d(2026, 7, 31)), None);
    }

    #[test]
    fn obligation_maps_direction_and_source() {
        let it = json!({"external_id": "x1", "kind": "contract", "direction": "out", "date": "2026-10-05", "amount_kop": "150000", "title": "Аренда", "counterparty": "ООО", "confidence": 0.9});
        let e = obligation_to_event(&it).unwrap();
        assert_eq!(e["amount_kop"], -150000);
        assert_eq!(e["source"], "contract");
        let inv = json!({"external_id": "x2", "kind": "act", "direction": "in", "date": "2026-10-05", "amount_kop": 500, "title": "t"});
        assert_eq!(obligation_to_event(&inv).unwrap()["source"], "invoice");
        assert!(obligation_to_event(&json!({"direction": "in"})).is_none());
        assert!(obligation_to_event(&json!({"external_id": "a", "direction": "up", "date": "2026-10-05", "amount_kop": "1"})).is_none());
    }
}
