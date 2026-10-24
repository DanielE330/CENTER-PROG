//! llm-gateway: the single exit to language models. Chooses the provider by
//! tenant policy, masks data for external providers, enforces token budgets
//! and writes an audit trail without message contents.

mod mask;
mod providers;

use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Result;
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use ci_core::{
    ApiError,
    auth::ServiceTokenSource,
    client::MtlsClient,
    config::{CoreConfig, secret},
    db,
    error::ApiResult,
    server, telemetry,
};
use serde_json::{Value, json};
use sqlx::PgPool;
use tokio::sync::RwLock;
use uuid::Uuid;

use crate::{
    mask::Masker,
    providers::{Clients, Provider},
};

#[derive(Clone)]
struct AppState {
    inner: Arc<Inner>,
}

struct Inner {
    pool: PgPool,
    providers: HashMap<&'static str, Provider>,
    clients: Clients,
    /// mTLS client for internal calls (tenancy settings).
    internal: MtlsClient,
    tenancy_url: String,
    tokens: Arc<ServiceTokenSource>,
    policies: RwLock<HashMap<Uuid, (Instant, Value)>>,
    daily_budget: i64,
}

fn optional_secret(name: &str) -> Option<String> {
    secret(name).ok().filter(|s| !s.is_empty())
}

fn build_providers() -> HashMap<&'static str, Provider> {
    let mut map = HashMap::new();
    let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.to_owned());
    if let Some(key) = optional_secret("openai_api_key") {
        map.insert(
            "openai",
            Provider::OpenAiCompatible {
                name: "openai",
                base_url: "https://api.openai.com/v1".into(),
                api_key: Some(key),
                model: env("OPENAI_MODEL", "gpt-5-mini"),
            },
        );
    }
    if let Some(key) = optional_secret("openrouter_api_key") {
        map.insert(
            "openrouter",
            Provider::OpenAiCompatible {
                name: "openrouter",
                base_url: "https://openrouter.ai/api/v1".into(),
                api_key: Some(key),
                model: env("OPENROUTER_MODEL", "anthropic/claude-sonnet-5.5"),
            },
        );
    }
    if let Some(key) = optional_secret("gemini_api_key") {
        map.insert(
            "gemini",
            Provider::OpenAiCompatible {
                name: "gemini",
                base_url: "https://generativelanguage.googleapis.com/v1beta/openai".into(),
                api_key: Some(key),
                model: env("GEMINI_MODEL", "gemini-2.5-flash"),
            },
        );
    }
    if let Some(key) = optional_secret("anthropic_api_key") {
        map.insert(
            "anthropic",
            Provider::Anthropic {
                api_key: key,
                model: env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            },
        );
    }
    map.insert("template", Provider::Template);
    map
}

#[tokio::main]
async fn main() -> Result<()> {
    let config = CoreConfig::from_env()?;
    telemetry::init(&config.service);
    if server::healthcheck_requested() {
        return server::run_healthcheck(&config).await;
    }
    let pool = db::connect_with_retry(
        CoreConfig::require(&config.database_url, "DATABASE_URL")?,
        &config.cert_dir,
    )
    .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    let internal = MtlsClient::new(ci_core::tls::CertPaths::in_dir(&config.cert_dir))?;
    let kc = std::env::var("KEYCLOAK_INTERNAL_URL")?;
    let providers = build_providers();
    tracing::info!(providers = ?providers.keys().collect::<Vec<_>>(), "providers configured");
    let inner = Inner {
        pool,
        providers,
        clients: Clients {
            external: reqwest::Client::builder()
                .use_rustls_tls()
                .timeout(Duration::from_secs(120))
                .build()?,
        },
        internal: internal.clone(),
        tenancy_url: std::env::var("TENANCY_URL").unwrap_or_else(|_| "https://tenancy:8443".into()),
        tokens: ServiceTokenSource::new(
            &format!("{kc}/realms/ci1/protocol/openid-connect/token"),
            "llm-gateway",
            internal,
        ),
        policies: RwLock::new(HashMap::new()),
        daily_budget: std::env::var("LLM_DAILY_TOKEN_BUDGET")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(300_000),
    };
    let app = Router::new()
        .route("/v1/chat/completions", post(chat))
        .route("/v1/route", get(route))
        .route(
            "/v1/models",
            get(|| async { Json(json!({ "object": "list", "data": [{ "id": "ci1", "object": "model" }] })) }),
        )
        .with_state(AppState { inner: Arc::new(inner) });
    server::serve(&config, server::base_router(app, 2 * 1024 * 1024)).await
}

