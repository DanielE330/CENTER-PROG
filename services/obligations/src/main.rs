//! obligations: future money movements (manual input, invoices, tax calendar,
//! confirmed recurring payments) and plan-fact matching against the statement.

mod taxcal;

use anyhow::Result;
use axum::{
    Extension, Json, Router,
    extract::{Path, Query, State},
    routing::{get, patch, post},
};
use chrono::{Duration, NaiveDate};
use ci_core::{
    ApiError,
    auth::{AuthUser, JwtVerifier, ServiceCaller},
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

const SCHEMA: &str = "obligations";
const GRACE_DAYS: i64 = 3;
const AMOUNT_TOLERANCE: f64 = 0.02;
const HORIZON_DAYS: i64 = 120;

#[derive(Clone)]
struct AppState {
    pool: PgPool,
}

#[derive(Clone, Debug, Serialize, Deserialize, sqlx::FromRow)]
struct Obligation {
    id: Uuid,
    kind: String,
    direction: String,
    amount_kop: i64,
    due_date: NaiveDate,
    certainty: f32,
    counterparty_ref: Option<String>,
    doc_no: Option<String>,
    doc_date: Option<NaiveDate>,
    source: String,
    source_ref: String,
    status: String,
    matched_transaction_id: Option<Uuid>,
    paid_date: Option<NaiveDate>,
    note: Option<String>,
}

const COLUMNS: &str = "id, kind, direction, amount_kop, due_date, certainty, counterparty_ref, doc_no, doc_date,
    source, source_ref, status, matched_transaction_id, paid_date, note";

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
    events::ensure_stream(&js, "OBLIGATIONS", &["ci.v1.obligations.>"]).await?;
    let verifier = JwtVerifier::new(
        CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?,
        &config.oidc_audience,
        CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?,
        MtlsClient::from_dir(&config.cert_dir)?,
    );

    tokio::spawn(events::run_outbox_relay(pool.clone(), js.clone(), SCHEMA.into()));
    spawn_consumer(
        js.clone(),
        pool.clone(),
        "TENANCY",
        "obligations-settings",
        "ci.v1.tenancy.settings_changed",
        Kind::Settings,
    );
    spawn_consumer(
        js,
        pool.clone(),
        "TRANSACTIONS",
        "obligations-categorized",
        "ci.v1.transactions.categorized",
        Kind::Categorized,
    );

    let app = Router::new()
        .route("/api/v1/obligations", get(list).post(create))
        .route("/api/v1/obligations/{id}", patch(update).delete(cancel))
        .route("/internal/v1/tenants/{tenant}/obligations", post(internal_upsert))
        .layer(Extension(verifier))
        .with_state(AppState { pool });
    server::serve(&config, server::base_router(app, 2 * 1024 * 1024)).await
}

#[derive(Clone, Copy)]
enum Kind {
    Settings,
    Categorized,
}

fn spawn_consumer(
    js: async_nats::jetstream::Context,
    pool: PgPool,
    stream: &'static str,
    durable: &'static str,
    filter: &'static str,
    kind: Kind,
) {
    tokio::spawn(async move {
        let result = match kind {
            Kind::Settings => {
                events::consume(js, stream, durable, filter, move |env| {
                    let pool = pool.clone();
                    async move { on_settings(&pool, env).await }
                })
                .await
            }
            Kind::Categorized => {
                events::consume(js, stream, durable, filter, move |env| {
                    let pool = pool.clone();
                    async move { on_categorized(&pool, env).await }
                })
                .await
            }
        };
        if let Err(err) = result {
            tracing::error!(error = %err, durable, "consumer stopped");
        }
    });
}

/// Announces the full set of open obligations; forecast replaces its copy.
async fn announce(tx: &mut Transaction<'static, Postgres>, tenant: Uuid) -> Result<()> {
    let open: Vec<Obligation> = sqlx::query_as(&format!(
        "select {COLUMNS} from event where status in ('planned', 'overdue') order by due_date"
    ))
    .fetch_all(&mut **tx)
    .await?;
    #[derive(Serialize)]
    struct Changed {
        items: Vec<Obligation>,
    }
    events::enqueue(
        tx,
        SCHEMA,
        &Envelope::new("obligations.changed", tenant, Changed { items: open }),
    )
    .await
}

