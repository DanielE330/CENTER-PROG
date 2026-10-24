//! advisor-tools: MCP server with the agent's tools. Reads go to internal APIs
//! with a token exchanged in Keycloak (read-only by construction); "writes" are
//! proposals that the user applies with a button.

mod mcp;

use std::sync::Arc;

use anyhow::{Context, Result};
use axum::{
    Extension, Json, Router,
    extract::{Path, State},
    routing::{get, post},
};
use ci_core::{
    ApiError,
    auth::{AuthUser, JwtVerifier},
    client::MtlsClient,
    config::CoreConfig,
    db,
    error::ApiResult,
    events::{self, Envelope},
    server, telemetry,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::PgPool;
use uuid::Uuid;

const SCHEMA: &str = "advisor_tools";

#[derive(Clone)]
pub struct AppState {
    pub inner: Arc<Inner>,
}

pub struct Inner {
    pub pool: PgPool,
    pub client: MtlsClient,
    pub token_url: String,
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
    let js = events::connect(CoreConfig::require(&config.nats_url, "NATS_URL")?, &config.cert_dir).await?;
    events::ensure_stream(&js, "ADVISOR", &["ci.v1.advisor.>"]).await?;
    let client = MtlsClient::from_dir(&config.cert_dir)?;
    let verifier = JwtVerifier::new(
        CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?,
        &config.oidc_audience,
        CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?,
        client.clone(),
    );
    let kc = std::env::var("KEYCLOAK_INTERNAL_URL")?;
    tokio::spawn(events::run_outbox_relay(pool.clone(), js, SCHEMA.into()));
    let state = AppState {
        inner: Arc::new(Inner {
            pool,
            client,
            token_url: format!("{kc}/realms/ci1/protocol/openid-connect/token"),
        }),
    };
    let app = Router::new()
        .route(
            "/mcp",
            post(mcp::handle).get(|| async { axum::http::StatusCode::METHOD_NOT_ALLOWED }),
        )
        .route("/api/v1/proposals", get(list_proposals))
        .route("/api/v1/proposals/{id}/accept", post(accept))
        .route("/api/v1/proposals/{id}/reject", post(reject))
        .layer(Extension(verifier))
        .with_state(state);
    server::serve(&config, server::base_router(app, 1024 * 1024)).await
}

#[derive(Deserialize)]
struct Exchanged {
    access_token: String,
}

impl Inner {
    /// RFC 8693 token exchange: the user's token becomes a narrower one for the
    /// `ci1-api` audience, issued to advisor-tools. Services treat it as read-only.
    pub async fn exchange(&self, user_token: &str) -> Result<String> {
        let resp: Exchanged = self
            .client
            .get()
            .post(&self.token_url)
            .form(&[
                ("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange"),
                ("client_id", "advisor-tools"),
                ("subject_token", user_token),
                ("subject_token_type", "urn:ietf:params:oauth:token-type:access_token"),
                ("requested_token_type", "urn:ietf:params:oauth:token-type:access_token"),
                ("audience", "ci1-api"),
                // The organization mapper only fires when this scope is requested.
                ("scope", "openid organization"),
            ])
            .send()
            .await?
            .error_for_status()
            .context("token exchange")?
            .json()
            .await?;
        Ok(resp.access_token)
    }

    pub async fn call(
        &self,
        method: reqwest::Method,
        service: &str,
        path: &str,
        token: &str,
        body: Option<&Value>,
    ) -> Result<Value> {
        let mut req = self
            .client
            .get()
            .request(method, format!("https://{service}:8443{path}"))
            .bearer_auth(token);
        if let Some(b) = body {
            req = req.json(b);
        }
        let resp = req.send().await?;
        let status = resp.status();
        let value: Value = resp.json().await.unwrap_or(Value::Null);
        if !status.is_success() {
            anyhow::bail!("{service} returned {status}");
        }
        Ok(value)
    }

    pub async fn propose(&self, user: &AuthUser, kind: &str, title: &str, payload: Value) -> Result<Uuid> {
        let id = Uuid::now_v7();
        let mut tx = db::tenant_tx(&self.pool, user.tenant_id).await?;
        sqlx::query(
            "insert into proposal (id, tenant_id, kind, title, payload, created_by) values ($1, $2, $3, $4, $5, $6)",
        )
        .bind(id)
        .bind(user.tenant_id)
        .bind(kind)
        .bind(title)
        .bind(&payload)
        .bind(&user.sub)
        .execute(&mut *tx)
        .await?;
        #[derive(Serialize)]
        struct Created<'a> {
            id: Uuid,
            kind: &'a str,
            title: &'a str,
        }
        events::enqueue(
            &mut tx,
            SCHEMA,
            &Envelope::new("advisor.proposal_created", user.tenant_id, Created { id, kind, title }),
        )
        .await?;
        tx.commit().await?;
        Ok(id)
    }
}

#[derive(Serialize, sqlx::FromRow)]
struct ProposalView {
    id: Uuid,
    kind: String,
    title: String,
    payload: Value,
    status: String,
    created_at: chrono::DateTime<chrono::Utc>,
}

async fn list_proposals(State(state): State<AppState>, user: AuthUser) -> ApiResult<Json<Vec<ProposalView>>> {
    let mut tx = db::tenant_tx(&state.inner.pool, user.tenant_id).await?;
    let rows: Vec<ProposalView> = sqlx::query_as(
        "select id, kind, title, payload, status, created_at from proposal order by created_at desc limit 50",
    )
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(rows))
}

/// Applies a proposal with the user's own token: the user's rights apply, not the agent's.
async fn accept(State(state): State<AppState>, user: AuthUser, Path(id): Path<Uuid>) -> ApiResult<Json<Value>> {
    user.require_write()?;
    let s = &state.inner;
    let mut tx = db::tenant_tx(&s.pool, user.tenant_id).await?;
    let (kind, payload): (String, Value) =
        sqlx::query_as("select kind, payload from proposal where id = $1 and status = 'pending' for update")
            .bind(id)
            .fetch_one(&mut *tx)
            .await?;
    let result = match kind.as_str() {
        "obligation" => {
            s.call(
                reqwest::Method::POST,
                "obligations",
                "/api/v1/obligations",
                &user.token,
                Some(&payload),
            )
            .await
        }
        "scenario" => {
            s.call(
                reqwest::Method::POST,
                "forecast",
                "/api/v1/scenarios",
                &user.token,
                Some(&payload),
            )
            .await
        }
        _ => Err(anyhow::anyhow!("unknown proposal kind")),
    };
    let status = if result.is_ok() { "accepted" } else { "failed" };
    sqlx::query("update proposal set status = $1, decided_at = now() where id = $2")
        .bind(status)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    let created = result.map_err(ApiError::Upstream)?;
    Ok(Json(json!({ "status": status, "result": created })))
}

async fn reject(State(state): State<AppState>, user: AuthUser, Path(id): Path<Uuid>) -> ApiResult<Json<Value>> {
    let mut tx = db::tenant_tx(&state.inner.pool, user.tenant_id).await?;
    let res =
        sqlx::query("update proposal set status = 'rejected', decided_at = now() where id = $1 and status = 'pending'")
            .bind(id)
            .execute(&mut *tx)
            .await?;
    tx.commit().await?;
    if res.rows_affected() == 0 {
        return Err(ApiError::NotFound);
    }
    Ok(Json(json!({ "status": "rejected" })))
}
