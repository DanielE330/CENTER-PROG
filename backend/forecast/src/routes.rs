use crate::AppState;
use axum::{
    Json,
    extract::{Path, Query, State},
    http::StatusCode,
};
use chrono::NaiveDate;
use common::{ApiError, CounterpartyRisk, Ctx, Event, R, Snapshot, Tx, err, internal, money, stringify_money};
use fc_model::{self as model, ForecastError, Fitted, Params, Scenario};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::Arc;

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

async fn snapshot_since(st: &AppState, ctx: &Ctx, cid: &str, rev: i64) -> R<Snapshot> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/snapshot?since_rev={rev}"), ctx).await?;
    serde_json::from_value(v).map_err(internal)
}

async fn current_revision(st: &AppState, ctx: &Ctx, cid: &str) -> R<i64> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}"), ctx).await?;
    v["data_revision"].as_i64().ok_or_else(|| internal("no data_revision"))
}

/// Операции снимка без внутренних переводов (они не выручка и не расход бизнеса) и остатки на конец дня.
fn history_of(snap: &Snapshot) -> (Vec<Tx>, BTreeMap<NaiveDate, i64>) {
    let hist = snap.history();
    let balances = model::daily_balances(&hist);
    (hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect(), balances)
}

fn sim_params(horizon: usize) -> Params {
    Params { horizon, today: Some(chrono::Local::now().date_naive()), ..Default::default() }
}

/// То, что меняется без переобучения модели: ожидаемые события и флаги риска контрагентов (ФНС).
struct Live {
    events: Vec<Event>,
    risks: Vec<CounterpartyRisk>,
}

impl Live {
    fn of(snap: Snapshot) -> Arc<Live> {
        Arc::new(Live { events: snap.events, risks: snap.counterparty_risks })
    }
}

async fn simulate(f: Arc<Fitted>, live: Arc<Live>, horizon: usize, scenario: Scenario) -> R<model::ForecastResult> {
    tokio::task::spawn_blocking(move || model::simulate_with_risk(&f, &live.events, &scenario, &sim_params(horizon), &live.risks))
        .await
        .map_err(internal)?
        .map_err(map_fc_err)
}

// ---------- обновление прогноза: полный пересчёт или дописывание новых данных ----------
/// Полный пересчёт обязателен, если с последнего обучения прошло больше стольких дней…
const FULL_FIT_MAX_AGE_DAYS: i64 = 30;
/// …или дописано операций больше этой доли от обученных.
const FULL_FIT_MAX_GROWTH: f64 = 0.25;

/// Как получен прогноз: `mode` — full | incremental; `reason` — почему именно так
/// (full: mode_full | forced | first | reset | backdated | expired | grown; incremental: events_only | new_operations).
#[derive(Clone, Debug, Serialize)]
struct UpdateInfo {
    mode: &'static str,
    reason: &'static str,
    ops_added: usize,
    full_fit_at: String,
}

struct Cached {
    fitted: Fitted,
    fit_revision: i64,
    full_fit_at: String,
    full_fit_ops: usize,
    ops_added: usize,
}

fn update_mode(st: &AppState, cid: &str) -> String {
    lock(st)
        .query_row("SELECT update_mode FROM forecast_settings WHERE company_id=?", [cid], |r| r.get(0))
        .optional()
        .ok()
        .flatten()
        .unwrap_or_else(|| "full".into())
}

fn load_cached(st: &AppState, cid: &str) -> Option<Cached> {
    let row: Option<(i64, String, i64, i64, String, String)> = lock(st)
        .query_row(
            "SELECT fit_revision, full_fit_at, full_fit_ops, ops_added, model_version, blob FROM fitted_models WHERE company_id=?",
            [cid],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)),
        )
        .optional()
        .ok()
        .flatten();
    let (fit_revision, full_fit_at, full_fit_ops, ops_added, version, blob) = row?;
    if version != model::MODEL_VERSION {
        return None; // модель другой версии: кэш не переиспользуем
    }
    let fitted = serde_json::from_str(&blob).ok()?;
    Some(Cached { fitted, fit_revision, full_fit_at, full_fit_ops: full_fit_ops as usize, ops_added: ops_added as usize })
}

