//! HTTP-слой BFF: сессия, вход, проксирование к внутренним сервисам и оркестрация (налоговый календарь, интеграции).
//! Бизнес-логики данных и расчётов здесь нет: ledger, forecast и planfact отвечают готовым публичным JSON.

use crate::AppState;
use axum::{
    Json,
    extract::{FromRequestParts, Path, Query, RawQuery, Request},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
};
use chrono::{Datelike, Duration, NaiveDate};
use common::{ApiError, Ctx, R, Reply, Snapshot, Tx, Upstream, err, internal};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

// ---------- роли и права ----------
/// Роль участника компании. Незнакомая роль трактуется как `Viewer` (минимум прав).
/// В интерфейсе: director — «Владелец», admin — «Администратор», editor — «Аналитик», viewer — «Наблюдатель».
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Role {
    Director,
    Admin,
    Editor,
    Viewer,
}

/// Право: `Read` (GET и вычисляющие POST), `Edit` (изменение данных компании), `Admin` (команда, название компании,
/// настройки уведомлений: директор и администратор), `Manage` (только директор: способы его входа, токены других, передача владения).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Perm {
    Read,
    Edit,
    Admin,
    Manage,
}

impl Role {
    pub fn parse(s: &str) -> Role {
        match s {
            "director" => Role::Director,
            "admin" => Role::Admin,
            "editor" => Role::Editor,
            _ => Role::Viewer,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Role::Director => "director",
            Role::Admin => "admin",
            Role::Editor => "editor",
            Role::Viewer => "viewer",
        }
    }
    pub fn can(self, perm: Perm) -> bool {
        match perm {
            Perm::Read => true,
            Perm::Edit => self != Role::Viewer,
            Perm::Admin => matches!(self, Role::Director | Role::Admin),
            Perm::Manage => self == Role::Director,
        }
    }
    pub fn permissions(self) -> Value {
        json!({"read": self.can(Perm::Read), "edit": self.can(Perm::Edit), "admin": self.can(Perm::Admin), "manage": self.can(Perm::Manage)})
    }
}

pub fn forbidden() -> ApiError {
    err(StatusCode::FORBIDDEN, "forbidden", "Недостаточно прав для этого действия")
}

fn banned_error(reason: Option<&str>) -> ApiError {
    let msg = match reason.map(str::trim).filter(|r| !r.is_empty()) {
        Some(r) => format!("Компания заблокирована оператором: {r}"),
        None => "Компания заблокирована оператором".to_string(),
    };
    err(StatusCode::FORBIDDEN, "company_banned", msg)
}

/// Служебный адрес пользователя только с ЭЦП наружу не отдаётся.
pub fn public_email(e: &str) -> Option<&str> {
    (!e.is_empty() && !e.ends_with("@ecp.invalid")).then_some(e)
}

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
    /// роль по членству в компании (company_members), а не по owner_id
    pub role: Role,
    pub banned: bool,
    pub ban_reason: Option<String>,
    pub must_change_password: bool,
    pub password_generated: bool,
}

impl Principal {
    /// Пустой участник: только чтобы записать в журнал событие от имени пользователя в чужой (новой для него) компании.
    fn empty(user_id: &str) -> Principal {
        Principal {
            user_id: user_id.to_string(), email: String::new(), has_email: false, company_id: String::new(), company_name: String::new(),
            company_inn: String::new(), company_ogrn: String::new(), role: Role::Viewer, banned: false, ban_reason: None,
            must_change_password: false, password_generated: false,
        }
    }

    /// Директор, зарегистрированный по ЭЦП, обязан задать email и пароль; пока не задал — доступны только me, logout и set-credentials.
    pub fn password_required(&self) -> bool {
        !self.has_email
    }

    /// Проверка права: иначе 403 `forbidden`.
    pub fn require(&self, perm: Perm) -> R<()> {
        if self.role.can(perm) { Ok(()) } else { Err(forbidden()) }
    }

    /// Общие условия для всех маршрутов, кроме me/logout/set-credentials: компания не заблокирована, пароль задан.
    fn ensure_active(&self) -> R<()> {
        if self.banned {
            return Err(banned_error(self.ban_reason.as_deref()));
        }
        if self.password_required() {
            return Err(err(StatusCode::FORBIDDEN, "password_required", "Задайте email и пароль для входа"));
        }
        Ok(())
    }

    pub fn me_json(&self) -> Value {
        json!({
            "user_id": self.user_id, "email": self.has_email.then_some(public_email(&self.email)).flatten(),
            "company_id": self.company_id, "company_name": self.company_name, "company_inn": self.company_inn, "company_ogrn": self.company_ogrn,
            "role": self.role.as_str(), "permissions": self.role.permissions(),
            "must_change_password": self.must_change_password, "password_generated": self.password_generated,
            "password_required": self.password_required(), "company_banned": self.banned, "ban_reason": self.ban_reason,
        })
    }
}

/// Реквизиты компании, которые ledger знает со слов сертификата ЭЦП.
#[derive(Default)]
pub struct Requisites<'a> {
    pub name: Option<&'a str>,
    pub inn: Option<&'a str>,
    pub ogrn: Option<&'a str>,
}

/// Компания и роль пользователя в ней (ответ ledger `companies/ensure` и `members/by-user`).
pub struct Company {
    pub id: String,
    pub name: String,
    pub inn: String,
    pub ogrn: String,
    pub role: Role,
    pub banned: bool,
    pub ban_reason: Option<String>,
}

impl Company {
    fn from_json(v: &Value) -> Company {
        let s = |k: &str| v[k].as_str().unwrap_or_default().to_string();
        Company {
            id: s("company_id"),
            name: s("company_name"),
            inn: s("inn"),
            ogrn: s("ogrn"),
            role: Role::parse(v["role"].as_str().unwrap_or("")),
            banned: v["banned"].as_bool().unwrap_or(false),
            ban_reason: v["ban_reason"].as_str().map(str::to_string),
        }
    }
}

fn cookie_token(h: &HeaderMap) -> Option<String> {
    let c = h.get(header::COOKIE)?.to_str().ok()?;
    c.split(';').filter_map(|p| p.trim().strip_prefix("session=")).next().map(|s| s.to_string())
}

/// Компания пользователя при входе: сотруднику возвращается компания директора, новому пользователю создаётся
/// компания, и он становится её директором (создаёт компанию только вход директора, не сотрудника).
async fn ensure_company(st: &AppState, ctx: &Ctx, user_id: &str, email: &str, req: Requisites<'_>) -> R<Company> {
    let v = st.ledger.post("/internal/v1/companies/ensure", ctx, &json!({"owner_id": user_id, "email": email, "name": req.name, "inn": req.inn, "ogrn": req.ogrn})).await?;
    Ok(Company::from_json(&v))
}

