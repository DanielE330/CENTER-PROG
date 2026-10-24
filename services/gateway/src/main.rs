//! gateway: Backend-for-Frontend. Logs users in through Keycloak (code + PKCE),
//! keeps tokens server-side, proxies `/api/v1/*` to services over mTLS and
//! streams tenant events to the browser over SSE.

mod session;

use std::{convert::Infallible, sync::Arc, time::Duration};

use anyhow::{Context, Result};
use axum::{
    Extension, Json, Router,
    body::Body,
    extract::{Query, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::{
        IntoResponse, Redirect, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{any, get, post},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ci_core::{
    ApiError,
    auth::JwtVerifier,
    client::MtlsClient,
    config::{CoreConfig, secret},
    error::ApiResult,
    events, server, telemetry,
};
use futures::StreamExt;
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::session::{LoginState, Session, random_token};

const COOKIE: &str = "__Host-ci1_sid";
const CSRF_HEADER: &str = "x-ci1-csrf";

#[derive(Clone)]
struct AppState {
    inner: Arc<Inner>,
}

struct Inner {
    public_url: String,
    auth_url: String,
    token_url: String,
    logout_url: String,
    client_id: String,
    client_secret: String,
    client: MtlsClient,
    store: session::Store,
    verifier: Arc<JwtVerifier>,
    id_verifier: Arc<JwtVerifier>,
    nats: async_nats::Client,
    routes: Vec<(&'static str, &'static str)>,
}

/// Longest prefix wins. The table is the only place that knows service names.
const ROUTES: &[(&str, &str)] = &[
    ("/api/v1/transactions/category", "categorizer"),
    ("/api/v1/categories", "categorizer"),
    ("/api/v1/imports", "ingest"),
    ("/api/v1/transactions", "ingest"),
    ("/api/v1/obligations", "obligations"),
    ("/api/v1/forecast", "forecast"),
    ("/api/v1/gaps", "forecast"),
    ("/api/v1/scenarios", "forecast"),
    ("/api/v1/plan-fact", "forecast"),
    ("/api/v1/alerts", "alerts"),
    ("/api/v1/settings", "tenancy"),
    ("/api/v1/flags", "tenancy"),
    ("/api/v1/advisor", "advisor"),
    ("/api/v1/proposals", "advisor-tools"),
    ("/api/v1/demo", "connectors"),
    ("/api/v1/counterparties", "connectors"),
];

#[tokio::main]
async fn main() -> Result<()> {
    let config = CoreConfig::from_env()?;
    telemetry::init(&config.service);
    if server::healthcheck_requested() {
        return server::run_healthcheck(&config).await;
    }
    let public_url = std::env::var("PUBLIC_URL")
        .context("PUBLIC_URL")?
        .trim_end_matches('/')
        .to_owned();
    let kc_internal = std::env::var("KEYCLOAK_INTERNAL_URL").context("KEYCLOAK_INTERNAL_URL")?;
    let issuer = CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?.to_owned();
    let jwks = CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?.to_owned();
    // Long enough for an agent answer from the local model; other services answer in milliseconds.
    let client = MtlsClient::with_timeout(
        ci_core::tls::CertPaths::in_dir(&config.cert_dir),
        std::time::Duration::from_secs(240),
    )?;
    let nats_url = CoreConfig::require(&config.nats_url, "NATS_URL")?;
    let nats = events::connect_client(nats_url, &config.cert_dir).await?;
    let js = async_nats::jetstream::new(nats.clone());
    let store = session::Store::new(&js, &secret("session_key")?).await?;

    let inner = Inner {
        auth_url: format!("{issuer}/protocol/openid-connect/auth"),
        logout_url: format!("{issuer}/protocol/openid-connect/logout"),
        token_url: format!("{kc_internal}/realms/ci1/protocol/openid-connect/token"),
        client_id: "gateway".into(),
        client_secret: secret("gateway_client_secret")?,
        verifier: JwtVerifier::new(&issuer, &config.oidc_audience, &jwks, client.clone()),
        id_verifier: JwtVerifier::new(&issuer, "gateway", &jwks, client.clone()),
        client,
        store,
        nats,
        public_url,
        routes: ROUTES.to_vec(),
    };
    let state = AppState { inner: Arc::new(inner) };
    let app = Router::new()
        .route("/api/v1/auth/login", get(login))
        .route("/api/v1/auth/callback", get(callback))
        .route("/api/v1/auth/logout", post(logout))
        .route("/api/v1/me", get(me))
        .route("/api/v1/stream", get(stream))
        .route("/api/v1/{*rest}", any(proxy))
        .layer(Extension(Arc::clone(&state.inner.verifier)))
        .with_state(state);
    server::serve(&config, server::base_router(app, 25 * 1024 * 1024)).await
}

fn session_id(headers: &HeaderMap) -> Option<String> {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .filter_map(|c| c.trim().split_once('='))
        .find(|(k, _)| *k == COOKIE)
        .map(|(_, v)| v.to_owned())
}

fn cookie(value: &str, max_age: i64) -> HeaderValue {
    HeaderValue::from_str(&format!(
        "{COOKIE}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={max_age}"
    ))
    .unwrap_or_else(|_| HeaderValue::from_static(""))
}

#[derive(Deserialize)]
struct LoginQuery {
    return_to: Option<String>,
}

async fn login(State(state): State<AppState>, Query(q): Query<LoginQuery>) -> ApiResult<Response> {
    let s = &state.inner;
    let verifier = random_token(32);
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let nonce = random_token(16);
    let st = random_token(24);
    // Only local paths: no open redirect.
    let return_to = q
        .return_to
        .filter(|r| r.starts_with('/') && !r.starts_with("//"))
        .unwrap_or_else(|| "/".into());
    s.store
        .put_login(
            &st,
            &LoginState {
                verifier,
                nonce: nonce.clone(),
                return_to,
            },
        )
        .await?;
    let mut url = url::Url::parse(&s.auth_url).map_err(|e| ApiError::Internal(e.into()))?;
    url.query_pairs_mut()
        .append_pair("client_id", &s.client_id)
        .append_pair("response_type", "code")
        .append_pair("scope", "openid organization")
        .append_pair("redirect_uri", &format!("{}/api/v1/auth/callback", s.public_url))
        .append_pair("state", &st)
        .append_pair("nonce", &nonce)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256");
    Ok(Redirect::to(url.as_str()).into_response())
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: String,
    id_token: Option<String>,
    expires_in: i64,
}

#[derive(Deserialize)]
struct CallbackQuery {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
}

#[derive(Deserialize)]
struct IdClaims {
    nonce: Option<String>,
}

async fn callback(State(state): State<AppState>, Query(q): Query<CallbackQuery>) -> ApiResult<Response> {
    let s = &state.inner;
    if let Some(err) = q.error {
        tracing::info!(error = %err, "login rejected by identity provider");
        return Ok(Redirect::to("/?login_error=1").into_response());
    }
    let (code, st) = q.code.zip(q.state).ok_or(ApiError::BadRequest("missing code".into()))?;
    let login = s.store.take_login(&st).await?.ok_or(ApiError::Unauthorized)?;
    let redirect_uri = format!("{}/api/v1/auth/callback", s.public_url);
    let tokens: TokenResponse = s
        .client
        .get()
        .post(&s.token_url)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", code.as_str()),
            ("redirect_uri", redirect_uri.as_str()),
            ("client_id", s.client_id.as_str()),
            ("client_secret", s.client_secret.as_str()),
            ("code_verifier", login.verifier.as_str()),
        ])
        .send()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?
        .error_for_status()
        .map_err(|e| ApiError::Upstream(e.into()))?
        .json()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?;
    let id_token = tokens.id_token.ok_or(ApiError::Unauthorized)?;
    let id_claims = id_nonce(&s.id_verifier, &id_token).await?;
    if id_claims.nonce.as_deref() != Some(login.nonce.as_str()) {
        return Err(ApiError::Unauthorized);
    }
    s.verifier
        .verify(&tokens.access_token)
        .await
        .map_err(|_| ApiError::Unauthorized)?;
    let sid = random_token(32);
    s.store
        .put_session(
            &sid,
            &Session {
                access_token: tokens.access_token,
                refresh_token: tokens.refresh_token,
                id_token,
                access_exp: chrono::Utc::now().timestamp() + tokens.expires_in,
            },
        )
        .await?;
    let mut resp = Redirect::to(&login.return_to).into_response();
    resp.headers_mut()
        .insert(header::SET_COOKIE, cookie(&sid, session::SESSION_TTL.as_secs() as i64));
    Ok(resp)
}

async fn id_nonce(verifier: &JwtVerifier, token: &str) -> ApiResult<IdClaims> {
    // Signature, issuer and audience are checked by the verifier; the nonce is read separately.
    verifier.verify(token).await.map_err(|_| ApiError::Unauthorized)?;
    let payload = token.split('.').nth(1).ok_or(ApiError::Unauthorized)?;
    let bytes = URL_SAFE_NO_PAD.decode(payload).map_err(|_| ApiError::Unauthorized)?;
    serde_json::from_slice(&bytes).map_err(|_| ApiError::Unauthorized)
}

/// Returns a valid access token for the session, refreshing it when it is about to expire.
async fn access_token(s: &Inner, headers: &HeaderMap) -> ApiResult<String> {
    let sid = session_id(headers).ok_or(ApiError::Unauthorized)?;
    let mut session = s.store.get_session(&sid).await?.ok_or(ApiError::Unauthorized)?;
    if session.access_exp - chrono::Utc::now().timestamp() > 30 {
        return Ok(session.access_token);
    }
    let resp = s
        .client
        .get()
        .post(&s.token_url)
        .form(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", session.refresh_token.as_str()),
            ("client_id", s.client_id.as_str()),
            ("client_secret", s.client_secret.as_str()),
        ])
        .send()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?;
    if !resp.status().is_success() {
        // Revoked or reused refresh token: the session is over.
        s.store.delete_session(&sid).await?;
        return Err(ApiError::Unauthorized);
    }
    let tokens: TokenResponse = resp.json().await.map_err(|e| ApiError::Upstream(e.into()))?;
    session.access_token = tokens.access_token;
    session.refresh_token = tokens.refresh_token;
    session.access_exp = chrono::Utc::now().timestamp() + tokens.expires_in;
    s.store.put_session(&sid, &session).await?;
    Ok(session.access_token)
}

