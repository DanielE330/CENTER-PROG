//! Alert rules. Texts are written for an owner without financial training:
//! what happens, when, how much, how likely.

use chrono::{Datelike, NaiveDate};
use serde::Deserialize;
use serde_json::json;

#[derive(Debug, Deserialize)]
pub struct Gap {
    pub start_date: NaiveDate,
    pub end_date: NaiveDate,
    pub probability: f64,
    pub shortfall_p50_kop: i64,
    pub shortfall_p90_kop: i64,
    /// `warning`: below the minimum balance; `critical`: beyond the overdraft limit.
    pub level: String,
    #[serde(default)]
    pub drivers: serde_json::Value,
}

#[derive(Debug, Deserialize)]
pub struct ForecastUpdated {
    pub run_id: uuid::Uuid,
    pub as_of: NaiveDate,
    pub scenario_id: Option<uuid::Uuid>,
    #[serde(default)]
    pub gaps: Vec<Gap>,
}

#[derive(Debug, Deserialize)]
pub struct OverdueItem {
    pub id: uuid::Uuid,
    pub direction: String,
    pub amount_kop: i64,
    pub due_date: NaiveDate,
}

#[derive(Debug, Deserialize)]
pub struct OverdueData {
    pub as_of: NaiveDate,
    pub items: Vec<OverdueItem>,
}

#[derive(Debug, PartialEq)]
pub struct Draft {
    pub kind: String,
    pub severity: String,
    pub dedup_key: String,
    pub title: String,
    pub body: String,
    pub data: serde_json::Value,
}

pub fn rubles(kop: i64) -> String {
    let rub = (kop.abs() + 50) / 100;
    let digits = rub.to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push('\u{a0}');
        }
        out.push(ch);
    }
    format!("{out}\u{a0}₽")
}

pub fn likelihood(p: f64) -> &'static str {
    match p {
        p if p >= 0.8 => "почти наверняка",
        p if p >= 0.5 => "скорее всего",
        p if p >= 0.3 => "вполне возможно",
        _ => "есть риск",
    }
}

pub fn from_forecast(f: &ForecastUpdated) -> Vec<Draft> {
    f.gaps
        .iter()
        .filter(|g| (g.start_date - f.as_of).num_days() <= 30)
        .map(|g| {
            let days = (g.start_date - f.as_of).num_days();
            let critical = g.level == "critical";
            let severity = if critical {
                "critical"
            } else if g.probability >= 0.5 || (days <= 14 && g.probability >= 0.3) {
                "high"
            } else {
                "warning"
            };
            let what = if critical {
                "превысите лимит овердрафта"
            } else {
                "не хватит денег на обязательные платежи"
            };
            Draft {
                kind: if critical { "gap_critical".into() } else { "gap".into() },
                severity: severity.into(),
                // One alert per gap week: a gap moving by a day is the same gap.
                dedup_key: format!(
                    "gap:{}:{}-w{}",
                    g.level,
                    g.start_date.year(),
                    g.start_date.iso_week().week()
                ),
                title: format!(
                    "{} {} {}: {} {}",
                    capitalize(likelihood(g.probability)),
                    g.start_date.format("%d.%m"),
                    what,
                    "около",
                    rubles(g.shortfall_p50_kop)
                ),
                body: format!(
                    "Через {days} дн. ({}) вероятность {:.0}%. В плохом случае не хватит {}. Разрыв продлится до {}.",
                    g.start_date.format("%d.%m.%Y"),
                    g.probability * 100.0,
                    rubles(g.shortfall_p90_kop),
                    g.end_date.format("%d.%m.%Y"),
                ),
                data: json!({
                    "run_id": f.run_id, "start_date": g.start_date, "end_date": g.end_date,
                    "probability": g.probability, "shortfall_p50_kop": g.shortfall_p50_kop,
                    "shortfall_p90_kop": g.shortfall_p90_kop, "drivers": g.drivers
                }),
            }
        })
        .collect()
}

/// One summary per direction instead of a warning per invoice: twenty separate
/// "payment did not arrive" cards are noise, one line with the total is a signal.
pub fn from_overdue(o: &OverdueData) -> Vec<Draft> {
    let mut out = Vec::new();
    for (direction, kind_key) in [("inflow", "overdue:incoming"), ("outflow", "overdue:outgoing")] {
        let items: Vec<&OverdueItem> = o.items.iter().filter(|i| i.direction == direction).collect();
        if items.is_empty() {
            continue;
        }
        let total: i64 = items.iter().map(|i| i.amount_kop).sum();
        let oldest = items.iter().map(|i| i.due_date).min().unwrap_or(o.as_of);
        let incoming = direction == "inflow";
        out.push(Draft {
            kind: "overdue".into(),
            severity: if incoming { "warning" } else { "high" }.into(),
            dedup_key: kind_key.into(),
            title: if incoming {
                format!("Не пришли оплаты: {} шт. на {}", items.len(), rubles(total))
            } else {
                format!("Не оплачено {} шт. на {}", items.len(), rubles(total))
            },
            body: if incoming {
                format!(
                    "Самая старая ожидалась {}. Прогноз уже учитывает, как обычно задерживают оплату эти клиенты; \
                     стоит напомнить о счетах.",
                    oldest.format("%d.%m.%Y")
                )
            } else {
                "Проверьте платежи: просрочка обязательных платежей может стоить пеней.".into()
            },
            data: json!({ "count": items.len(), "amount_kop": total, "oldest_due": oldest,
                          "ids": items.iter().map(|i| i.id).collect::<Vec<_>>() }),
        });
    }
    out
}

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    chars
        .next()
        .map(|c| c.to_uppercase().collect::<String>() + chars.as_str())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    fn forecast(prob: f64, start: &str, level: &str) -> ForecastUpdated {
        ForecastUpdated {
            run_id: uuid::Uuid::nil(),
            as_of: d("2026-06-01"),
            scenario_id: None,
            gaps: vec![Gap {
                start_date: d(start),
                end_date: d(start),
                probability: prob,
                shortfall_p50_kop: 41_000_000,
                shortfall_p90_kop: 60_000_000,
                level: level.into(),
                drivers: json!([]),
            }],
        }
    }

    #[test]
    fn close_likely_gap_is_high() {
        assert_eq!(
            from_forecast(&forecast(0.64, "2026-06-10", "warning"))[0].severity,
            "high"
        );
    }

    #[test]
    fn overdraft_breach_is_critical() {
        assert_eq!(
            from_forecast(&forecast(0.25, "2026-06-20", "critical"))[0].severity,
            "critical"
        );
    }

    #[test]
    fn far_gap_is_not_alerted() {
        assert!(from_forecast(&forecast(0.9, "2026-08-01", "warning")).is_empty());
    }

    #[test]
    fn overdue_invoices_collapse_into_one_alert() {
        let item = |amount| OverdueItem {
            id: uuid::Uuid::now_v7(),
            direction: "inflow".into(),
            amount_kop: amount,
            due_date: d("2026-05-01"),
        };
        let data = OverdueData {
            as_of: d("2026-07-01"),
            items: vec![item(10_000), item(20_000), item(30_000)],
        };
        let drafts = from_overdue(&data);
        assert_eq!(drafts.len(), 1);
        assert!(drafts[0].title.contains("3 шт."));
    }

    #[test]
    fn rubles_are_grouped() {
        assert_eq!(rubles(41_000_000), "410\u{a0}000\u{a0}₽");
    }
}
