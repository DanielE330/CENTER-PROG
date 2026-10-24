//! alerts: turns forecast gaps and overdue obligations into warnings,
//! collapses repeats and announces changes for the browser stream.

mod rules;

use anyhow::Result;
use axum::{
    Extension, Json, Router,
    extract::{Path, Query, State},
    routing::{get, post},
};
use ci_core::{
    auth::{AuthUser, JwtVerifier},
    client::MtlsClient,
    config::CoreConfig,
    db,
    error::ApiResult,
    events::{self, Envelope},
    server, telemetry,
};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Postgres, Transaction};
use uuid::Uuid;

use crate::rules::Draft;

const SCHEMA: &str = "alerts";

#[derive(Clone)]
struct AppState {
    pool: PgPool,
}

#[derive(Serialize, sqlx::FromRow)]
struct AlertView {
    id: Uuid,
    kind: String,
    severity: String,
    title: String,
    body: String,
    data: serde_json::Value,
    status: String,
    created_at: chrono::DateTime<chrono::Utc>,
    updated_at: chrono::DateTime<chrono::Utc>,
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
    events::ensure_stream(&js, "ALERTS", &["ci.v1.alerts.>"]).await?;
    let verifier = JwtVerifier::new(
        CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?,
        &config.oidc_audience,
        CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?,
        MtlsClient::from_dir(&config.cert_dir)?,
    );

    tokio::spawn(events::run_outbox_relay(pool.clone(), js.clone(), SCHEMA.into()));
    tokio::spawn({
        let (js, pool) = (js.clone(), pool.clone());
        async move {
            let r = events::consume(
                js,
                "FORECAST",
                "alerts-forecast",
                "ci.v1.forecast.updated",
                move |env| {
                    let pool = pool.clone();
                    async move { on_forecast(&pool, env).await }
                },
            )
            .await;
            if let Err(err) = r {
                tracing::error!(error = %err, "forecast consumer stopped");
            }
        }
    });
    tokio::spawn({
        let pool = pool.clone();
        async move {
            let r = events::consume(
                js,
                "OBLIGATIONS",
                "alerts-overdue",
                "ci.v1.obligations.overdue",
                move |env| {
                    let pool = pool.clone();
                    async move { on_overdue(&pool, env).await }
                },
            )
            .await;
            if let Err(err) = r {
                tracing::error!(error = %err, "overdue consumer stopped");
            }
        }
    });

    let app = Router::new()
        .route("/api/v1/alerts", get(list))
        .route("/api/v1/alerts/{id}/ack", post(ack))
        .layer(Extension(verifier))
        .with_state(AppState { pool });
    server::serve(&config, server::base_router(app, 64 * 1024)).await
}

/// Inserts or refreshes an alert; announces it only when something visible changed.
async fn upsert(tx: &mut Transaction<'static, Postgres>, tenant: Uuid, d: &Draft) -> Result<()> {
    let changed: Option<(Uuid, String, String)> = sqlx::query_as(
        "insert into alert (id, tenant_id, kind, severity, dedup_key, title, body, data)
         values ($1, $2, $3, $4, $5, $6, $7, $8)
         on conflict (tenant_id, dedup_key) do update
           set severity = excluded.severity, title = excluded.title, body = excluded.body, data = excluded.data,
               status = case when alert.status = 'resolved' then 'open' else alert.status end, updated_at = now()
           where alert.title is distinct from excluded.title or alert.severity is distinct from excluded.severity
              or alert.status = 'resolved'
         returning id, severity, title",
    )
    .bind(Uuid::now_v7())
    .bind(tenant)
    .bind(&d.kind)
    .bind(&d.severity)
    .bind(&d.dedup_key)
    .bind(&d.title)
    .bind(&d.body)
    .bind(&d.data)
    .fetch_optional(&mut **tx)
    .await?;
    if let Some((id, severity, title)) = changed {
        #[derive(Serialize)]
        struct Raised {
            id: Uuid,
            kind: String,
            severity: String,
            title: String,
        }
        let event = Raised {
            id,
            kind: d.kind.clone(),
            severity,
            title,
        };
        events::enqueue(tx, SCHEMA, &Envelope::new("alerts.raised", tenant, event)).await?;
    }
    Ok(())
}

async fn on_forecast(pool: &PgPool, env: Envelope<rules::ForecastUpdated>) -> Result<()> {
    let tenant = env.tenant_id;
    let mut tx = db::tenant_tx(pool, tenant).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    if env.data.scenario_id.is_some() {
        // Scenario runs are "what if", not warnings.
        tx.commit().await?;
        return Ok(());
    }
    let drafts = rules::from_forecast(&env.data);
    let keys: Vec<String> = drafts.iter().map(|d| d.dedup_key.clone()).collect();
    for d in &drafts {
        upsert(&mut tx, tenant, d).await?;
    }
    // Gaps that disappeared from the latest forecast are resolved.
    sqlx::query("update alert set status = 'resolved', updated_at = now() where kind like 'gap%' and status <> 'resolved' and not (dedup_key = any($1))")
        .bind(&keys)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(())
}

async fn on_overdue(pool: &PgPool, env: Envelope<rules::OverdueData>) -> Result<()> {
    let tenant = env.tenant_id;
    let mut tx = db::tenant_tx(pool, tenant).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    let drafts = rules::from_overdue(&env.data);
    let keys: Vec<String> = drafts.iter().map(|d| d.dedup_key.clone()).collect();
    for d in &drafts {
        upsert(&mut tx, tenant, d).await?;
    }
    // Everything paid in a direction: its summary closes by itself.
    sqlx::query(
        "update alert set status = 'resolved', updated_at = now() where kind = 'overdue' and status <> 'resolved' and not (dedup_key = any($1))",
    )
    .bind(&keys)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

#[derive(Deserialize)]
struct ListQuery {
    status: Option<String>,
}

async fn list(
    State(state): State<AppState>,
    user: AuthUser,
    Query(q): Query<ListQuery>,
) -> ApiResult<Json<Vec<AlertView>>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let rows: Vec<AlertView> = sqlx::query_as(
        "select id, kind, severity, title, body, data, status, created_at, updated_at from alert
         where ($1::text is null or status = $1)
         order by case severity when 'critical' then 0 when 'high' then 1 when 'warning' then 2 else 3 end, updated_at desc
         limit 200",
    )
    .bind(q.status)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(rows))
}

async fn ack(
    State(state): State<AppState>,
    user: AuthUser,
    Path(id): Path<Uuid>,
) -> ApiResult<Json<serde_json::Value>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let res = sqlx::query("update alert set status = 'acked', updated_at = now() where id = $1 and status = 'open'")
        .bind(id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(serde_json::json!({ "acked": res.rows_affected() == 1 })))
}