/// Участник по членству; без членства сессия недействительна (компания здесь НЕ создаётся).
async fn principal_of(st: &AppState, ctx: &Ctx, user_id: String, email: String, has_email: bool, must_change_password: bool, password_generated: bool) -> R<Principal> {
    let r = st.ledger.json(Method::GET, &format!("/internal/v1/members/by-user/{user_id}"), ctx, None).await?;
    if r.status == StatusCode::NOT_FOUND {
        return Err(err(StatusCode::UNAUTHORIZED, "unauthenticated", "Сессия недействительна"));
    }
    let c = Company::from_json(&r.into_result()?);
    Ok(Principal {
        user_id, email, has_email, company_id: c.id, company_name: c.name, company_inn: c.inn, company_ogrn: c.ogrn,
        role: c.role, banned: c.banned, ban_reason: c.ban_reason, must_change_password, password_generated,
    })
}

async fn load_principal(parts: &mut Parts, st: &AppState) -> R<Principal> {
    let ctx = Ctx::from_request_parts(parts, st).await.unwrap_or_default();
    let token = cookie_token(&parts.headers).ok_or_else(|| err(StatusCode::UNAUTHORIZED, "unauthenticated", "Требуется вход"))?;
    let r = st.auth.json(Method::POST, "/internal/v1/session", &ctx, Some(&json!({"token": token}))).await?;
    if !r.status.is_success() {
        return Err(err(StatusCode::UNAUTHORIZED, "unauthenticated", "Сессия недействительна"));
    }
    let v = r.json();
    principal_of(
        st,
        &ctx,
        v["user_id"].as_str().unwrap_or_default().to_string(),
        v["email"].as_str().unwrap_or_default().to_string(),
        v["has_email"].as_bool().unwrap_or(true),
        v["must_change_password"].as_bool().unwrap_or(false),
        v["password_generated"].as_bool().unwrap_or(false),
    )
    .await
}

/// Обычный участник: сессия действительна, компания не заблокирована, пароль задан. Права проверяют `Reader`/`Editor`/`Manager`.
impl FromRequestParts<AppState> for Principal {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
        let p = load_principal(parts, st).await?;
        p.ensure_active()?;
        Ok(p)
    }
}

/// Сессия без проверки блокировки и пароля: только для me, logout (cookie) и set-credentials.
pub struct RawPrincipal(pub Principal);

impl FromRequestParts<AppState> for RawPrincipal {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
        Ok(RawPrincipal(load_principal(parts, st).await?))
    }
}

macro_rules! perm_extractor {
    ($(#[$doc:meta])* $name:ident, $perm:expr) => {
        $(#[$doc])*
        pub struct $name(pub Principal);
        impl FromRequestParts<AppState> for $name {
            type Rejection = ApiError;
            async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
                let p = Principal::from_request_parts(parts, st).await?;
                p.require($perm)?;
                Ok($name(p))
            }
        }
    };
}

perm_extractor!(
    /// Право `read`: любая роль.
    Reader,
    Perm::Read
);
perm_extractor!(
    /// Право `edit`: director и editor; viewer получает 403 `forbidden` до разбора тела запроса.
    Editor,
    Perm::Edit
);
perm_extractor!(
    /// Право `admin`: director и admin (команда, название компании, настройки уведомлений).
    Administrator,
    Perm::Admin
);
perm_extractor!(
    /// Право `manage`: только director.
    Manager,
    Perm::Manage
);

/// Доступ внешнего клиента: `Authorization: Bearer ckt_…`. Токен привязан к пользователю, а значит к его единственной
/// компании, поэтому идентификатор компании в путях внешнего API не нужен. Права токена = права роли владельца
/// (viewer: scope `read`, editor/director: `write`) и проверяются при каждом запросе, поэтому понижение роли действует сразу.
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
        let p = match principal_of(st, &ctx, v["user_id"].as_str().unwrap_or_default().to_string(), v["email"].as_str().unwrap_or_default().to_string(), true, false, false).await {
            Ok(p) => p,
            // владелец токена больше не состоит в компании (удалён): токен аннулирован
            Err(e) if e.0 == StatusCode::UNAUTHORIZED => return Err(err(StatusCode::UNAUTHORIZED, "invalid_token", "Токен недействителен или отозван")),
            Err(e) => return Err(e),
        };
        if p.banned {
            return Err(banned_error(p.ban_reason.as_deref()));
        }
        Ok(TokenPrincipal(p))
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
    // пароль верен, но нужен второй фактор: сессии и cookie ещё нет, клиент идёт на шаг 2FA.
    // Заблокированной компании билет не выдаём: код вводить незачем.
    if let Some(step) = v["mfa"].as_str() {
        let uid = v["user_id"].as_str().unwrap_or_default();
        let r = st.ledger.json(Method::GET, &format!("/internal/v1/members/by-user/{uid}"), &ctx, None).await?;
        if r.status.is_success() {
            let co = Company::from_json(&r.json());
            if co.banned {
                return Err(banned_error(co.ban_reason.as_deref()));
            }
        }
        return Ok(Json(json!({"mfa": step, "mfa_token": v["mfa_token"]})).into_response());
    }
    finish_login(&st, &ctx, &v, c.company_name.as_deref(), None).await
}

/// Общий конец входа по паролю (и после второго фактора): компания и роль, проверка блокировки, cookie сессии.
/// `extra` — поля поверх ответа (резервные коды при включении 2FA).
async fn finish_login(st: &AppState, ctx: &Ctx, v: &Value, company_name: Option<&str>, extra: Option<Value>) -> R<Response> {
    let uid = v["user_id"].as_str().unwrap_or_default();
    let email = v["email"].as_str().unwrap_or_default();
    let token = v["token"].as_str().unwrap_or_default();
    // сотрудник попадает в компанию директора; компанию создаёт только вход нового пользователя (директора)
    let co = ensure_company(st, ctx, uid, email, Requisites { name: company_name, ..Default::default() }).await?;
    if co.banned {
        let _ = auth_call(st, ctx, "logout", json!({"token": token})).await; // заблокированной компании сессия не нужна
        return Err(banned_error(co.ban_reason.as_deref()));
    }
    let mut body = json!({
        "email": email, "company_id": co.id, "company_name": co.name, "role": co.role.as_str(), "permissions": co.role.permissions(),
        "must_change_password": v["must_change_password"].as_bool().unwrap_or(false), "password_generated": v["password_generated"].as_bool().unwrap_or(false),
        "password_required": false,
    });
    if let (Some(extra), Some(obj)) = (extra, body.as_object_mut()) {
        if let Some(e) = extra.as_object() {
            obj.extend(e.clone());
        }
    }
    Ok(session_response(st, token, v["ttl"].as_i64().unwrap_or(3600), body))
}

