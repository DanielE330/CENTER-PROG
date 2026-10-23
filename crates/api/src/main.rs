mod db;
mod forecast;
mod http;

use axum::{
    Router,
    extract::Request,
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::Response,
    routing::{delete, get, patch, post},
};
use std::sync::Arc;
use tower_http::services::{ServeDir, ServeFile};

#[derive(Clone)]
pub struct Config {
    pub auth_url: String,
    pub parser_url: String,
    pub cookie_secure: bool,
}

#[derive(Clone)]
pub struct AppState {
    pub db: db::Db,
    pub http: reqwest::Client,
    pub cfg: Arc<Config>,
}

fn env(k: &str, d: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| d.to_string())
}

#[tokio::main]
async fn main() {
    let cfg = Config {
        auth_url: env("AUTH_URL", "http://127.0.0.1:8001"),
        parser_url: env("PARSER_URL", "http://127.0.0.1:8002"),
        cookie_secure: env("COOKIE_SECURE", "0") == "1",
    };
    let state = AppState {
        db: db::open(&env("DB_PATH", "data/core.db")),
        http: reqwest::Client::builder().timeout(std::time::Duration::from_secs(60)).build().unwrap(),
        cfg: Arc::new(cfg),
    };

    let co = "/api/v1/companies/{cid}";
    let api = Router::new()
        .route("/api/v1/auth/register", post(http::register))
        .route("/api/v1/auth/login", post(http::login))
        .route("/api/v1/auth/logout", post(http::logout))
        .route("/api/v1/auth/me", get(http::me))
        .route(&format!("{co}/imports"), post(http::upload_import).layer(axum::extract::DefaultBodyLimit::max(26 * 1024 * 1024)))
        .route(&format!("{co}/imports/{{iid}}"), get(http::get_import))
        .route(&format!("{co}/imports/{{iid}}/confirm"), post(http::confirm_import))
        .route(&format!("{co}/transactions"), get(http::list_transactions))
        .route(&format!("{co}/transactions/{{id}}/category"), patch(http::set_category))
        .route(&format!("{co}/expected-events"), get(http::list_events).post(http::create_event))
        .route(&format!("{co}/expected-events/tax-calendar"), post(http::tax_calendar))
        .route(&format!("{co}/expected-events/{{eid}}"), delete(http::delete_event))
        .route(&format!("{co}/forecasts"), post(http::create_forecast))
        .route(&format!("{co}/forecasts/latest"), get(http::latest_forecast))
        .route(&format!("{co}/forecasts/{{rid}}"), get(http::get_forecast))
        .route(&format!("{co}/backtest"), get(http::run_backtest))
        .route(&format!("{co}/scenarios"), post(http::create_scenario))
        .route(&format!("{co}/plan-fact"), get(http::plan_fact))
        .layer(middleware::from_fn(security_layer))
        .with_state(state.clone());

    let web_dir = env("WEB_DIR", "web/dist");
    let spa = ServeDir::new(&web_dir).fallback(ServeFile::new(format!("{web_dir}/index.html")));
    let public = Router::new()
        .route("/live", get(|| async { "ok" }))
        .merge(api)
        .fallback_service(spa)
        .layer(middleware::from_fn(static_headers));

    // Внутренний API: только loopback / служебная сеть, без cookie-сессий
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

fn set_common(h: &mut axum::http::HeaderMap) {
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    h.insert("x-frame-options", HeaderValue::from_static("DENY"));
    h.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    h.insert(
        "content-security-policy",
        HeaderValue::from_static("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"),
    );
}

async fn static_headers(req: Request, next: Next) -> Response {
    let mut res = next.run(req).await;
    set_common(res.headers_mut());
    res
}

/// CSRF (проверка Origin для изменяющих запросов), request id, no-store, лог без тел и query.
async fn security_layer(req: Request, next: Next) -> Response {
    let rid = uuid::Uuid::new_v4().to_string();
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    if method != axum::http::Method::GET && method != axum::http::Method::HEAD {
        if let Some(origin) = req.headers().get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
            let host = req.headers().get(header::HOST).and_then(|v| v.to_str().ok()).unwrap_or("");
            let origin_host = origin.split("://").nth(1).unwrap_or("");
            if origin_host != host {
                return http::error_response(StatusCode::FORBIDDEN, "bad_origin", "Запрос с чужого origin отклонён", &rid);
            }
        }
    }
    let mut res = next.run(req).await;
    res.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    res.headers_mut().insert("x-request-id", HeaderValue::from_str(&rid).unwrap());
    eprintln!("{rid} {method} {path} -> {}", res.status().as_u16());
    res
}
