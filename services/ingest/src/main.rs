//! ingest: accepts bank statements, normalizes and deduplicates operations,
//! checks balance continuity and publishes `transactions.ingested`.

mod parse;

use std::sync::Arc;

use anyhow::Result;
use axum::{
    Extension, Json, Router,
    extract::{Multipart, Path, Query, State},
    routing::{get, patch, post},
};
use chrono::NaiveDate;
use ci_core::{
    ApiError,
    auth::{AuthUser, JwtVerifier, ServiceCaller},
    client::MtlsClient,
    config::{CoreConfig, secret},
    db,
    error::ApiResult,
    events::{self, Envelope},
    server, telemetry,
};
use ci_domain::counterparty_ref;
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use uuid::Uuid;

use crate::parse::ParsedRow;

const SCHEMA: &str = "ingest";
const EVENT_CHUNK: usize = 500;

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    hmac_key: Arc<Vec<u8>>,
}

#[derive(Serialize)]
struct IngestedItem {
    id: Uuid,
    op_date: NaiveDate,
    amount_kop: i64,
    counterparty_ref: Option<String>,
    counterparty_name: String,
    purpose: String,
    has_inn: bool,
    balance_eod_kop: i64,
}

#[derive(Serialize)]
struct IngestedData {
    import_id: Uuid,
    items: Vec<IngestedItem>,
}

#[derive(Serialize, Deserialize, Default)]
struct BalanceGap {
    after: NaiveDate,
    before: NaiveDate,
    missing_kop: i64,
}

#[derive(Serialize, Deserialize, Default)]
struct ImportStats {
    import_id: Uuid,
    format: String,
    rows: usize,
    inserted: usize,
    duplicates: usize,
    date_from: Option<NaiveDate>,
    date_to: Option<NaiveDate>,
    balance_gaps: Vec<BalanceGap>,
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
    events::ensure_stream(&js, "TRANSACTIONS", &["ci.v1.transactions.>"]).await?;

    let client = MtlsClient::from_dir(&config.cert_dir)?;
    let verifier = JwtVerifier::new(
        CoreConfig::require(&config.oidc_issuer, "OIDC_ISSUER")?,
        &config.oidc_audience,
        CoreConfig::require(&config.oidc_jwks_url, "OIDC_JWKS_URL")?,
        client,
    );
    let state = AppState {
        pool: pool.clone(),
        hmac_key: Arc::new(secret("counterparty_hmac_key")?.into_bytes()),
    };

    tokio::spawn(events::run_outbox_relay(pool.clone(), js.clone(), SCHEMA.into()));
    tokio::spawn({
        let pool = pool.clone();
        let js = js.clone();
        async move {
            let result = events::consume(
                js,
                "TRANSACTIONS",
                "ingest-categorized",
                "ci.v1.transactions.categorized",
                move |env| {
                    let pool = pool.clone();
                    async move { apply_categories(&pool, env).await }
                },
            )
            .await;
            if let Err(err) = result {
                tracing::error!(error = %err, "categorized consumer stopped");
            }
        }
    });

    let app = Router::new()
        .route("/api/v1/imports", post(upload))
        .route("/api/v1/imports/{id}", get(get_import))
        .route("/api/v1/transactions", get(list_transactions))
        .route(
            "/api/v1/transactions/{id}",
            patch(|| async { Err::<(), _>(ApiError::BadRequest("use categorizer".into())) }),
        )
        .route("/internal/v1/tenants/{tenant}/transactions", post(internal_batch))
        .layer(Extension(verifier))
        .with_state(state);
    server::serve(&config, server::base_router(app, 25 * 1024 * 1024)).await
}

