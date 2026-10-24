//! Демо «Прошла неделя»: у демо-пользователя «сегодня» — дата отсечки внутри выписки (последняя операция в ledger), а
//! остальные операции датасета лежат в сервисе demo_feed. Кнопка в интерфейсе вызывает `advance-week`: api берёт у
//! ленты операции следующих 7 дней и прогоняет их через ОБЫЧНЫЙ импорт ledger (upload -> confirm). Прогноз пересчитывается
//! сам (forecast: `latest` пересчитывает устаревший базовый прогноз), api только замеряет время расчёта и сравнивает
//! прежний прогноз с фактом.
//!
//! Кто считается демо-компанией: у ledger нет признака «демо», поэтому демо — это компания пользователя с e-mail
//! `DEMO_EMAIL` (по умолчанию `demo@example.com`, его заводит auth при `DEMO_SEED=1`) и только при `DEMO_FEED_ENABLED=1`.
//! Для всех остальных `status` отвечает `{"enabled": false}`, а `advance-week` — 403 `demo_disabled`.

use crate::AppState;
use crate::http::{Editor, Principal, Reader};
use axum::{
    Json,
    extract::{Path, State},
    http::{Method, StatusCode},
};
use chrono::NaiveDate;
use common::{Ctx, R, Upstream, err, internal, mw::env};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Instant;

pub struct Demo {
    pub enabled: bool,
    /// e-mail демо-пользователя
    pub email: String,
    /// код компании в датасете, чьи операции отдаёт лента
    pub external_company: String,
    pub feed: Upstream,
}

impl Demo {
    pub fn from_env() -> Self {
        Demo {
            enabled: env("DEMO_FEED_ENABLED", "0") == "1",
            email: env("DEMO_EMAIL", "demo@example.com"),
            external_company: env("DEMO_COMPANY", "C001"),
            feed: Upstream::new("demo_feed", "Сервис ленты демо", &env("DEMO_FEED_URL", "http://127.0.0.1:8012"), 30),
        }
    }

    /// Демо-компания: флаг включён, у пользователя есть настоящий e-mail и он равен DEMO_EMAIL.
    pub fn is_demo(&self, p: &Principal) -> bool {
        self.matches(&p.email, p.has_email)
    }

    fn matches(&self, email: &str, has_email: bool) -> bool {
        self.enabled && has_email && !self.email.is_empty() && email.trim().eq_ignore_ascii_case(self.email.trim())
    }
}

/// Неделя из ленты (ответ `GET /internal/v1/feed/week`).
#[derive(Deserialize, Debug)]
struct FeedWeek {
    from: NaiveDate,
    to: NaiveDate,
    count: usize,
    #[serde(default)]
    weeks_left: u64,
    filename: String,
    csv: String,
}

fn guard(st: &AppState, p: &Principal, cid: &str) -> R<()> {
    if p.company_id != cid {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено"));
    }
    if !st.demo.is_demo(p) {
        return Err(err(StatusCode::FORBIDDEN, "demo_disabled", "Демо-режим недоступен для этой компании"));
    }
    Ok(())
}

fn actor_ctx(ctx: &Ctx, p: &Principal) -> Ctx {
    Ctx { rid: ctx.rid.clone(), actor: p.user_id.clone() }
}

/// Дата последней операции компании в ledger — это и есть демо-«сегодня».
async fn last_tx_date(st: &AppState, ctx: &Ctx, cid: &str) -> R<Option<NaiveDate>> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/transactions?limit=1&offset=0&sort=date&order=desc"), ctx).await?;
    Ok(v["items"][0]["date"].as_str().and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok()))
}

fn feed_path(kind: &str, ext: &str, after: Option<NaiveDate>) -> String {
    match after {
        Some(d) => format!("/internal/v1/feed/{kind}?company={ext}&after={d}"),
        None => format!("/internal/v1/feed/{kind}?company={ext}"),
    }
}

