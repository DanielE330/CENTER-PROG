//! tenancy: business settings of an organization (tax regime, "today" for
//! replay, overdraft limit, LLM policy, feature flags) and onboarding of
//! organizations and users into Keycloak.

mod keycloak;

use std::sync::Arc;

use anyhow::Result;
use axum::{
    Extension, Json, Router,
    extract::{Path, State},
    routing::{get, post, put},
};
use chrono::NaiveDate;
use ci_core::{
    ApiError,
    auth::{AuthUser, JwtVerifier, ServiceCaller, ServiceTokenSource},
    client::MtlsClient,
    config::CoreConfig,
    db,
    error::ApiResult,
    events::{self, Envelope},
    server, telemetry,
};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;
use uuid::Uuid;

const SCHEMA: &str = "tenancy";
const TAX_REGIMES: &[&str] = &["usn_income", "usn_income_expense", "osno", "eshn", "patent"];

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    keycloak: Arc<keycloak::Admin>,
}

#[derive(Clone, Debug, Serialize, Deserialize, sqlx::FromRow)]
pub struct Settings {
    id: Uuid,
    name: String,
    alias: String,
    tax_regime: Option<String>,
    tax_regime_suggested: Option<String>,
    tax_regime_confidence: Option<f32>,
    tax_estimate: serde_json::Value,
    as_of: NaiveDate,
    overdraft_limit_kop: i64,
    min_balance_kop: i64,
    llm_policy: serde_json::Value,
    demo_company: Option<String>,
}

const SELECT: &str = "select id, name, alias, tax_regime, tax_regime_suggested, tax_regime_confidence, tax_estimate,
    as_of, overdraft_limit_kop, min_balance_kop, llm_policy, demo_company from organization";

#[tokio::main]
async fn main() -> Result<()> {
    let config = CoreConfig::from_env()?;
    telemetry::init(&config.service);
    if std::env::args().any(|a| a == "--healthcheck") {
        let client = MtlsClient::from_dir(&config.cert_dir)?;
        client
            .get()
            .get(format!("https://{}:{}/healthz", config.service, config.listen.port()))
            .send()
            .await?
            .error_for_status()?;
        return Ok(());
    }
    let pool = db::connect_with_retry(
        CoreConfig::require(&config.database_url, "DATABASE_URL")?,
        &config.cert_dir,
    )
    .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    let js = events::connect(CoreConfig::require(&config.nats_url, "NATS_URL")?, &config.cert_dir).await?;
    events::ensure_stream(&js, "TENANCY", &["ci.v1.tenancy.>"]).await?;

    let client = MtlsClient::from_dir(&config.cert_dir)?;
    let issuer = CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?;
    let verifier = JwtVerifier::new(
        issuer,
        &config.oidc_audience,
        CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?,
        client.clone(),
    );
    let internal_base = std::env::var("KEYCLOAK_INTERNAL_URL")?;
    let tokens = ServiceTokenSource::new(
        &format!("{internal_base}/realms/ci1/protocol/openid-connect/token"),
        "tenancy",
        client.clone(),
    );
    let keycloak = Arc::new(keycloak::Admin::new(&internal_base, "ci1", client, tokens));
    let state = AppState {
        pool: pool.clone(),
        keycloak,
    };

    tokio::spawn(events::run_outbox_relay(pool.clone(), js.clone(), SCHEMA.into()));
    tokio::spawn({
        let pool = pool.clone();
        async move {
            let result = events::consume(
                js,
                "CATEGORIZER",
                "tenancy-tax",
                "ci.v1.categorizer.tax_regime_suggested",
                move |env| {
                    let pool = pool.clone();
                    async move { apply_tax_suggestion(&pool, env).await }
                },
            )
            .await;
            if let Err(err) = result {
                tracing::error!(error = %err, "tax suggestion consumer stopped");
            }
        }
    });

    let app = Router::new()
        .route("/api/v1/settings", get(get_settings).put(put_settings))
        .route("/api/v1/settings/tax-regime/confirm", post(confirm_tax_regime))
        .route("/api/v1/flags", get(get_flags))
        .route("/internal/v1/organizations", post(create_organization))
        .route("/internal/v1/tenants/{tenant}/settings", get(internal_settings))
        .route("/internal/v1/tenants/{tenant}/as-of", put(internal_as_of))
        .layer(Extension(verifier))
        .with_state(state);
    server::serve(&config, server::base_router(app, 1024 * 1024)).await
}