async fn as_of(tx: &mut Transaction<'static, Postgres>) -> Result<Option<NaiveDate>> {
    Ok(sqlx::query_as::<_, (NaiveDate,)>("select as_of from tenant_settings")
        .fetch_optional(&mut **tx)
        .await?
        .map(|r| r.0))
}

/// Marks planned obligations past due date plus grace as overdue. Returns how many changed.
async fn mark_overdue(tx: &mut Transaction<'static, Postgres>) -> Result<usize> {
    let Some(today) = as_of(tx).await? else { return Ok(0) };
    let res = sqlx::query("update event set status = 'overdue' where status = 'planned' and due_date + $1::int < $2")
        .bind(GRACE_DAYS as i32)
        .bind(today)
        .execute(&mut **tx)
        .await?;
    Ok(res.rows_affected() as usize)
}

/// Announces the full list of overdue obligations (possibly empty), so alerts can keep
/// one summary warning up to date and close it when everything is paid.
async fn announce_overdue(tx: &mut Transaction<'static, Postgres>, tenant: Uuid) -> Result<()> {
    let today = as_of(tx).await?.unwrap_or_else(|| chrono::Utc::now().date_naive());
    let items: Vec<Obligation> = sqlx::query_as(&format!(
        "select {COLUMNS} from event where status = 'overdue' order by due_date"
    ))
    .fetch_all(&mut **tx)
    .await?;
    #[derive(Serialize)]
    struct Overdue {
        as_of: NaiveDate,
        items: Vec<Obligation>,
    }
    events::enqueue(
        tx,
        SCHEMA,
        &Envelope::new("obligations.overdue", tenant, Overdue { as_of: today, items }),
    )
    .await
}

#[derive(Deserialize)]
struct SettingsEvent {
    as_of: NaiveDate,
    tax_regime: Option<String>,
    #[serde(default)]
    tax_estimate: serde_json::Value,
}

async fn on_settings(pool: &PgPool, env: Envelope<SettingsEvent>) -> Result<()> {
    let tenant = env.tenant_id;
    let mut tx = db::tenant_tx(pool, tenant).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    sqlx::query(
        "insert into tenant_settings (tenant_id, as_of, tax_regime, tax_estimate) values ($1, $2, $3, $4)
         on conflict (tenant_id) do update set as_of = excluded.as_of, tax_regime = excluded.tax_regime, tax_estimate = excluded.tax_estimate",
    )
    .bind(tenant)
    .bind(env.data.as_of)
    .bind(&env.data.tax_regime)
    .bind(&env.data.tax_estimate)
    .execute(&mut *tx)
    .await?;
    // The calendar is regenerated for the horizon; already matched payments keep their status.
    sqlx::query("delete from event where source = 'tax_calendar' and status = 'planned'")
        .execute(&mut *tx)
        .await?;
    if let Some(regime) = &env.data.tax_regime {
        let estimate: taxcal::TaxEstimate = serde_json::from_value(env.data.tax_estimate.clone()).unwrap_or_default();
        for p in taxcal::schedule(
            regime,
            &estimate,
            env.data.as_of,
            env.data.as_of + Duration::days(HORIZON_DAYS),
        ) {
            sqlx::query(
                "insert into event (id, tenant_id, kind, direction, amount_kop, due_date, certainty, source, source_ref, note)
                 values ($1, $2, 'tax', 'outflow', $3, $4, 0.95, 'tax_calendar', $5, $6)
                 on conflict (tenant_id, source, source_ref) do nothing",
            )
            .bind(Uuid::now_v7())
            .bind(tenant)
            .bind(p.amount_kop)
            .bind(p.due)
            .bind(p.due.to_string())
            .bind(&p.label)
            .execute(&mut *tx)
            .await?;
        }
    }
    mark_overdue(&mut tx).await?;
    announce_overdue(&mut tx, tenant).await?;
    announce(&mut tx, tenant).await?;
    tx.commit().await?;
    Ok(())
}

#[derive(Deserialize)]
struct CategorizedItem {
    id: Uuid,
    op_date: NaiveDate,
    amount_kop: i64,
    counterparty_ref: Option<String>,
    group: String,
}

#[derive(Deserialize)]
struct CategorizedData {
    items: Vec<CategorizedItem>,
}

fn amount_matches(planned: i64, actual: i64) -> bool {
    let diff = (planned - actual).abs() as f64;
    diff <= planned as f64 * AMOUNT_TOLERANCE
}