/// GET .../demo/status
pub async fn status(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    if p.company_id != cid {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено"));
    }
    if !st.demo.is_demo(&p) {
        return Ok(Json(json!({"enabled": false})));
    }
    let actx = actor_ctx(&ctx, &p);
    let as_of = last_tx_date(&st, &actx, &cid).await?;
    let r = st.demo.feed.json(Method::GET, &feed_path("status", &st.demo.external_company, as_of), &actx, None).await;
    let Ok(r) = r.and_then(|r| r.into_result()) else {
        return Ok(Json(json!({"enabled": true, "available": false, "as_of": as_of})));
    };
    Ok(Json(json!({
        "enabled": true, "available": true, "as_of": as_of, "cutoff": r["cutoff"], "next_from": r["next_from"],
        "weeks_left": r["weeks_left"], "ops_left": r["ops_left"], "finished": r["finished"], "last_date": r["last_date"],
    })))
}

/// Только один «шаг недели» одновременно: два клика подряд не должны взять одну и ту же неделю дважды.
static STEP: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// POST .../demo/advance-week -> {added, from, to, new_as_of, ...}
pub async fn advance_week(State(st): State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>) -> R<(StatusCode, Json<Value>)> {
    guard(&st, &p, &cid)?;
    let actx = actor_ctx(&ctx, &p);
    let _one_at_a_time = STEP.lock().await;

    let as_of = last_tx_date(&st, &actx, &cid)
        .await?
        .ok_or_else(|| err(StatusCode::CONFLICT, "demo_not_seeded", "Сначала загрузите демо-выписку (infra/scripts/seed_demo.sh)"))?;
    let ext = &st.demo.external_company;
    let week: FeedWeek = serde_json::from_value(st.demo.feed.get(&feed_path("week", ext, Some(as_of)), &actx).await?).map_err(internal)?;

    // прежний прогноз: с чем сравнивать факт новой недели (его отсутствие не мешает)
    let prev = fetch_latest(&st, &actx, &cid).await.ok().flatten();

    // обычный импорт: upload (preview) -> confirm
    let up = st.ledger.upload(&format!("/internal/v1/companies/{cid}/imports"), &actx, week.filename.clone(), week.csv.into_bytes()).await?.into_result()?;
    let iid = up["import_id"].as_str().ok_or_else(|| internal("ledger: no import_id"))?.to_string();
    let conf = st.ledger.post(&format!("/internal/v1/companies/{cid}/imports/{iid}/confirm"), &actx, &json!({})).await?;
    let added = conf["inserted"].as_u64().unwrap_or(0);

    // пересчёт: чтение latest пересчитывает устаревший базовый прогноз; если прогноза ещё не было, строим новый
    let t0 = Instant::now();
    let mut fc = fetch_latest(&st, &actx, &cid).await?;
    if fc.is_none() {
        fc = Some(st.forecast.post(&format!("/internal/v1/companies/{cid}/forecasts"), &actx, &json!({"horizon_days": 30})).await?);
    }
    let recalc_ms = t0.elapsed().as_millis() as u64;
    let fc = fc.unwrap_or(Value::Null);

    let new_as_of = fc["result"]["as_of"].as_str().and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok()).unwrap_or(week.to);
    let actual = fc["result"]["opening_balance"].as_str().and_then(|s| s.parse::<i64>().ok());
    let vs = prev.as_ref().and_then(|pf| forecast_vs_fact(&pf["result"], new_as_of, actual?));

    let _ = st
        .ledger
        .post(&format!("/internal/v1/companies/{cid}/audit"), &actx, &json!({"action": "demo.advance_week", "object_id": iid, "outcome": "ok"}))
        .await;
    Ok((
        StatusCode::CREATED,
        Json(json!({
            "added": added, "skipped_duplicates": conf["skipped_duplicates"], "rows": week.count,
            "from": week.from, "to": week.to, "new_as_of": new_as_of, "previous_as_of": as_of,
            "weeks_left": week.weeks_left, "import_id": iid, "recalc_ms": recalc_ms, "forecast_vs_fact": vs,
        })),
    ))
}

/// Последний базовый прогноз; None, если его ещё нет (404 от forecast).
async fn fetch_latest(st: &AppState, ctx: &Ctx, cid: &str) -> R<Option<Value>> {
    let r = st.forecast.json(Method::GET, &format!("/internal/v1/companies/{cid}/forecasts/latest"), ctx, None).await?;
    if r.status == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    r.into_result().map(Some)
}

