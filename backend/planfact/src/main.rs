//! planfact: сравнение плана и факта. Без собственной БД: историю и события берёт у ledger,
//! регулярные платежи (шаблоны плана) — у forecast.

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    routing::get,
};
use chrono::{Datelike, Duration, NaiveDate};
use common::{Ctx, R, Snapshot, Tx, Upstream, internal, money, mw};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Clone)]
struct AppState {
    ledger: Upstream,
    forecast: Upstream,
}

/// Регулярный платёж из ответа forecast (используются только поля, нужные для сопоставления).
#[derive(Deserialize)]
struct Rule {
    key: String,
    counterparty: String,
    kind: String,
    anchors: Vec<u32>,
    amount: i64,
}

#[tokio::main]
async fn main() {
    let st = AppState {
        ledger: Upstream::new("ledger", "Сервис данных", &mw::env("LEDGER_URL", "http://127.0.0.1:3002"), 30),
        forecast: Upstream::new("forecast", "Сервис прогноза", &mw::env("FORECAST_URL", "http://127.0.0.1:3003"), 60),
    };
    let app = Router::new().route("/internal/v1/companies/{cid}/plan-fact", get(plan_fact)).with_state(st);
    mw::serve("planfact", &mw::env("INTERNAL_ADDR", "127.0.0.1:3004"), app).await;
}

#[derive(Deserialize)]
struct PfQuery {
    days: Option<i64>,
}

struct Plan {
    date: NaiveDate,
    amount: i64,
    title: String,
    source: &'static str,
    key: Option<String>,
}

async fn plan_fact(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Query(q): Query<PfQuery>) -> R<Json<Value>> {
    let days = q.days.unwrap_or(90).clamp(14, 365);
    let snap: Snapshot = serde_json::from_value(st.ledger.get(&format!("/internal/v1/companies/{cid}/snapshot"), &ctx).await?).map_err(internal)?;
    let events = snap.events;
    let txs: Vec<Tx> = snap.transactions.into_iter().map(|h| h.tx).filter(|t| t.category != "transfers").collect();
    let as_of = txs.iter().map(|t| t.date).max().ok_or_else(common::error::no_data)?;
    let from = as_of - Duration::days(days);
    let rv = st.forecast.get(&format!("/internal/v1/companies/{cid}/rules?as_of={as_of}"), &ctx).await?;
    let rules: Vec<Rule> = serde_json::from_value(rv["rules"].clone()).map_err(internal)?;
    Ok(money(build(&txs, &events, &rules, as_of, from, days)))
}

fn build(txs: &[Tx], events: &[common::Event], rules: &[Rule], as_of: NaiveDate, from: NaiveDate, days: i64) -> Value {
    let mut plans: Vec<Plan> = vec![];
    for r in rules {
        for d in from.iter_days().take_while(|d| *d <= as_of) {
            let hit = match r.kind.as_str() {
                "monthly" => r.anchors.iter().any(|a| d.day() == *a),
                _ => d.weekday().num_days_from_monday() == r.anchors[0],
            };
            if hit {
                plans.push(Plan { date: d, amount: r.amount, title: r.counterparty.clone(), source: "recurring", key: Some(r.key.clone()) });
            }
        }
    }
    for e in events.iter().filter(|e| e.date >= from && e.date <= as_of) {
        plans.push(Plan { date: e.date, amount: e.amount, title: e.title.clone(), source: "event", key: None });
    }
    let mut used = vec![false; txs.len()];
    let mut items = vec![];
    let mut weeks: BTreeMap<NaiveDate, [i64; 4]> = BTreeMap::new(); // plan_in, plan_out, fact_in, fact_out
    plans.sort_by_key(|p| p.date);
    for pl in &plans {
        let found = txs.iter().enumerate().find(|(i, t)| {
            !used[*i]
                && t.amount.signum() == pl.amount.signum()
                && (t.date - pl.date).num_days().abs() <= 5
                && match &pl.key {
                    Some(k) => {
                        let tk = format!("{}:{}", if t.amount > 0 { "in" } else { "out" }, if !t.inn.is_empty() { t.inn.clone() } else { t.counterparty.to_lowercase() });
                        &tk == k || (t.amount - pl.amount).abs() * 3 <= pl.amount.abs() && t.counterparty == pl.title
                    }
                    None => (t.amount - pl.amount).abs() * 10 <= pl.amount.abs() * 3,
                }
        });
        let (status, fact, fact_date) = match found {
            Some((i, t)) => {
                used[i] = true;
                let tol = pl.amount.abs() * 15 / 100;
                (if (t.amount - pl.amount).abs() <= tol { "paid" } else { "deviation" }, Some(t.amount), Some(t.date))
            }
            None => (if pl.date > as_of - Duration::days(5) { "pending" } else { "missed" }, None, None),
        };
        let wk = pl.date - Duration::days(pl.date.weekday().num_days_from_monday() as i64);
        let w = weeks.entry(wk).or_insert([0; 4]);
        if pl.amount > 0 {
            w[0] += pl.amount;
            w[2] += fact.unwrap_or(0);
        } else {
            w[1] += -pl.amount;
            w[3] += -fact.unwrap_or(0);
        }
        items.push(json!({"date": pl.date, "title": pl.title, "source": pl.source, "plan": pl.amount, "fact": fact, "fact_date": fact_date,
            "diff": fact.map(|f| f - pl.amount), "status": status}));
    }
    let weeks_json: Vec<Value> = weeks
        .iter()
        .map(|(w, v)| json!({"week_start": w, "plan_in": v[0], "plan_out": v[1], "fact_in": v[2], "fact_out": v[3]}))
        .collect();
    let missed = items.iter().filter(|i| i["status"] == "missed").count();
    json!({
        "as_of": as_of, "days": days, "weeks": weeks_json, "items": items, "missed": missed,
        "note": "План по регулярным платежам построен по шаблонам, найденным во всей истории (in-sample); ручные события и счета — по введённым датам."
    })
}