async fn logout(State(state): State<AppState>, headers: HeaderMap) -> ApiResult<Response> {
    let s = &state.inner;
    require_csrf(&headers)?;
    let mut target = format!("{}/", s.public_url);
    if let Some(sid) = session_id(&headers) {
        if let Some(session) = s.store.get_session(&sid).await? {
            target = format!(
                "{}?id_token_hint={}&post_logout_redirect_uri={}",
                s.logout_url,
                session.id_token,
                url::form_urlencoded::byte_serialize(format!("{}/", s.public_url).as_bytes()).collect::<String>()
            );
        }
        s.store.delete_session(&sid).await?;
    }
    let mut resp = Json(json!({ "logout_url": target })).into_response();
    resp.headers_mut().insert(header::SET_COOKIE, cookie("", 0));
    Ok(resp)
}

async fn me(State(state): State<AppState>, headers: HeaderMap) -> ApiResult<Json<serde_json::Value>> {
    let s = &state.inner;
    let token = access_token(s, &headers).await?;
    let claims = s.verifier.verify(&token).await.map_err(|_| ApiError::Unauthorized)?;
    let payload: serde_json::Value = token
        .split('.')
        .nth(1)
        .and_then(|p| URL_SAFE_NO_PAD.decode(p).ok())
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default();
    Ok(Json(json!({
        "sub": claims.sub,
        "email": payload.get("email"),
        "name": payload.get("name"),
        "tenant_id": claims.tenant_id(),
        "organization": organization_alias(payload.get("organization")),
        "roles": claims.roles(),
    })))
}

