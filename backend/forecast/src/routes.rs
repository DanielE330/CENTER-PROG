use crate::AppState;
use axum::{
    Json,
    extract::{Path, Query, State},
    http::StatusCode,
};
use chrono::NaiveDate;
use common::{ApiError, Ctx, R, Snapshot, Tx, err, internal, money, stringify_money};
use fc_model::{self as model, ForecastError, Params, Scenario};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

fn lock(st: &AppState) -> std::sync::MutexGuard<'_, rusqlite::Connection> {
    st.db.lock().unwrap_or_else(|e| e.into_inner())
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

fn map_fc_err(e: ForecastError) -> ApiError {
    match e {
        ForecastError::NoData => common::error::no_data(),
        ForecastError::NoBalance => common::error::no_balance(),
    }
}

async fn snapshot(st: &AppState, ctx: &Ctx, cid: &str) -> R<Snapshot> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/snapshot"), ctx).await?;
    serde_json::from_value(v).map_err(internal)
}

async fn current_revision(st: &AppState, ctx: &Ctx, cid: &str) -> R<i64> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}"), ctx).await?;
    v["data_revision"].as_i64().ok_or_else(|| internal("no data_revision"))
}

async fn compute(snap: Snapshot, horizon: usize, scenario: Scenario) -> R<model::ForecastResult> {
    tokio::task::spawn_blocking(move || {
        let hist = snap.history();
        let balances = model::daily_balances(&hist);
        // внутренние переводы не являются выручкой или расходом бизнеса
        let txs: Vec<Tx> = hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect();
        model::forecast(&txs, &balances, &snap.events, &scenario, &Params { horizon, today: Some(chrono::Local::now().date_naive()), ..Default::default() })
    })
    .await
    .map_err(internal)?
    .map_err(map_fc_err)
}

fn save_run(st: &AppState, cid: &str, rev: i64, scenario: Option<&Scenario>, res: &model::ForecastResult) -> R<(String, String)> {
    let id = uuid::Uuid::new_v4().to_string();
    let at = now();
    let c = lock(st);
    c.execute(
        "INSERT INTO forecast_runs(id, company_id, created_at, data_revision, scenario, params, result) VALUES(?,?,?,?,?,?,?)",
        params![
            id, cid, at, rev, scenario.map(|s| serde_json::to_string(s).unwrap()),
            json!({"horizon": res.horizon, "seed": res.seed, "model": res.model_version, "n_paths": res.n_paths}).to_string(),
            serde_json::to_string(res).unwrap()
        ],
    )
    .map_err(internal)?;
    Ok((id, at))
}

fn run_json(id: &str, at: &str, rev: i64, stale: bool, res: &model::ForecastResult, scenario: Option<&Scenario>) -> Value {
    json!({"run_id": id, "created_at": at, "data_revision": rev, "stale": stale, "scenario": scenario, "result": res})
}

fn horizon_of(h: Option<i64>) -> R<usize> {
    let h = h.unwrap_or(90);
    if !(7..=90).contains(&h) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_horizon", "Горизонт от 7 до 90 дней"));
    }
    Ok(h as usize)
}

#[derive(Deserialize, Default)]
pub struct ForecastBody {
    horizon_days: Option<i64>,
}

pub async fn create_forecast(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<ForecastBody>>) -> R<(StatusCode, Json<Value>)> {
    let h = horizon_of(body.and_then(|b| b.0.horizon_days))?;
    let snap = snapshot(&st, &ctx, &cid).await?;
    let rev = snap.data_revision;
    let res = compute(snap, h, Scenario::default()).await?;
    let (id, at) = save_run(&st, &cid, rev, None, &res)?;
    Ok((StatusCode::CREATED, money(run_json(&id, &at, rev, false, &res, None))))
}

fn read_run(st: &AppState, cid: &str, cur: i64, rid: Option<&str>) -> R<Json<Value>> {
    let c = lock(st);
    let sql = if rid.is_some() {
        "SELECT id, created_at, data_revision, scenario, result FROM forecast_runs WHERE company_id=?1 AND id=?2"
    } else {
        "SELECT id, created_at, data_revision, scenario, result FROM forecast_runs WHERE company_id=?1 AND scenario IS NULL AND ?2='' ORDER BY created_at DESC LIMIT 1"
    };
    let row: Option<(String, String, i64, Option<String>, String)> = c
        .query_row(sql, params![cid, rid.unwrap_or("")], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))
        .optional()
        .map_err(internal)?;
    let (id, at, rev, sc, result) = row.ok_or_else(|| err(StatusCode::NOT_FOUND, "no_forecast", "Прогноз ещё не построен"))?;
    let mut v = json!({"run_id": id, "created_at": at, "data_revision": rev, "stale": rev != cur,
        "scenario": sc.and_then(|s| serde_json::from_str::<Value>(&s).ok()), "result": serde_json::from_str::<Value>(&result).map_err(internal)?});
    stringify_money(&mut v);
    Ok(Json(v))
}

