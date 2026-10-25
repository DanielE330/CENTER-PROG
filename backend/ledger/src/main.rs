//! ledger: единственный владелец ledger.db — компании, импорты (staging + parser), операции,
//! ожидаемые события, ревизия данных и аудит. Внутренний HTTP API (без cookie-сессий).

mod admin;
mod counterparties;
mod integrations;
mod settings;
mod db;
mod members;
mod routes;
#[cfg(test)]
mod tests;

use axum::{
    Router,
    extract::DefaultBodyLimit,
    routing::{get, patch, post},
};
use common::{Upstream, mw};

#[derive(Clone)]
pub struct AppState {
    pub db: db::Db,
    pub parser: Upstream,
    /// главный ключ шифрования паролей интеграций (`INTEGRATIONS_KEY`); без него пароли не принимаются
    pub integrations_key: Option<[u8; 32]>,
}

#[tokio::main]
async fn main() {
    let st = AppState {
        db: db::open(&mw::env("DB_PATH", "data/run/ledger.db")),
        parser: Upstream::new("parser", "Сервис разбора", &mw::env("PARSER_URL", "http://127.0.0.1:8002"), 60),
        integrations_key: integrations::master_key(&mw::env("INTEGRATIONS_KEY", "")),
    };
    if st.integrations_key.is_none() {
        eprintln!("INTEGRATIONS_KEY не задан или короче 16 символов: подключения интеграций (1С) принимать пароли не будут");
    }
    let app = build(st);
    mw::serve("ledger", &mw::env("INTERNAL_ADDR", "127.0.0.1:3002"), app).await;
}

pub fn build(st: AppState) -> Router {
    use routes::*;
    let co = "/internal/v1/companies/{cid}";
    Router::new()
        .route("/internal/v1/companies/ensure", post(ensure_company))
        .route("/internal/v1/members/by-user/{uid}", get(members::by_user))
        .route("/internal/v1/admin/companies", get(admin::list_companies))
        .route("/internal/v1/admin/companies/{cid}", get(admin::get_company))
        .route("/internal/v1/admin/companies/{cid}/ban", post(admin::ban))
        .route("/internal/v1/admin/companies/{cid}/unban", post(admin::unban))
        .route("/internal/v1/admin/stats", get(admin::stats))
        .route(&format!("{co}/members"), get(members::list_members).post(members::add_member))
        .route(&format!("{co}/members/join"), post(members::join_member))
        .route(&format!("{co}/transfer"), post(members::transfer))
        .route(&format!("{co}/members/{{uid}}"), patch(members::patch_member).delete(members::delete_member))
        .route(co, get(get_company).patch(update_company))
        .route(&format!("{co}/snapshot"), get(snapshot))
        .route(&format!("{co}/imports"), get(list_imports).post(upload_import).layer(DefaultBodyLimit::max(26 * 1024 * 1024)))
        .route(&format!("{co}/imports/{{iid}}"), get(get_import).delete(delete_import))
        .route(&format!("{co}/imports/{{iid}}/confirm"), post(confirm_import))
        .route(&format!("{co}/transactions"), get(list_transactions))
        .route(&format!("{co}/transactions/{{id}}/category"), patch(set_category))
        .route(&format!("{co}/taxonomy"), get(get_taxonomy))
        .route(&format!("{co}/expected-events"), get(list_events).post(create_event))
        .route(&format!("{co}/expected-events/bulk"), post(bulk_events))
        .route(&format!("{co}/expected-events/{{eid}}"), axum::routing::delete(delete_event))
        .route(&format!("{co}/audit"), post(audit_event).get(list_audit))
        // флаги контрагентов по ИНН (проверка ФНС) и топ контрагентов по обороту
        .route(&format!("{co}/counterparty-risk"), get(counterparties::list_risk).put(counterparties::put_risk))
        .route(&format!("{co}/counterparties/top"), get(counterparties::top))
        // настройки компании по разделам (fns, advisor)
        .route(&format!("{co}/settings/{{key}}"), get(settings::get_settings).put(settings::put_settings))
        // подключения интеграций компании (1С): пароль шифруется, расшифрованные реквизиты — только для integrator
        .route(&format!("{co}/integrations"), get(integrations::list))
        .route(&format!("{co}/integrations/onec"), axum::routing::put(integrations::put_onec).delete(integrations::delete_onec))
        .route(&format!("{co}/integrations/onec/status"), axum::routing::put(integrations::put_status))
        .route(&format!("{co}/integrations/onec/credentials"), get(integrations::credentials))
        .with_state(st)
}
