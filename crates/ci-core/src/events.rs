//! Events over NATS JetStream. Rust services publish through an outbox table
//! written in the same transaction as the data; consumers are idempotent.

use std::{future::Future, path::Path, time::Duration};

use anyhow::{Context, Result};
use async_nats::{
    HeaderMap,
    jetstream::{self, consumer::pull, stream},
};
use chrono::{DateTime, Utc};
use futures::StreamExt;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sqlx::{PgPool, Postgres, Transaction};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Envelope<T> {
    pub id: Uuid,
    #[serde(rename = "type")]
    pub kind: String,
    pub schema: u32,
    pub tenant_id: Uuid,
    pub occurred_at: DateTime<Utc>,
    pub data: T,
}

impl<T> Envelope<T> {
    pub fn new(kind: &str, tenant_id: Uuid, data: T) -> Self {
        Self {
            id: Uuid::now_v7(),
            kind: kind.to_owned(),
            schema: 1,
            tenant_id,
            occurred_at: Utc::now(),
            data,
        }
    }
}

pub fn subject(kind: &str) -> String {
    format!("ci.v1.{kind}")
}

pub async fn connect(url: &str, cert_dir: &Path) -> Result<jetstream::Context> {
    Ok(jetstream::new(connect_client(url, cert_dir).await?))
}

/// Plain NATS client, for subscribers that only need core pub/sub (the SSE stream).
pub async fn connect_client(url: &str, cert_dir: &Path) -> Result<async_nats::Client> {
    let mut attempt = 0;
    let client = loop {
        let opts = async_nats::ConnectOptions::new()
            .require_tls(true)
            .add_root_certificates(cert_dir.join("ca.pem"))
            .add_client_certificate(cert_dir.join("cert.pem"), cert_dir.join("key.pem"))
            .retry_on_initial_connect();
        match tokio::time::timeout(Duration::from_secs(20), opts.connect(url)).await {
            Ok(Ok(client)) => break client,
            Ok(Err(err)) if attempt < 30 => tracing::warn!(error = %err, "NATS not ready"),
            Err(_) if attempt < 30 => tracing::warn!("NATS connect timeout"),
            Ok(Err(err)) => return Err(err.into()),
            Err(_) => anyhow::bail!("NATS connect timeout"),
        }
        attempt += 1;
        tokio::time::sleep(Duration::from_secs(2)).await;
    };
    Ok(client)
}

/// Streams are created by whoever publishes to them; creation is idempotent.
pub async fn ensure_stream(js: &jetstream::Context, name: &str, subjects: &[&str]) -> Result<()> {
    js.get_or_create_stream(stream::Config {
        name: name.to_owned(),
        subjects: subjects.iter().map(|s| (*s).to_owned()).collect(),
        max_age: Duration::from_secs(14 * 24 * 3600),
        duplicate_window: Duration::from_secs(120),
        ..Default::default()
    })
    .await
    .map_err(|e| anyhow::anyhow!("stream {name}: {e}"))?;
    Ok(())
}