fn save_cached(st: &AppState, cid: &str, c: &Cached) -> R<()> {
    let blob = serde_json::to_string(&c.fitted).map_err(internal)?;
    lock(st)
        .execute(
            "INSERT INTO fitted_models(company_id, fit_revision, full_fit_at, full_fit_ops, ops_added, model_version, blob) VALUES(?,?,?,?,?,?,?)
             ON CONFLICT(company_id) DO UPDATE SET fit_revision=excluded.fit_revision, full_fit_at=excluded.full_fit_at, full_fit_ops=excluded.full_fit_ops,
             ops_added=excluded.ops_added, model_version=excluded.model_version, blob=excluded.blob",
            params![cid, c.fit_revision, c.full_fit_at, c.full_fit_ops as i64, c.ops_added as i64, c.fitted.model_version, blob],
        )
        .map_err(internal)?;
    Ok(())
}

fn too_old(full_fit_at: &str) -> bool {
    chrono::DateTime::parse_from_rfc3339(full_fit_at).map_or(true, |t| (chrono::Utc::now() - t.with_timezone(&chrono::Utc)).num_days() > FULL_FIT_MAX_AGE_DAYS)
}

/// Обученная модель для расчёта и события компании. В режиме «Обновлять по новым данным» сохранённая модель
/// дописывается операциями, появившимися после её ревизии (или используется как есть, если изменились только события);
/// иначе — полный пересчёт по всей истории. Обученная модель сохраняется в обоих режимах, чтобы переключение режима
/// сразу давало быстрое обновление.
async fn prepare(st: &AppState, ctx: &Ctx, cid: &str, force_full: bool) -> R<(Arc<Fitted>, Arc<Live>, i64, UpdateInfo)> {
    let incremental = update_mode(st, cid) == "incremental";
    let mut reason = if force_full { "forced" } else if !incremental { "mode_full" } else { "first" };
    let cached = if incremental && !force_full { load_cached(st, cid) } else { None };
    if let Some(mut c) = cached {
        reason = "expired";
        if !too_old(&c.full_fit_at) {
            let delta = snapshot_since(st, ctx, cid, c.fit_revision).await?;
            reason = "reset";
            if delta.tx_reset_rev <= c.fit_revision {
                let (txs, balances) = history_of(&delta);
                let rev = delta.data_revision;
                let events = Live::of(delta);
                let (res, mut c) = tokio::task::spawn_blocking(move || {
                    let r = c.fitted.advance(&txs, &balances);
                    (r, c)
                })
                .await
                .map_err(internal)?;
                reason = match res {
                    Ok(n) => {
                        c.ops_added += n;
                        if c.ops_added as f64 > c.full_fit_ops as f64 * FULL_FIT_MAX_GROWTH { "grown" } else if n == 0 { "events_only" } else { "new_operations" }
                    }
                    Err(_) => "backdated",
                };
                if reason == "events_only" || reason == "new_operations" {
                    c.fit_revision = rev;
                    save_cached(st, cid, &c)?;
                    let info = UpdateInfo { mode: "incremental", reason, ops_added: c.ops_added, full_fit_at: c.full_fit_at.clone() };
                    return Ok((Arc::new(c.fitted), events, rev, info));
                }
            }
        }
    }
    let snap = snapshot(st, ctx, cid).await?;
    let rev = snap.data_revision;
    let (txs, balances) = history_of(&snap);
    let live = Live::of(snap);
    let fitted = tokio::task::spawn_blocking(move || model::fit(&txs, &balances)).await.map_err(internal)?.map_err(map_fc_err)?;
    let c = Cached { full_fit_ops: fitted.n_ops, fitted, fit_revision: rev, full_fit_at: now(), ops_added: 0 };
    save_cached(st, cid, &c)?;
    let info = UpdateInfo { mode: "full", reason, ops_added: 0, full_fit_at: c.full_fit_at.clone() };
    Ok((Arc::new(c.fitted), live, rev, info))
}