/// First organization alias from the claim (object or Keycloak's merged list form).
fn organization_alias(claim: Option<&serde_json::Value>) -> Option<String> {
    match claim? {
        serde_json::Value::Array(items) => items.iter().find_map(|i| match i {
            serde_json::Value::String(s) => Some(s.clone()),
            serde_json::Value::Object(o) => o.keys().next().cloned(),
            _ => None,
        }),
        serde_json::Value::Object(o) => o.keys().next().cloned(),
        _ => None,
    }
}

fn require_csrf(headers: &HeaderMap) -> ApiResult<()> {
    // A custom header cannot be sent cross-site without a CORS preflight, which we never allow.
    if headers.get(CSRF_HEADER).is_some() {
        Ok(())
    } else {
        Err(ApiError::Forbidden)
    }
}

fn upstream_for<'a>(routes: &'a [(&'static str, &'static str)], path: &str) -> Option<&'a str> {
    routes
        .iter()
        .filter(|(prefix, _)| {
            path == *prefix || path.starts_with(&format!("{prefix}/")) || path.starts_with(&format!("{prefix}?"))
        })
        .max_by_key(|(prefix, _)| prefix.len())
        .map(|(_, svc)| *svc)
}

async fn proxy(State(state): State<AppState>, req: Request) -> ApiResult<Response> {
    let s = &state.inner;
    let (parts, body) = req.into_parts();
    if parts.method != Method::GET && parts.method != Method::HEAD {
        require_csrf(&parts.headers)?;
    }
    let path = parts.uri.path();
    let svc = upstream_for(&s.routes, path).ok_or(ApiError::NotFound)?;
    let token = access_token(s, &parts.headers).await?;
    let path_and_query = parts.uri.path_and_query().map(|p| p.as_str()).unwrap_or(path);
    let url = format!("https://{svc}:8443{path_and_query}");
    let mut upstream = s.client.get().request(parts.method.clone(), url).bearer_auth(token);
    for name in [header::CONTENT_TYPE, header::ACCEPT] {
        if let Some(v) = parts.headers.get(&name) {
            upstream = upstream.header(name, v);
        }
    }
    let body = reqwest::Body::wrap_stream(body.into_data_stream());
    let resp = upstream
        .body(body)
        .send()
        .await
        .map_err(|e| ApiError::Upstream(e.into()))?;
    let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut out = Response::builder().status(status);
    if let Some(ct) = resp.headers().get(reqwest::header::CONTENT_TYPE) {
        out = out.header(header::CONTENT_TYPE, ct.as_bytes());
    }
    out = out.header(header::CACHE_CONTROL, "no-store");
    out.body(Body::from_stream(resp.bytes_stream()))
        .map_err(|e| ApiError::Internal(e.into()))
}

