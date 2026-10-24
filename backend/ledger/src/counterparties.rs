//! Флаги контрагентов компании по ИНН (результат проверки в ФНС) и топ контрагентов по обороту.
//! ledger только хранит и отдаёт: проверку делает сервис fns, оркестрирует api. Изменение уровня риска, статуса, долга
//! или признака банкротства увеличивает `data_revision`, поэтому прогноз пересчитывается; обновление одной лишь даты проверки — нет.

use crate::AppState;
use crate::routes::{lock, not_found, now};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::StatusCode,
};
use common::{CounterpartyRisk, R, err, internal};
use rusqlite::{Connection, OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

const MAX_ITEMS: usize = 200;
const LEVELS: [&str; 4] = ["low", "medium", "high", "unknown"];
/// Категории, контрагенты которых не проверяются: переводы между своими счетами, налоги (сама ФНС), зарплата (физлица),
/// кредиты и комиссии банка.
const SKIP_CATEGORIES: &str = "'transfers','taxes','payroll','loans','bank_fees'";

fn valid_inn(s: &str) -> bool {
    (s.len() == 10 || s.len() == 12) && s.bytes().all(|b| b.is_ascii_digit())
}

/// Флаги, которые меняют прогноз: заметный риск, ликвидация или банкротство.
fn significant(level: &str, status: &str, bankruptcy: bool) -> bool {
    matches!(level, "medium" | "high") || bankruptcy || matches!(status, "liquidating" | "liquidated" | "bankrupt")
}

fn bad(code: &str, msg: &str) -> common::ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, code, msg)
}

