use tracing_subscriber::{EnvFilter, fmt, prelude::*};

/// JSON logs to stdout. What may appear in them is controlled by types:
/// sensitive values use `ci_domain::Sensitive`/`Inn`, which mask themselves.
pub fn init(service: &str) {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::registry()
        .with(filter)
        .with(fmt::layer().json().with_current_span(false).with_target(false))
        .init();
    tracing::info!(service, "starting");
}
