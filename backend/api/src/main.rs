//! api (BFF): единственный публичный Rust-сервис. Проверка сессии через auth, CSRF, заголовки безопасности,
//! раздача SPA и маршрутизация/оркестрация вызовов к внутренним сервисам. Собственной БД нет.

mod admin;
mod advisor;
mod demo;
mod demo_login;
mod fns;
#[cfg(test)]
mod fns_tests;
mod http;
mod legacy;
mod mail;
#[cfg(test)]
mod roles_tests;

use axum::{
    Router,
    extract::Request,
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::Response,
    routing::{delete, get, patch, post},
};
use common::{Upstream, mw::env};
use std::sync::Arc;
use tower_http::services::{ServeDir, ServeFile};

pub struct Config {
    pub cookie_secure: bool,
}

#[derive(Clone)]
pub struct AppState {
    pub auth: Upstream,
    pub ecp: Upstream,
    pub gov: Upstream,
    pub ledger: Upstream,
    pub forecast: Upstream,
    pub planfact: Upstream,
    pub integrator: Upstream,
    pub advisor: Upstream,
    pub notifier: Upstream,
    pub backoffice: Upstream,
    pub notify: Upstream,
    pub fns: Upstream,
    pub demo: Arc<demo::Demo>,
    pub cfg: Arc<Config>,
}

#[tokio::main]
async fn main() {
    let state = AppState {
        // ecp и auth для SPA — один «сервис входа» (те же коды ошибок, что и раньше)
        auth: Upstream::new("auth", "Сервис входа", &env("AUTH_URL", "http://127.0.0.1:8001"), 30),
        ecp: Upstream::new("auth", "Сервис входа", &env("ECP_URL", "http://127.0.0.1:8007"), 60),
        gov: Upstream::new("gov", "Сервис государственных данных", &env("GOV_URL", "http://127.0.0.1:8003"), 60),
        ledger: Upstream::new("ledger", "Сервис данных", &env("LEDGER_URL", "http://127.0.0.1:3002"), 60),
        forecast: Upstream::new("forecast", "Сервис прогноза", &env("FORECAST_URL", "http://127.0.0.1:3003"), 80),
        planfact: Upstream::new("planfact", "Сервис план-факта", &env("PLANFACT_URL", "http://127.0.0.1:3004"), 60),
        integrator: Upstream::new("integrator", "Сервис интеграций", &env("INTEGRATOR_URL", "http://127.0.0.1:8008"), 30),
        advisor: Upstream::new("advisor", "Сервис консультанта", &env("ADVISOR_URL", "http://127.0.0.1:8011"), 85),
        notifier: Upstream::new("notifier", "Сервис уведомлений", &env("NOTIFIER_URL", "http://127.0.0.1:8009"), 60),
        backoffice: Upstream::new("backoffice", "Сервис оператора", &env("BACKOFFICE_URL", "http://127.0.0.1:8013"), 60),
        notify: Upstream::new("notify", "Почтовый сервис", &env("NOTIFY_URL", "http://127.0.0.1:8014"), 30),
        fns: Upstream::new("fns", "Сервис ФНС", &env("FNS_URL", "http://127.0.0.1:8015"), 70),
        demo: Arc::new(demo::Demo::from_env()),
        cfg: Arc::new(Config { cookie_secure: env("COOKIE_SECURE", "0") == "1" }),
    };

    // команды из версии с составом в auth переезжают в ledger (разово, идемпотентно, в фоне)
    tokio::spawn(legacy::run(state.clone()));

    let api = tenant_routes()
        // панель оператора платформы: отдельная cookie op_session, префикс /api/admin/v1 (admin.rs)
        .merge(admin::routes())
        // быстрый вход в демо-аккаунты (кнопки на странице входа), только при DEMO_LOGIN=1: demo_login.rs
        .merge(demo_login::routes(Arc::new(demo_login::DemoLogin::from_env())))
        .layer(middleware::from_fn(security_layer))
        .with_state(state.clone());

    let web_dir = env("WEB_DIR", "frontend/dist");
    let spa = ServeDir::new(&web_dir).fallback(ServeFile::new(format!("{web_dir}/index.html")));
    let public = Router::new()
        .route("/live", get(|| async { "ok" }))
        .merge(api)
        .fallback_service(spa)
        .layer(middleware::from_fn(static_headers));

    // Внутренний API: только служебная сеть, без cookie-сессий
    let internal = Router::new()
        .route("/internal/v1/health", get(|| async { "ok" }))
        .route("/internal/v1/companies/{cid}/summary", get(http::internal_summary))
        .with_state(state);

    let pub_addr = env("PUBLIC_ADDR", "0.0.0.0:3000");
    let int_addr = env("INTERNAL_ADDR", "127.0.0.1:3100");
    let l1 = tokio::net::TcpListener::bind(&pub_addr).await.expect("bind public");
    let l2 = tokio::net::TcpListener::bind(&int_addr).await.expect("bind internal");
    eprintln!("public api on http://{pub_addr}  internal api on http://{int_addr}");
    tokio::join!(
        async { axum::serve(l1, public).await.unwrap() },
        async { axum::serve(l2, internal).await.unwrap() }
    );
}

