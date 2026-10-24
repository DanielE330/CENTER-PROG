//! Postgres over mTLS (no passwords: the role is the certificate CN) and
//! transactions pinned to one tenant for row-level security.

use std::{path::Path, str::FromStr, time::Duration};

use anyhow::{Context, Result};
use sqlx::{
    PgPool, Postgres, Transaction,
    postgres::{PgConnectOptions, PgPoolOptions, PgSslMode},
};
use uuid::Uuid;

pub async fn connect(database_url: &str, cert_dir: &Path) -> Result<PgPool> {
    let options = PgConnectOptions::from_str(database_url)
        .context("DATABASE_URL")?
        .ssl_mode(PgSslMode::VerifyFull)
        .ssl_root_cert(cert_dir.join("ca.pem"))
        .ssl_client_cert(cert_dir.join("cert.pem"))
        .ssl_client_key(cert_dir.join("key.pem"));
    PgPoolOptions::new()
        .max_connections(8)
        // Recycled connections pick up renewed certificates.
        .max_lifetime(Duration::from_secs(1800))
        .acquire_timeout(Duration::from_secs(10))
        .connect_with(options)
        .await
        .context("connecting to Postgres")
}

/// Waits for Postgres to accept connections; services start before it is ready.
pub async fn connect_with_retry(database_url: &str, cert_dir: &Path) -> Result<PgPool> {
    let mut attempt = 0;
    loop {
        match connect(database_url, cert_dir).await {
            Ok(pool) => return Ok(pool),
            Err(err) if attempt < 30 => {
                attempt += 1;
                tracing::warn!(error = %err, attempt, "database not ready");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            Err(err) => return Err(err),
        }
    }
}

/// Opens a transaction where every RLS policy sees only `tenant`.
pub async fn tenant_tx(pool: &PgPool, tenant: Uuid) -> Result<Transaction<'static, Postgres>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query("select set_config('app.tenant_id', $1, true)")
        .bind(tenant.to_string())
        .execute(&mut *tx)
        .await?;
    Ok(tx)
}
