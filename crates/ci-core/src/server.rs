//! HTTPS server with mandatory client certificates. The caller's SPIFFE ID is
//! checked against the allow-list from `services.yaml` before any HTTP is read.

use std::{collections::HashSet, net::SocketAddr, sync::Arc, time::Duration};

use anyhow::Result;
use axum::{Router, extract::Request};
use hyper_util::{
    rt::{TokioExecutor, TokioIo},
    server::conn::auto::Builder,
    service::TowerToHyperService,
};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;
use tower::ServiceExt;

use crate::{
    config::CoreConfig,
    tls::{CertPaths, mtls_server_config, spiffe_service},
};

/// SPIFFE service name of the peer that opened this connection.
#[derive(Clone, Debug)]
pub struct Peer(pub String);

pub async fn serve(config: &CoreConfig, app: Router) -> Result<()> {
    let tls = mtls_server_config(&CertPaths::in_dir(&config.cert_dir))?;
    serve_with(config.listen, tls, Arc::new(config.allowed_clients.clone()), app).await
}

pub async fn serve_with(
    addr: SocketAddr,
    tls: Arc<rustls::ServerConfig>,
    allowed: Arc<HashSet<String>>,
    app: Router,
) -> Result<()> {
    let listener = TcpListener::bind(addr).await?;
    let acceptor = TlsAcceptor::from(tls);
    tracing::info!(%addr, "listening with mTLS");
    loop {
        let (tcp, remote) = match listener.accept().await {
            Ok(conn) => conn,
            Err(err) => {
                tracing::warn!(error = %err, "accept failed");
                continue;
            }
        };
        let acceptor = acceptor.clone();
        let allowed = Arc::clone(&allowed);
        let app = app.clone();
        tokio::spawn(async move {
            let stream = match tokio::time::timeout(Duration::from_secs(10), acceptor.accept(tcp)).await {
                Ok(Ok(stream)) => stream,
                Ok(Err(err)) => {
                    tracing::debug!(%remote, error = %err, "TLS handshake failed");
                    return;
                }
                Err(_) => return,
            };
            let peer = stream
                .get_ref()
                .1
                .peer_certificates()
                .and_then(|chain| chain.first())
                .and_then(spiffe_service);
            let Some(peer) = peer.filter(|p| allowed.contains(p)) else {
                tracing::warn!(%remote, "client certificate not in allow-list, closing");
                return;
            };
            let svc = app.map_request(move |mut req: Request<hyper::body::Incoming>| {
                req.extensions_mut().insert(Peer(peer.clone()));
                req.map(axum::body::Body::new)
            });
            let result = Builder::new(TokioExecutor::new())
                .serve_connection_with_upgrades(TokioIo::new(stream), TowerToHyperService::new(svc))
                .await;
            if let Err(err) = result {
                tracing::debug!(%remote, error = %err, "connection ended with error");
            }
        });
    }
}

/// Health endpoint plus request tracing and body limits shared by all services.
pub fn base_router(router: Router, body_limit: usize) -> Router {
    use tower_http::{limit::RequestBodyLimitLayer, trace::TraceLayer};
    router
        .route("/healthz", axum::routing::get(|| async { "ok" }))
        .layer(RequestBodyLimitLayer::new(body_limit))
        .layer(
            TraceLayer::new_for_http()
                // Only method and path: query strings may carry identifiers.
                .make_span_with(
                    |req: &Request| tracing::info_span!("http", method = %req.method(), path = %req.uri().path()),
                ),
        )
}