/// Тенантские маршруты (`/api/v1/*`, `/api/ext/v1/*`): право для каждого маршрута задаёт его обработчик
/// (экстракторы `Reader`/`Editor`/`Manager`, `relay` по методу), таблица «маршрут -> право» — в docs/roles.md.
pub fn tenant_routes() -> Router<AppState> {
    let co = "/api/v1/companies/{cid}";
    Router::new()
        .route("/api/v1/auth/register", post(http::register))
        .route("/api/v1/auth/login", post(http::login))
        // второй фактор (TOTP): после верного пароля клиент получает билет и идёт на настройку или ввод кода
        .route("/api/v1/auth/mfa/setup", post(http::mfa_setup))
        .route("/api/v1/auth/mfa/enable", post(http::mfa_enable))
        .route("/api/v1/auth/mfa/verify", post(http::mfa_verify))
        .route("/api/v1/auth/ecp/challenge", post(http::ecp_challenge))
        .route("/api/v1/auth/ecp/login", post(http::ecp_login))
        .route("/api/v1/auth/logout", post(http::logout))
        .route("/api/v1/auth/me", get(http::me))
        .route("/api/v1/account/change-password", post(http::change_password))
        // почта: ссылки из писем открываются без входа
        .route("/api/v1/auth/password/forgot", post(mail::forgot))
        .route("/api/v1/auth/password/reset", post(mail::reset))
        .route("/api/v1/account/email/verify", post(mail::verify))
        .route("/api/v1/account/email/resend", post(mail::resend))
        .route("/api/v1/account/notifications", get(mail::get_prefs).put(mail::put_prefs))
        .route("/api/v1/account/notifications/test", post(mail::test))
        .route("/api/v1/notifications/unsubscribe", post(mail::unsubscribe))
        .route("/api/v1/account/set-credentials", post(http::set_credentials))
        .route("/api/v1/members", get(http::members_list).post(http::members_create))
        .route("/api/v1/members/{uid}", patch(http::members_update).delete(http::members_delete))
        .route("/api/v1/members/{uid}/reset-password", post(http::members_reset_password))
        // приглашения по ссылке (одноразовый код, роль, привязка к email), передача владения, выход из команды, журнал действий
        .route("/api/v1/team/invites", get(http::team_invites_list).post(http::team_invite_create))
        .route("/api/v1/team/invites/{id}", delete(http::team_invite_delete))
        .route("/api/v1/team/accept", post(http::team_accept))
        .route("/api/v1/team/leave", post(http::team_leave))
        .route("/api/v1/team/transfer", post(http::team_transfer))
        .route("/api/v1/team/activity", get(http::team_activity))
        .route("/api/v1/team/activity/export", get(http::team_activity_export))
        .route("/api/v1/account/methods", get(http::account_methods))
        .route("/api/v1/account/email", post(http::account_add_email).delete(http::account_remove_email))
        .route("/api/v1/account/signatures", post(http::account_add_signature))
        .route("/api/v1/account/signatures/{id}", delete(http::account_remove_signature))
        .route("/api/v1/account/tokens", get(http::tokens_list).post(http::tokens_create))
        .route("/api/v1/account/tokens/{id}", delete(http::tokens_delete))
        // внешний API: Authorization: Bearer <токен>, компания определяется токеном
        .route("/api/ext/v1/company", get(http::ext_company))
        .route("/api/ext/v1/forecast", get(http::ext_forecast_latest).post(http::ext_forecast_create))
        .route("/api/ext/v1/transactions", get(http::ext_transactions))
        .route("/api/ext/v1/expected-events", get(http::ext_events).post(http::ext_events))
        .route("/api/ext/v1/plan-fact", get(http::ext_plan_fact))
        .route("/api/ext/v1/fns/ens", get(fns::ext_ens))
        .route("/api/ext/v1/fns/counterparties", get(fns::ext_counterparties))
        .route(co, patch(http::update_company))
        .route(&format!("{co}/imports"), get(http::to_ledger).post(http::to_ledger))
        .route(&format!("{co}/imports/{{iid}}"), get(http::to_ledger))
        .route(&format!("{co}/imports/{{iid}}/confirm"), post(http::to_ledger))
        .route(&format!("{co}/transactions"), get(http::to_ledger))
        .route(&format!("{co}/transactions/{{id}}/category"), patch(http::to_ledger))
        .route(&format!("{co}/taxonomy"), get(http::to_ledger))
        .route(&format!("{co}/expected-events"), get(http::to_ledger).post(http::to_ledger))
        .route(&format!("{co}/expected-events/tax-calendar"), post(http::tax_calendar))
        .route(&format!("{co}/expected-events/{{eid}}"), delete(http::to_ledger))
        .route(&format!("{co}/integrations/sync"), post(http::integrations_sync))
        // сведения ФНС: налоговые события на год, ЕНС, проверка контрагентов (fns.rs)
        .route(&format!("{co}/integrations/fns/sync"), post(fns::sync))
        .route(&format!("{co}/fns/ens"), get(fns::ens))
        .route(&format!("{co}/fns/ens/import"), post(fns::ens_import))
        .route(&format!("{co}/fns/counterparties"), get(fns::counterparties))
        .route(&format!("{co}/advisor/ask"), post(advisor::ask))
        .route(&format!("{co}/advisor/suggestions/apply"), post(advisor::apply))
        // демо «Прошла неделя»: только для демо-пользователя при DEMO_FEED_ENABLED=1
        .route(&format!("{co}/demo/status"), get(demo::status))
        .route(&format!("{co}/demo/advance-week"), post(demo::advance_week))
        .route("/api/v1/gov/key-rate", get(http::gov_key_rate))
        .route(&format!("{co}/forecasts"), post(http::create_forecast))
        .route(&format!("{co}/forecasts/latest"), get(http::to_forecast))
        .route(&format!("{co}/forecasts/{{rid}}"), get(http::to_forecast))
        .route(&format!("{co}/backtest"), get(http::to_forecast))
        .route(&format!("{co}/scenarios"), post(http::create_scenario))
        .route(&format!("{co}/plan-fact"), get(http::to_planfact))
        // Telegram-уведомления, цели недели и ретро: сервис notifier (привязка чата, расписание, agile-цикл)
        .route(&format!("{co}/notifications"), get(http::to_notifier))
        .route(&format!("{co}/notifications/link"), post(http::notifier_link))
        .route(&format!("{co}/notifications/{{*tail}}"), get(http::to_notifier).post(http::to_notifier).put(http::to_notifier).patch(http::to_notifier).delete(http::to_notifier))
}