/// Флаги компании; `with_names` — дописать названия контрагента из операций (нужны forecast для сопоставления).
pub(crate) fn load_risks(c: &Connection, cid: &str, with_names: bool) -> R<Vec<CounterpartyRisk>> {
    let mut s = c
        .prepare("SELECT inn, risk_level, status, tax_debt_kop, bankruptcy_flag, checked_at, source FROM counterparty_risk WHERE company_id=? ORDER BY inn")
        .map_err(internal)?;
    let mut out: Vec<CounterpartyRisk> = s
        .query_map([cid], |r| {
            Ok(CounterpartyRisk {
                inn: r.get(0)?,
                names: vec![],
                risk_level: r.get(1)?,
                status: r.get(2)?,
                tax_debt_kop: r.get(3)?,
                bankruptcy_flag: r.get::<_, i64>(4)? != 0,
                checked_at: r.get(5)?,
                source: r.get(6)?,
            })
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    if with_names && !out.is_empty() {
        let mut q = c.prepare("SELECT DISTINCT counterparty FROM transactions WHERE company_id=? AND inn=? AND counterparty<>'' LIMIT 8").map_err(internal)?;
        for r in out.iter_mut() {
            r.names = q.query_map(params![cid, r.inn], |x| x.get::<_, String>(0)).map_err(internal)?.filter_map(|x| x.ok()).collect();
        }
    }
    Ok(out)
}

fn risk_json(r: &CounterpartyRisk) -> Value {
    json!({"inn": r.inn, "risk_level": r.risk_level, "status": r.status, "tax_debt_kop": r.tax_debt_kop,
        "bankruptcy_flag": r.bankruptcy_flag, "checked_at": r.checked_at, "source": r.source})
}

#[derive(Deserialize)]
pub struct RiskQuery {
    inn: Option<String>,
}

/// GET counterparty-risk[?inn=] -> {items:[{inn, risk_level, status, tax_debt_kop, bankruptcy_flag, checked_at, source}]}
pub async fn list_risk(State(st): State<AppState>, Path(cid): Path<String>, Query(q): Query<RiskQuery>) -> R<Json<Value>> {
    let c = lock(&st);
    let items: Vec<Value> = load_risks(&c, &cid, false)?.iter().filter(|r| q.inn.as_deref().is_none_or(|i| i == r.inn)).map(risk_json).collect();
    Ok(Json(json!({"items": items})))
}

#[derive(Deserialize)]
pub struct RiskItem {
    inn: String,
    risk_level: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    tax_debt_kop: i64,
    #[serde(default)]
    bankruptcy_flag: bool,
    #[serde(default)]
    checked_at: Option<String>,
    #[serde(default)]
    source: String,
}

#[derive(Deserialize)]
pub struct RiskBody {
    items: Vec<RiskItem>,
}

/// PUT counterparty-risk {items:[...]} -> {upserted, changed}. Идемпотентно по (company_id, inn); все записи или ни одной.
pub async fn put_risk(State(st): State<AppState>, Path(cid): Path<String>, Json(b): Json<RiskBody>) -> R<Json<Value>> {
    if b.items.len() > MAX_ITEMS {
        return Err(bad("too_many_items", "Не более 200 контрагентов за раз"));
    }
    let mut c = lock(&st);
    let exists: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [&cid], |r| r.get(0)).map_err(internal)?;
    if exists == 0 {
        return Err(not_found());
    }
    let tx = c.transaction().map_err(internal)?;
    let (mut upserted, mut changed) = (0usize, 0usize);
    {
        for it in &b.items {
            if !valid_inn(&it.inn) {
                return Err(bad("invalid_inn", "ИНН: 10 или 12 цифр"));
            }
            let level = it.risk_level.trim().to_lowercase();
            if !LEVELS.contains(&level.as_str()) {
                return Err(bad("invalid_risk_level", "risk_level: low | medium | high | unknown"));
            }
            if it.tax_debt_kop < 0 || it.tax_debt_kop >= 1_000_000_000_000_000 {
                return Err(bad("invalid_debt", "Задолженность: неотрицательное число копеек"));
            }
            let status: String = it.status.trim().to_lowercase().chars().take(32).collect();
            let source: String = it.source.trim().chars().take(32).collect();
            let checked = it.checked_at.as_deref().map(str::trim).filter(|s| !s.is_empty()).map(|s| s.chars().take(40).collect::<String>()).unwrap_or_else(now);
            let prev: Option<(String, String, i64, i64)> = tx
                .query_row("SELECT risk_level, status, tax_debt_kop, bankruptcy_flag FROM counterparty_risk WHERE company_id=? AND inn=?", params![cid, it.inn], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
                .optional()
                .map_err(internal)?;
            // прогноз зависит только от значимых флагов: «нейтральная» запись (низкий риск, действует) равна отсутствию записи
            let eff = |l: &str, s: &str, d: i64, b: bool| (significant(l, s, b)).then(|| (l.to_string(), s.to_string(), d, b));
            let same = eff(&level, &status, it.tax_debt_kop, it.bankruptcy_flag) == prev.as_ref().and_then(|p| eff(&p.0, &p.1, p.2, p.3 != 0));
            tx.execute(
                "INSERT INTO counterparty_risk(company_id, inn, risk_level, status, tax_debt_kop, bankruptcy_flag, checked_at, source) VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(company_id, inn) DO UPDATE SET risk_level=excluded.risk_level, status=excluded.status, tax_debt_kop=excluded.tax_debt_kop,
                 bankruptcy_flag=excluded.bankruptcy_flag, checked_at=excluded.checked_at, source=excluded.source",
                params![cid, it.inn, level, status, it.tax_debt_kop, it.bankruptcy_flag as i64, checked, source],
            )
            .map_err(internal)?;
            upserted += 1;
            if !same {
                changed += 1;
            }
        }
    }
    // ревизия двигается, только если изменились значимые для прогноза флаги
    if changed > 0 {
        tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    }
    tx.commit().map_err(internal)?;
    Ok(Json(json!({"upserted": upserted, "changed": changed})))
}

#[derive(Deserialize)]
pub struct TopQuery {
    limit: Option<i64>,
    /// in — только поступления (клиенты), out — только списания (поставщики), по умолчанию все
    direction: Option<String>,
}

/// GET counterparties/top?limit=20&direction= -> топ контрагентов с ИНН по обороту (кроме переводов, налогов, зарплаты, кредитов и комиссий)
/// вместе с сохранёнными флагами. Суммы — числа (копейки), не для публикации в SPA без перевода в строки.
pub async fn top(State(st): State<AppState>, Path(cid): Path<String>, Query(q): Query<TopQuery>) -> R<Json<Value>> {
    let limit = q.limit.unwrap_or(20).clamp(1, 100);
    let dir = match q.direction.as_deref() {
        None | Some("") | Some("all") => "all",
        Some("in") => "in",
        Some("out") => "out",
        _ => return Err(bad("invalid_direction", "direction: all | in | out")),
    };
    let c = lock(&st);
    let exists: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [&cid], |r| r.get(0)).map_err(internal)?;
    if exists == 0 {
        return Err(not_found());
    }
    let cond = match dir {
        "in" => "AND amount_kop>0",
        "out" => "AND amount_kop<0",
        _ => "",
    };
    let base = format!(
        "FROM transactions WHERE company_id=?1 AND category NOT IN ({SKIP_CATEGORIES}) AND length(inn) IN (10,12) AND inn NOT GLOB '*[^0-9]*'
         AND inn <> COALESCE((SELECT inn FROM companies WHERE id=?1), '') {cond}"
    );
    let total_income: i64 = c
        .query_row(&format!("SELECT COALESCE(SUM(CASE WHEN amount_kop>0 THEN amount_kop ELSE 0 END),0) {base}"), [&cid], |r| r.get(0))
        .map_err(internal)?;
    let mut s = c
        .prepare(&format!(
            "SELECT inn, SUM(ABS(amount_kop)) AS turnover, SUM(CASE WHEN amount_kop>0 THEN amount_kop ELSE 0 END), SUM(CASE WHEN amount_kop<0 THEN -amount_kop ELSE 0 END), COUNT(*), MAX(date)
             {base} GROUP BY inn ORDER BY turnover DESC, inn LIMIT ?2"
        ))
        .map_err(internal)?;
    let rows: Vec<(String, i64, i64, i64, i64, String)> = s
        .query_map(params![cid, limit], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    let risks: BTreeMap<String, CounterpartyRisk> = load_risks(&c, &cid, false)?.into_iter().map(|r| (r.inn.clone(), r)).collect();
    let mut items = vec![];
    for (inn, turnover, income, expense, n, last) in rows {
        let name: String = c
            .query_row(
                "SELECT counterparty FROM transactions WHERE company_id=? AND inn=? GROUP BY counterparty ORDER BY COUNT(*) DESC, counterparty LIMIT 1",
                params![cid, inn],
                |r| r.get(0),
            )
            .map_err(internal)?;
        items.push(json!({
            "inn": inn, "name": name, "turnover_kop": turnover, "income_kop": income, "expense_kop": expense, "tx_count": n, "last_date": last,
            "income_share": if total_income > 0 { income as f64 / total_income as f64 } else { 0.0 },
            "risk": risks.get(&inn).map(risk_json),
        }));
    }
    Ok(Json(json!({"items": items, "total_income_kop": total_income})))
}