/// Сравнение прежнего прогноза с фактом: остаток на дату `date` по прогнозу (P10/P50/P90) и фактический остаток.
/// `prev_result` — поле `result` прогноза, построенного до новой недели. None, если даты нет в его горизонте.
pub fn forecast_vs_fact(prev_result: &Value, date: NaiveDate, actual: i64) -> Option<Value> {
    let day = prev_result["days"].as_array()?.iter().find(|d| d["date"].as_str() == Some(&date.to_string()))?;
    let k = |f: &str| day[f].as_str().and_then(|s| s.parse::<i64>().ok());
    let (p10, p50, p90) = (k("p10")?, k("p50")?, k("p90")?);
    Some(json!({
        "date": date, "predicted_as_of": prev_result["as_of"],
        "p10": p10.to_string(), "p50": p50.to_string(), "p90": p90.to_string(),
        "actual": actual.to_string(), "error": (actual - p50).to_string(),
        "within_band": actual >= p10.min(p90) && actual <= p10.max(p90),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn demo(enabled: bool) -> Demo {
        Demo { enabled, email: "demo@example.com".into(), external_company: "C001".into(), feed: Upstream::new("demo_feed", "Лента", "http://127.0.0.1:1", 1) }
    }

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn demo_requires_flag_and_demo_email() {
        assert!(demo(true).matches("demo@example.com", true));
        assert!(demo(true).matches("  Demo@Example.COM ", true));
        assert!(!demo(false).matches("demo@example.com", true), "без DEMO_FEED_ENABLED демо выключено");
        assert!(!demo(true).matches("user@example.com", true));
        assert!(!demo(true).matches("demo@example.com", false), "служебный адрес ЭЦП не считается");
        let mut off = demo(true);
        off.email = String::new();
        assert!(!off.matches("", true));
    }

    #[test]
    fn feed_week_parses_service_answer() {
        let w: FeedWeek = serde_json::from_value(json!({"company": "C001", "from": "2026-07-13", "to": "2026-07-19", "window_end": "2026-07-19", "count": 11,
            "weeks_left": 10, "filename": "C001_2026-07-13_2026-07-19.csv", "csv": "h\nr\n"}))
        .unwrap();
        assert_eq!((w.from, w.to, w.count, w.weeks_left), (d("2026-07-13"), d("2026-07-19"), 11, 10));
        assert!(serde_json::from_value::<FeedWeek>(json!({"from": "вчера"})).is_err());
    }

    #[test]
    fn feed_path_has_cursor_only_when_known() {
        assert_eq!(feed_path("week", "C001", Some(d("2026-07-12"))), "/internal/v1/feed/week?company=C001&after=2026-07-12");
        assert_eq!(feed_path("status", "C001", None), "/internal/v1/feed/status?company=C001");
    }

    #[test]
    fn forecast_vs_fact_compares_with_median_and_band() {
        let prev = json!({"as_of": "2026-07-12", "days": [
            {"date": "2026-07-18", "p10": "900", "p50": "1000", "p90": "1100"},
            {"date": "2026-07-19", "p10": "950", "p50": "1050", "p90": "1150"}]});
        let v = forecast_vs_fact(&prev, d("2026-07-19"), 1200).unwrap();
        assert_eq!(v["p50"], "1050");
        assert_eq!(v["error"], "150");
        assert_eq!(v["within_band"], false);
        assert_eq!(v["predicted_as_of"], "2026-07-12");
        let ok = forecast_vs_fact(&prev, d("2026-07-18"), 1000).unwrap();
        assert_eq!((ok["error"].as_str(), ok["within_band"].as_bool()), (Some("0"), Some(true)));
    }

    #[test]
    fn forecast_vs_fact_none_outside_horizon_or_on_garbage() {
        let prev = json!({"days": [{"date": "2026-07-18", "p10": "1", "p50": "2", "p90": "3"}]});
        assert!(forecast_vs_fact(&prev, d("2026-09-01"), 5).is_none());
        assert!(forecast_vs_fact(&json!({}), d("2026-07-18"), 5).is_none());
        assert!(forecast_vs_fact(&json!({"days": [{"date": "2026-07-18", "p10": "x", "p50": "2", "p90": "3"}]}), d("2026-07-18"), 5).is_none());
    }
}
