//! Консультант: публичные ручки для SPA. Вопрос уходит в сервис advisor (Hermes Agent по подписке Codex или агент через
//! llm_gateway; без модели — шаблонный ответ из прогноза). Что консультанту можно, решает владелец или администратор в
//! настройках (`advisor/settings`, раздел `advisor` в ledger); права записи получает только пользователь с правом edit.
//! Предложения применяет пользователь запросом `suggestions/apply`, и здесь параметры проверяются заново (ответу модели не верим).

use crate::AppState;
use crate::http::{Administrator, Editor, Perm, Principal, Reader};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
};
use chrono::{Duration, NaiveDate};
use common::{Ctx, R, err};
use serde::Deserialize;
use serde_json::{Map, Value, json};

const MAX_QUESTION: usize = 2000;
const MAX_HISTORY: usize = 20;
const MAX_HISTORY_TEXT: usize = 4000;
/// Единственный тип предложения, который можно применить.
pub const ADD_EXPECTED_PAYMENT: &str = "add_expected_payment";

#[derive(Deserialize)]
pub struct HistoryItem {
    role: String,
    content: String,
}

#[derive(Deserialize)]
pub struct AskBody {
    question: String,
    #[serde(default)]
    history: Vec<HistoryItem>,
    #[serde(default)]
    lang: Option<String>,
}

/// Права консультанта: код и нужно ли право edit у пользователя (тот же каталог, что в advisor/permissions.py).
pub const PERMISSIONS: [(&str, bool); 8] = [
    ("read_forecast", false),
    ("read_operations", false),
    ("read_counterparties", false),
    ("run_scenarios", false),
    ("propose_payments", false),
    ("make_plans", false),
    ("add_payments", true),
    ("recalc_forecast", true),
];

fn default_permissions() -> Vec<String> {
    ["read_forecast", "read_operations", "read_counterparties", "run_scenarios", "propose_payments", "make_plans"].map(String::from).to_vec()
}

fn yes() -> bool {
    true
}

#[derive(Deserialize, serde::Serialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AdvisorSettings {
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default = "default_permissions")]
    pub permissions: Vec<String>,
    /// подсказки «что написать» под полем чата
    #[serde(default = "yes")]
    pub show_prompts: bool,
}

impl Default for AdvisorSettings {
    fn default() -> Self {
        Self { enabled: true, permissions: default_permissions(), show_prompts: true }
    }
}

impl AdvisorSettings {
    fn validate(&self) -> R<()> {
        if let Some(bad) = self.permissions.iter().find(|p| !PERMISSIONS.iter().any(|(k, _)| k == p)) {
            return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_permission", format!("Неизвестное право консультанта: {}", chars(bad, 40))));
        }
        Ok(())
    }
    /// Права для этого пользователя: выданные владельцем, без записи для роли без права edit; порядок каталога.
    pub fn effective(&self, p: &Principal) -> Vec<String> {
        PERMISSIONS
            .iter()
            .filter(|(k, edit)| self.permissions.iter().any(|x| x == k) && (!edit || p.role.can(Perm::Edit)))
            .map(|(k, _)| k.to_string())
            .collect()
    }
}

async fn load_settings(st: &AppState, ctx: &Ctx, cid: &str) -> R<(AdvisorSettings, Value)> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/settings/advisor"), ctx).await?;
    Ok((serde_json::from_value(v["value"].clone()).unwrap_or_default(), v["updated_at"].clone()))
}

/// Состояние движка (Hermes подключён ли) из advisor; сервис недоступен — null.
async fn engine(st: &AppState, ctx: &Ctx) -> Value {
    st.advisor.get("/internal/v1/engine", ctx).await.map(|v| json!({"engine": v["engine"], "hermes": v["hermes"]})).unwrap_or(Value::Null)
}

fn settings_json(s: &AdvisorSettings, p: &Principal, updated_at: &Value, engine: Value) -> Value {
    let catalog: Vec<Value> = PERMISSIONS.iter().map(|(k, edit)| json!({"code": k, "needs_edit": edit})).collect();
    json!({"settings": s, "defaults": AdvisorSettings::default(), "updated_at": updated_at, "catalog": catalog,
           "effective": s.effective(p), "engine": engine})
}

/// GET /api/v1/companies/{cid}/advisor/settings -> {settings, defaults, updated_at, catalog, effective, engine}
pub async fn get_settings(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let (s, at) = load_settings(&st, &ctx, &cid).await?;
    Ok(Json(settings_json(&s, &p, &at, engine(&st, &ctx).await)))
}