/// Plan-fact: a payment closes the open obligation of the same counterparty
/// (or the tax obligation for tax payments) with a matching amount and date window.
async fn on_categorized(pool: &PgPool, env: Envelope<CategorizedData>) -> Result<()> {
    let tenant = env.tenant_id;
    let mut tx = db::tenant_tx(pool, tenant).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    let mut matched = 0;
    for item in &env.data.items {
        let direction = if item.amount_kop >= 0 { "inflow" } else { "outflow" };
        let candidates: Vec<(Uuid, i64)> = sqlx::query_as(
            "select id, amount_kop from event
             where status in ('planned', 'overdue') and direction = $1
               and ((counterparty_ref is not null and counterparty_ref = $2) or (kind = 'tax' and $3 = 'taxes'))
               and $4 between coalesce(doc_date, due_date - 15) and due_date + case when kind = 'invoice_out' then 120 else 45 end
             order by abs(due_date - $4)",
        )
        .bind(direction)
        .bind(&item.counterparty_ref)
        .bind(&item.group)
        .bind(item.op_date)
        .fetch_all(&mut *tx)
        .await?;
        if let Some((id, _)) = candidates
            .into_iter()
            .find(|(_, amount)| amount_matches(*amount, item.amount_kop.abs()))
        {
            sqlx::query("update event set status = 'paid', matched_transaction_id = $1, paid_date = $2 where id = $3")
                .bind(item.id)
                .bind(item.op_date)
                .bind(id)
                .execute(&mut *tx)
                .await?;
            matched += 1;
        }
    }
    let overdue = mark_overdue(&mut tx).await?;
    if matched > 0 || overdue > 0 {
        announce_overdue(&mut tx, tenant).await?;
        announce(&mut tx, tenant).await?;
    }
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
) -> ApiResult<Json<Vec<Obligation>>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let rows: Vec<Obligation> = sqlx::query_as(&format!(
        "select {COLUMNS} from event where ($1::text is null or status = $1) order by due_date limit 1000"
    ))
    .bind(q.status)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(rows))
}

#[derive(Deserialize)]
struct NewObligation {
    kind: Option<String>,
    direction: String,
    amount_kop: i64,
    due_date: NaiveDate,
    certainty: Option<f32>,
    counterparty_ref: Option<String>,
    note: Option<String>,
}

fn validate(o: &NewObligation) -> ApiResult<()> {
    if o.amount_kop <= 0 {
        return Err(ApiError::BadRequest("amount_kop must be positive".into()));
    }
    if !matches!(o.direction.as_str(), "inflow" | "outflow") {
        return Err(ApiError::BadRequest("direction is inflow or outflow".into()));
    }
    if o.certainty.is_some_and(|c| !(0.0..=1.0).contains(&c)) {
        return Err(ApiError::BadRequest("certainty is between 0 and 1".into()));
    }
    if o.note.as_ref().is_some_and(|n| n.chars().count() > 500) {
        return Err(ApiError::BadRequest("note is too long".into()));
    }
    Ok(())
}

async fn create(
    State(state): State<AppState>,
    user: AuthUser,
    Json(body): Json<NewObligation>,
) -> ApiResult<Json<Obligation>> {
    user.require_write()?;
    validate(&body)?;
    let kind = body.kind.clone().unwrap_or_else(|| "manual".into());
    if !matches!(
        kind.as_str(),
        "manual" | "invoice_out" | "invoice_in" | "recurring" | "contract"
    ) {
        return Err(ApiError::BadRequest("kind not allowed here".into()));
    }
    let id = Uuid::now_v7();
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let row: Obligation = sqlx::query_as(&format!(
        "insert into event (id, tenant_id, kind, direction, amount_kop, due_date, certainty, counterparty_ref, source, source_ref, note)
         values ($1, $2, $3, $4, $5, $6, $7, $8, 'user', $9, $10) returning {COLUMNS}"
    ))
    .bind(id)
    .bind(user.tenant_id)
    .bind(&kind)
    .bind(&body.direction)
    .bind(body.amount_kop)
    .bind(body.due_date)
    .bind(body.certainty.unwrap_or(1.0))
    .bind(&body.counterparty_ref)
    .bind(id.to_string())
    .bind(&body.note)
    .fetch_one(&mut *tx)
    .await?;
    announce(&mut tx, user.tenant_id).await?;
    tx.commit().await?;
    Ok(Json(row))
}

