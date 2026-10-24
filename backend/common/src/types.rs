//! Структуры, которыми обмениваются сервисы. Суммы — целые копейки (знак: + поступление, − списание).

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Tx {
    pub date: NaiveDate,
    pub amount: i64,
    pub counterparty: String,
    pub inn: String,
    pub purpose: String,
    pub category: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub id: String,
    pub date: NaiveDate,
    pub amount: i64,
    pub title: String,
    pub source: String,
    pub confidence: f64,
    pub counterparty: String,
}

/// Операция вместе с остатком после неё (если банк его передал).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HistoryRow {
    #[serde(flatten)]
    pub tx: Tx,
    pub balance: Option<i64>,
}

/// Согласованный снимок данных компании от ledger: история, события и ревизия данных.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub data_revision: i64,
    pub transactions: Vec<HistoryRow>,
    pub events: Vec<Event>,
}

impl Snapshot {
    pub fn history(&self) -> Vec<(Tx, Option<i64>)> {
        self.transactions.iter().map(|r| (r.tx.clone(), r.balance)).collect()
    }
}