async fn upload(
    State(state): State<AppState>,
    user: AuthUser,
    mut multipart: Multipart,
) -> ApiResult<Json<ImportStats>> {
    user.require_write()?;
    let mut bytes = Vec::new();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| ApiError::BadRequest(e.to_string()))?
    {
        if field.name() == Some("file") {
            bytes = field
                .bytes()
                .await
                .map_err(|e| ApiError::BadRequest(e.to_string()))?
                .to_vec();
        }
    }
    if bytes.is_empty() {
        return Err(ApiError::BadRequest("multipart field `file` is required".into()));
    }
    let (format, rows) = parse::parse_any(&bytes).map_err(|e| ApiError::BadRequest(format!("{e:#}")))?;
    let sha = hex_sha256(&bytes);
    Ok(Json(store(&state, user.tenant_id, "upload", format, &sha, rows).await?))
}

#[derive(Deserialize)]
struct BatchRow {
    operation_date: String,
    amount: String,
    direction: String,
    counterparty_inn: Option<String>,
    counterparty_name: Option<String>,
    payment_purpose: Option<String>,
    balance_eod: String,
}

#[derive(Deserialize)]
struct Batch {
    batch_id: String,
    rows: Vec<BatchRow>,
}

/// Used by the replay connector; the caller is authenticated by certificate and token.
async fn internal_batch(
    State(state): State<AppState>,
    caller: ServiceCaller,
    Path(tenant): Path<Uuid>,
    Json(batch): Json<Batch>,
) -> ApiResult<Json<ImportStats>> {
    if caller.service != "connectors" {
        return Err(ApiError::Forbidden);
    }
    let rows = batch
        .rows
        .into_iter()
        .map(|r| {
            parse::row_from_fields(
                &r.operation_date,
                &r.amount,
                &r.direction,
                r.counterparty_inn.as_deref(),
                r.counterparty_name.unwrap_or_default(),
                r.payment_purpose.unwrap_or_default(),
                &r.balance_eod,
            )
        })
        .collect::<Result<Vec<_>>>()
        .map_err(|e| ApiError::BadRequest(format!("{e:#}")))?;
    let sha = hex_sha256(batch.batch_id.as_bytes());
    Ok(Json(store(&state, tenant, "replay", "json", &sha, rows).await?))
}

fn hex_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn dedup_key(row: &ParsedRow) -> String {
    let normalized: String = row
        .purpose
        .expose()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    let mut h = Sha256::new();
    h.update(row.op_date.to_string());
    h.update(b"|");
    h.update(row.amount.0.to_le_bytes());
    h.update(b"|");
    h.update(row.inn.as_ref().map(|i| i.expose()).unwrap_or(""));
    h.update(b"|");
    h.update(row.counterparty_name.expose());
    h.update(b"|");
    h.update(normalized);
    format!("{:x}", h.finalize())
}

