//! HTTP client that presents this service's certificate and trusts only our CA.
//! It is rebuilt when the cert-agent renews the certificate.

use std::{fs, path::Path, sync::Arc, time::SystemTime};

use anyhow::{Context, Result};
use arc_swap::ArcSwap;

use crate::tls::CertPaths;

struct Built {
    client: reqwest::Client,
    stamp: Option<SystemTime>,
}

#[derive(Clone)]
pub struct MtlsClient {
    paths: CertPaths,
    inner: Arc<ArcSwap<Built>>,
}

fn stamp(path: &Path) -> Option<SystemTime> {
    fs::metadata(path).and_then(|m| m.modified()).ok()
}

fn build(paths: &CertPaths) -> Result<reqwest::Client> {
    let mut identity_pem = fs::read(&paths.cert).context("reading client cert")?;
    identity_pem.extend_from_slice(b"\n");
    identity_pem.extend(fs::read(&paths.key).context("reading client key")?);
    let identity = reqwest::Identity::from_pem(&identity_pem).context("client identity")?;
    let ca = reqwest::Certificate::from_pem(&fs::read(&paths.ca).context("reading CA")?).context("CA")?;
    reqwest::Client::builder()
        .use_rustls_tls()
        .tls_built_in_root_certs(false)
        .add_root_certificate(ca)
        .identity(identity)
        .https_only(true)
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .context("building mTLS client")
}

impl MtlsClient {
    pub fn new(paths: CertPaths) -> Result<Self> {
        crate::tls::ensure_crypto_provider();
        let client = build(&paths)?;
        Ok(Self {
            inner: Arc::new(ArcSwap::from_pointee(Built {
                client,
                stamp: stamp(&paths.cert),
            })),
            paths,
        })
    }

    pub fn from_dir(dir: &Path) -> Result<Self> {
        Self::new(CertPaths::in_dir(dir))
    }

    /// Current client; cheap to call per request.
    pub fn get(&self) -> reqwest::Client {
        let current = self.inner.load();
        let now = stamp(&self.paths.cert);
        if now != current.stamp {
            match build(&self.paths) {
                Ok(client) => {
                    self.inner.store(Arc::new(Built {
                        client: client.clone(),
                        stamp: now,
                    }));
                    tracing::info!("client certificate reloaded");
                    return client;
                }
                Err(err) => tracing::warn!(error = %err, "client certificate reload failed"),
            }
        }
        current.client.clone()
    }
}