fn save_run(st: &AppState, cid: &str, rev: i64, scenario: Option<&Scenario>, res: &model::ForecastResult, upd: &UpdateInfo) -> R<(String, String)> {
    let id = uuid::Uuid::new_v4().to_string();
    let at = now();
    let c = lock(st);
    c.execute(
        "INSERT INTO forecast_runs(id, company_id, created_at, data_revision, scenario, params, result) VALUES(?,?,?,?,?,?,?)",
        params![
            id, cid, at, rev, scenario.map(|s| serde_json::to_string(s).unwrap()),
            json!({"horizon": res.horizon, "seed": res.seed, "model": res.model_version, "n_paths": res.n_paths, "update": upd}).to_string(),
            serde_json::to_string(res).unwrap()
        ],
    )
    .map_err(internal)?;
    // пошаговый журнал расчёта в лог сервиса (docker compose logs forecast)
    let kind = if scenario.is_some() { "scenario" } else { "forecast" };
    eprintln!("[{at}] {kind} {id} company={cid} revision={rev} update={}", serde_json::to_string(upd).unwrap_or_default());
    for t in &res.trace {
        eprintln!("[{at}] {kind} {id} step {}. {}: {}", t.step, t.title, t.detail);
    }
    Ok((id, at))
}

fn run_json(id: &str, at: &str, rev: i64, stale: bool, res: &model::ForecastResult, scenario: Option<&Scenario>, upd: &UpdateInfo) -> Value {
    json!({"run_id": id, "created_at": at, "data_revision": rev, "stale": stale, "scenario": scenario, "result": res, "update": upd})
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
    /// «Пересчитать целиком»: заново обучить модель по всей истории даже в режиме «Обновлять по новым данным»
    #[serde(default)]
    full: bool,
}

pub async fn create_forecast(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<ForecastBody>>) -> R<(StatusCode, Json<Value>)> {
    let b = body.map(|b| b.0).unwrap_or_default();
    let h = horizon_of(b.horizon_days)?;
    let (f, events, rev, upd) = prepare(&st, &ctx, &cid, b.full).await?;
    let res = simulate(f, events, h, Scenario::default()).await?;
    let (id, at) = save_run(&st, &cid, rev, None, &res, &upd)?;
    Ok((StatusCode::CREATED, money(run_json(&id, &at, rev, false, &res, None, &upd))))
}