/// Writes an event to `<schema>.outbox` inside the caller's transaction.
pub async fn enqueue<T: Serialize>(
    tx: &mut Transaction<'static, Postgres>,
    schema: &str,
    envelope: &Envelope<T>,
) -> Result<()> {
    let payload = serde_json::to_value(envelope)?;
    sqlx::query(&format!(
        "insert into {schema}.outbox (id, tenant_id, subject, payload) values ($1, $2, $3, $4)"
    ))
    .bind(envelope.id)
    .bind(envelope.tenant_id)
    .bind(subject(&envelope.kind))
    .bind(payload)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// Background task: publishes outbox rows in order. `Nats-Msg-Id` lets the
/// stream drop duplicates if we crash between publish and the update.
pub async fn run_outbox_relay(pool: PgPool, js: jetstream::Context, schema: String) {
    let select = format!(
        "select id, subject, payload from {schema}.outbox where published_at is null order by created_at limit 100"
    );
    let mark = format!("update {schema}.outbox set published_at = now() where id = $1");
    loop {
        match relay_once(&pool, &js, &select, &mark).await {
            Ok(0) => tokio::time::sleep(Duration::from_millis(500)).await,
            Ok(_) => {}
            Err(err) => {
                tracing::warn!(error = %err, "outbox relay failed");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
    }
}

async fn relay_once(pool: &PgPool, js: &jetstream::Context, select: &str, mark: &str) -> Result<usize> {
    // The relay runs as the service role; outbox has no RLS (it carries tenant_id inside).
    let rows: Vec<(Uuid, String, serde_json::Value)> = sqlx::query_as(select).fetch_all(pool).await?;
    for (id, subject, payload) in &rows {
        let mut headers = HeaderMap::new();
        headers.insert("Nats-Msg-Id", id.to_string().as_str());
        js.publish_with_headers(subject.clone(), headers, serde_json::to_vec(payload)?.into())
            .await?
            .await
            .context("publish ack")?;
        sqlx::query(mark).bind(id).execute(pool).await?;
    }
    Ok(rows.len())
}

/// Durable pull consumer. The handler must be idempotent: delivery is at-least-once.
pub async fn consume<T, F, Fut>(
    js: jetstream::Context,
    stream_name: &str,
    durable: &str,
    filter: &str,
    handler: F,
) -> Result<()>
where
    T: DeserializeOwned,
    F: Fn(Envelope<T>) -> Fut,
    Fut: Future<Output = Result<()>>,
{
    // Events that failed five times land here instead of blocking the stream.
    ensure_stream(&js, "DLQ", &["ci.dlq.>"]).await?;
    let stream = loop {
        match js.get_stream(stream_name).await {
            Ok(s) => break s,
            Err(err) => {
                tracing::info!(stream = stream_name, error = %err, "waiting for stream");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
    };
    let consumer = stream
        .get_or_create_consumer(
            durable,
            pull::Config {
                durable_name: Some(durable.to_owned()),
                filter_subject: filter.to_owned(),
                ack_wait: Duration::from_secs(60),
                max_deliver: 5,
                ..Default::default()
            },
        )
        .await
        .map_err(|e| anyhow::anyhow!("consumer {durable}: {e}"))?;
    let mut messages = consumer.messages().await.map_err(|e| anyhow::anyhow!("{e}"))?;
    while let Some(msg) = messages.next().await {
        let msg = match msg {
            Ok(m) => m,
            Err(err) => {
                tracing::warn!(error = %err, "consumer error");
                continue;
            }
        };
        let delivered = msg.info().map(|i| i.delivered).unwrap_or(1);
        let result = match serde_json::from_slice::<Envelope<T>>(&msg.payload) {
            Ok(env) => handler(env).await,
            Err(err) => Err(anyhow::anyhow!("bad payload: {err}")),
        };
        match result {
            Ok(()) => {
                let _ = msg.ack().await;
            }
            Err(err) if delivered >= 5 => {
                tracing::error!(error = %err, subject = %msg.subject, "giving up, moving to DLQ");
                let _ = js.publish(format!("ci.dlq.{durable}"), msg.payload.clone()).await;
                let _ = msg.ack().await;
            }
            Err(err) => {
                tracing::warn!(error = %err, delivered, "handler failed, will retry");
                let _ = msg
                    .ack_with(jetstream::AckKind::Nak(Some(Duration::from_secs(5))))
                    .await;
            }
        }
    }
    Ok(())
}

/// Returns false when this event was already handled (inside the same transaction).
pub async fn first_delivery(tx: &mut Transaction<'static, Postgres>, schema: &str, id: Uuid) -> Result<bool> {
    let res = sqlx::query(&format!(
        "insert into {schema}.processed_event (id) values ($1) on conflict do nothing"
    ))
    .bind(id)
    .execute(&mut **tx)
    .await?;
    Ok(res.rows_affected() == 1)
}
