//! Shared runtime for the Rust services: mTLS server and client, JWT checks,
//! tenant-scoped database transactions, events with an outbox, logging.

pub mod auth;
pub mod client;
pub mod config;
pub mod db;
pub mod error;
pub mod events;
pub mod server;
pub mod telemetry;
pub mod tls;

pub use error::ApiError;
