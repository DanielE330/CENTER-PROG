//! Certificates from step-ca: a server config that reloads the leaf when the
//! cert-agent sidecar renews it, and helpers to read SPIFFE IDs from peers.

use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime},
};

use anyhow::{Context, Result, anyhow};
use arc_swap::ArcSwap;
use rustls::{
    RootCertStore, ServerConfig,
    server::{ClientHello, ResolvesServerCert, WebPkiClientVerifier},
    sign::CertifiedKey,
};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};

pub const SPIFFE_PREFIX: &str = "spiffe://ci1/svc/";
const RELOAD_CHECK: Duration = Duration::from_secs(15);

#[derive(Clone, Debug)]
pub struct CertPaths {
    pub cert: PathBuf,
    pub key: PathBuf,
    pub ca: PathBuf,
}

impl CertPaths {
    pub fn in_dir(dir: &Path) -> Self {
        Self {
            cert: dir.join("cert.pem"),
            key: dir.join("key.pem"),
            ca: dir.join("ca.pem"),
        }
    }
}

pub fn ensure_crypto_provider() {
    // Installing twice returns Err, which is fine: some other code got there first.
    let _ = rustls::crypto::ring::default_provider().install_default();
}

pub fn load_certs(path: &Path) -> Result<Vec<CertificateDer<'static>>> {
    let certs = CertificateDer::pem_file_iter(path)
        .with_context(|| format!("reading {}", path.display()))?
        .collect::<Result<Vec<_>, _>>()
        .with_context(|| format!("parsing {}", path.display()))?;
    if certs.is_empty() {
        return Err(anyhow!("no certificates in {}", path.display()));
    }
    Ok(certs)
}

pub fn load_key(path: &Path) -> Result<PrivateKeyDer<'static>> {
    PrivateKeyDer::from_pem_file(path).with_context(|| format!("reading key {}", path.display()))
}

pub fn root_store(ca: &Path) -> Result<RootCertStore> {
    let mut roots = RootCertStore::empty();
    for cert in load_certs(ca)? {
        roots.add(cert).context("adding root certificate")?;
    }
    Ok(roots)
}

fn certified_key(paths: &CertPaths) -> Result<CertifiedKey> {
    let certs = load_certs(&paths.cert)?;
    let key = load_key(&paths.key)?;
    let signing = rustls::crypto::ring::sign::any_supported_type(&key).context("unsupported key type")?;
    Ok(CertifiedKey::new(certs, signing))
}

fn mtime(path: &Path) -> Option<SystemTime> {
    fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Serves the current leaf certificate and re-reads it after renewal.
#[derive(Debug)]
pub struct ReloadingResolver {
    paths: CertPaths,
    current: ArcSwap<CertifiedKey>,
    state: Mutex<(Instant, Option<SystemTime>)>,
}

impl ReloadingResolver {
    pub fn new(paths: CertPaths) -> Result<Self> {
        let key = certified_key(&paths)?;
        let stamp = mtime(&paths.cert);
        Ok(Self {
            current: ArcSwap::from_pointee(key),
            state: Mutex::new((Instant::now(), stamp)),
            paths,
        })
    }

    fn maybe_reload(&self) {
        let Ok(mut state) = self.state.lock() else { return };
        if state.0.elapsed() < RELOAD_CHECK {
            return;
        }
        state.0 = Instant::now();
        let stamp = mtime(&self.paths.cert);
        if stamp == state.1 {
            return;
        }
        match certified_key(&self.paths) {
            Ok(key) => {
                self.current.store(Arc::new(key));
                state.1 = stamp;
                tracing::info!("server certificate reloaded");
            }
            // A half-written file during renewal: keep serving the old one and retry later.
            Err(err) => tracing::warn!(error = %err, "certificate reload failed"),
        }
    }
}

impl ResolvesServerCert for ReloadingResolver {
    fn resolve(&self, _hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        self.maybe_reload();
        Some(self.current.load_full())
    }
}

/// TLS server config that requires a client certificate issued by our CA.
pub fn mtls_server_config(paths: &CertPaths) -> Result<Arc<ServerConfig>> {
    ensure_crypto_provider();
    let verifier = WebPkiClientVerifier::builder(Arc::new(root_store(&paths.ca)?))
        .build()
        .context("client verifier")?;
    let mut config = ServerConfig::builder()
        .with_client_cert_verifier(verifier)
        .with_cert_resolver(Arc::new(ReloadingResolver::new(paths.clone())?));
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(Arc::new(config))
}

/// Service name from a `spiffe://ci1/svc/<name>` URI SAN of a DER certificate.
pub fn spiffe_service(cert: &CertificateDer<'_>) -> Option<String> {
    let (_, parsed) = x509_parser::parse_x509_certificate(cert.as_ref()).ok()?;
    let san = parsed.subject_alternative_name().ok()??;
    san.value.general_names.iter().find_map(|name| match name {
        x509_parser::extensions::GeneralName::URI(uri) => uri.strip_prefix(SPIFFE_PREFIX).map(str::to_owned),
        _ => None,
    })
}

/// Seconds until the leaf certificate expires; exported as a metric and logged.
pub fn seconds_until_expiry(path: &Path) -> Result<i64> {
    let certs = load_certs(path)?;
    let first = certs.first().ok_or_else(|| anyhow!("empty chain"))?;
    let (_, parsed) = x509_parser::parse_x509_certificate(first.as_ref()).map_err(|e| anyhow!("{e}"))?;
    let not_after = parsed.validity().not_after.timestamp();
    Ok(not_after - chrono::Utc::now().timestamp())
}