/// PUT /api/v1/companies/{cid}/advisor/settings {enabled, permissions[], show_prompts} — владелец и администратор
pub async fn put_settings(State(st): State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<Value>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let mut s: AdvisorSettings = serde_json::from_value(b).map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_settings", format!("Настройки консультанта: {e}")))?;
    s.validate()?;
    s.permissions = PERMISSIONS.iter().filter(|(k, _)| s.permissions.iter().any(|x| x == k)).map(|(k, _)| k.to_string()).collect();
    let actx = actor(&ctx, &p);
    let r = st.ledger.json(axum::http::Method::PUT, &format!("/internal/v1/companies/{cid}/settings/advisor"), &actx, Some(&json!(s))).await?;
    if !r.status.is_success() {
        return Err(err(StatusCode::BAD_GATEWAY, "settings_not_saved", "Настройки не сохранены"));
    }
    audit(&st, &ctx, &p, "advisor.settings", &s.permissions.join(",")).await;
    Ok(Json(settings_json(&s, &p, &r.json()["updated_at"], engine(&st, &ctx).await)))
}

fn disabled() -> common::ApiError {
    err(StatusCode::FORBIDDEN, "advisor_disabled", "ИИ-консультант выключен в настройках компании")
}

/// GET /api/v1/companies/{cid}/advisor/prompts?lang=&mode=chat|plan -> {items:[{text, kind}]} — подсказки под полем чата
pub async fn prompts(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>, axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let (s, _) = load_settings(&st, &ctx, &cid).await?;
    if !s.enabled {
        return Err(disabled());
    }
    if !s.show_prompts {
        return Ok(Json(json!({"items": []})));
    }
    let lang = if q.get("lang").map(String::as_str) == Some("en") { "en" } else { "ru" };
    let perms = s.effective(&p).join(",");
    let mode = if q.get("mode").map(String::as_str) == Some("plan") { "plan" } else { "chat" };
    let v = st.advisor.get(&format!("/internal/v1/companies/{cid}/prompts?lang={lang}&permissions={perms}&mode={mode}"), &actor(&ctx, &p)).await?;
    let items: Vec<Value> = v["items"]
        .as_array()
        .map(|a| a.iter().take(6).map(|i| json!({"text": chars(i["text"].as_str().unwrap_or_default(), 160), "kind": i["kind"]})).collect())
        .unwrap_or_default();
    Ok(Json(json!({"items": items})))
}

#[derive(Deserialize)]
pub struct ApplyBody {
    #[serde(rename = "type")]
    kind: String,
    params: Value,
}