async fn load(pool: &PgPool, tenant: Uuid) -> ApiResult<Settings> {
    let mut tx = db::tenant_tx(pool, tenant).await?;
    let settings: Settings = sqlx::query_as(SELECT).fetch_one(&mut *tx).await?;
    tx.commit().await?;
    Ok(settings)
}

/// Saves and announces settings in one transaction so consumers never miss a change.
async fn publish(tx: &mut sqlx::Transaction<'static, sqlx::Postgres>, tenant: Uuid) -> ApiResult<Settings> {
    let settings: Settings = sqlx::query_as(SELECT).fetch_one(&mut **tx).await?;
    events::enqueue(
        tx,
        SCHEMA,
        &Envelope::new("tenancy.settings_changed", tenant, settings.clone()),
    )
    .await?;
    Ok(settings)
}

async fn get_settings(State(state): State<AppState>, user: AuthUser) -> ApiResult<Json<Settings>> {
    Ok(Json(load(&state.pool, user.tenant_id).await?))
}

#[derive(Deserialize)]
struct SettingsUpdate {
    overdraft_limit_kop: Option<i64>,
    min_balance_kop: Option<i64>,
    llm_policy: Option<LlmPolicy>,
}

#[derive(Serialize, Deserialize)]
struct LlmPolicy {
    provider: String,
    external_allowed: bool,
    masking: bool,
}

async fn put_settings(
    State(state): State<AppState>,
    user: AuthUser,
    Json(update): Json<SettingsUpdate>,
) -> ApiResult<Json<Settings>> {
    if !user.roles.iter().any(|r| r == "owner") {
        return Err(ApiError::Forbidden);
    }
    if update.overdraft_limit_kop.is_some_and(|v| v < 0) || update.min_balance_kop.is_some_and(|v| v < 0) {
        return Err(ApiError::BadRequest("limits must be non-negative".into()));
    }
    if let Some(policy) = &update.llm_policy {
        if !["local", "anthropic", "openai", "gemini", "openrouter", "template"].contains(&policy.provider.as_str()) {
            return Err(ApiError::BadRequest("unknown LLM provider".into()));
        }
        // Masking can be relaxed only for the local model: data then never leaves the contour.
        if !policy.masking && policy.provider != "local" {
            return Err(ApiError::BadRequest("external providers always get masked data".into()));
        }
    }
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    sqlx::query(
        "update organization set
           overdraft_limit_kop = coalesce($1, overdraft_limit_kop),
           min_balance_kop = coalesce($2, min_balance_kop),
           llm_policy = coalesce($3, llm_policy),
           updated_at = now()",
    )
    .bind(update.overdraft_limit_kop)
    .bind(update.min_balance_kop)
    .bind(update.llm_policy.map(|p| serde_json::to_value(p).unwrap_or_default()))
    .execute(&mut *tx)
    .await?;
    let settings = publish(&mut tx, user.tenant_id).await?;
    tx.commit().await?;
    Ok(Json(settings))
}

#[derive(Deserialize)]
struct ConfirmTax {
    tax_regime: String,
}

async fn confirm_tax_regime(
    State(state): State<AppState>,
    user: AuthUser,
    Json(body): Json<ConfirmTax>,
) -> ApiResult<Json<Settings>> {
    user.require_write()?;
    if !TAX_REGIMES.contains(&body.tax_regime.as_str()) {
        return Err(ApiError::BadRequest("unknown tax regime".into()));
    }
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    sqlx::query("update organization set tax_regime = $1, updated_at = now()")
        .bind(&body.tax_regime)
        .execute(&mut *tx)
        .await?;
    let settings = publish(&mut tx, user.tenant_id).await?;
    tx.commit().await?;
    Ok(Json(settings))
}