/// Если данные в ledger изменились после последнего базового расчёта (импорт, новое событие, правка категории),
/// пересчитываем прогноз сразу, с тем же горизонтом. Ошибка пересчёта не мешает отдать прежний результат.
async fn refresh_if_stale(st: &AppState, ctx: &Ctx, cid: &str, cur: i64) -> bool {
    let run: Option<(i64, String)> = {
        let c = lock(st);
        c.query_row(
            "SELECT data_revision, params FROM forecast_runs WHERE company_id=? AND scenario IS NULL ORDER BY created_at DESC LIMIT 1",
            [cid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .ok()
        .flatten()
    };
    let Some((rev, params)) = run else { return false };
    if rev == cur {
        return false;
    }
    let horizon = serde_json::from_str::<Value>(&params).ok().and_then(|v| v["horizon"].as_u64()).unwrap_or(30) as usize;
    let Ok(snap) = snapshot(st, ctx, cid).await else { return false };
    let rev = snap.data_revision;
    match compute(snap, horizon, Scenario::default()).await {
        Ok(res) => save_run(st, cid, rev, None, &res).is_ok(),
        Err(_) => false,
    }
}

pub async fn latest_forecast(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    let cur = current_revision(&st, &ctx, &cid).await?;
    let recomputed = refresh_if_stale(&st, &ctx, &cid, cur).await;
    // ревизия могла сдвинуться во время пересчёта: stale считаем по свежему значению
    let cur = if recomputed { current_revision(&st, &ctx, &cid).await.unwrap_or(cur) } else { cur };
    let Json(mut v) = read_run(&st, &cid, cur, None)?;
    v["recomputed"] = json!(recomputed);
    Ok(Json(v))
}

pub async fn get_forecast(State(st): State<AppState>, ctx: Ctx, Path((cid, rid)): Path<(String, String)>) -> R<Json<Value>> {
    let cur = current_revision(&st, &ctx, &cid).await?;
    read_run(&st, &cid, cur, Some(&rid))
}

#[derive(Deserialize)]
pub struct ScenarioBody {
    horizon_days: Option<i64>,
    scenario: Scenario,
}

pub async fn create_scenario(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<ScenarioBody>) -> R<(StatusCode, Json<Value>)> {
    let h = horizon_of(b.horizon_days)?;
    let s = &b.scenario;
    if !(0..=90).contains(&s.delay_days) || !(0.0..=100.0).contains(&s.revenue_drop_pct) || !(0.0..=300.0).contains(&s.fixed_cost_growth_pct) || s.extra_expense_kop.map_or(false, |v| v <= 0 || v >= 1_000_000_000_000_000) || s.extra_income_kop.map_or(false, |v| v <= 0 || v >= 1_000_000_000_000_000) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_scenario", "Параметры сценария вне допустимых пределов"));
    }
    if s.is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "empty_scenario", "Задайте хотя бы одно изменение"));
    }
    // базовый и сценарный расчёты используют один seed — разница объясняется только сценарием
    let snap = snapshot(&st, &ctx, &cid).await?;
    let rev = snap.data_revision;
    let base = compute(snap.clone(), h, Scenario::default()).await?;
    let sc = compute(snap, h, b.scenario.clone()).await?;
    let (bid, bat) = save_run(&st, &cid, rev, None, &base)?;
    let (sid, sat) = save_run(&st, &cid, rev, Some(&b.scenario), &sc)?;
    let out = json!({
        "base": run_json(&bid, &bat, rev, false, &base, None),
        "scenario": run_json(&sid, &sat, rev, false, &sc, Some(&b.scenario)),
        "delta": {
            "shortfall_probability": sc.risk.shortfall_probability - base.risk.shortfall_probability,
            "max_deficit_p90": sc.risk.max_deficit_p90 - base.risk.max_deficit_p90,
        }
    });
    Ok((StatusCode::CREATED, money(out)))
}

pub async fn run_backtest(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    let snap = snapshot(&st, &ctx, &cid).await?;
    let r = tokio::task::spawn_blocking(move || {
        let hist = snap.history();
        let balances = model::daily_balances(&hist);
        let txs: Vec<Tx> = hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect();
        model::backtest(&txs, &balances, 14)
    })
    .await
    .map_err(internal)?
    .ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "not_enough_history", "Для backtest нужно не менее 5 месяцев истории"))?;
    Ok(money(serde_json::to_value(r).map_err(internal)?))
}

#[derive(Deserialize)]
pub struct RulesQuery {
    as_of: Option<NaiveDate>,
}

/// Найденные регулярные платежи (для план-факта). Суммы — числа, не для публикации в SPA.
pub async fn rules(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Query(q): Query<RulesQuery>) -> R<Json<Value>> {
    let snap = snapshot(&st, &ctx, &cid).await?;
    let txs: Vec<Tx> = snap.transactions.into_iter().map(|h| h.tx).filter(|t| t.category != "transfers").collect();
    let as_of = q.as_of.or_else(|| txs.iter().map(|t| t.date).max()).ok_or_else(|| map_fc_err(ForecastError::NoData))?;
    let rules = tokio::task::spawn_blocking(move || model::detect_rules(&txs, as_of)).await.map_err(internal)?;
    Ok(Json(json!({"as_of": as_of, "rules": rules})))
}

/// Краткая сводка риска для служебных потребителей (бывший internal summary).
pub async fn summary(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    let cur = current_revision(&st, &ctx, &cid).await?;
    let recomputed = refresh_if_stale(&st, &ctx, &cid, cur).await;
    let cur = if recomputed { current_revision(&st, &ctx, &cid).await.unwrap_or(cur) } else { cur };
    let v = read_run(&st, &cid, cur, None)?;
    let r = &v.0["result"];
    Ok(Json(json!({
        "run_id": v.0["run_id"], "stale": v.0["stale"], "as_of": r["as_of"], "horizon": r["horizon"],
        "risk": {"level": r["risk"]["level"], "shortfall_probability": r["risk"]["shortfall_probability"],
                 "first_break_date": r["risk"]["first_break_date"], "max_deficit_p90": r["risk"]["max_deficit_p90"]},
    })))
}
