//! Почта: подтверждение адреса, восстановление пароля по ссылке, приглашения сотрудников и настройки рассылок.
//! Ссылки выпускает auth (хранит только хэш), письма собирает и отправляет notify. Сбой почты не ломает основное
//! действие (регистрацию, приглашение): вызывающий узнаёт об этом по флагу `email_sent`.

use crate::AppState;
use crate::http::{Principal, Reader, Role, public_email, with_actor};
use axum::{
    Json,
    extract::State,
    http::{Method, StatusCode},
};
use common::{ApiError, Ctx, R, err};
use serde::Deserialize;
use serde_json::{Value, json};

fn link_err(v: &Value) -> ApiError {
    match v["detail"]["code"].as_str().unwrap_or("") {
        "token_invalid" => err(
            StatusCode::NOT_FOUND,
            "token_invalid",
            "Ссылка недействительна или устарела. Запросите новое письмо",
        ),
        "too_many_requests" => err(
            StatusCode::TOO_MANY_REQUESTS,
            "too_many_requests",
            "Письмо уже отправлено, повторить можно через минуту",
        ),
        "already_verified" => err(StatusCode::CONFLICT, "already_verified", "Почта уже подтверждена"),
        "invalid_password" => err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_password", "Пароль должен быть от 8 до 128 символов"),
        "no_email" | "user_not_found" => err(StatusCode::NOT_FOUND, "not_found", "Не найдено"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

fn role_text(role: &str) -> &'static str {
    match role {
        "director" => "владелец",
        "admin" => "администратор",
        "editor" => "аналитик",
        _ => "наблюдатель",
    }
}

/// Письмо через notify. `false`, если notify недоступен или отклонил письмо (подробности — в его журнале).
pub async fn send(st: &AppState, ctx: &Ctx, kind: &str, to: &str, data: Value, dedup_key: Option<String>) -> bool {
    let body = json!({"kind": kind, "to": to, "data": data, "dedup_key": dedup_key});
    match st.notify.json(Method::POST, "/internal/v1/send", ctx, Some(&body)).await {
        Ok(r) if r.status.is_success() => true,
        Ok(r) => {
            eprintln!("{} notify rejected {kind}: {}", ctx.rid, r.status.as_u16());
            false
        }
        Err(_) => false,
    }
}

/// Приглашение по ссылке (`/?invite=<код>`, 7 дней) на указанный адрес: (отправлено, упёрлись в лимит 50 писем в час).
pub async fn send_team_invite(st: &AppState, ctx: &Ctx, p: &Principal, to: &str, code: &str, role: &str) -> (bool, bool) {
    let inviter = public_email(&p.email).unwrap_or_default();
    let data = json!({"token": code, "company": p.company_name, "company_id": p.company_id, "inviter": inviter, "role_text": role_text(role)});
    let body = json!({"kind": "team_invite", "to": to, "data": data});
    match st.notify.json(Method::POST, "/internal/v1/send", ctx, Some(&body)).await {
        Ok(r) if r.status.is_success() => (true, false),
        Ok(r) => (false, r.status == StatusCode::TOO_MANY_REQUESTS),
        Err(_) => (false, false),
    }
}

/// Ссылка auth (`verify` | `invite`) для пользователя: (token, email).
async fn issue_link(st: &AppState, ctx: &Ctx, uid: &str, kind: &str) -> R<(String, String)> {
    let r = st
        .auth
        .json(Method::POST, &format!("/internal/v1/users/{uid}/email-link"), ctx, Some(&json!({"kind": kind})))
        .await?;
    let v = r.json();
    if !r.status.is_success() {
        return Err(link_err(&v));
    }
    match (v["token"].as_str(), v["email"].as_str()) {
        (Some(t), Some(e)) if !t.is_empty() && !e.is_empty() => Ok((t.to_string(), e.to_string())),
        _ => Err(err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа")),
    }
}

/// Письмо «Подтвердите почту». После регистрации и смены адреса; ошибки не прерывают вызывающего.
pub async fn send_verify(st: &AppState, ctx: &Ctx, uid: &str) -> bool {
    match issue_link(st, ctx, uid, "verify").await {
        Ok((token, email)) => send(st, ctx, "verify_email", &email, json!({"token": token}), None).await,
        Err(_) => false,
    }
}

/// Приглашение нового сотрудника с паролем (`invite`) или ссылка после сброса пароля администратором (`member_reset`).
pub async fn send_member_link(st: &AppState, ctx: &Ctx, p: &Principal, uid: &str, role: &str, kind: &str) -> bool {
    let Ok((token, email)) = issue_link(st, ctx, uid, "invite").await else {
        return false;
    };
    let inviter = public_email(&p.email).unwrap_or("Администратор компании");
    let data = json!({"token": token, "company": p.company_name, "inviter": inviter, "role_text": role_text(role)});
    send(st, ctx, kind, &email, data, None).await
}

// ---------- без сессии ----------
#[derive(Deserialize)]
pub struct ForgotBody {
    email: String,
}

/// POST /api/v1/auth/password/forgot {email} -> 202 всегда: ответ не выдаёт, зарегистрирован ли адрес.
pub async fn forgot(State(st): State<AppState>, ctx: Ctx, Json(b): Json<ForgotBody>) -> R<(StatusCode, Json<Value>)> {
    let r = st
        .auth
        .json(Method::POST, "/internal/v1/password/forgot", &ctx, Some(&json!({"email": b.email})))
        .await?;
    let v = r.json();
    if let (true, Some(token), Some(email)) = (r.status.is_success(), v["token"].as_str(), v["email"].as_str()) {
        send(&st, &ctx, "reset_password", email, json!({"token": token}), None).await;
    }
    Ok((StatusCode::ACCEPTED, Json(json!({"ok": true}))))
}

#[derive(Deserialize)]
pub struct ResetBody {
    token: String,
    password: String,
    #[serde(default)]
    password_confirm: Option<String>,
}

/// POST /api/v1/auth/password/reset {token, password, password_confirm?}: новый пароль по ссылке из письма.
/// Ссылка одноразовая; все сессии пользователя закрываются, дальше обычный вход.
pub async fn reset(State(st): State<AppState>, ctx: Ctx, Json(b): Json<ResetBody>) -> R<Json<Value>> {
    if b.password_confirm.as_deref().is_some_and(|c| c != b.password) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    let r = st
        .auth
        .json(
            Method::POST,
            "/internal/v1/password/reset",
            &ctx,
            Some(&json!({"token": b.token, "password": b.password})),
        )
        .await?;
    let v = r.json();
    if !r.status.is_success() {
        return Err(link_err(&v));
    }
    Ok(Json(json!({"ok": true, "email": v["email"]})))
}

#[derive(Deserialize)]
pub struct TokenBody {
    token: String,
}

/// POST /api/v1/account/email/verify {token}: ссылка из письма открывается и без входа (например, на телефоне).
pub async fn verify(State(st): State<AppState>, ctx: Ctx, Json(b): Json<TokenBody>) -> R<Json<Value>> {
    let r = st
        .auth
        .json(Method::POST, "/internal/v1/email/verify", &ctx, Some(&json!({"token": b.token})))
        .await?;
    let v = r.json();
    if !r.status.is_success() {
        return Err(link_err(&v));
    }
    Ok(Json(json!({"ok": true, "email": v["email"], "email_verified": true})))
}

/// POST /api/v1/notifications/unsubscribe {token}: отписка по ссылке из рассылки, без входа.
pub async fn unsubscribe(State(st): State<AppState>, ctx: Ctx, Json(b): Json<TokenBody>) -> R<Json<Value>> {
    st.notify.post("/internal/v1/unsubscribe", &ctx, &json!({"token": b.token})).await?;
    Ok(Json(json!({"ok": true})))
}

// ---------- свой аккаунт ----------
/// POST /api/v1/account/email/resend: письмо подтверждения ещё раз (не чаще раза в минуту).
pub async fn resend(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx) -> R<Json<Value>> {
    let actx = with_actor(&ctx, &p);
    let (token, email) = issue_link(&st, &actx, &p.user_id, "verify").await?;
    if !send(&st, &actx, "verify_email", &email, json!({"token": token}), None).await {
        return Err(mail_down());
    }
    Ok(Json(json!({"ok": true, "email": email})))
}

fn mail_down() -> ApiError {
    err(
        StatusCode::SERVICE_UNAVAILABLE,
        "notify_unavailable",
        "Сервис уведомлений недоступен, попробуйте позже",
    )
}

fn not_verified() -> ApiError {
    err(
        StatusCode::CONFLICT,
        "email_not_verified",
        "Сначала подтвердите почту: ссылка в письме после регистрации",
    )
}

fn prefs_path(p: &Principal) -> String {
    format!("/internal/v1/prefs/{}?company_id={}&role={}", p.user_id, p.company_id, p.role.as_str())
}

fn prefs_json(p: &Principal, prefs: Value) -> Value {
    let mut v = prefs;
    v["email"] = json!(public_email(&p.email));
    v["email_verified"] = json!(p.email_verified);
    v["role_default_on"] = json!(matches!(p.role, Role::Director | Role::Admin));
    v
}

/// GET /api/v1/account/notifications: свои настройки рассылок (любая роль) и статус почты.
pub async fn get_prefs(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx) -> R<Json<Value>> {
    let v = st.notify.get(&prefs_path(&p), &with_actor(&ctx, &p)).await?;
    Ok(Json(prefs_json(&p, v)))
}

#[derive(Deserialize)]
pub struct PrefsBody {
    #[serde(default)]
    alerts: Option<bool>,
    #[serde(default)]
    reports: Option<bool>,
}

/// PUT /api/v1/account/notifications {alerts?, reports?}: свои почтовые подписки. Когда и что приходит (расписание,
/// порог разрыва), задаёт администратор в настройках уведомлений компании (notifier). Пока почта не подтверждена,
/// включить рассылки нельзя: письма всё равно не ушли бы.
pub async fn put_prefs(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Json(b): Json<PrefsBody>) -> R<Json<Value>> {
    if !p.email_verified && (b.alerts == Some(true) || b.reports == Some(true)) {
        return Err(not_verified());
    }
    let body = json!({"alerts": b.alerts, "reports": b.reports});
    let actx = with_actor(&ctx, &p);
    let v = st.notify.json(Method::PUT, &prefs_path(&p), &actx, Some(&body)).await?.into_result()?;
    crate::http::audit(&st, &ctx, &p, "notifications.update", "").await;
    Ok(Json(prefs_json(&p, v)))
}

/// POST /api/v1/account/notifications/test: тестовое письмо себе, не чаще раза в минуту.
pub async fn test(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx) -> R<Json<Value>> {
    if !p.email_verified {
        return Err(not_verified());
    }
    let email = public_email(&p.email).ok_or_else(not_verified)?;
    let minute = chrono::Utc::now().timestamp() / 60;
    if !send(
        &st,
        &with_actor(&ctx, &p),
        "test",
        email,
        json!({}),
        Some(format!("test:{}:{minute}", p.user_id)),
    )
    .await
    {
        return Err(mail_down());
    }
    Ok(Json(json!({"ok": true, "email": email})))
}

/// Удалённый сотрудник больше не получает рассылок (best effort).
pub async fn forget_user(st: &AppState, ctx: &Ctx, uid: &str) {
    let _ = st.notify.json(Method::DELETE, &format!("/internal/v1/users/{uid}"), ctx, None).await;
}