async fn store(
    state: &AppState,
    tenant: Uuid,
    source: &str,
    format: &str,
    sha: &str,
    rows: Vec<ParsedRow>,
) -> ApiResult<ImportStats> {
    let mut tx = db::tenant_tx(&state.pool, tenant).await?;
    let import_id = Uuid::now_v7();
    let created: Option<(Uuid,)> = sqlx::query_as(
        "insert into import (id, tenant_id, file_sha256, format, source) values ($1, $2, $3, $4, $5)
         on conflict (tenant_id, file_sha256) do nothing returning id",
    )
    .bind(import_id)
    .bind(tenant)
    .bind(sha)
    .bind(format)
    .bind(source)
    .fetch_optional(&mut *tx)
    .await?;
    if created.is_none() {
        let (stats,): (serde_json::Value,) =
            sqlx::query_as("select stats from import where tenant_id = $1 and file_sha256 = $2")
                .bind(tenant)
                .bind(sha)
                .fetch_one(&mut *tx)
                .await?;
        tx.commit().await?;
        return serde_json::from_value(stats).map_err(|e| ApiError::Internal(e.into()));
    }

    let tenant_str = tenant.to_string();
    let mut items = Vec::new();
    for row in &rows {
        let cp_ref = match (&row.inn, row.counterparty_name.expose()) {
            (Some(inn), _) => Some(inn.counterparty_ref(&tenant_str, &state.hmac_key)),
            (None, name) if !name.is_empty() => Some(counterparty_ref(&tenant_str, name, &state.hmac_key)),
            _ => None,
        };
        let id = Uuid::now_v7();
        let inserted: Option<(Uuid,)> = sqlx::query_as(
            "insert into transaction (id, tenant_id, import_id, op_date, amount_kop, counterparty_ref, counterparty_inn,
                                      counterparty_name, purpose, balance_eod_kop, dedup_key)
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
             on conflict (tenant_id, dedup_key) do nothing returning id",
        )
        .bind(id)
        .bind(tenant)
        .bind(import_id)
        .bind(row.op_date)
        .bind(row.amount.0)
        .bind(&cp_ref)
        .bind(row.inn.as_ref().map(|i| i.expose()))
        .bind(row.counterparty_name.expose())
        .bind(row.purpose.expose())
        .bind(row.balance_eod.0)
        .bind(dedup_key(row))
        .fetch_optional(&mut *tx)
        .await?;
        if inserted.is_some() {
            items.push(IngestedItem {
                id,
                op_date: row.op_date,
                amount_kop: row.amount.0,
                counterparty_ref: cp_ref,
                counterparty_name: row.counterparty_name.expose().to_owned(),
                purpose: row.purpose.expose().to_owned(),
                has_inn: row.inn.is_some(),
                balance_eod_kop: row.balance_eod.0,
            });
        }
    }

    let inserted = items.len();
    let mut chunk = Vec::with_capacity(EVENT_CHUNK);
    for item in items {
        chunk.push(item);
        if chunk.len() == EVENT_CHUNK {
            let data = IngestedData {
                import_id,
                items: std::mem::take(&mut chunk),
            };
            events::enqueue(&mut tx, SCHEMA, &Envelope::new("transactions.ingested", tenant, data)).await?;
        }
    }
    if !chunk.is_empty() {
        events::enqueue(
            &mut tx,
            SCHEMA,
            &Envelope::new(
                "transactions.ingested",
                tenant,
                IngestedData {
                    import_id,
                    items: chunk,
                },
            ),
        )
        .await?;
    }

    let stats = ImportStats {
        import_id,
        format: format.to_owned(),
        rows: rows.len(),
        inserted,
        duplicates: rows.len() - inserted,
        date_from: rows.iter().map(|r| r.op_date).min(),
        date_to: rows.iter().map(|r| r.op_date).max(),
        balance_gaps: balance_gaps(&mut tx, tenant).await?,
    };
    sqlx::query("update import set stats = $1 where id = $2")
        .bind(serde_json::to_value(&stats).map_err(|e| ApiError::Internal(e.into()))?)
        .bind(import_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    tracing::info!(%import_id, rows = stats.rows, inserted, "statement stored");
    Ok(stats)
}

/// Days where yesterday's balance plus today's flows does not give today's balance:
/// operations are missing from the statement between those dates.
async fn balance_gaps(tx: &mut sqlx::Transaction<'static, sqlx::Postgres>, tenant: Uuid) -> ApiResult<Vec<BalanceGap>> {
    let days: Vec<(NaiveDate, i64, i64)> = sqlx::query_as(
        "select op_date, sum(amount_kop)::bigint, max(balance_eod_kop) from transaction
         where tenant_id = $1 group by op_date order by op_date",
    )
    .bind(tenant)
    .fetch_all(&mut **tx)
    .await?;
    Ok(find_gaps(&days))
}

fn find_gaps(days: &[(NaiveDate, i64, i64)]) -> Vec<BalanceGap> {
    days.windows(2)
        .filter_map(|w| {
            let (prev_date, _, prev_balance) = w[0];
            let (date, flow, balance) = w[1];
            let diff = balance - (prev_balance + flow);
            (diff != 0).then_some(BalanceGap {
                after: prev_date,
                before: date,
                missing_kop: diff,
            })
        })
        .collect()
}