async fn policy(s: &Inner, tenant: Uuid) -> ApiResult<Value> {
    if let Some((at, p)) = s.policies.read().await.get(&tenant)
        && at.elapsed() < Duration::from_secs(60)
    {
        return Ok(p.clone());
    }
    let token = s.tokens.token().await.map_err(ApiError::Upstream)?;
    let settings: Value = s
        .internal
        .get()
        .get(format!("{}/internal/v1/tenants/{tenant}/settings", s.tenancy_url))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?
        .error_for_status()
        .map_err(|e| ApiError::Upstream(e.into()))?
        .json()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?;
    let p = settings["llm_policy"].clone();
    s.policies.write().await.insert(tenant, (Instant::now(), p.clone()));
    Ok(p)
}

/// Provider order for a request: the tenant's cloud provider (if the owner allowed sending
/// masked data and the key is configured), then the template.
fn plan<'a>(s: &'a Inner, policy: &Value) -> Vec<&'a Provider> {
    let wanted = policy["provider"].as_str().unwrap_or("template");
    let external_allowed = policy["external_allowed"].as_bool().unwrap_or(false);
    let mut order = Vec::new();
    if let Some(p) = s.providers.get(wanted)
        && (!p.is_external() || external_allowed)
    {
        order.push(p);
    }
    if let Some(p) = s.providers.get("template")
        && !order.iter().any(|o| o.name() == p.name())
    {
        order.push(p);
    }
    order
}

/// Which provider a request of this tenant would use first. The advisor picks its agent
/// runtime by it: a cloud model gets Hermes, without one the advisor answers by template.
async fn route(State(state): State<AppState>, headers: HeaderMap) -> ApiResult<Json<Value>> {
    let s = &state.inner;
    let tenant = headers
        .get("x-tenant-id")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| Uuid::parse_str(v).ok())
        .ok_or_else(|| ApiError::BadRequest("x-tenant-id is required".into()))?;
    let policy = policy(s, tenant).await?;
    let first = plan(s, &policy).into_iter().next();
    Ok(Json(json!({
        "provider": first.map(|p| p.name()),
        "external": first.is_some_and(|p| p.is_external()),
    })))
}

/// Reasoning models sometimes leak `<think>…</think>` (or a lone closing tag) into the answer.
fn strip_reasoning(text: &str) -> String {
    let mut out = text.to_owned();
    while let (Some(a), Some(b)) = (out.find("<think>"), out.find("</think>")) {
        if b < a {
            break;
        }
        out.replace_range(a..b + "</think>".len(), "");
    }
    if let Some(b) = out.find("</think>") {
        out = out[b + "</think>".len()..].to_owned();
    }
    out.trim().to_owned()
}

/// Masks prose as text and tool results as JSON: only string values are masked there,
/// so amounts in kopecks (10+ digits) are not mistaken for INN or account numbers.
fn mask_request(request: &Value, masker: &mut Masker) -> Value {
    let mut out = request.clone();
    if let Some(messages) = out["messages"].as_array_mut() {
        for m in messages {
            let Some(text) = m["content"].as_str() else { continue };
            let masked = match serde_json::from_str::<Value>(text) {
                Ok(mut json) if m["role"] == "tool" && (json.is_object() || json.is_array()) => {
                    mask_strings(&mut json, masker);
                    json.to_string()
                }
                _ => masker.mask(text),
            };
            m["content"] = json!(masked);
        }
    }
    out
}

fn mask_strings(value: &mut Value, masker: &mut Masker) {
    match value {
        Value::String(s) => *s = masker.mask(s),
        Value::Array(items) => items.iter_mut().for_each(|v| mask_strings(v, masker)),
        Value::Object(map) => map.values_mut().for_each(|v| mask_strings(v, masker)),
        _ => {}
    }
}

async fn used_today(pool: &PgPool, tenant: Uuid) -> Result<i64, sqlx::Error> {
    let (n,): (Option<i64>,) = sqlx::query_as(
        "select sum(total_tokens)::bigint from audit where tenant_id = $1 and at >= date_trunc('day', now())",
    )
    .bind(tenant)
    .fetch_one(pool)
    .await?;
    Ok(n.unwrap_or(0))
}