#[derive(Deserialize)]
struct Patch {
    amount_kop: Option<i64>,
    due_date: Option<NaiveDate>,
    certainty: Option<f32>,
    note: Option<String>,
}

async fn update(
    State(state): State<AppState>,
    user: AuthUser,
    Path(id): Path<Uuid>,
    Json(body): Json<Patch>,
) -> ApiResult<Json<Obligation>> {
    user.require_write()?;
    if body.amount_kop.is_some_and(|a| a <= 0) || body.certainty.is_some_and(|c| !(0.0..=1.0).contains(&c)) {
        return Err(ApiError::BadRequest("invalid values".into()));
    }
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let row: Obligation = sqlx::query_as(&format!(
        "update event set amount_kop = coalesce($1, amount_kop), due_date = coalesce($2, due_date),
           certainty = coalesce($3, certainty), note = coalesce($4, note),
           status = case when status = 'overdue' and coalesce($2, due_date) + 3 >= coalesce((select as_of from tenant_settings), current_date) then 'planned' else status end
         where id = $5 returning {COLUMNS}"
    ))
    .bind(body.amount_kop)
    .bind(body.due_date)
    .bind(body.certainty)
    .bind(body.note)
    .bind(id)
    .fetch_one(&mut *tx)
    .await?;
    announce(&mut tx, user.tenant_id).await?;
    tx.commit().await?;
    Ok(Json(row))
}

async fn cancel(State(state): State<AppState>, user: AuthUser, Path(id): Path<Uuid>) -> ApiResult<Json<Obligation>> {
    user.require_write()?;
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let row: Obligation = sqlx::query_as(&format!(
        "update event set status = 'cancelled' where id = $1 returning {COLUMNS}"
    ))
    .bind(id)
    .fetch_one(&mut *tx)
    .await?;
    announce(&mut tx, user.tenant_id).await?;
    tx.commit().await?;
    Ok(Json(row))
}

#[derive(Deserialize)]
struct ExternalObligation {
    kind: String,
    direction: String,
    amount_kop: i64,
    due_date: NaiveDate,
    certainty: Option<f32>,
    counterparty_ref: Option<String>,
    doc_no: Option<String>,
    doc_date: Option<NaiveDate>,
    source: String,
    source_ref: String,
}

/// Invoices and other events from connectors; idempotent by (source, source_ref).
async fn internal_upsert(
    State(state): State<AppState>,
    caller: ServiceCaller,
    Path(tenant): Path<Uuid>,
    Json(items): Json<Vec<ExternalObligation>>,
) -> ApiResult<Json<serde_json::Value>> {
    if !["connectors", "advisor-tools"].contains(&caller.service.as_str()) {
        return Err(ApiError::Forbidden);
    }
    let mut tx = db::tenant_tx(&state.pool, tenant).await?;
    let mut upserted = 0;
    for o in &items {
        if o.amount_kop <= 0 || !matches!(o.direction.as_str(), "inflow" | "outflow") {
            return Err(ApiError::BadRequest("invalid obligation".into()));
        }
        sqlx::query(
            "insert into event (id, tenant_id, kind, direction, amount_kop, due_date, certainty, counterparty_ref, doc_no, doc_date, source, source_ref)
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
             on conflict (tenant_id, source, source_ref) do update
               set amount_kop = excluded.amount_kop, due_date = excluded.due_date, certainty = excluded.certainty
             where event.status = 'planned'",
        )
        .bind(Uuid::now_v7())
        .bind(tenant)
        .bind(&o.kind)
        .bind(&o.direction)
        .bind(o.amount_kop)
        .bind(o.due_date)
        .bind(o.certainty.unwrap_or(0.9))
        .bind(&o.counterparty_ref)
        .bind(&o.doc_no)
        .bind(o.doc_date)
        .bind(&o.source)
        .bind(&o.source_ref)
        .execute(&mut *tx)
        .await?;
        upserted += 1;
    }
    mark_overdue(&mut tx).await?;
    announce_overdue(&mut tx, tenant).await?;
    announce(&mut tx, tenant).await?;
    tx.commit().await?;
    Ok(Json(serde_json::json!({ "upserted": upserted })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn amount_within_two_percent_matches() {
        assert!(amount_matches(100_000, 101_500));
        assert!(!amount_matches(100_000, 103_000));
    }
}
