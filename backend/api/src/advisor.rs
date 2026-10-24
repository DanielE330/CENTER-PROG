//! Консультант: публичные ручки для SPA. Вопрос уходит в сервис advisor (агент на облачной модели через llm_gateway,
//! без провайдера — шаблонный ответ из прогноза). Консультант только предлагает действия; применяет предложение
//! пользователь отдельным запросом `suggestions/apply`, и здесь параметры проверяются заново (ответу модели не верим).

use crate::AppState;
use crate::http::Principal;
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

/// POST /api/v1/companies/{cid}/advisor/ask -> {answer, suggestions[], provider, model, notice, usage, tools_used}
pub async fn ask(State(st): State<AppState>, p: Principal, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<AskBody>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let body = advisor_request(&b, &p.company_name)?;
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
    });
    audit(&st, &ctx, &p, "advisor.ask", out["provider"].as_str().unwrap_or("none")).await;
    Ok(Json(out))
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
pub async fn apply(State(st): State<AppState>, p: Principal, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<ApplyBody>) -> R<(StatusCode, Json<Value>)> {
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
