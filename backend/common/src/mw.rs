//! Общее для внутренних сервисов: чтение env, лог запросов с x-request-id, запуск сервера.

use axum::{
    Router,
    extract::Request,
    http::HeaderValue,
    middleware::{self, Next},
    response::Response,
    routing::get,
};

pub fn env(k: &str, d: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| d.to_string())
}

/// Берёт x-request-id из запроса (или создаёт), логирует без тел и query, возвращает заголовок в ответе.
pub async fn request_log(mut req: Request, next: Next) -> Response {
    let rid = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 64)
        .map(str::to_string)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    if let Ok(v) = HeaderValue::from_str(&rid) {
        req.headers_mut().insert("x-request-id", v);
    }
    let (method, path) = (req.method().clone(), req.uri().path().to_string());
    let mut res = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&rid) {
        res.headers_mut().insert("x-request-id", v);
    }
    eprintln!("{rid} {method} {path} -> {}", res.status().as_u16());
    res
}

/// Добавляет /live, /internal/v1/health и лог запросов; слушает только адрес из env.
pub async fn serve(name: &str, addr: &str, router: Router) {
    let app = router
        .route("/live", get(|| async { "ok" }))
        .route("/internal/v1/health", get(|| async { "ok" }))
        .layer(middleware::from_fn(request_log));
    let l = tokio::net::TcpListener::bind(addr).await.expect("bind");
    eprintln!("{name} listening on http://{addr}");
    axum::serve(l, app).await.unwrap();
}