async fn get_import(
    State(state): State<AppState>,
    user: AuthUser,
    Path(id): Path<Uuid>,
) -> ApiResult<Json<serde_json::Value>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let (stats,): (serde_json::Value,) = sqlx::query_as("select stats from import where id = $1")
        .bind(id)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(stats))
}

#[derive(Deserialize)]
struct ListQuery {
    from: Option<NaiveDate>,
    to: Option<NaiveDate>,
    limit: Option<i64>,
    offset: Option<i64>,
}

#[derive(Serialize, sqlx::FromRow)]
struct TransactionView {
    id: Uuid,
    op_date: NaiveDate,
    amount_kop: i64,
    counterparty_ref: Option<String>,
    counterparty_name: Option<String>,
    purpose: Option<String>,
    balance_eod_kop: i64,
    category_id: Option<String>,
    category_group: Option<String>,
}

async fn list_transactions(
    State(state): State<AppState>,
    user: AuthUser,
    Query(q): Query<ListQuery>,
) -> ApiResult<Json<serde_json::Value>> {
    let mut tx = db::tenant_tx(&state.pool, user.tenant_id).await?;
    let rows: Vec<TransactionView> = sqlx::query_as(
        "select id, op_date, amount_kop, counterparty_ref, counterparty_name, purpose, balance_eod_kop, category_id, category_group
         from transaction
         where ($1::date is null or op_date >= $1) and ($2::date is null or op_date <= $2)
         order by op_date desc, id desc limit $3 offset $4",
    )
    .bind(q.from)
    .bind(q.to)
    .bind(q.limit.unwrap_or(200).clamp(1, 1000))
    .bind(q.offset.unwrap_or(0).max(0))
    .fetch_all(&mut *tx)
    .await?;
    let (total,): (i64,) = sqlx::query_as("select count(*) from transaction")
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Json(json!({ "total": total, "items": rows })))
}

#[derive(Deserialize)]
struct CategorizedItem {
    id: Uuid,
    category_id: String,
    group: String,
}

#[derive(Deserialize)]
struct CategorizedData {
    items: Vec<CategorizedItem>,
}

/// Keeps a read-only copy of categories so the operations list needs one call.
async fn apply_categories(pool: &PgPool, env: Envelope<CategorizedData>) -> Result<()> {
    let mut tx = db::tenant_tx(pool, env.tenant_id).await?;
    if !events::first_delivery(&mut tx, SCHEMA, env.id).await? {
        return Ok(());
    }
    for item in env.data.items {
        sqlx::query("update transaction set category_id = $1, category_group = $2 where id = $3")
            .bind(&item.category_id)
            .bind(&item.group)
            .bind(item.id)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ci_domain::Kopecks;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn continuous_balances_have_no_gaps() {
        let days = [(d("2024-10-01"), -100, 900), (d("2024-10-02"), 50, 950)];
        assert!(find_gaps(&days).is_empty());
    }

    #[test]
    fn jump_in_balance_is_reported_as_gap() {
        let days = [(d("2024-10-01"), 0, 1000), (d("2024-10-10"), 0, 700)];
        let gaps = find_gaps(&days);
        assert_eq!(gaps.len(), 1);
        assert_eq!(gaps[0].missing_kop, -300);
    }

    #[test]
    fn dedup_key_ignores_whitespace_and_case_in_purpose() {
        let mk = |p: &str| ParsedRow {
            op_date: d("2024-10-01"),
            amount: Kopecks(100),
            inn: None,
            counterparty_name: ci_domain::Sensitive::new("A"),
            purpose: ci_domain::Sensitive::new(p),
            balance_eod: Kopecks(0),
        };
        assert_eq!(dedup_key(&mk("Оплата  ЕНП")), dedup_key(&mk("оплата енп")));
    }
}