fn authorize(p: &Principal, cid: &str) -> R<()> {
    if p.company_id == cid { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn actor(ctx: &Ctx, p: &Principal) -> Ctx {
    Ctx { rid: ctx.rid.clone(), actor: p.user_id.clone() }
}

fn chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Тело запроса к advisor: вопрос, последние реплики и контекст компании.
fn advisor_request(b: &AskBody, company_name: &str) -> R<Value> {
    let question = b.question.trim();
    if question.is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_question", "Введите вопрос"));
    }
    if question.chars().count() > MAX_QUESTION {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_question", "Вопрос длиннее 2000 символов"));
    }
    let lang = match b.lang.as_deref() {
        None | Some("ru") => "ru",
        Some("en") => "en",
        _ => return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_lang", "lang: ru | en")),
    };
    let skip = b.history.len().saturating_sub(MAX_HISTORY);
    let history: Vec<Value> = b
        .history
        .iter()
        .skip(skip)
        .filter(|h| (h.role == "user" || h.role == "assistant") && !h.content.trim().is_empty())
        .map(|h| json!({"role": h.role, "content": chars(&h.content, MAX_HISTORY_TEXT)}))
        .collect();
    Ok(json!({"question": question, "history": history, "lang": lang, "context": {"company_name": chars(company_name, 200)}}))
}

/// POST /api/v1/companies/{cid}/advisor/ask -> {answer, suggestions[], actions[], followups[], provider, model, notice, usage, tools_used}
/// 403 advisor_disabled — консультант выключен в настройках.
pub async fn ask(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<AskBody>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let mut body = advisor_request(&b, &p.company_name)?;
    let (s, _) = load_settings(&st, &ctx, &cid).await?;
    if !s.enabled {
        return Err(disabled());
    }
    body["permissions"] = json!(s.effective(&p));
    let v = st.advisor.post(&format!("/internal/v1/companies/{cid}/ask"), &actor(&ctx, &p), &body).await?;
    // наружу — только известные поля; предложения проходят ту же проверку, что и при применении
    let suggestions: Vec<Value> = v["suggestions"]
        .as_array()
        .map(|a| a.iter().filter_map(|s| clean_suggestion(s, chrono::Local::now().date_naive())).collect())
        .unwrap_or_default();
    let out = json!({
        "answer": v["answer"].as_str().unwrap_or_default(),
        "suggestions": suggestions,
        "provider": v["provider"].as_str().unwrap_or("none"),
        "model": v["model"],
        "notice": v["notice"],
        "usage": v["usage"],
        "tools_used": v["tools_used"],
        // что консультант записал сам (по праву add_payments/recalc_forecast) и подсказки следующего вопроса
        "actions": clean_actions(&v["actions"]),
        "followups": v["followups"].as_array().map(|a| a.iter().filter_map(Value::as_str).take(3).map(|q| chars(q, 160)).collect::<Vec<_>>()).unwrap_or_default(),
    });
    audit(&st, &ctx, &p, "advisor.ask", out["provider"].as_str().unwrap_or("none")).await;
    Ok(Json(out))
}

fn clean_actions(v: &Value) -> Vec<Value> {
    v.as_array()
        .map(|a| {
            a.iter()
                .take(5)
                .filter_map(|x| {
                    let kind = x["type"].as_str().filter(|k| matches!(*k, ADD_EXPECTED_PAYMENT | "recalc_forecast"))?;
                    Some(json!({"type": kind, "id": x["id"].as_str().unwrap_or_default(), "title": chars(x["title"].as_str().unwrap_or_default(), 300)}))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn clean_suggestion(s: &Value, today: NaiveDate) -> Option<Value> {
    if s["type"].as_str()? != ADD_EXPECTED_PAYMENT {
        return None;
    }
    let params = validate_payment(&s["params"], today).ok()?;
    Some(json!({
        "id": s["id"].as_str().unwrap_or_default(),
        "type": ADD_EXPECTED_PAYMENT,
        "params": {"direction": params.direction, "amount_kop": params.amount_kop.to_string(), "date": params.date, "title": params.title, "counterparty": params.counterparty},
        "title": s["title"].as_str().unwrap_or_default(),
    }))
}

pub struct Payment {
    pub direction: String,
    pub amount_kop: i64,
    pub date: NaiveDate,
    pub title: String,
    pub counterparty: String,
}

fn bad(code: &str, msg: &str) -> common::ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, code, msg)
}

/// Проверка параметров «ожидаемый платёж»: те же границы, что у создания события в ledger.
pub fn validate_payment(params: &Value, today: NaiveDate) -> R<Payment> {
    let o: &Map<String, Value> = params.as_object().ok_or_else(|| bad("invalid_params", "params должен быть объектом"))?;
    let direction = match o.get("direction").and_then(Value::as_str) {
        Some(d @ ("in" | "out")) => d.to_string(),
        _ => return Err(bad("invalid_direction", "direction: in | out")),
    };
    let amount_kop = match o.get("amount_kop") {
        Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
        Some(Value::Number(n)) => n.as_i64(),
        _ => None,
    }
    .filter(|a| *a > 0 && *a < 1_000_000_000_000_000)
    .ok_or_else(|| bad("invalid_amount", "Сумма должна быть положительным целым числом копеек"))?;
    let date = o
        .get("date")
        .and_then(Value::as_str)
        .and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok())
        .ok_or_else(|| bad("invalid_date", "Дата в формате ГГГГ-ММ-ДД"))?;
    if date < today - Duration::days(120) || date > today + Duration::days(730) {
        return Err(bad("invalid_date", "Дата вне допустимого диапазона"));
    }
    let title = o.get("title").and_then(Value::as_str).unwrap_or_default().trim().to_string();
    if title.is_empty() || title.chars().count() > 200 {
        return Err(bad("invalid_title", "Название от 1 до 200 символов"));
    }
    let counterparty = o.get("counterparty").and_then(Value::as_str).unwrap_or_default().trim().to_string();
    if counterparty.chars().count() > 200 {
        return Err(bad("invalid_counterparty", "Контрагент не длиннее 200 символов"));
    }
    Ok(Payment { direction, amount_kop, date, title, counterparty })
}

/// POST /api/v1/companies/{cid}/advisor/suggestions/apply {type, params} -> 201 {id, type}
/// Создаёт ожидаемое событие в ledger (обычная функция создания события), источник `manual`.
pub async fn apply(State(st): State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<ApplyBody>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    if b.kind != ADD_EXPECTED_PAYMENT {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_type", "Неизвестный тип предложения"));
    }
    let pay = validate_payment(&b.params, chrono::Local::now().date_naive())?;
    let event = json!({
        "date": pay.date, "amount_kop": pay.amount_kop.to_string(), "direction": pay.direction,
        "title": pay.title, "counterparty": pay.counterparty, "source": "manual", "confidence": 0.8,
    });
    let actx = actor(&ctx, &p);
    let r = st.ledger.post(&format!("/internal/v1/companies/{cid}/expected-events"), &actx, &event).await?;
    let id = r["id"].as_str().unwrap_or_default().to_string();
    audit(&st, &ctx, &p, "advisor.suggestion.apply", &id).await;
    Ok((StatusCode::CREATED, Json(json!({"id": id, "type": ADD_EXPECTED_PAYMENT}))))
}

async fn audit(st: &AppState, ctx: &Ctx, p: &Principal, action: &str, object: &str) {
    let _ = st
        .ledger
        .post(&format!("/internal/v1/companies/{}/audit", p.company_id), &actor(ctx, p), &json!({"action": action, "object_id": object, "outcome": "ok"}))
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }
    const TODAY: &str = "2026-10-01";

    fn ok() -> Value {
        json!({"direction": "in", "amount_kop": "25000050", "date": "2026-10-12", "title": " Предоплата ", "counterparty": "ООО «Ромашка»"})
    }

    #[test]
    fn valid_payment_is_normalized() {
        let p = validate_payment(&ok(), d(TODAY)).unwrap();
        assert_eq!((p.direction.as_str(), p.amount_kop, p.title.as_str(), p.date), ("in", 25_000_050, "Предоплата", d("2026-10-12")));
        let mut v = ok();
        v["amount_kop"] = json!(100);
        v.as_object_mut().unwrap().remove("counterparty");
        let p = validate_payment(&v, d(TODAY)).unwrap();
        assert_eq!((p.amount_kop, p.counterparty.as_str()), (100, ""));
    }

    #[test]
    fn invalid_payments_are_rejected_with_codes() {
        let code = |mut v: Value, k: &str, x: Value| {
            v[k] = x;
            validate_payment(&v, d(TODAY)).err().map(|e| e.1)
        };
        assert_eq!(code(ok(), "direction", json!("up")).as_deref(), Some("invalid_direction"));
        for a in [json!("0"), json!("-5"), json!("1.5"), json!("abc"), json!(0), json!(1.5), json!(1_000_000_000_000_000i64), Value::Null] {
            assert_eq!(code(ok(), "amount_kop", a).as_deref(), Some("invalid_amount"));
        }
        for a in [json!("2026-13-01"), json!("12.10.2026"), json!("2026-05-01"), json!("2028-10-02"), json!(5)] {
            assert_eq!(code(ok(), "date", a).as_deref(), Some("invalid_date"));
        }
        assert_eq!(code(ok(), "title", json!("   ")).as_deref(), Some("invalid_title"));
        assert_eq!(code(ok(), "title", json!("я".repeat(201))).as_deref(), Some("invalid_title"));
        assert_eq!(code(ok(), "counterparty", json!("я".repeat(201))).as_deref(), Some("invalid_counterparty"));
        assert!(validate_payment(&json!("str"), d(TODAY)).is_err());
        assert!(validate_payment(&json!({}), d(TODAY)).is_err());
    }

    #[test]
    fn date_range_boundaries_match_ledger() {
        let mut v = ok();
        for (date, good) in [("2026-06-03", true), ("2026-06-02", false), ("2028-09-30", true), ("2028-10-01", false)] {
            v["date"] = json!(date);
            assert_eq!(validate_payment(&v, d(TODAY)).is_ok(), good, "{date}");
        }
    }

    #[test]
    fn suggestions_from_advisor_are_filtered() {
        let good = json!({"id": "a1", "type": ADD_EXPECTED_PAYMENT, "params": ok(), "title": "Добавить"});
        let c = clean_suggestion(&good, d(TODAY)).unwrap();
        assert_eq!((c["params"]["amount_kop"].as_str(), c["id"].as_str(), c["params"]["title"].as_str()), (Some("25000050"), Some("a1"), Some("Предоплата")));
        assert!(clean_suggestion(&json!({"id": "x", "type": "delete_everything", "params": ok()}), d(TODAY)).is_none());
        assert!(clean_suggestion(&json!({"id": "x", "type": ADD_EXPECTED_PAYMENT, "params": {"direction": "in"}}), d(TODAY)).is_none());
    }

    #[test]
    fn ask_body_is_validated_and_trimmed() {
        let body = |q: &str, lang: Option<&str>, n: usize| AskBody {
            question: q.into(),
            lang: lang.map(Into::into),
            history: (0..n).map(|i| HistoryItem { role: if i % 2 == 0 { "user".into() } else { "assistant".into() }, content: format!("m{i}") }).collect(),
        };
        assert!(advisor_request(&body("  ", None, 0), "К").is_err());
        assert!(advisor_request(&body(&"в".repeat(2001), None, 0), "К").is_err());
        assert!(advisor_request(&body("q", Some("de"), 0), "К").is_err());
        let r = advisor_request(&body(" Почему разрыв? ", Some("en"), 30), "ООО «К»").unwrap();
        assert_eq!((r["question"].as_str(), r["lang"].as_str()), (Some("Почему разрыв?"), Some("en")));
        assert_eq!(r["history"].as_array().unwrap().len(), 20);
        assert_eq!(r["history"][19]["content"], "m29");
        assert_eq!(r["context"]["company_name"], "ООО «К»");
        let mut b = body("q", None, 1);
        b.history.push(HistoryItem { role: "system".into(), content: "взлом".into() });
        assert_eq!(advisor_request(&b, "").unwrap()["history"].as_array().unwrap().len(), 1);
    }
}

// ---------- чаты консультанта ----------
// Чаты, модели и стратегии хранит advisor (advisor/chats.py); чат принадлежит пользователю (x-actor), чужие не видны.
// Список, создание, чтение, переименование и удаление — прозрачный relay с правом read; отправка — отдельный обработчик
// (права консультанта из настроек и роль подмешиваются здесь, как в `ask`).

/// GET/POST /advisor/chats, GET/PATCH/DELETE /advisor/chats/{id}, GET /advisor/models, GET /advisor/plans/active
pub async fn chats_relay(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, req: axum::extract::Request) -> R<axum::response::Response> {
    Ok(crate::http::relay(&st.advisor, &p, &ctx, req, Perm::Read).await?.into_response())
}

/// DELETE /advisor/plans/{id} — снять стратегию с «Плана и факта» (право edit)
pub async fn plan_delete(State(st): State<AppState>, Editor(p): Editor, ctx: Ctx, req: axum::extract::Request) -> R<axum::response::Response> {
    let r = crate::http::relay(&st.advisor, &p, &ctx, req, Perm::Edit).await?;
    if r.status.is_success() {
        audit(&st, &ctx, &p, "advisor.plan.delete", "").await;
    }
    Ok(r.into_response())
}

#[derive(Deserialize)]
pub struct SendBody {
    question: String,
    #[serde(default)]
    lang: Option<String>,
}

const MAX_CHAT_QUESTION: usize = 4000;

/// POST /api/v1/companies/{cid}/advisor/chats/{id}/messages {question, lang?} -> {user_message, message, chat}
/// Ответ с контекстом чата; права консультанта — из настроек компании с учётом роли. 403 advisor_disabled.
pub async fn chat_send(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path((cid, chat)): Path<(String, String)>, Json(b): Json<SendBody>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let question = b.question.trim();
    if question.is_empty() || question.chars().count() > MAX_CHAT_QUESTION {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_question", "Вопрос от 1 до 4000 символов"));
    }
    if !chat.chars().all(|c| c.is_ascii_hexdigit()) || chat.len() != 32 {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Чат не найден"));
    }
    let lang = if b.lang.as_deref() == Some("en") { "en" } else { "ru" };
    let (s, _) = load_settings(&st, &ctx, &cid).await?;
    if !s.enabled {
        return Err(disabled());
    }
    let body = json!({"question": question, "lang": lang, "permissions": s.effective(&p), "context": {"company_name": chars(&p.company_name, 200)}});
    let mut v = st.advisor.post(&format!("/internal/v1/companies/{cid}/advisor/chats/{chat}/messages"), &actor(&ctx, &p), &body).await?;
    // предложения в ответе проходят ту же проверку, что и при применении
    let today = chrono::Local::now().date_naive();
    let meta = &mut v["message"]["meta"];
    let sugg: Vec<Value> = meta["suggestions"].as_array().map(|a| a.iter().filter_map(|x| clean_suggestion(x, today)).collect()).unwrap_or_default();
    meta["suggestions"] = json!(sugg);
    meta["actions"] = json!(clean_actions(&meta["actions"]));
    audit(&st, &ctx, &p, "advisor.chat.message", v["message"]["meta"]["provider"].as_str().unwrap_or("none")).await;
    Ok(Json(json!({"user_message": v["user_message"], "message": v["message"], "chat": v["chat"]})))
}