async fn chat(State(state): State<AppState>, headers: HeaderMap, Json(request): Json<Value>) -> ApiResult<Response> {
    let s = &state.inner;
    let tenant = headers
        .get("x-tenant-id")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| Uuid::parse_str(v).ok())
        .ok_or_else(|| ApiError::BadRequest("x-tenant-id is required".into()))?;
    if used_today(&s.pool, tenant).await? >= s.daily_budget {
        return Ok((
            StatusCode::TOO_MANY_REQUESTS,
            Json(json!({ "error": "daily token budget exhausted" })),
        )
            .into_response());
    }
    let policy = policy(s, tenant).await?;
    let wants_stream = request["stream"].as_bool().unwrap_or(false);
    let mut last_err = None;
    for provider in plan(s, &policy) {
        let mut masker = Masker::default();
        // Every request is masked: all models are external.
        let body = mask_request(&request, &mut masker);
        match provider.complete(&s.clients, &body).await {
            Ok(mut resp) => {
                if let Some(text) = resp["choices"][0]["message"]["content"].as_str() {
                    resp["choices"][0]["message"]["content"] = json!(strip_reasoning(&masker.unmask(text)));
                }
                let total = resp["usage"]["total_tokens"].as_i64().unwrap_or(0);
                sqlx::query(
                    "insert into audit (id, tenant_id, provider, total_tokens, masked_entities, external) values ($1, $2, $3, $4, $5, $6)",
                )
                .bind(Uuid::now_v7())
                .bind(tenant)
                .bind(provider.name())
                .bind(total)
                .bind(masker.replaced as i32)
                .bind(provider.is_external())
                .execute(&s.pool)
                .await?;
                return Ok(if wants_stream {
                    as_sse(&resp)
                } else {
                    Json(resp).into_response()
                });
            }
            Err(err) => {
                tracing::warn!(provider = provider.name(), error = %err, "provider failed, trying next");
                last_err = Some(err);
            }
        }
    }
    Err(ApiError::Upstream(
        last_err.unwrap_or_else(|| anyhow::anyhow!("no provider available")),
    ))
}

/// Clients that asked for streaming get the full answer as one SSE chunk:
/// unmasking needs the whole text, so we never stream raw provider output.
fn as_sse(resp: &Value) -> Response {
    let message = &resp["choices"][0]["message"];
    let mut delta = json!({ "role": "assistant", "content": message["content"] });
    if let Some(calls) = message["tool_calls"].as_array() {
        let indexed: Vec<Value> = calls
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let mut c = c.clone();
                c["index"] = json!(i);
                c
            })
            .collect();
        delta["tool_calls"] = json!(indexed);
    }
    let chunk = json!({
        "id": resp["id"], "object": "chat.completion.chunk", "created": resp["created"], "model": resp["model"],
        "choices": [{ "index": 0, "delta": delta, "finish_reason": resp["choices"][0]["finish_reason"] }]
    });
    let usage = json!({ "id": resp["id"], "object": "chat.completion.chunk", "created": resp["created"],
        "model": resp["model"], "choices": [], "usage": resp["usage"] });
    let body = format!("data: {chunk}\n\ndata: {usage}\n\ndata: [DONE]\n\n");
    ([(axum::http::header::CONTENT_TYPE, "text/event-stream")], body).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reasoning_tags_are_removed() {
        assert_eq!(strip_reasoning("<think>долго</think>\n\nОтвет"), "Ответ");
        assert_eq!(strip_reasoning("</think>\n\nОтвет"), "Ответ");
        assert_eq!(strip_reasoning("Просто ответ"), "Просто ответ");
    }

    #[test]
    fn amounts_in_tool_json_survive_masking() {
        let req = json!({ "messages": [
            {"role": "tool", "content": r#"{"gaps":[{"shortfall_p50_kop":465443600,"label":"ООО «Ромашка» ИНН 0048401219"}]}"#}
        ]});
        let mut m = Masker::default();
        let out = mask_request(&req, &mut m);
        let parsed: Value = serde_json::from_str(out["messages"][0]["content"].as_str().unwrap()).unwrap();
        assert_eq!(parsed["gaps"][0]["shortfall_p50_kop"], 465_443_600);
        let label = parsed["gaps"][0]["label"].as_str().unwrap();
        assert!(!label.contains("Ромашка") && !label.contains("0048401219"));
    }
}
