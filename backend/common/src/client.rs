//! Клиент межсервисных вызовов: таймауты, 503 при недоступности, проброс x-request-id и актора.

use crate::error::{ApiError, R, err};
use axum::{
    extract::FromRequestParts,
    http::{Method, StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
};
use bytes::Bytes;
use serde_json::Value;
use std::convert::Infallible;
use std::time::Duration;

/// Контекст запроса: идентификатор запроса и пользователь-актор (для аудита).
#[derive(Clone, Debug, Default)]
pub struct Ctx {
    pub rid: String,
    pub actor: String,
}

impl<S: Send + Sync> FromRequestParts<S> for Ctx {
    type Rejection = Infallible;
    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Infallible> {
        let h = |k: &str| parts.headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        Ok(Ctx { rid: h("x-request-id"), actor: h("x-actor") })
    }
}

pub struct Reply {
    pub status: StatusCode,
    pub content_type: Option<String>,
    pub body: Bytes,
}

impl Reply {
    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or(Value::Null)
    }

    /// 2xx -> JSON тела; иначе ошибка с тем же статусом, кодом и сообщением, что вернул сервис.
    pub fn into_result(self) -> R<Value> {
        if self.status.is_success() {
            return Ok(self.json());
        }
        let v = self.json();
        match (v["error"]["code"].as_str(), v["error"]["message"].as_str()) {
            (Some(c), Some(m)) => Err(err(self.status, c, m)),
            _ => Err(err(StatusCode::BAD_GATEWAY, "upstream_error", "Ошибка внутреннего сервиса")),
        }
    }

    /// Ответ «как есть»: статус и Content-Type сохраняются, тело передаётся без изменений.
    pub fn into_response(self) -> Response {
        let mut r = (self.status, self.body).into_response();
        r.headers_mut().remove(header::CONTENT_TYPE);
        if let Some(ct) = self.content_type.and_then(|c| header::HeaderValue::from_str(&c).ok()) {
            r.headers_mut().insert(header::CONTENT_TYPE, ct);
        }
        r
    }
}

#[derive(Clone)]
pub struct Upstream {
    name: &'static str,
    label: &'static str,
    base: String,
    timeout: Duration,
    client: reqwest::Client,
}

impl Upstream {
    /// name — префикс кода ошибки (`<name>_unavailable`), label — как сервис назван в сообщении.
    pub fn new(name: &'static str, label: &'static str, base: &str, timeout_secs: u64) -> Self {
        let client = reqwest::Client::builder().connect_timeout(Duration::from_secs(3)).build().expect("http client");
        Upstream { name, label, base: base.trim_end_matches('/').to_string(), timeout: Duration::from_secs(timeout_secs), client }
    }

    fn unavailable(&self) -> ApiError {
        err(StatusCode::SERVICE_UNAVAILABLE, format!("{}_unavailable", self.name), format!("{} недоступен", self.label))
    }

    /// `path` — путь с query, начиная с '/'. Сетевая ошибка или таймаут -> 503.
    pub async fn send(&self, method: Method, path: &str, ctx: &Ctx, content_type: Option<&str>, body: Option<Bytes>) -> R<Reply> {
        let mut rb = self.client.request(method, format!("{}{path}", self.base)).timeout(self.timeout);
        if !ctx.rid.is_empty() {
            rb = rb.header("x-request-id", &ctx.rid);
        }
        if !ctx.actor.is_empty() {
            rb = rb.header("x-actor", &ctx.actor);
        }
        if let Some(ct) = content_type {
            rb = rb.header(header::CONTENT_TYPE, ct);
        }
        if let Some(b) = body {
            rb = rb.body(b);
        }
        let r = rb.send().await.map_err(|e| {
            eprintln!("{} {} unreachable: {}", ctx.rid, self.name, e.without_url());
            self.unavailable()
        })?;
        let status = StatusCode::from_u16(r.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
        let content_type = r.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).map(str::to_string);
        let body = r.bytes().await.map_err(|_| self.unavailable())?;
        Ok(Reply { status, content_type, body })
    }

    pub async fn json(&self, method: Method, path: &str, ctx: &Ctx, body: Option<&Value>) -> R<Reply> {
        let b = body.map(|v| Bytes::from(serde_json::to_vec(v).unwrap()));
        self.send(method, path, ctx, body.map(|_| "application/json"), b).await
    }

    /// Передать файл как multipart-поле `file`.
    pub async fn upload(&self, path: &str, ctx: &Ctx, filename: String, data: Vec<u8>) -> R<Reply> {
        let part = reqwest::multipart::Part::bytes(data).file_name(filename);
        let mut rb = self.client.post(format!("{}{path}", self.base)).timeout(self.timeout).multipart(reqwest::multipart::Form::new().part("file", part));
        if !ctx.rid.is_empty() {
            rb = rb.header("x-request-id", &ctx.rid);
        }
        let r = rb.send().await.map_err(|_| self.unavailable())?;
        let status = StatusCode::from_u16(r.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
        let body = r.bytes().await.map_err(|_| self.unavailable())?;
        Ok(Reply { status, content_type: None, body })
    }

    pub async fn get(&self, path: &str, ctx: &Ctx) -> R<Value> {
        self.json(Method::GET, path, ctx, None).await?.into_result()
    }

    pub async fn post(&self, path: &str, ctx: &Ctx, body: &Value) -> R<Value> {
        self.json(Method::POST, path, ctx, Some(body)).await?.into_result()
    }
}