/// Server-sent events for this tenant: forecast updates, new alerts, agent proposals.
async fn stream(State(state): State<AppState>, headers: HeaderMap) -> ApiResult<impl IntoResponse> {
    let s = &state.inner;
    let token = access_token(s, &headers).await?;
    let claims = s.verifier.verify(&token).await.map_err(|_| ApiError::Unauthorized)?;
    let tenant = claims.tenant_id().ok_or(ApiError::Forbidden)?.to_string();
    let mut subs = Vec::new();
    for subject in [
        "ci.v1.forecast.updated",
        "ci.v1.alerts.raised",
        "ci.v1.advisor.proposal_created",
    ] {
        subs.push(
            s.nats
                .subscribe(subject)
                .await
                .map_err(|e| ApiError::Upstream(e.into()))?,
        );
    }
    let merged = futures::stream::select_all(subs).filter_map(move |msg| {
        let tenant = tenant.clone();
        async move {
            let value: serde_json::Value = serde_json::from_slice(&msg.payload).ok()?;
            if value.get("tenant_id").and_then(|t| t.as_str()) != Some(tenant.as_str()) {
                return None;
            }
            // Only the type and data: the browser refetches details through the API.
            let kind = value.get("type").and_then(|t| t.as_str()).unwrap_or("event").to_owned();
            let data = json!({ "type": kind, "data": value.get("data") });
            Some(Ok::<_, Infallible>(Event::default().event(kind).data(data.to_string())))
        }
    });
    Ok(Sse::new(merged).keep_alive(KeepAlive::new().interval(Duration::from_secs(20))))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn longest_prefix_wins() {
        assert_eq!(
            upstream_for(ROUTES, "/api/v1/transactions/category/123"),
            Some("categorizer")
        );
        assert_eq!(upstream_for(ROUTES, "/api/v1/transactions"), Some("ingest"));
        assert_eq!(upstream_for(ROUTES, "/api/v1/forecast"), Some("forecast"));
    }

    #[test]
    fn unknown_path_is_not_proxied() {
        assert_eq!(upstream_for(ROUTES, "/api/v1/admin"), None);
        assert_eq!(upstream_for(ROUTES, "/api/v1/forecastx"), None);
    }

    #[test]
    fn session_cookie_is_read_among_others() {
        let mut h = HeaderMap::new();
        h.insert(header::COOKIE, HeaderValue::from_static("a=1; __Host-ci1_sid=abc; b=2"));
        assert_eq!(session_id(&h).as_deref(), Some("abc"));
    }

    #[test]
    fn mutation_without_csrf_header_is_forbidden() {
        assert!(require_csrf(&HeaderMap::new()).is_err());
    }
}