async fn get_flags(State(state): State<AppState>, user: AuthUser) -> ApiResult<Json<serde_json::Value>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let rows: Vec<(String, bool)> = sqlx::query_as("select flag, enabled from feature_flag")
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(serde_json::Value::Object(
        rows.into_iter().map(|(k, v)| (k, serde_json::Value::Bool(v))).collect(),
    )))
}

#[derive(Deserialize)]
struct NewOrganization {
    name: String,
    alias: String,
    owner_email: String,
    owner_password: String,
    as_of: Option<NaiveDate>,
    demo_company: Option<String>,
}

#[derive(Serialize)]
struct CreatedOrganization {
    tenant_id: Uuid,
    user_id: String,
}

/// Onboarding used by the seed job: organization and owner in Keycloak, settings here.
async fn create_organization(
    State(state): State<AppState>,
    caller: ServiceCaller,
    Json(body): Json<NewOrganization>,
) -> ApiResult<Json<CreatedOrganization>> {
    if caller.service != "connectors" {
        return Err(ApiError::Forbidden);
    }
    if body.alias.is_empty() || !body.alias.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        return Err(ApiError::BadRequest("alias must be [a-z0-9-]".into()));
    }
    let tenant_id = state
        .keycloak
        .ensure_organization(&body.name, &body.alias)
        .await
        .map_err(ApiError::Upstream)?;
    let user_id = state
        .keycloak
        .ensure_owner(&body.owner_email, &body.owner_password, tenant_id)
        .await
        .map_err(ApiError::Upstream)?;
    let mut tx = db::tenant_tx(&state.pool, tenant_id).await?;
    sqlx::query(
        "insert into organization (id, name, alias, as_of, demo_company) values ($1, $2, $3, coalesce($4, current_date), $5)
         on conflict (id) do update set name = excluded.name, as_of = excluded.as_of, demo_company = excluded.demo_company",
    )
    .bind(tenant_id)
    .bind(&body.name)
    .bind(&body.alias)
    .bind(body.as_of)
    .bind(&body.demo_company)
    .execute(&mut *tx)
    .await?;
    publish(&mut tx, tenant_id).await?;
    tx.commit().await?;
    Ok(Json(CreatedOrganization { tenant_id, user_id }))
}

async fn internal_settings(
    State(state): State<AppState>,
    caller: ServiceCaller,
    Path(tenant): Path<Uuid>,
) -> ApiResult<Json<Settings>> {
    if !["llm-gateway", "connectors", "advisor-tools"].contains(&caller.service.as_str()) {
        return Err(ApiError::Forbidden);
    }
    Ok(Json(load(&state.pool, tenant).await?))
}

#[derive(Deserialize)]
struct AsOf {
    as_of: NaiveDate,
}

async fn internal_as_of(
    State(state): State<AppState>,
    caller: ServiceCaller,
    Path(tenant): Path<Uuid>,
    Json(body): Json<AsOf>,
) -> ApiResult<Json<Settings>> {
    if caller.service != "connectors" {
        return Err(ApiError::Forbidden);
    }
    let mut tx = db::tenant_tx(&state.pool, tenant).await?;
    let updated = sqlx::query("update organization set as_of = $1, updated_at = now()")
        .bind(body.as_of)
        .execute(&mut *tx)
        .await?;
    if updated.rows_affected() == 0 {
        return Err(ApiError::NotFound);
    }
    let settings = publish(&mut tx, tenant).await?;
    tx.commit().await?;
    Ok(Json(settings))
}

#[derive(Deserialize)]
struct TaxSuggestion {
    regime: String,
    confidence: f32,
    estimate: serde_json::Value,
}

async fn apply_tax_suggestion(pool: &PgPool, env: Envelope<TaxSuggestion>) -> Result<()> {
    let mut tx = db::tenant_tx(pool, env.tenant_id).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    sqlx::query(
        "update organization set tax_regime_suggested = $1, tax_regime_confidence = $2, tax_estimate = $3, updated_at = now()",
    )
    .bind(&env.data.regime)
    .bind(env.data.confidence)
    .bind(&env.data.estimate)
    .execute(&mut *tx)
    .await?;
    publish(&mut tx, env.tenant_id)
        .await
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    tx.commit().await?;
    Ok(())
}
