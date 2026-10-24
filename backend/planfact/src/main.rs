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

#[cfg(test)]
mod tests {
    use super::*;
    use common::Event;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    fn tx(date: &str, amount: i64, cp: &str, inn: &str) -> Tx {
        Tx { date: d(date), amount, counterparty: cp.into(), inn: inn.into(), purpose: String::new(), category: "other_expense".into() }
    }

    fn event(date: &str, amount: i64, title: &str) -> Event {
        Event { id: title.into(), date: d(date), amount, title: title.into(), source: "manual".into(), confidence: 0.9, counterparty: String::new() }
    }

    fn rule(key: &str, cp: &str, kind: &str, anchors: &[u32], amount: i64) -> Rule {
        Rule { key: key.into(), counterparty: cp.into(), kind: kind.into(), anchors: anchors.to_vec(), amount }
    }

    // as_of = вторник 2026-03-31; окно 30 дней: с 2026-03-01 (воскресенье).
    fn run(txs: &[Tx], events: &[Event], rules: &[Rule]) -> Value {
        build(txs, events, rules, d("2026-03-31"), d("2026-03-01"), 30)
    }

    fn items(v: &Value) -> &Vec<Value> {
        v["items"].as_array().unwrap()
    }

    #[test]
    fn monthly_rule_paid_deviation_and_missed() {
        let rules = [
            rule("out:7701", "ООО Аренда", "monthly", &[5], -2_000_000),
            rule("out:7702", "ООО Охрана", "monthly", &[10], -300_000),
            rule("out:7703", "ООО Связь", "monthly", &[15], -100_000),
        ];
        let txs = [
            tx("2026-03-06", -2_050_000, "ООО Аренда", "7701"), // +2.5%: в допуске 15%
            tx("2026-03-11", -450_000, "ООО Охрана", "7702"),   // +50%: отклонение
        ];
        let v = run(&txs, &[], &rules);
        let it = items(&v);
        assert_eq!(it.len(), 3);
        assert_eq!((it[0]["date"].as_str(), it[0]["status"].as_str(), it[0]["source"].as_str()), (Some("2026-03-05"), Some("paid"), Some("recurring")));
        assert_eq!((it[0]["plan"].as_i64(), it[0]["fact"].as_i64(), it[0]["diff"].as_i64()), (Some(-2_000_000), Some(-2_050_000), Some(-50_000)));
        assert_eq!(it[0]["fact_date"], "2026-03-06");
        assert_eq!((it[1]["status"].as_str(), it[1]["diff"].as_i64()), (Some("deviation"), Some(-150_000)));
        assert_eq!((it[2]["status"].as_str(), it[2]["fact"].is_null(), it[2]["diff"].is_null()), (Some("missed"), true, true));
        assert_eq!(v["missed"], 1);
        assert_eq!((v["as_of"].as_str(), v["days"].as_i64()), (Some("2026-03-31"), Some(30)));
    }

    #[test]
    fn rule_matches_by_counterparty_name_when_key_differs_but_amount_close() {
        let rules = [rule("out:7701", "ООО Аренда", "monthly", &[5], -1_000_000)];
        // другой ИНН (ключ не совпал), но то же название и сумма в пределах 33%
        let v = run(&[tx("2026-03-05", -1_200_000, "ООО Аренда", "9999")], &[], &rules);
        assert_eq!(items(&v)[0]["status"], "deviation");
        // другое название — не сопоставляется
        let v = run(&[tx("2026-03-05", -1_000_000, "ООО Иное", "9999")], &[], &rules);
        assert_eq!(items(&v)[0]["status"], "missed");
    }

    #[test]
    fn match_window_is_five_days_and_sign_must_agree() {
        let rules = [rule("out:7701", "ООО Аренда", "monthly", &[10], -1_000_000)];
        let v = run(&[tx("2026-03-15", -1_000_000, "ООО Аренда", "7701")], &[], &rules);
        assert_eq!(items(&v)[0]["status"], "paid"); // ровно 5 дней
        let v = run(&[tx("2026-03-16", -1_000_000, "ООО Аренда", "7701")], &[], &rules);
        assert_eq!(items(&v)[0]["status"], "missed"); // 6 дней
        let v = run(&[tx("2026-03-10", 1_000_000, "ООО Аренда", "7701")], &[], &rules);
        assert_eq!(items(&v)[0]["status"], "missed"); // противоположный знак
    }

