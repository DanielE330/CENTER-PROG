use std::{collections::HashSet, env, net::SocketAddr, path::PathBuf};

use anyhow::{Context, Result};

/// Settings every service reads from the environment.
#[derive(Clone, Debug)]
pub struct CoreConfig {
    pub service: String,
    pub listen: SocketAddr,
    pub cert_dir: PathBuf,
    pub allowed_clients: HashSet<String>,
    pub database_url: Option<String>,
    pub nats_url: Option<String>,
    pub oidc_issuer: Option<String>,
    pub oidc_jwks_url: Option<String>,
    pub oidc_audience: String,
}

impl CoreConfig {
    pub fn from_env() -> Result<Self> {
        let service = env::var("SERVICE_NAME").context("SERVICE_NAME is required")?;
        let listen = env::var("LISTEN_ADDR")
            .unwrap_or_else(|_| "0.0.0.0:8443".into())
            .parse()
            .context("LISTEN_ADDR")?;
        let mut allowed_clients: HashSet<String> = env::var("ALLOWED_CLIENTS")
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        // A service may always call itself: the container healthcheck does exactly that.
        allowed_clients.insert(service.clone());
        Ok(Self {
            listen,
            cert_dir: env::var("CERT_DIR").unwrap_or_else(|_| "/certs".into()).into(),
            allowed_clients,
            database_url: env::var("DATABASE_URL").ok(),
            nats_url: env::var("NATS_URL").ok(),
            oidc_issuer: env::var("OIDC_ISSUER").ok(),
            oidc_jwks_url: env::var("OIDC_JWKS_URL").ok(),
            oidc_audience: env::var("OIDC_AUDIENCE").unwrap_or_else(|_| "ci1-api".into()),
            service,
        })
    }

    pub fn require<'a>(value: &'a Option<String>, name: &str) -> Result<&'a str> {
        value.as_deref().with_context(|| format!("{name} is required"))
    }
}

/// Reads a secret mounted as a file (`/run/secrets/<name>`), never from plain env.
pub fn secret(name: &str) -> Result<String> {
    let dir = env::var("SECRETS_DIR").unwrap_or_else(|_| "/run/secrets".into());
    let path = PathBuf::from(dir).join(name);
    let value = std::fs::read_to_string(&path).with_context(|| format!("secret {name} is missing"))?;
    Ok(value.trim().to_owned())
}
