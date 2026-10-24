//! forecast: расчёт прогноза остатка, риска разрыва, сценариев и backtest.
//! Владеет таблицами forecast_runs, forecast_settings и fitted_models (forecast.db; последняя — кэш обученной модели
//! для режима «Обновлять по новым данным»). Историю и события берёт у ledger по HTTP,
//! ревизию данных для признака stale сравнивает с ledger.

mod routes;

use axum::{
    Router,
    routing::{get, post},
};
use common::{Upstream, mw};
use rusqlite::Connection;
use std::sync::{Arc, Mutex};

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Mutex<Connection>>,
    pub ledger: Upstream,
}

fn open_db(path: &str) -> Arc<Mutex<Connection>> {
    if let Some(dir) = std::path::Path::new(path).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let conn = Connection::open(path).expect("open db");
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS forecast_runs(
            id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
            created_at TEXT NOT NULL, data_revision INTEGER NOT NULL,
            scenario TEXT, params TEXT NOT NULL, result TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_company ON forecast_runs(company_id, created_at);
        CREATE TABLE IF NOT EXISTS forecast_settings(
            company_id TEXT PRIMARY KEY, update_mode TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fitted_models(
            company_id TEXT PRIMARY KEY, fit_revision INTEGER NOT NULL, full_fit_at TEXT NOT NULL,
            full_fit_ops INTEGER NOT NULL, ops_added INTEGER NOT NULL, model_version TEXT NOT NULL, blob TEXT NOT NULL);",
    )
    .expect("migrate");
    Arc::new(Mutex::new(conn))
}

#[tokio::main]
async fn main() {
    let st = AppState {
        db: open_db(&mw::env("DB_PATH", "data/run/forecast.db")),
        ledger: Upstream::new("ledger", "Сервис данных", &mw::env("LEDGER_URL", "http://127.0.0.1:3002"), 60),
    };
    let co = "/internal/v1/companies/{cid}";
    let app = Router::new()
        .route(&format!("{co}/forecasts"), post(routes::create_forecast))
        .route(&format!("{co}/forecasts/latest"), get(routes::latest_forecast))
        .route(&format!("{co}/forecasts/{{rid}}"), get(routes::get_forecast))
        .route(&format!("{co}/scenarios"), post(routes::create_scenario))
        .route(&format!("{co}/backtest"), get(routes::run_backtest))
        .route(&format!("{co}/summary"), get(routes::summary))
        .route(&format!("{co}/rules"), get(routes::rules))
        .route(&format!("{co}/forecast-settings"), get(routes::get_settings).put(routes::put_settings))
        .with_state(st);
    mw::serve("forecast", &mw::env("INTERNAL_ADDR", "127.0.0.1:3003"), app).await;
}
