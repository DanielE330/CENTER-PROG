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

/// Флаги контрагента по данным ФНС (проверка по ИНН). Хранит ledger, учитывает forecast.
/// `risk_level`: low | medium | high; `status`: active | liquidating | liquidated | bankrupt | unknown.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CounterpartyRisk {
    pub inn: String,
    /// названия контрагента в операциях компании с этим ИНН (по ним флаги сопоставляются с событиями и регулярными платежами)
    #[serde(default)]
    pub names: Vec<String>,
    pub risk_level: String,
    pub status: String,
    #[serde(default)]
    pub tax_debt_kop: i64,
    #[serde(default)]
    pub bankruptcy_flag: bool,
    #[serde(default)]
    pub checked_at: String,
    #[serde(default)]
    pub source: String,
}

/// Согласованный снимок данных компании от ledger: история, события и ревизия данных.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub data_revision: i64,
    pub transactions: Vec<HistoryRow>,
    pub events: Vec<Event>,
    /// флаги контрагентов от ФНС; по умолчанию пусто (тогда расчёт прогноза не меняется)
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub counterparty_risks: Vec<CounterpartyRisk>,
}

impl Snapshot {
    pub fn history(&self) -> Vec<(Tx, Option<i64>)> {
        self.transactions.iter().map(|r| (r.tx.clone(), r.balance)).collect()
    }
}