    #[test]
    fn events_match_by_amount_and_each_fact_is_used_once() {
        let events = [event("2026-03-15", 1_000_000, "Оплата А"), event("2026-03-16", 1_000_000, "Оплата Б")];
        let txs = [tx("2026-03-17", 1_100_000, "Клиент", "1")];
        let v = run(&txs, &events, &[]);
        let it = items(&v);
        assert_eq!(it.len(), 2);
        assert_eq!((it[0]["title"].as_str(), it[0]["status"].as_str(), it[0]["source"].as_str()), (Some("Оплата А"), Some("paid"), Some("event")));
        // единственная операция уже занята первым событием
        assert_eq!(it[1]["status"], "missed");
        assert_eq!(v["missed"], 1);
        // сумма вне 30% — не сопоставляется
        let v = run(&[tx("2026-03-17", 1_400_000, "Клиент", "1")], &events[..1], &[]);
        assert_eq!(items(&v)[0]["status"], "missed");
    }

    #[test]
    fn recent_unmatched_plan_is_pending_not_missed() {
        // as_of - 5 дней = 2026-03-26: позже этой даты — «ожидается»
        let events = [event("2026-03-27", -500_000, "Свежий"), event("2026-03-26", -500_000, "Граница")];
        let v = run(&[], &events, &[]);
        let it = items(&v);
        assert_eq!((it[0]["title"].as_str(), it[0]["status"].as_str()), (Some("Граница"), Some("missed")));
        assert_eq!((it[1]["title"].as_str(), it[1]["status"].as_str()), (Some("Свежий"), Some("pending")));
        assert_eq!(v["missed"], 1);
    }

    #[test]
    fn events_outside_window_are_ignored() {
        let events = [event("2026-02-28", -100, "До окна"), event("2026-04-01", -100, "После as_of"), event("2026-03-01", -100, "Первый день")];
        let v = run(&[], &events, &[]);
        assert_eq!(items(&v).len(), 1);
        assert_eq!(items(&v)[0]["title"], "Первый день");
    }

    #[test]
    fn weekly_rule_generates_plan_for_each_weekday_occurrence() {
        // понедельники марта 2026: 2, 9, 16, 23, 30
        let rules = [rule("out:7", "ООО Недельный", "weekly", &[0], -100_000)];
        let v = run(&[tx("2026-03-09", -100_000, "ООО Недельный", "7")], &[], &rules);
        let dates: Vec<_> = items(&v).iter().map(|i| i["date"].as_str().unwrap().to_string()).collect();
        assert_eq!(dates, ["2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23", "2026-03-30"]);
        let st: Vec<_> = items(&v).iter().map(|i| i["status"].as_str().unwrap().to_string()).collect();
        assert_eq!(st, ["missed", "paid", "missed", "missed", "pending"]);
    }

    #[test]
    fn weekly_aggregation_splits_in_and_out_by_week() {
        let events = [
            event("2026-03-03", 1_000_000, "Приход"),   // неделя с 03-02
            event("2026-03-04", -400_000, "Расход"),    // та же неделя
            event("2026-03-10", -200_000, "Расход 2"),  // неделя с 03-09
        ];
        let txs = [tx("2026-03-03", 1_000_000, "К", "1"), tx("2026-03-04", -380_000, "П", "2")];
        let v = run(&txs, &events, &[]);
        let w = v["weeks"].as_array().unwrap();
        assert_eq!(w.len(), 2);
        assert_eq!(w[0], json!({"week_start": "2026-03-02", "plan_in": 1_000_000, "plan_out": 400_000, "fact_in": 1_000_000, "fact_out": 380_000}));
        assert_eq!(w[1], json!({"week_start": "2026-03-09", "plan_in": 0, "plan_out": 200_000, "fact_in": 0, "fact_out": 0}));
    }

    #[test]
    fn response_amounts_are_strings_after_money() {
        let v = run(&[tx("2026-03-03", -1000, "П", "2")], &[event("2026-03-03", -1000, "Расход")], &[]);
        let Json(out) = money(v);
        assert_eq!(out["items"][0]["plan"], "-1000");
        assert_eq!(out["items"][0]["fact"], "-1000");
        assert_eq!(out["weeks"][0]["plan_out"], "1000");
    }

    #[test]
    fn empty_input_gives_empty_report() {
        let v = run(&[], &[], &[]);
        assert!(items(&v).is_empty());
        assert!(v["weeks"].as_array().unwrap().is_empty());
        assert_eq!(v["missed"], 0);
    }
}
