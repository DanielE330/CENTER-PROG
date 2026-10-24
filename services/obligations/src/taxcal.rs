//! Tax calendar: dates come from rules, amounts from the history-based estimate
//! that categorizer sends. Dates on weekends and holidays move to the next working day.
//!
//! The rules are a simplification for the MVP and must be checked by someone
//! who knows Russian tax law before production use.

use chrono::{Datelike, Duration, NaiveDate, Weekday};
use serde::Deserialize;

/// Fixed public holidays. The official calendar also moves weekend days; the
/// working-day set observed in the dataset is a better source for past dates.
const HOLIDAYS: &[(u32, u32)] = &[
    (1, 1),
    (1, 2),
    (1, 3),
    (1, 4),
    (1, 5),
    (1, 6),
    (1, 7),
    (1, 8),
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
];

pub fn is_working_day(d: NaiveDate) -> bool {
    !matches!(d.weekday(), Weekday::Sat | Weekday::Sun) && !HOLIDAYS.contains(&(d.month(), d.day()))
}

pub fn next_working_day(mut d: NaiveDate) -> NaiveDate {
    while !is_working_day(d) {
        d += Duration::days(1);
    }
    d
}

#[derive(Debug, Default, Deserialize)]
pub struct TaxEstimate {
    /// Typical single tax payment (ЕНП) in a month without quarterly peaks.
    #[serde(default)]
    pub monthly_kop: i64,
    /// Extra amount paid in peak months (advances, quarterly VAT and so on).
    #[serde(default)]
    pub peak_extra_kop: i64,
}

#[derive(Debug, PartialEq, Eq)]
pub struct TaxPayment {
    pub due: NaiveDate,
    pub amount_kop: i64,
    pub label: String,
}

/// Months in which the regime adds a peak on top of the monthly payment.
fn peak_months(regime: &str) -> &'static [u32] {
    match regime {
        // Advances for Q1, H1, 9 months and the annual payment.
        "usn_income" | "usn_income_expense" => &[4, 7, 10, 3],
        // Profit tax advances after each quarter; VAT is already spread over months.
        "osno" => &[1, 4, 7, 10],
        // Half-year advance and the annual payment.
        "eshn" => &[7, 3],
        _ => &[],
    }
}

/// Payments due on the 28th of each month in `[from, to]`.
pub fn schedule(regime: &str, estimate: &TaxEstimate, from: NaiveDate, to: NaiveDate) -> Vec<TaxPayment> {
    let peaks = peak_months(regime);
    let mut out = Vec::new();
    let (mut y, mut m) = (from.year(), from.month());
    while let Some(day) = NaiveDate::from_ymd_opt(y, m, 28) {
        if day > to {
            break;
        }
        let due = next_working_day(day);
        if due >= from {
            let peak = peaks.contains(&m);
            let amount = estimate.monthly_kop + if peak { estimate.peak_extra_kop } else { 0 };
            if amount > 0 {
                out.push(TaxPayment {
                    due,
                    amount_kop: amount,
                    label: if peak {
                        format!("ЕНП {m:02}.{y} (с авансом)")
                    } else {
                        format!("ЕНП {m:02}.{y}")
                    },
                });
            }
        }
        (y, m) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn weekend_28th_moves_to_monday() {
        // 2026-11-28 is a Saturday.
        assert_eq!(next_working_day(d("2026-11-28")), d("2026-11-30"));
    }

    #[test]
    fn usn_adds_peak_in_october_only() {
        let est = TaxEstimate {
            monthly_kop: 100,
            peak_extra_kop: 50,
        };
        let plan = schedule("usn_income", &est, d("2026-09-01"), d("2026-11-30"));
        let amounts: Vec<i64> = plan.iter().map(|p| p.amount_kop).collect();
        assert_eq!(amounts, vec![100, 150, 100]);
    }

    #[test]
    fn zero_estimate_produces_nothing() {
        let plan = schedule("osno", &TaxEstimate::default(), d("2026-01-01"), d("2026-12-31"));
        assert!(plan.is_empty());
    }
}