fn mfa_err(s: StatusCode, v: &Value) -> ApiError {
    match (s.as_u16(), v["detail"]["code"].as_str().unwrap_or("")) {
        (401, "invalid_mfa_code") => err(s, "invalid_mfa_code", "Неверный код. Проверьте время на телефоне или введите резервный код"),
        (401, _) => err(s, "mfa_session_expired", "Время ввода кода истекло, войдите заново"),
        (429, _) => err(s, "too_many_attempts", "Слишком много попыток, подождите несколько минут"),
        (409, _) => err(s, "mfa_setup_not_started", "Сначала откройте настройку двухфакторной защиты"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
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

/// Первый вход: секрет, otpauth-ссылка и QR (SVG рисуется на сервере, наружу ничего не уходит).
pub async fn mfa_setup(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<MfaTicket>) -> R<Json<Value>> {
    let (s, v) = auth_call(&st, &ctx, "mfa/setup", json!({"mfa_token": c.mfa_token})).await?;
    if !s.is_success() {
        return Err(mfa_err(s, &v));
    }
    Ok(Json(v))
}

pub async fn mfa_enable(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<MfaCode>) -> R<Response> {
    let (s, v) = auth_call(&st, &ctx, "mfa/enable", json!({"mfa_token": c.mfa_token, "code": c.code})).await?;
    if !s.is_success() {
        return Err(mfa_err(s, &v));
    }
    let extra = json!({"backup_codes": v["backup_codes"]});
    finish_login(&st, &ctx, &v, None, Some(extra)).await
}

pub async fn mfa_verify(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<MfaCode>) -> R<Response> {
    let (s, v) = auth_call(&st, &ctx, "mfa/verify", json!({"mfa_token": c.mfa_token, "code": c.code})).await?;
    if !s.is_success() {
        return Err(mfa_err(s, &v));
    }
    let extra = v["backup_codes_left"].as_i64().map(|n| json!({"backup_codes_left": n}));
    finish_login(&st, &ctx, &v, None, extra).await
}

#[derive(Deserialize)]
pub struct EcpLogin {
    challenge: String,
    signature: String,
    #[serde(default)]
    register: bool,
    #[serde(default)]
    company_name: Option<String>,
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
        "not_registered" => err(s, "not_registered", "Этот сертификат не зарегистрирован. Зарегистрируйтесь по ЭЦП"),
        "already_registered" => err(s, "already_registered", "Этот сертификат уже зарегистрирован. Войдите по ЭЦП"),
        "invalid_company_name" => err(s, "invalid_company_name", "Укажите название юридического лица (2–200 символов)"),
        "signature_invalid" => err(s, "signature_invalid", "Подпись не прошла проверку"),
        "certificate_expired" => err(s, "certificate_expired", "Срок действия сертификата истёк"),
        "certificate_not_yet_valid" => err(s, "certificate_not_yet_valid", "Срок действия сертификата ещё не наступил"),
        "signature_time_invalid" => err(s, "signature_time_invalid", "Время подписи не совпадает с текущим: проверьте дату и время на компьютере"),
        "bad_signature" | "no_certificate" => err(s, "bad_signature", "Не удалось разобрать подпись"),
        "too_many_attempts" => err(s, "too_many_attempts", "Слишком много попыток, подождите"),
        "mfa_required" => err(s, "mfa_required", "У этой учётной записи включена двухфакторная защита: войдите по email, паролю и коду"),
        "auth_unavailable" => err(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable", "Сервис входа недоступен"),
        "verifier_unavailable" => err(StatusCode::SERVICE_UNAVAILABLE, "verifier_unavailable", "Проверка ЭЦП на сервере недоступна"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

/// Вход по ЭЦП: личность определяется сертификатом, email и пароль не нужны.
pub async fn ecp_login(axum::extract::State(st): axum::extract::State<AppState>, ctx: Ctx, Json(c): Json<EcpLogin>) -> R<Response> {
    let (s, v) = service_call(&st.ecp, &ctx, "ecp/login", json!({"challenge": c.challenge, "signature": c.signature, "register": c.register, "company_name": c.company_name})).await?;
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
    let token = v["token"].as_str().unwrap_or_default();
    if co.banned {
        let _ = auth_call(&st, &ctx, "logout", json!({"token": token})).await;
        return Err(banned_error(co.ban_reason.as_deref()));
    }
    let has_email = v["has_email"].as_bool().unwrap_or(false);
    // новый директор по ЭЦП входит без пароля: до POST /account/set-credentials остальные маршруты отвечают 403 password_required
    Ok(session_response(&st, token, v["ttl"].as_i64().unwrap_or(3600), json!({
        "email": has_email.then_some(email), "company_id": co.id, "company_name": co.name, "company_inn": co.inn, "company_ogrn": co.ogrn, "ecp": true,
        "role": co.role.as_str(), "permissions": co.role.permissions(), "password_required": !has_email,
        "must_change_password": v["must_change_password"].as_bool().unwrap_or(false), "password_generated": v["password_generated"].as_bool().unwrap_or(false),
    })))
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

/// Доступен и при `password_required`, и для заблокированной компании: интерфейс по флагам решает, что показать.
pub async fn me(RawPrincipal(p): RawPrincipal) -> Json<Value> {
    Json(p.me_json())
}

// ---------- аккаунт: способы входа, пароль и токены внешнего API ----------
fn account_err(s: StatusCode, v: &Value) -> ApiError {
    match v["detail"]["code"].as_str().unwrap_or("") {
        "email_exists" => err(s, "email_exists", "К аккаунту уже привязан email"),
        "email_taken" => err(s, "email_taken", "Этот email уже используется"),
        "invalid_email" => err(s, "invalid_email", "Некорректный email"),
        "invalid_password" => err(s, "invalid_password", "Пароль должен быть от 8 до 128 символов"),
        "invalid_current_password" => err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_current_password", "Текущий пароль указан неверно"),
        "too_many_attempts" => err(StatusCode::TOO_MANY_REQUESTS, "too_many_attempts", "Слишком много попыток, подождите несколько минут"),
        "last_method" => err(s, "last_method", "Нельзя удалить единственный способ входа: сначала добавьте другой"),
        "signature_taken" => err(s, "signature_taken", "Эта подпись уже привязана к другому аккаунту"),
        "too_many_signatures" => err(s, "too_many_signatures", "Достигнут предел числа подписей"),
        "signature_not_found" | "no_email" | "token_not_found" | "user_not_found" => err(StatusCode::NOT_FOUND, "not_found", "Не найдено"),
        "too_many_tokens" => err(s, "too_many_tokens", "Достигнут предел числа токенов, удалите ненужные"),
        "invalid_token_name" => err(s, "invalid_token_name", "Название токена от 1 до 80 символов"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

async fn account_call(st: &AppState, ctx: &Ctx, p: &Principal, method: Method, path: &str, body: Option<Value>) -> R<Value> {
    user_call(st, ctx, p, &p.user_id, method, path, body).await
}

async fn user_call(st: &AppState, ctx: &Ctx, p: &Principal, uid: &str, method: Method, path: &str, body: Option<Value>) -> R<Value> {
    let r = st.auth.json(method, &format!("/internal/v1/users/{uid}{path}"), &with_actor(ctx, p), body.as_ref()).await?;
    if r.status.is_success() { Ok(r.json()) } else { Err(account_err(r.status, &r.json())) }
}

pub async fn account_methods(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx) -> R<Json<Value>> {
    Ok(Json(account_call(&st, &ctx, &p, Method::GET, "/methods", None).await?))
}

#[derive(Deserialize)]
pub struct EmailBody {
    email: String,
    password: String,
    #[serde(default)]
    password_confirm: Option<String>,
}

/// manage: способы входа директора (почта, ЭЦП) — только директор.
pub async fn account_add_email(axum::extract::State(st): axum::extract::State<AppState>, Manager(p): Manager, ctx: Ctx, Json(b): Json<EmailBody>) -> R<Json<Value>> {
    if b.password_confirm.as_deref() != Some(b.password.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    let v = account_call(&st, &ctx, &p, Method::POST, "/email", Some(json!({"email": b.email, "password": b.password}))).await?;
    audit(&st, &ctx, &p, "account.email_add", "").await;
    Ok(Json(v))
}

/// Пароль директора удалить нельзя: у директора всегда есть почта и пароль (подписи можно добавлять и удалять).
pub async fn account_remove_email(Manager(_p): Manager) -> R<Json<Value>> {
    Err(err(StatusCode::CONFLICT, "password_required", "Пароль директора нельзя удалить"))
}

/// Привязка сертификата: подпись проверяется так же, как при входе, затем отпечаток добавляется к аккаунту.
pub async fn account_add_signature(axum::extract::State(st): axum::extract::State<AppState>, Manager(p): Manager, ctx: Ctx, Json(c): Json<EcpLogin>) -> R<Json<Value>> {
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

pub async fn account_remove_signature(axum::extract::State(st): axum::extract::State<AppState>, Manager(p): Manager, ctx: Ctx, Path(id): Path<String>) -> R<Json<Value>> {
    let v = account_call(&st, &ctx, &p, Method::DELETE, &format!("/signatures/{id}"), None).await?;
    audit(&st, &ctx, &p, "account.signature_remove", "").await;
    Ok(Json(v))
}

#[derive(Deserialize)]
pub struct ChangePassword {
    #[serde(default)]
    current: Option<String>,
    new: String,
    #[serde(default)]
    new_confirm: Option<String>,
}

/// POST /api/v1/account/change-password {current?, new, new_confirm?}: любая роль, свой пароль. Текущий пароль обязателен,
/// кроме первой смены после выданного пароля (`must_change_password`). Остальные сессии пользователя закрываются.
pub async fn change_password(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, headers: HeaderMap, Json(b): Json<ChangePassword>) -> R<Json<Value>> {
    if b.new_confirm.as_deref().is_some_and(|c| c != b.new) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    let body = json!({"current": b.current, "new": b.new, "keep_token": cookie_token(&headers)});
    let v = account_call(&st, &ctx, &p, Method::POST, "/password", Some(body)).await?;
    audit(&st, &ctx, &p, "account.password_change", "").await;
    Ok(Json(json!({"ok": true, "must_change_password": v["must_change_password"].as_bool().unwrap_or(false)})))
}

/// POST /api/v1/account/set-credentials {email, password, password_confirm?}: обязательный шаг директора после регистрации по
/// ЭЦП. Сертификаты остаются способом входа; email и пароль добавляются, после чего открываются остальные маршруты.
pub async fn set_credentials(axum::extract::State(st): axum::extract::State<AppState>, RawPrincipal(p): RawPrincipal, ctx: Ctx, Json(b): Json<EmailBody>) -> R<Json<Value>> {
    if p.banned {
        return Err(banned_error(p.ban_reason.as_deref()));
    }
    if p.has_email {
        return Err(err(StatusCode::CONFLICT, "credentials_exist", "Email и пароль уже заданы"));
    }
    if b.password_confirm.as_deref().is_some_and(|c| c != b.password) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    let v = account_call(&st, &ctx, &p, Method::POST, "/email-password", Some(json!({"email": b.email, "password": b.password}))).await?;
    let email = v["email"].as_str().unwrap_or_default().to_string();
    // в списке сотрудников у директора теперь настоящий адрес вместо служебного
    let _ = st.ledger.json(Method::PATCH, &format!("/internal/v1/companies/{}/members/{}", p.company_id, p.user_id), &with_actor(&ctx, &p), Some(&json!({"email": email}))).await;
    audit(&st, &ctx, &p, "account.credentials_set", "").await;
    Ok(Json(json!({"email": email, "password_required": false})))
}

#[derive(Deserialize)]
pub struct TokensQuery {
    #[serde(default)]
    scope: Option<String>,
}

/// Участники компании из ledger.
async fn company_members(st: &AppState, ctx: &Ctx, p: &Principal) -> R<Vec<Value>> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{}/members", p.company_id), &with_actor(ctx, p)).await?;
    Ok(v["items"].as_array().cloned().unwrap_or_default())
}

/// Токены всех сотрудников компании (для директора): с владельцем и его ролью.
async fn company_tokens(st: &AppState, ctx: &Ctx, p: &Principal) -> R<Vec<Value>> {
    let members = company_members(st, ctx, p).await?;
    let ids: Vec<&str> = members.iter().filter_map(|m| m["user_id"].as_str()).collect();
    let r = st.auth.json(Method::GET, &format!("/internal/v1/tokens?user_ids={}", ids.join(",")), &with_actor(ctx, p), None).await?;
    if !r.status.is_success() {
        return Err(err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"));
    }
    let items = r.json()["items"].as_array().cloned().unwrap_or_default();
    Ok(items
        .into_iter()
        .map(|mut t| {
            let owner = members.iter().find(|m| m["user_id"] == t["user_id"]);
            let email = owner.and_then(|m| m["email"].as_str()).and_then(public_email).map(str::to_string);
            t["owner_id"] = t["user_id"].clone();
            t["owner_email"] = json!(email);
            t["owner_role"] = owner.map(|m| m["role"].clone()).unwrap_or(Value::Null);
            t["mine"] = json!(t["user_id"].as_str() == Some(p.user_id.as_str()));
            t
        })
        .collect())
}

/// Свои токены; директору `?scope=company` — токены всех сотрудников компании.
pub async fn tokens_list(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, Query(q): Query<TokensQuery>) -> R<Json<Value>> {
    match q.scope.as_deref() {
        None | Some("") | Some("own") => Ok(Json(account_call(&st, &ctx, &p, Method::GET, "/tokens", None).await?)),
        Some("company") => {
            p.require(Perm::Manage)?;
            Ok(Json(json!({"items": company_tokens(&st, &ctx, &p).await?})))
        }
        _ => Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_scope", "scope: own | company")),
    }
}

#[derive(Deserialize)]
pub struct TokenBody {
    name: String,
}

/// Новый токен возвращается один раз; хранится только его хэш. Выпускать токен для себя может любой сотрудник;
/// права токена равны правам его роли.
pub async fn tokens_create(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, Json(b): Json<TokenBody>) -> R<(StatusCode, Json<Value>)> {
    let v = account_call(&st, &ctx, &p, Method::POST, "/tokens", Some(json!({"name": b.name}))).await?;
    audit(&st, &ctx, &p, "account.token_create", v["id"].as_str().unwrap_or("")).await;
    Ok((StatusCode::CREATED, Json(v)))
}

/// Свой токен удаляет любой сотрудник; директор может отозвать токен любого сотрудника своей компании.
pub async fn tokens_delete(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, Path(id): Path<String>) -> R<StatusCode> {
    let owner = if p.role.can(Perm::Manage) {
        let all = company_tokens(&st, &ctx, &p).await?;
        let t = all.iter().find(|t| t["id"].as_str() == Some(id.as_str())).ok_or_else(|| err(StatusCode::NOT_FOUND, "not_found", "Не найдено"))?;
        t["user_id"].as_str().unwrap_or_default().to_string()
    } else {
        p.user_id.clone()
    };
    user_call(&st, &ctx, &p, &owner, Method::DELETE, &format!("/tokens/{id}"), None).await?;
    audit(&st, &ctx, &p, if owner == p.user_id { "account.token_delete" } else { "account.token_revoke_other" }, &id).await;
    Ok(StatusCode::NO_CONTENT)
}

// ---------- сотрудники компании ----------
fn member_json(m: &Value, me: &str) -> Value {
    json!({"user_id": m["user_id"], "email": m["email"].as_str().and_then(public_email), "role": m["role"], "created_at": m["created_at"], "is_me": m["user_id"].as_str() == Some(me)})
}

/// GET /api/v1/members: список сотрудников (любая роль).
pub async fn members_list(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx) -> R<Json<Value>> {
    let items: Vec<Value> = company_members(&st, &ctx, &p).await?.iter().map(|m| member_json(m, &p.user_id)).collect();
    Ok(Json(json!({"items": items})))
}

#[derive(Deserialize)]
pub struct MemberBody {
    email: String,
    role: String,
    #[serde(default)]
    password: Option<String>,
    #[serde(default)]
    generate: bool,
}

fn staff_role(r: &str) -> R<&'static str> {
    match r {
        "admin" => Ok("admin"),
        "editor" => Ok("editor"),
        "viewer" => Ok("viewer"),
        _ => Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_role", "Роль сотрудника: admin, editor или viewer")),
    }
}

/// Администратора назначает, понижает и удаляет только директор: администратор управляет редакторами и наблюдателями.
fn may_grant(p: &Principal, role: &str) -> R<()> {
    if role == "admin" { p.require(Perm::Manage) } else { Ok(()) }
}

/// POST /api/v1/members {email, role, password?|generate?} (admin) -> 201 {user_id, email, role, password?}.
/// Пароль сотрудника: заданный директором или администратором либо сгенерированный сервером (показывается только в этом ответе).
pub async fn members_create(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Json(b): Json<MemberBody>) -> R<(StatusCode, Json<Value>)> {
    let role = staff_role(&b.role)?;
    may_grant(&p, role)?;
    if b.email.trim().to_lowercase().ends_with("@ecp.invalid") {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_email", "Некорректный email"));
    }
    if b.generate == b.password.is_some() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_password", "Укажите пароль или включите генерацию пароля"));
    }
    let actx = with_actor(&ctx, &p);
    let (s, u) = auth_call(&st, &actx, "users/create", json!({"email": b.email, "password": b.password, "generate": b.generate})).await?;
    if !s.is_success() {
        return Err(account_err(s, &u));
    }
    let uid = u["user_id"].as_str().unwrap_or_default().to_string();
    let email = u["email"].as_str().unwrap_or_default().to_string();
    let added = st.ledger.post(&format!("/internal/v1/companies/{}/members", p.company_id), &actx, &json!({"user_id": uid, "email": email, "role": role})).await;
    if let Err(e) = added {
        // не оставляем пользователя без компании
        let _ = st.auth.json(Method::DELETE, &format!("/internal/v1/users/{uid}"), &actx, None).await;
        return Err(e);
    }
    audit(&st, &ctx, &p, "member.create", &uid).await;
    let mut out = json!({"user_id": uid, "email": email, "role": role, "must_change_password": true, "password_generated": b.generate});
    if let Some(pw) = u["password"].as_str() {
        out["password"] = json!(pw);
    }
    Ok((StatusCode::CREATED, Json(out)))
}

#[derive(Deserialize)]
pub struct MemberRole {
    role: String,
}

/// PATCH /api/v1/members/{user_id} {role} (admin). Директора менять нельзя (409 cannot_change_director), владение передаёт `team_transfer`.
pub async fn members_update(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(uid): Path<String>, Json(b): Json<MemberRole>) -> R<Json<Value>> {
    let role = if b.role == "director" { "director" } else { staff_role(&b.role)? }; // для director ответит ledger: 409
    may_grant(&p, role)?;
    let target = find_member(&st, &ctx, &p, &uid).await?;
    if target["role"] != "director" {
        may_grant(&p, target["role"].as_str().unwrap_or(""))?;
    }
    let v = st.ledger.json(Method::PATCH, &format!("/internal/v1/companies/{}/members/{uid}", p.company_id), &with_actor(&ctx, &p), Some(&json!({"role": role}))).await?.into_result()?;
    sync_telegram_role(&st, &ctx, &p, &uid, Some(role)).await;
    audit(&st, &ctx, &p, "member.role_change", &uid).await;
    Ok(Json(member_json(&v, &p.user_id)))
}

/// Участник своей компании (чужой — 404) и его роль.
async fn find_member(st: &AppState, ctx: &Ctx, p: &Principal, uid: &str) -> R<Value> {
    company_members(st, ctx, p).await?.into_iter().find(|m| m["user_id"].as_str() == Some(uid)).ok_or_else(|| err(StatusCode::NOT_FOUND, "not_found", "Не найдено"))
}

/// DELETE /api/v1/members/{user_id} (admin): сессии и токены сотрудника аннулируются. Директора удалить нельзя, администратора удаляет директор.
pub async fn members_delete(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(uid): Path<String>) -> R<StatusCode> {
    let m = find_member(&st, &ctx, &p, &uid).await?;
    if m["role"] == "director" {
        return Err(err(StatusCode::CONFLICT, "cannot_remove_director", "Директора удалить нельзя"));
    }
    may_grant(&p, m["role"].as_str().unwrap_or(""))?;
    let actx = with_actor(&ctx, &p);
    // сначала аннулируем доступ (сессии, токены), затем членство; повтор безопасен
    let r = st.auth.json(Method::DELETE, &format!("/internal/v1/users/{uid}"), &actx, None).await?;
    if !r.status.is_success() && r.status != StatusCode::NOT_FOUND {
        return Err(err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"));
    }
    st.ledger.json(Method::DELETE, &format!("/internal/v1/companies/{}/members/{uid}", p.company_id), &actx, None).await?.into_result()?;
    sync_telegram_role(&st, &ctx, &p, &uid, None).await;
    audit(&st, &ctx, &p, "member.delete", &uid).await;
    Ok(StatusCode::NO_CONTENT)
}

/// POST /api/v1/members/{user_id}/reset-password (admin) -> {password}: новый сгенерированный пароль, виден один раз,
/// сессии сотрудника закрыты, при следующем входе смена обязательна. Пароль администратора сбрасывает директор.
pub async fn members_reset_password(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(uid): Path<String>) -> R<Json<Value>> {
    let m = find_member(&st, &ctx, &p, &uid).await?;
    if m["role"] == "director" {
        return Err(err(StatusCode::CONFLICT, "cannot_reset_director", "Пароль директора меняется в настройках аккаунта"));
    }
    may_grant(&p, m["role"].as_str().unwrap_or(""))?;
    let v = user_call(&st, &ctx, &p, &uid, Method::POST, "/reset-password", None).await?;
    audit(&st, &ctx, &p, "member.reset_password", &uid).await;
    Ok(Json(json!({"password": v["password"], "must_change_password": true})))
}

// ---------- команда компании: приглашения по ссылке, передача владения, журнал действий ----------
// Состав и роли хранит ledger (company_members, см. выше); здесь — одноразовые приглашения (auth) и всё, что вокруг них.
fn team_err(s: StatusCode, v: &Value) -> ApiError {
    let (code, msg) = match v["detail"]["code"].as_str().or_else(|| v["error"]["code"].as_str()).unwrap_or("") {
        "invalid_role" => ("invalid_role", "Неизвестная роль"),
        "invite_invalid" => ("invite_invalid", "Приглашение недействительно или истекло"),
        "already_member" => ("already_member", "Вы уже состоите в этой команде"),
        "has_company_data" => ("has_team", "У вас уже есть своя компания с данными или командой: приглашение принять нельзя"),
        "not_found" | "invite_not_found" => ("not_found", "Не найдено"),
        "too_many_members" => ("too_many_members", "Достигнут предел числа участников"),
        "invite_email_mismatch" => ("invite_email_mismatch", "Приглашение выписано на другой email: войдите под адресом, на который оно отправлено"),
        "cannot_remove_director" => ("owner_cannot_leave", "Владелец не может выйти: сначала передайте владение"),
        "same_owner" => ("same_owner", "Этот участник уже владелец"),
        "invalid_email" => ("invalid_email", "Некорректный email"),
        _ => return err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    };
    err(s, code, msg)
}

async fn team_call(up: &Upstream, ctx: &Ctx, p: &Principal, method: Method, path: &str, body: Option<Value>) -> R<Value> {
    let r = up.json(method, path, &with_actor(ctx, p), body.as_ref()).await?;
    if r.status.is_success() { Ok(r.json()) } else { Err(team_err(r.status, &r.json())) }
}

/// Telegram-роль выводится из веб-роли: после изменения или удаления сообщаем боту (ошибка не мешает основному действию).
async fn sync_telegram_role(st: &AppState, ctx: &Ctx, p: &Principal, web_user: &str, role: Option<&str>) {
    let _ = st
        .notifier
        .post(&format!("/internal/v1/companies/{}/notifications/web-role", p.company_id), &with_actor(ctx, p), &json!({"web_user_id": web_user, "web_role": role}))
        .await;
}

pub async fn team_invites_list(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx) -> R<Json<Value>> {
    Ok(Json(team_call(&st.auth, &ctx, &p, Method::GET, &format!("/internal/v1/companies/{}/invites", p.company_id), None).await?))
}

pub async fn team_invite_create(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Json(b): Json<Value>) -> R<(StatusCode, Json<Value>)> {
    let role = b["role"].as_str().unwrap_or("").to_string();
    may_grant(&p, &role)?;
    let email = b["email"].as_str().unwrap_or("").trim().to_string();
    let body = json!({"role": role, "actor": p.user_id, "email": email});
    let mut v = team_call(&st.auth, &ctx, &p, Method::POST, &format!("/internal/v1/companies/{}/invites", p.company_id), Some(body)).await?;
    // приглашение на email уходит письмом; без настроенного SMTP сайт показывает ссылку, и её отправляют вручную
    let mut emailed = false;
    if !email.is_empty() {
        let inviter = if p.has_email { p.email.clone() } else { String::new() };
        let mail = json!({"company_id": p.company_id, "to": email, "code": v["code"], "company": p.company_name, "role": v["role"], "inviter": inviter});
        if let Ok(m) = st.notifier.post("/internal/v1/mail/invite", &with_actor(&ctx, &p), &mail).await {
            emailed = m["sent"].as_bool().unwrap_or(false);
            v["email_limited"] = json!(m["limited"].as_bool().unwrap_or(false));
        }
    }
    v["emailed"] = json!(emailed);
    audit(&st, &ctx, &p, "team.invite_create", v["role"].as_str().unwrap_or("")).await;
    Ok((StatusCode::CREATED, Json(v)))
}

pub async fn team_invite_delete(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(id): Path<String>) -> R<StatusCode> {
    team_call(&st.auth, &ctx, &p, Method::DELETE, &format!("/internal/v1/companies/{}/invites/{id}", p.company_id), None).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Принять приглашение (одноразовый код из ссылки): пользователь переезжает из своей пустой компании в компанию пригласившего.
/// Сначала проверка приглашения, затем запись в ledger, и только потом код расходуется: сбой посередине не сжигает ссылку.
pub async fn team_accept(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Json(b): Json<Value>) -> R<Json<Value>> {
    let body = json!({"code": b["code"].as_str().unwrap_or(""), "user_id": p.user_id});
    let inv = team_call(&st.auth, &ctx, &p, Method::POST, "/internal/v1/invites/check", Some(body.clone())).await?;
    let cid = inv["company_id"].as_str().unwrap_or_default().to_string();
    let role = inv["role"].as_str().unwrap_or("viewer").to_string();
    let email = if p.has_email { p.email.clone() } else { String::new() };
    let joined = team_call(&st.ledger, &ctx, &p, Method::POST, &format!("/internal/v1/companies/{cid}/members/join"), Some(json!({"user_id": p.user_id, "email": email, "role": role}))).await?;
    let _ = st.auth.json(Method::POST, "/internal/v1/invites/consume", &with_actor(&ctx, &p), Some(&body)).await;
    let joined_p = Principal { company_id: cid.clone(), ..Principal::empty(&p.user_id) };
    audit(&st, &ctx, &joined_p, "team.invite_accept", &p.user_id).await;
    Ok(Json(json!({"company_id": cid, "role": joined["role"]})))
}

/// Владелец передаёт владение участнику и сам становится администратором (id компании не меняется).
pub async fn team_transfer(axum::extract::State(st): axum::extract::State<AppState>, Manager(p): Manager, ctx: Ctx, Json(b): Json<Value>) -> R<Json<Value>> {
    let new_owner = b["user_id"].as_str().unwrap_or("").to_string();
    let v = team_call(&st.ledger, &ctx, &p, Method::POST, &format!("/internal/v1/companies/{}/transfer", p.company_id), Some(json!({"user_id": new_owner}))).await?;
    sync_telegram_role(&st, &ctx, &p, &new_owner, Some("director")).await;
    sync_telegram_role(&st, &ctx, &p, &p.user_id, Some("admin")).await;
    audit(&st, &ctx, &p, "team.transfer_ownership", &new_owner).await;
    Ok(Json(v))
}

/// Участник выходит из чужой команды и получает собственную компанию; директор выйти не может (сначала передаёт владение).
pub async fn team_leave(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx) -> R<StatusCode> {
    if p.role == Role::Director {
        return Err(team_err(StatusCode::CONFLICT, &json!({"detail": {"code": "cannot_remove_director"}})));
    }
    audit(&st, &ctx, &p, "team.leave", &p.user_id).await;
    sync_telegram_role(&st, &ctx, &p, &p.user_id, None).await;
    st.ledger.json(Method::DELETE, &format!("/internal/v1/companies/{}/members/{}", p.company_id, p.user_id), &with_actor(&ctx, &p), None).await?.into_result()?;
    ensure_company(&st, &ctx, &p.user_id, &p.email, Requisites::default()).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// id пользователя -> email или название сертификата (для журнала).
async fn actor_names(st: &AppState, ctx: &Ctx, p: &Principal) -> BTreeMap<String, String> {
    let mut names = BTreeMap::new();
    let Ok(members) = company_members(st, ctx, p).await else { return names };
    let ids: Vec<&str> = members.iter().filter_map(|m| m["user_id"].as_str()).collect();
    let Ok(v) = team_call(&st.auth, ctx, p, Method::GET, &format!("/internal/v1/users/labels?user_ids={}", ids.join(",")), None).await else { return names };
    if let Some(map) = v["items"].as_object() {
        for (id, it) in map {
            let name = it["email"].as_str().filter(|s| !s.is_empty()).or(it["label"].as_str().filter(|s| !s.is_empty()));
            if let Some(name) = name {
                names.insert(id.clone(), name.to_string());
            }
        }
    }
    names
}

/// Ячейка CSV: кавычки удваиваются, а значения, которые Excel принял бы за формулу, получают префикс-апостроф.
pub fn csv_cell(s: &str) -> String {
    let s = s.replace(['\r', '\n'], " ");
    let s = if s.starts_with(['=', '+', '-', '@', '\t']) { format!("'{s}") } else { s };
    format!("\"{}\"", s.replace('"', "\"\""))
}

#[derive(Deserialize)]
pub struct ExportQuery {
    q: Option<String>,
}

/// Выгрузка журнала в CSV (UTF-8 с BOM, открывается в Excel). Фильтр `q` — та же подстрока действия, что на странице.
pub async fn team_activity_export(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, Query(eq): Query<ExportQuery>) -> R<Response> {
    let actx = with_actor(&ctx, &p);
    let q: String = eq.q.unwrap_or_default().chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '.').collect();
    let mut rows: Vec<Value> = vec![];
    let mut offset = 0;
    loop {
        let v = st.ledger.get(&format!("/internal/v1/companies/{}/audit?limit=200&offset={offset}&q={q}", p.company_id), &actx).await?;
        let items = v["items"].as_array().cloned().unwrap_or_default();
        let n = items.len();
        rows.extend(items);
        offset += 200;
        if n < 200 || rows.len() >= 20_000 {
            break;
        }
    }
    let names = actor_names(&st, &ctx, &p).await;
    let mut out = String::from("\u{feff}\"Время\";\"Кто\";\"Действие\";\"Объект\";\"Результат\"\r\n");
    for r in &rows {
        let actor = r["actor"].as_str().unwrap_or("");
        let who = names.get(actor).map(String::as_str).unwrap_or(actor);
        let cells = [r["at"].as_str().unwrap_or(""), who, r["action"].as_str().unwrap_or(""), r["object_id"].as_str().unwrap_or(""), r["outcome"].as_str().unwrap_or("")];
        out.push_str(&cells.iter().map(|c| csv_cell(c)).collect::<Vec<_>>().join(";"));
        out.push_str("\r\n");
    }
    audit(&st, &ctx, &p, "team.activity_export", &rows.len().to_string()).await;
    Ok((StatusCode::OK, [(header::CONTENT_TYPE, "text/csv; charset=utf-8"), (header::CONTENT_DISPOSITION, "attachment; filename=\"activity.csv\"")], out).into_response())
}

/// Журнал действий компании с именами участников (id -> email или название сертификата).
pub async fn team_activity(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, RawQuery(q): RawQuery) -> R<Json<Value>> {
    let path = match q {
        Some(q) => format!("/internal/v1/companies/{}/audit?{q}", p.company_id),
        None => format!("/internal/v1/companies/{}/audit", p.company_id),
    };
    let mut v = st.ledger.get(&path, &with_actor(&ctx, &p)).await?;
    let names = actor_names(&st, &ctx, &p).await;
    if let Some(items) = v["items"].as_array_mut() {
        for it in items.iter_mut() {
            let actor = it["actor"].as_str().unwrap_or("").to_string();
            if let Some(n) = names.get(&actor) {
                it["actor_name"] = json!(n);
            }
        }
    }
    Ok(Json(v))
}

/// Ссылка привязки Telegram доступна любой роли: роль в боте выводится из веб-роли того, кто привязывает.
pub async fn notifier_link(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, Path(cid): Path<String>) -> R<Response> {
    authorize(&p, &cid)?;
    let body = json!({"web_user_id": p.user_id, "web_role": p.role.as_str()});
    audit(&st, &ctx, &p, "telegram.link_request", "").await;
    Ok(st.notifier.json(Method::POST, &format!("/internal/v1/companies/{cid}/notifications/link"), &with_actor(&ctx, &p), Some(&body)).await?.into_response())
}

/// Право на запрос к уведомлениям: настройки и команда бота — администратору, цели и ретро — тому, кто может менять данные,
/// остальные чтения — любой роли.
pub fn notifications_perm(method: &Method, path: &str) -> Perm {
    let sub = path.split('/').skip(6).collect::<Vec<_>>();
    let first = sub.first().copied().unwrap_or("");
    let read = *method == Method::GET || *method == Method::HEAD;
    match (read, first) {
        (true, "team") => Perm::Admin,
        (true, _) => Perm::Read,
        (false, "goals") | (false, "retro") => Perm::Edit,
        (false, _) => Perm::Admin,
    }
}

pub async fn to_notifier(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    let method = req.method().clone();
    let perm = notifications_perm(&method, req.uri().path());
    let section = req.uri().path().split('/').nth(6).unwrap_or("").to_string();
    let r = relay(&st.notifier, &p, &ctx, req, perm).await?;
    if method != Method::GET && r.status.is_success() {
        audit(&st, &ctx, &p, &format!("notifications.{}.{}", section, method.as_str().to_lowercase()), "").await;
    }
    Ok(r.into_response())
}

// ---------- внешний API по токену: /api/ext/v1/* ----------
async fn ext_forward(up: &Upstream, p: &Principal, ctx: &Ctx, req: Request, tail: &str) -> R<Response> {
    let q = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
    let path = format!("/internal/v1/companies/{}/{tail}{q}", p.company_id);
    Ok(forward(up, p, ctx, req, path).await?.into_response())
}

/// Права токена по роли владельца: viewer — `read`, editor и director — `write` (чтение + изменение данных).
fn token_scope(role: Role) -> &'static str {
    if role.can(Perm::Edit) { "write" } else { "read" }
}

pub async fn ext_company(TokenPrincipal(p): TokenPrincipal) -> Json<Value> {
    Json(json!({"company_id": p.company_id, "company_name": p.company_name, "inn": p.company_inn, "ogrn": p.company_ogrn, "scope": token_scope(p.role)}))
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

/// GET — чтение, POST — создание ожидаемого платежа (scope `write`: токен viewer получает 403 `forbidden`).
pub async fn ext_events(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    p.require(if req.method() == Method::GET || req.method() == Method::HEAD { Perm::Read } else { Perm::Edit })?;
    ext_forward(&st.ledger, &p, &ctx, req, "expected-events").await
}

pub async fn ext_plan_fact(axum::extract::State(st): axum::extract::State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, req: Request) -> R<Response> {
    ext_forward(&st.planfact, &p, &ctx, req, "plan-fact").await
}

// ---------- проксирование к внутренним сервисам ----------
const MAX_BODY: usize = 26 * 1024 * 1024;

/// Переписывает публичный путь /api/v1/companies/{cid}/... в /internal/v1/companies/{cid}/... и пересылает запрос
/// как есть (метод, query, тело, Content-Type). Ответ сервиса возвращается без изменений.
async fn relay(up: &Upstream, p: &Principal, ctx: &Ctx, req: Request, perm: Perm) -> R<Reply> {
    let uri = req.uri().clone();
    let cid = uri.path().split('/').nth(4).unwrap_or_default().to_string();
    authorize(p, &cid)?;
    p.require(perm)?;
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

/// Права по методу: GET/HEAD — `read`, любое изменение (POST/PATCH/DELETE: импорт и подтверждение, ожидаемые платежи,
/// смена категории) — `edit`. Название компании (PATCH на саму компанию) идёт отдельным маршрутом `update_company` (`manage`).
pub async fn to_ledger(axum::extract::State(st): axum::extract::State<AppState>, p: Principal, ctx: Ctx, req: Request) -> R<Response> {
    let perm = if req.method() == Method::GET || req.method() == Method::HEAD { Perm::Read } else { Perm::Edit };
    Ok(relay(&st.ledger, &p, &ctx, req, perm).await?.into_response())
}

/// PATCH /api/v1/companies/{cid}: название и реквизиты компании (admin).
pub async fn update_company(axum::extract::State(st): axum::extract::State<AppState>, Administrator(p): Administrator, ctx: Ctx, req: Request) -> R<Response> {
    let r = relay(&st.ledger, &p, &ctx, req, Perm::Admin).await?;
    if r.status.is_success() {
        audit(&st, &ctx, &p, "company.update", &p.company_id).await;
    }
    Ok(r.into_response())
}

pub async fn to_forecast(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, req: Request) -> R<Response> {
    Ok(relay(&st.forecast, &p, &ctx, req, Perm::Read).await?.into_response())
}

pub async fn to_planfact(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, req: Request) -> R<Response> {
    Ok(relay(&st.planfact, &p, &ctx, req, Perm::Read).await?.into_response())
}

/// Построение прогноза — вычисляющий POST: право `read` (viewer тоже может строить прогнозы и сценарии).
pub async fn create_forecast(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, req: Request) -> R<Response> {
    let r = relay(&st.forecast, &p, &ctx, req, Perm::Read).await?;
    if r.status.is_success() {
        audit(&st, &ctx, &p, "forecast.create", r.json()["run_id"].as_str().unwrap_or("")).await;
    }
    Ok(r.into_response())
}

pub async fn create_scenario(axum::extract::State(st): axum::extract::State<AppState>, Reader(p): Reader, ctx: Ctx, req: Request) -> R<Response> {
    let r = relay(&st.forecast, &p, &ctx, req, Perm::Read).await?;
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
pub async fn tax_calendar(axum::extract::State(st): axum::extract::State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<TaxBody>>) -> R<(StatusCode, Json<Value>)> {
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
pub async fn gov_key_rate(axum::extract::State(st): axum::extract::State<AppState>, _p: Reader, ctx: Ctx) -> R<Json<Value>> {
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
pub async fn integrations_sync(axum::extract::State(st): axum::extract::State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<SyncBody>>) -> R<Json<Value>> {
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

    #[test]
    fn csv_cells_are_quoted_and_formula_safe() {
        assert_eq!(csv_cell("простой"), "\"простой\"");
        assert_eq!(csv_cell("a\"b"), "\"a\"\"b\"");
        assert_eq!(csv_cell("=HYPERLINK(\"x\")"), "\"'=HYPERLINK(\"\"x\"\")\"");
        assert_eq!(csv_cell("+1"), "\"'+1\"");
        assert_eq!(csv_cell("-2"), "\"'-2\"");
        assert_eq!(csv_cell("@cmd"), "\"'@cmd\"");
        assert_eq!(csv_cell("a\nb"), "\"a b\"");
    }

    #[test]
    fn four_roles_and_their_permissions() {
        let can = |r: Role| [r.can(Perm::Read), r.can(Perm::Edit), r.can(Perm::Admin), r.can(Perm::Manage)];
        assert_eq!(can(Role::Director), [true, true, true, true]);
        assert_eq!(can(Role::Admin), [true, true, true, false]);
        assert_eq!(can(Role::Editor), [true, true, false, false]);
        assert_eq!(can(Role::Viewer), [true, false, false, false]);
        assert_eq!(Role::parse("unknown"), Role::Viewer); // неизвестная роль = минимум прав
        assert_eq!(Role::parse("admin").as_str(), "admin");
        assert_eq!(Role::Admin.permissions(), json!({"read": true, "edit": true, "admin": true, "manage": false}));
    }

    #[test]
    fn notifications_need_the_right_permission() {
        let c = "/api/v1/companies/c1";
        let (get, post) = (Method::GET, Method::POST);
        assert_eq!(notifications_perm(&get, &format!("{c}/notifications")), Perm::Read);
        assert_eq!(notifications_perm(&get, &format!("{c}/notifications/sprint")), Perm::Read);
        assert_eq!(notifications_perm(&get, &format!("{c}/notifications/team")), Perm::Admin);
        assert_eq!(notifications_perm(&Method::PATCH, &format!("{c}/notifications/goals/3")), Perm::Edit);
        assert_eq!(notifications_perm(&post, &format!("{c}/notifications/retro")), Perm::Edit);
        assert_eq!(notifications_perm(&Method::PUT, &format!("{c}/notifications/prefs")), Perm::Admin);
        assert_eq!(notifications_perm(&post, &format!("{c}/notifications/departments")), Perm::Admin);
        assert_eq!(notifications_perm(&Method::DELETE, &format!("{c}/notifications/chats/5")), Perm::Admin);
    }
}
