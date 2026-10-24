//! Общий крейт: типы данных, деньги, ошибки API и клиент межсервисных вызовов.
//! Бизнес-логики здесь нет.

pub mod client;
pub mod error;
pub mod money;
pub mod mw;
pub mod types;

pub use client::{Ctx, Reply, Upstream};
pub use error::{ApiError, R, err, error_response, internal};
pub use money::{money, stringify_money};
pub use types::{CounterpartyRisk, Event, HistoryRow, Snapshot, Tx};