fn read_run(st: &AppState, cid: &str, cur: i64, rid: Option<&str>) -> R<Json<Value>> {
    let c = lock(st);
    let sql = if rid.is_some() {
        "SELECT id, created_at, data_revision, scenario, result, params FROM forecast_runs WHERE company_id=?1 AND id=?2"
    } else {
        "SELECT id, created_at, data_revision, scenario, result, params FROM forecast_runs WHERE company_id=?1 AND scenario IS NULL AND ?2='' ORDER BY created_at DESC LIMIT 1"
    };
    let row: Option<(String, String, i64, Option<String>, String, String)> = c
        .query_row(sql, params![cid, rid.unwrap_or("")], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))
        .optional()
        .map_err(internal)?;
    let (id, at, rev, sc, result, prm) = row.ok_or_else(|| err(StatusCode::NOT_FOUND, "no_forecast", "Прогноз ещё не построен"))?;
    // прогоны до появления режимов обновления — полный пересчёт
    let upd = serde_json::from_str::<Value>(&prm).ok().map(|p| p["update"].clone()).filter(|u| !u.is_null()).unwrap_or_else(|| json!({"mode": "full", "reason": "mode_full", "ops_added": 0, "full_fit_at": at}));
    let mut v = json!({"run_id": id, "created_at": at, "data_revision": rev, "stale": rev != cur,
        "scenario": sc.and_then(|s| serde_json::from_str::<Value>(&s).ok()), "result": serde_json::from_str::<Value>(&result).map_err(internal)?, "update": upd});
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
    let Ok((f, events, rev, upd)) = prepare(st, ctx, cid, false).await else { return false };
    match simulate(f, events, horizon, Scenario::default()).await {
        Ok(res) => save_run(st, cid, rev, None, &res, &upd).is_ok(),
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
    // базовый и сценарный расчёты используют одну обученную модель и один seed — разница объясняется только сценарием
    let (f, events, rev, upd) = prepare(&st, &ctx, &cid, false).await?;
    let base = simulate(f.clone(), events.clone(), h, Scenario::default()).await?;
    let sc = simulate(f, events, h, b.scenario.clone()).await?;
    let (bid, bat) = save_run(&st, &cid, rev, None, &base, &upd)?;
    let (sid, sat) = save_run(&st, &cid, rev, Some(&b.scenario), &sc, &upd)?;
    let out = json!({
        "base": run_json(&bid, &bat, rev, false, &base, None, &upd),
        "scenario": run_json(&sid, &sat, rev, false, &sc, Some(&b.scenario), &upd),
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

// ---------- режим обновления прогноза ----------
#[derive(Deserialize)]
pub struct SettingsBody {
    update_mode: String,
}

pub async fn get_settings(State(st): State<AppState>, Path(cid): Path<String>) -> Json<Value> {
    Json(json!({"update_mode": update_mode(&st, &cid)}))
}

pub async fn put_settings(State(st): State<AppState>, Path(cid): Path<String>, Json(b): Json<SettingsBody>) -> R<Json<Value>> {
    if !["full", "incremental"].contains(&b.update_mode.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_update_mode", "Режим обновления: full или incremental"));
    }
    lock(&st)
        .execute(
            "INSERT INTO forecast_settings(company_id, update_mode, updated_at) VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET update_mode=excluded.update_mode, updated_at=excluded.updated_at",
            params![cid, b.update_mode, now()],
        )
        .map_err(internal)?;
    Ok(Json(json!({"update_mode": b.update_mode})))
}

// ---------- оценка качества на контрольном датасете ----------

#[derive(Deserialize)]
pub struct EvalRow {
    date: NaiveDate,
    /// подписанная сумма, коп.
    amount: i64,
    #[serde(default)]
    counterparty: String,
    #[serde(default)]
    inn: String,
    #[serde(default)]
    purpose: String,
    /// остаток на конец дня, коп.
    balance: Option<i64>,
}

#[derive(Deserialize)]
pub struct EvalBody {
    company: String,
    rows: Vec<EvalRow>,
}

const MAX_EVAL_ROWS: usize = 50_000;

/// POST evaluate {company, rows} -> бэктест одной компании контрольного датасета, без сохранения в базу.
/// Те же параметры и разметка, что в `eval` (forecast/src/bin/eval.rs): итог по всем компаниям собирает интерфейс.
pub async fn evaluate(Json(b): Json<EvalBody>) -> R<Json<Value>> {
    if b.rows.is_empty() || b.rows.len() > MAX_EVAL_ROWS {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_rows", "Нужно от 1 до 50 000 операций компании"));
    }
    let company = b.company.chars().take(64).collect::<String>();
    let ops = b.rows.len();
    let r = tokio::task::spawn_blocking(move || {
        let rows: Vec<(Tx, Option<i64>)> = b
            .rows
            .into_iter()
            .map(|r| (Tx { date: r.date, amount: r.amount, counterparty: r.counterparty, inn: r.inn, purpose: r.purpose, category: "x".into() }, r.balance))
            .collect();
        let balances = model::daily_balances(&rows);
        let txs: Vec<Tx> = rows.into_iter().map(|r| r.0).collect();
        model::backtest(&txs, &balances, 14)
    })
    .await
    .map_err(internal)?;
    let mut v = json!({"company": company, "operations": ops, "backtest": r});
    stringify_money(&mut v);
    Ok(Json(v))
}