fn set_common(h: &mut axum::http::HeaderMap) {
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    h.insert("x-frame-options", HeaderValue::from_static("DENY"));
    h.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    h.insert(
        "content-security-policy",
        HeaderValue::from_static("default-src 'self'; script-src 'self' chrome-extension:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"),
    );
}

async fn static_headers(req: Request, next: Next) -> Response {
    let mut res = next.run(req).await;
    set_common(res.headers_mut());
    res
}

/// CSRF (проверка Origin для изменяющих запросов), request id, no-store, лог без тел и query.
/// request id кладётся в заголовок запроса: оттуда его берут обработчики и пробрасывают во внутренние сервисы.
async fn security_layer(mut req: Request, next: Next) -> Response {
    let rid = uuid::Uuid::new_v4().to_string();
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    if method != axum::http::Method::GET && method != axum::http::Method::HEAD {
        if let Some(origin) = req.headers().get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
            let host = req.headers().get(header::HOST).and_then(|v| v.to_str().ok()).unwrap_or("");
            let origin_host = origin.split("://").nth(1).unwrap_or("");
            if origin_host != host {
                return common::error_response(StatusCode::FORBIDDEN, "bad_origin", "Запрос с чужого origin отклонён", &rid);
            }
        }
    }
    req.headers_mut().insert("x-request-id", HeaderValue::from_str(&rid).unwrap());
    req.headers_mut().remove("x-actor"); // актора выставляет только сам api
    let mut res = next.run(req).await;
    res.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    res.headers_mut().insert("x-request-id", HeaderValue::from_str(&rid).unwrap());
    eprintln!("{rid} {method} {path} -> {}", res.status().as_u16());
    res
}
