//! HTTP-слой публичного и внутреннего API. Формулы прогноза здесь не живут — см. forecast.rs.

use crate::forecast::{self, Event, ForecastError, Params, Scenario, Tx};
use crate::AppState;
use axum::{
    Json,
    extract::{FromRequestParts, Multipart, Path, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header, request::Parts},
    response::{IntoResponse, Response},
};
use chrono::{Datelike, Duration, NaiveDate};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};

// ---------- ошибки ----------
pub struct ApiError(StatusCode, &'static str, String);
type R<T> = Result<T, ApiError>;

fn err(s: StatusCode, code: &'static str, msg: impl Into<String>) -> ApiError {
    ApiError(s, code, msg.into())
}
fn internal<E: std::fmt::Display>(e: E) -> ApiError {
    eprintln!("internal error: {e}"); // без тел запросов и данных
    err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "Внутренняя ошибка")
}

pub fn error_response(s: StatusCode, code: &str, msg: &str, rid: &str) -> Response {
    (s, Json(json!({"error": {"code": code, "message": msg, "request_id": rid}}))).into_response()
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": {"code": self.1, "message": self.2}}))).into_response()
    }
}

// ---------- деньги ----------
const MONEY_KEYS: &[&str] = &[
    "opening_balance", "p10", "p50", "p90", "amount", "median_path_deficit", "max_deficit_p90",
    "deficit_given_break_median", "amount_kop", "balance_kop", "mae_model", "mae_last_value", "mae_drift",
    "plan_in", "plan_out", "fact_in", "fact_out", "plan", "fact", "diff", "predicted", "actual",
];

/// Суммы в JSON — десятичные строки (JS не теряет точность).
pub fn stringify_money(v: &mut Value) {
    match v {
        Value::Object(m) => {
            for (k, x) in m.iter_mut() {
                if MONEY_KEYS.contains(&k.as_str()) && x.is_i64() {
                    *x = Value::String(x.as_i64().unwrap().to_string());
                } else {
                    stringify_money(x);
                }
            }
        }
        Value::Array(a) => a.iter_mut().for_each(stringify_money),
        _ => {}
    }
}

fn money(mut v: Value) -> Json<Value> {
    stringify_money(&mut v);
    Json(v)
}

// ---------- аутентификация ----------
pub struct Principal {
    pub user_id: String,
    pub email: String,
    pub company_id: String,
}

fn cookie_token(h: &HeaderMap) -> Option<String> {
    let c = h.get(header::COOKIE)?.to_str().ok()?;
    c.split(';').filter_map(|p| p.trim().strip_prefix("session=")).next().map(|s| s.to_string())
}

fn lock(st: &AppState) -> std::sync::MutexGuard<'_, rusqlite::Connection> {
    st.db.lock().unwrap_or_else(|e| e.into_inner())
}

fn ensure_company(st: &AppState, user_id: &str, email: &str, name: Option<&str>) -> R<String> {
    let c = lock(st);
    if let Some(id) = c.query_row("SELECT id FROM companies WHERE owner_id=?", [user_id], |r| r.get::<_, String>(0)).optional().map_err(internal)? {
        return Ok(id);
    }
    let id = uuid::Uuid::new_v4().to_string();
    c.execute(
        "INSERT INTO companies(id, owner_id, name, created_at) VALUES(?,?,?,?)",
        params![id, user_id, name.map(str::to_string).unwrap_or_else(|| format!("Компания {}", email.split('@').next().unwrap_or(""))), now()],
    )
    .map_err(internal)?;
    Ok(id)
}

impl FromRequestParts<AppState> for Principal {
    type Rejection = ApiError;
    async fn from_request_parts(parts: &mut Parts, st: &AppState) -> R<Self> {
        let token = cookie_token(&parts.headers).ok_or_else(|| err(StatusCode::UNAUTHORIZED, "unauthenticated", "Требуется вход"))?;
        let r = st
            .http
            .post(format!("{}/internal/v1/session", st.cfg.auth_url))
            .json(&json!({"token": token}))
            .send()
            .await
            .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable", "Сервис входа недоступен"))?;
        if !r.status().is_success() {
            return Err(err(StatusCode::UNAUTHORIZED, "unauthenticated", "Сессия недействительна"));
        }
        let v: Value = r.json().await.map_err(internal)?;
        let user_id = v["user_id"].as_str().unwrap_or_default().to_string();
        let email = v["email"].as_str().unwrap_or_default().to_string();
        let company_id = ensure_company(st, &user_id, &email, None)?;
        Ok(Principal { user_id, email, company_id })
    }
}

/// Нельзя доверять company_id из пути: сверяем с principal. Чужая компания выглядит как несуществующая.
fn authorize(p: &Principal, cid: &str) -> R<()> {
    if p.company_id == cid { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

fn audit(st: &AppState, p: &Principal, action: &str, object: &str, outcome: &str) {
    let c = lock(st);
    let _ = c.execute(
        "INSERT INTO audit_events(at, actor, company_id, action, object_id, outcome) VALUES(?,?,?,?,?,?)",
        params![now(), p.user_id, p.company_id, action, object, outcome],
    );
}

#[derive(Deserialize)]
pub struct Creds {
    email: String,
    password: String,
    #[serde(default)]
    password_confirm: Option<String>,
    #[serde(default)]
    company_name: Option<String>,
}

async fn auth_call(st: &AppState, path: &str, body: Value) -> R<(StatusCode, Value)> {
    let r = st
        .http
        .post(format!("{}/internal/v1/{path}", st.cfg.auth_url))
        .json(&body)
        .send()
        .await
        .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "auth_unavailable", "Сервис входа недоступен"))?;
    let s = StatusCode::from_u16(r.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let v: Value = r.json().await.unwrap_or(Value::Null);
    Ok((s, v))
}

fn auth_err(s: StatusCode, v: &Value) -> ApiError {
    let code = v["detail"]["code"].as_str().unwrap_or("");
    match (s.as_u16(), code) {
        (401, _) => err(s, "invalid_credentials", "Неверный email или пароль"),
        (429, _) => err(s, "too_many_attempts", "Слишком много попыток входа, подождите несколько минут"),
        (409, _) => err(s, "email_taken", "Этот email уже зарегистрирован"),
        (422, "invalid_email") => err(s, "invalid_email", "Некорректный email"),
        (422, _) => err(s, "invalid_password", "Пароль должен быть от 8 до 128 символов"),
        _ => err(StatusCode::BAD_GATEWAY, "auth_error", "Ошибка сервиса входа"),
    }
}

fn session_response(st: &AppState, token: &str, ttl: i64, body: Value) -> Response {
    let secure = if st.cfg.cookie_secure { "; Secure" } else { "" };
    let mut res = Json(body).into_response();
    res.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_str(&format!("session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl}{secure}")).unwrap(),
    );
    res
}

pub async fn login(State(st): State<AppState>, Json(c): Json<Creds>) -> R<Response> {
    let (s, v) = auth_call(&st, "login", json!({"email": c.email, "password": c.password})).await?;
    if !s.is_success() {
        return Err(auth_err(s, &v));
    }
    let uid = v["user_id"].as_str().unwrap_or_default();
    let email = v["email"].as_str().unwrap_or_default();
    let cid = ensure_company(&st, uid, email, c.company_name.as_deref())?;
    let name = company_name(&st, &cid);
    Ok(session_response(&st, v["token"].as_str().unwrap_or_default(), v["ttl"].as_i64().unwrap_or(3600), json!({"email": email, "company_id": cid, "company_name": name})))
}

fn company_name(st: &AppState, cid: &str) -> String {
    lock(st).query_row("SELECT name FROM companies WHERE id=?", [cid], |r| r.get(0)).unwrap_or_default()
}

pub async fn register(State(st): State<AppState>, Json(mut c): Json<Creds>) -> R<Response> {
    let name = c.company_name.take().map(|n| n.trim().to_string()).unwrap_or_default();
    if name.chars().count() < 2 || name.chars().count() > 200 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_company_name", "Укажите название юридического лица (2–200 символов)"));
    }
    if c.password_confirm.as_deref() != Some(c.password.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch", "Пароли не совпадают"));
    }
    c.company_name = Some(name);
    let (s, v) = auth_call(&st, "register", json!({"email": c.email, "password": c.password})).await?;
    if !s.is_success() {
        return Err(auth_err(s, &v));
    }
    login(State(st), Json(c)).await
}

pub async fn logout(State(st): State<AppState>, headers: HeaderMap) -> R<Response> {
    if let Some(t) = cookie_token(&headers) {
        let _ = auth_call(&st, "logout", json!({"token": t})).await;
    }
    let mut res = StatusCode::NO_CONTENT.into_response();
    res.headers_mut().insert(header::SET_COOKIE, HeaderValue::from_static("session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"));
    Ok(res)
}

pub async fn me(State(st): State<AppState>, p: Principal) -> Json<Value> {
    Json(json!({"email": p.email, "company_id": p.company_id, "company_name": company_name(&st, &p.company_id)}))
}

// ---------- импорт ----------
const CATEGORIES: &[&str] = &[
    "revenue", "other_income", "suppliers", "payroll", "rent", "taxes", "loans", "utilities", "bank_fees", "marketing", "transfers", "other_expense",
];

fn dedup_keys(txs: &[Value]) -> Vec<String> {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    txs.iter()
        .map(|t| {
            let base = format!("{}|{}|{}|{}", t["date"].as_str().unwrap_or(""), t["amount_kop"].as_str().unwrap_or(""), t["inn"].as_str().unwrap_or(""), t["purpose"].as_str().unwrap_or(""));
            let n = seen.entry(base.clone()).or_insert(0);
            *n += 1;
            format!("{base}|{n}")
        })
        .collect()
}

pub async fn upload_import(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, mut mp: Multipart) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let mut file: Option<(String, Vec<u8>)> = None;
    while let Some(f) = mp.next_field().await.map_err(|_| err(StatusCode::BAD_REQUEST, "bad_upload", "Не удалось прочитать файл"))? {
        if f.name() == Some("file") {
            let name = f.file_name().unwrap_or("upload").to_string();
            let data = f.bytes().await.map_err(|_| err(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large", "Файл больше 25 МБ"))?;
            file = Some((name, data.to_vec()));
        }
    }
    let (name, data) = file.ok_or_else(|| err(StatusCode::BAD_REQUEST, "no_file", "Файл не передан"))?;
    let lname = name.to_lowercase();
    if !(lname.ends_with(".csv") || lname.ends_with(".xlsx") || lname.ends_with(".txt")) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "unsupported_type", "Поддерживаются CSV и XLSX"));
    }
    if data.len() > 25 * 1024 * 1024 {
        return Err(err(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large", "Файл больше 25 МБ"));
    }
    let digest = hex::encode(Sha256::digest(&data));
    {
        let c = lock(&st);
        let dup: Option<String> = c
            .query_row("SELECT id FROM imports WHERE company_id=? AND digest=? AND status='confirmed'", params![cid, digest], |r| r.get(0))
            .optional()
            .map_err(internal)?;
        if dup.is_some() {
            return Err(err(StatusCode::CONFLICT, "duplicate_file", "Этот файл уже загружен"));
        }
    }
    let part = reqwest::multipart::Part::bytes(data).file_name(name.clone());
    let r = st
        .http
        .post(format!("{}/internal/v1/parse", st.cfg.parser_url))
        .multipart(reqwest::multipart::Form::new().part("file", part))
        .send()
        .await
        .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "parser_unavailable", "Сервис разбора недоступен"))?;
    let status = r.status();
    let parsed: Value = r.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        let code = parsed["detail"]["code"].as_str().unwrap_or("unreadable_file");
        let msg = match code {
            "no_date_column" => "Не найдена колонка с датой операции",
            "no_amount_column" => "Не найдена колонка с суммой",
            "empty_file" => "Файл пуст",
            "too_many_rows" => "Слишком много строк (максимум 200 000)",
            "file_too_large" => "Файл больше 25 МБ",
            "bad_encoding" => "Не удалось определить кодировку файла",
            _ => "Не удалось разобрать файл",
        };
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "parse_failed", format!("{msg} ({code})")));
    }
    let txs = parsed["transactions"].as_array().cloned().unwrap_or_default();
    if txs.is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "no_valid_rows", "В файле нет ни одной корректной операции"));
    }
    // уже существующие операции (перекрывающиеся выписки)
    let keys = dedup_keys(&txs);
    let existing: HashSet<String> = {
        let c = lock(&st);
        let mut s = c.prepare("SELECT dedup_key FROM transactions WHERE company_id=? AND date>=? AND date<=?").map_err(internal)?;
        let from = parsed["stats"]["date_from"].as_str().unwrap_or("0000-00-00").to_string();
        let to = parsed["stats"]["date_to"].as_str().unwrap_or("9999-99-99").to_string();
        s.query_map(params![cid, from, to], |r| r.get::<_, String>(0)).map_err(internal)?.filter_map(|x| x.ok()).collect()
    };
    let dups = keys.iter().filter(|k| existing.contains(*k)).count();
    let mut cats: BTreeMap<String, usize> = BTreeMap::new();
    for t in &txs {
        *cats.entry(t["category"].as_str().unwrap_or("").to_string()).or_default() += 1;
    }
    let errors: Vec<Value> = parsed["errors"].as_array().cloned().unwrap_or_default();
    let summary = json!({
        "stats": parsed["stats"],
        "rows_total": txs.len() + errors.len(),
        "errors_total": errors.len(),
        "errors": errors.iter().take(100).collect::<Vec<_>>(),
        "duplicates_existing": dups,
        "categories": cats,
        "sample": txs.iter().take(15).collect::<Vec<_>>(),
    });
    let id = uuid::Uuid::new_v4().to_string();
    {
        let c = lock(&st);
        c.execute(
            "INSERT INTO imports(id, company_id, filename, digest, status, summary, payload, created_at) VALUES(?,?,?,?, 'preview', ?,?,?)",
            params![id, cid, name, digest, summary.to_string(), serde_json::to_string(&txs).unwrap(), now()],
        )
        .map_err(internal)?;
    }
    audit(&st, &p, "import.upload", &id, "ok");
    let mut out = json!({"import_id": id, "status": "preview", "summary": summary});
    stringify_money(&mut out);
    Ok((StatusCode::CREATED, Json(out)))
}

pub async fn list_imports(State(st): State<AppState>, p: Principal, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let c = lock(&st);
    let mut s = c
        .prepare("SELECT id, filename, status, created_at, summary FROM imports WHERE company_id=? ORDER BY created_at DESC LIMIT 50")
        .map_err(internal)?;
    let items: Vec<Value> = s
        .query_map([&cid], |r| {
            let summary: Value = serde_json::from_str(&r.get::<_, String>(4)?).unwrap_or(Value::Null);
            Ok(json!({"import_id": r.get::<_, String>(0)?, "filename": r.get::<_, String>(1)?, "status": r.get::<_, String>(2)?,
                "created_at": r.get::<_, String>(3)?, "rows_ok": summary["stats"]["rows_ok"], "errors_total": summary["errors_total"],
                "date_from": summary["stats"]["date_from"], "date_to": summary["stats"]["date_to"]}))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(Json(json!({"items": items})))
}

#[derive(Deserialize)]
pub struct CompanyPatch {
    name: String,
}

pub async fn update_company(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Json(b): Json<CompanyPatch>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let name = b.name.trim();
    if name.chars().count() < 2 || name.chars().count() > 200 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_company_name", "Название от 2 до 200 символов"));
    }
    lock(&st).execute("UPDATE companies SET name=? WHERE id=?", params![name, cid]).map_err(internal)?;
    audit(&st, &p, "company.update", &cid, "ok");
    Ok(Json(json!({"company_id": cid, "company_name": name})))
}

pub async fn get_import(State(st): State<AppState>, p: Principal, Path((cid, iid)): Path<(String, String)>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let c = lock(&st);
    let row: Option<(String, String, String)> = c
        .query_row("SELECT status, summary, filename FROM imports WHERE id=? AND company_id=?", params![iid, cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .optional()
        .map_err(internal)?;
    let (status, summary, filename) = row.ok_or_else(|| err(StatusCode::NOT_FOUND, "not_found", "Не найдено"))?;
    Ok(Json(json!({"import_id": iid, "status": status, "filename": filename, "summary": serde_json::from_str::<Value>(&summary).unwrap_or(Value::Null)})))
}

pub async fn confirm_import(State(st): State<AppState>, p: Principal, Path((cid, iid)): Path<(String, String)>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let mut c = lock(&st);
    let row: Option<(String, Option<String>)> = c
        .query_row("SELECT status, payload FROM imports WHERE id=? AND company_id=?", params![iid, cid], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?;
    let (status, payload) = row.ok_or_else(|| err(StatusCode::NOT_FOUND, "not_found", "Не найдено"))?;
    if status != "preview" {
        return Err(err(StatusCode::CONFLICT, "already_confirmed", "Импорт уже подтверждён"));
    }
    let txs: Vec<Value> = serde_json::from_str(&payload.unwrap_or_default()).map_err(internal)?;
    let keys = dedup_keys(&txs);
    let tx = c.transaction().map_err(internal)?;
    let (mut inserted, mut skipped) = (0usize, 0usize);
    {
        let mut ins = tx
            .prepare(
                "INSERT OR IGNORE INTO transactions(company_id, import_id, row_no, date, amount_kop, balance_kop, counterparty, inn, purpose, category, category_conf, dedup_key)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            )
            .map_err(internal)?;
        for (t, k) in txs.iter().zip(keys.iter()) {
            let amount: i64 = t["amount_kop"].as_str().and_then(|s| s.parse().ok()).ok_or_else(|| internal("bad amount"))?;
            let balance: Option<i64> = t["balance_kop"].as_str().and_then(|s| s.parse().ok());
            let n = ins
                .execute(params![
                    cid, iid, t["row_no"].as_i64().unwrap_or(0), t["date"].as_str().unwrap_or(""), amount, balance,
                    t["counterparty"].as_str().unwrap_or(""), t["inn"].as_str().unwrap_or(""), t["purpose"].as_str().unwrap_or(""),
                    t["category"].as_str().unwrap_or("other_expense"), t["category_confidence"].as_f64().unwrap_or(0.0), k
                ])
                .map_err(internal)?;
            if n == 1 { inserted += 1 } else { skipped += 1 }
        }
    }
    tx.execute("UPDATE imports SET status='confirmed', payload=NULL WHERE id=?", [&iid]).map_err(internal)?;
    tx.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    tx.commit().map_err(internal)?;
    drop(c);
    audit(&st, &p, "import.confirm", &iid, "ok");
    Ok(Json(json!({"import_id": iid, "status": "confirmed", "inserted": inserted, "skipped_duplicates": skipped})))
}

// ---------- операции ----------
#[derive(Deserialize)]
pub struct TxQuery {
    limit: Option<i64>,
    offset: Option<i64>,
    category: Option<String>,
    direction: Option<String>,
    q: Option<String>,
    sort: Option<String>,
    order: Option<String>,
}

pub async fn list_transactions(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Query(q): Query<TxQuery>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let offset = q.offset.unwrap_or(0).max(0);
    let cat = q.category.unwrap_or_default();
    let dir = q.direction.unwrap_or_default();
    let search = q.q.unwrap_or_default().trim().to_lowercase();
    let order_col = match q.sort.as_deref() {
        Some("amount") => "ABS(amount_kop)",
        Some("counterparty") => "counterparty COLLATE NOCASE",
        Some("category") => "category",
        _ => "date",
    };
    let dir_sql = if q.order.as_deref() == Some("asc") { "ASC" } else { "DESC" };
    let c = lock(&st);
    let where_ = "company_id=?1 AND (?2='' OR category=?2) AND (?3='' OR (?3='in' AND amount_kop>0) OR (?3='out' AND amount_kop<0)) AND (?6='' OR instr(lower(counterparty),?6)>0 OR instr(lower(purpose),?6)>0)";
    let total: i64 = c.query_row(&format!("SELECT COUNT(*) FROM transactions WHERE {where_}"), params![cid, cat, dir, "", "", search], |r| r.get(0)).map_err(internal)?;
    let mut s = c
        .prepare(&format!(
            "SELECT id, date, amount_kop, counterparty, purpose, category, category_conf, manual, balance_kop FROM transactions WHERE {where_} ORDER BY {order_col} {dir_sql}, id {dir_sql} LIMIT ?4 OFFSET ?5"
        ))
        .map_err(internal)?;
    let items: Vec<Value> = s
        .query_map(params![cid, cat, dir, limit, offset, search], |r| {
            Ok(json!({"id": r.get::<_, i64>(0)?, "date": r.get::<_, String>(1)?, "amount_kop": r.get::<_, i64>(2)?, "counterparty": r.get::<_, String>(3)?,
                "purpose": r.get::<_, String>(4)?, "category": r.get::<_, String>(5)?, "category_confidence": r.get::<_, f64>(6)?,
                "manual": r.get::<_, i64>(7)? == 1, "balance_kop": r.get::<_, Option<i64>>(8)?}))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(money(json!({"items": items, "total": total, "limit": limit, "offset": offset})))
}

#[derive(Deserialize)]
pub struct CategoryBody {
    category: String,
}

pub async fn set_category(State(st): State<AppState>, p: Principal, Path((cid, id)): Path<(String, i64)>, Json(b): Json<CategoryBody>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    if !CATEGORIES.contains(&b.category.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_category", "Неизвестная категория"));
    }
    let c = lock(&st);
    let n = c.execute("UPDATE transactions SET category=?, manual=1, category_conf=1.0 WHERE id=? AND company_id=?", params![b.category, id, cid]).map_err(internal)?;
    if n == 0 {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено"));
    }
    c.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    Ok(Json(json!({"id": id, "category": b.category})))
}

// ---------- ожидаемые события ----------
#[derive(Deserialize)]
pub struct EventBody {
    date: NaiveDate,
    amount_kop: String,
    direction: String,
    title: String,
    #[serde(default)]
    counterparty: String,
    source: Option<String>,
    confidence: Option<f64>,
}

fn parse_kop(s: &str) -> Option<i64> {
    let v: i64 = s.trim().parse().ok()?;
    (v > 0 && v < 1_000_000_000_000_000).then_some(v)
}

fn load_events(st: &AppState, cid: &str) -> R<Vec<Event>> {
    let c = lock(st);
    let mut s = c.prepare("SELECT id, date, amount_kop, title, source, confidence, counterparty FROM expected_events WHERE company_id=? ORDER BY date").map_err(internal)?;
    let v = s
        .query_map([cid], |r| {
            Ok(Event {
                id: r.get(0)?,
                date: NaiveDate::parse_from_str(&r.get::<_, String>(1)?, "%Y-%m-%d").unwrap_or_default(),
                amount: r.get(2)?,
                title: r.get(3)?,
                source: r.get(4)?,
                confidence: r.get(5)?,
                counterparty: r.get(6)?,
            })
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(v)
}

pub async fn list_events(State(st): State<AppState>, p: Principal, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let ev = load_events(&st, &cid)?;
    Ok(money(json!({"items": ev})))
}

fn insert_event(st: &AppState, cid: &str, date: NaiveDate, amount: i64, title: &str, cp: &str, source: &str, conf: f64) -> R<String> {
    let id = uuid::Uuid::new_v4().to_string();
    let c = lock(st);
    c.execute(
        "INSERT INTO expected_events(id, company_id, date, amount_kop, title, counterparty, source, confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        params![id, cid, date.to_string(), amount, title, cp, source, conf, now()],
    )
    .map_err(internal)?;
    c.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [cid]).map_err(internal)?;
    Ok(id)
}

pub async fn create_event(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Json(b): Json<EventBody>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let amount = parse_kop(&b.amount_kop).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_amount", "Сумма должна быть положительным целым числом копеек"))?;
    let signed = match b.direction.as_str() {
        "in" => amount,
        "out" => -amount,
        _ => return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_direction", "direction: in | out")),
    };
    let title = b.title.trim();
    if title.is_empty() || title.chars().count() > 200 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_title", "Название от 1 до 200 символов"));
    }
    let source = b.source.unwrap_or_else(|| "manual".into());
    if !["manual", "invoice", "contract", "tax"].contains(&source.as_str()) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_source", "Неизвестный источник"));
    }
    let today = chrono::Local::now().date_naive();
    if b.date < today - Duration::days(120) || b.date > today + Duration::days(730) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_date", "Дата вне допустимого диапазона"));
    }
    let conf = b.confidence.unwrap_or(0.9);
    if !(0.0..=1.0).contains(&conf) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_confidence", "confidence от 0 до 1"));
    }
    let id = insert_event(&st, &cid, b.date, signed, title, b.counterparty.trim(), &source, conf)?;
    audit(&st, &p, "event.create", &id, "ok");
    Ok((StatusCode::CREATED, Json(json!({"id": id}))))
}

pub async fn delete_event(State(st): State<AppState>, p: Principal, Path((cid, eid)): Path<(String, String)>) -> R<StatusCode> {
    authorize(&p, &cid)?;
    let c = lock(&st);
    let n = c.execute("DELETE FROM expected_events WHERE id=? AND company_id=?", params![eid, cid]).map_err(internal)?;
    if n == 0 {
        return Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено"));
    }
    c.execute("UPDATE companies SET data_revision=data_revision+1 WHERE id=?", [&cid]).map_err(internal)?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Deserialize)]
pub struct TaxBody {
    amount_kop: String,
}

/// ЕНП до 28-го числа: два ближайших срока после даты выписки.
pub async fn tax_calendar(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Json(b): Json<TaxBody>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let amount = parse_kop(&b.amount_kop).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_amount", "Некорректная сумма"))?;
    let (txs, _) = load_history(&st, &cid)?;
    let base = txs.iter().map(|t| t.0.date).max().unwrap_or_else(|| chrono::Local::now().date_naive());
    let existing = load_events(&st, &cid)?;
    let mut created = vec![];
    let mut y = base.year();
    let mut m = base.month();
    for _ in 0..4 {
        let d = NaiveDate::from_ymd_opt(y, m, 28).unwrap();
        if d > base && created.len() < 2 && !existing.iter().any(|e| e.date == d && e.source == "tax") {
            insert_event(&st, &cid, d, -amount, "ЕНП (налоговый календарь)", "ФНС России", "tax", 1.0)?;
            created.push(d.to_string());
        }
        if m == 12 { y += 1; m = 1 } else { m += 1 }
    }
    Ok((StatusCode::CREATED, Json(json!({"created": created}))))
}

// ---------- прогноз ----------
type History = (Vec<(Tx, Option<i64>)>, i64);

fn load_history(st: &AppState, cid: &str) -> R<History> {
    let c = lock(st);
    let rev: i64 = c.query_row("SELECT data_revision FROM companies WHERE id=?", [cid], |r| r.get(0)).map_err(internal)?;
    let mut s = c
        .prepare("SELECT date, amount_kop, balance_kop, counterparty, inn, purpose, category FROM transactions WHERE company_id=? ORDER BY date, id")
        .map_err(internal)?;
    let v = s
        .query_map([cid], |r| {
            Ok((
                Tx {
                    date: NaiveDate::parse_from_str(&r.get::<_, String>(0)?, "%Y-%m-%d").unwrap_or_default(),
                    amount: r.get(1)?,
                    counterparty: r.get(3)?,
                    inn: r.get(4)?,
                    purpose: r.get(5)?,
                    category: r.get(6)?,
                },
                r.get::<_, Option<i64>>(2)?,
            ))
        })
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok((v, rev))
}

fn map_fc_err(e: ForecastError) -> ApiError {
    match e {
        ForecastError::NoData => err(StatusCode::UNPROCESSABLE_ENTITY, "no_data", "Сначала загрузите и подтвердите банковскую выписку"),
        ForecastError::NoBalance => err(StatusCode::UNPROCESSABLE_ENTITY, "no_balance", "В выписке нет остатка на счёте — прогноз невозможен"),
    }
}

async fn compute(st: &AppState, cid: &str, horizon: usize, scenario: Scenario) -> R<(forecast::ForecastResult, i64)> {
    let (hist, rev) = load_history(st, cid)?;
    let events = load_events(st, cid)?;
    let res = tokio::task::spawn_blocking(move || {
        let balances = forecast::daily_balances(&hist);
        // внутренние переводы не являются выручкой или расходом бизнеса
        let txs: Vec<Tx> = hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect();
        forecast::forecast(&txs, &balances, &events, &scenario, &Params { horizon, ..Default::default() })
    })
    .await
    .map_err(internal)?
    .map_err(map_fc_err)?;
    Ok((res, rev))
}

fn save_run(st: &AppState, cid: &str, rev: i64, scenario: Option<&Scenario>, res: &forecast::ForecastResult) -> R<(String, String)> {
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

fn run_json(id: &str, at: &str, rev: i64, stale: bool, res: &forecast::ForecastResult, scenario: Option<&Scenario>) -> Value {
    json!({"run_id": id, "created_at": at, "data_revision": rev, "stale": stale, "scenario": scenario, "result": res})
}

fn horizon_of(h: Option<i64>) -> R<usize> {
    let h = h.unwrap_or(30);
    if !(7..=90).contains(&h) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_horizon", "Горизонт от 7 до 90 дней"));
    }
    Ok(h as usize)
}

#[derive(Deserialize, Default)]
pub struct ForecastBody {
    horizon_days: Option<i64>,
}

pub async fn create_forecast(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, body: Option<Json<ForecastBody>>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let h = horizon_of(body.and_then(|b| b.0.horizon_days))?;
    let (res, rev) = compute(&st, &cid, h, Scenario::default()).await?;
    let (id, at) = save_run(&st, &cid, rev, None, &res)?;
    audit(&st, &p, "forecast.create", &id, "ok");
    Ok((StatusCode::CREATED, money(run_json(&id, &at, rev, false, &res, None))))
}

fn read_run(st: &AppState, cid: &str, rid: Option<&str>) -> R<Json<Value>> {
    let c = lock(st);
    let cur: i64 = c.query_row("SELECT data_revision FROM companies WHERE id=?", [cid], |r| r.get(0)).map_err(internal)?;
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

pub async fn latest_forecast(State(st): State<AppState>, p: Principal, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    read_run(&st, &cid, None)
}

pub async fn get_forecast(State(st): State<AppState>, p: Principal, Path((cid, rid)): Path<(String, String)>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    read_run(&st, &cid, Some(&rid))
}

#[derive(Deserialize)]
pub struct ScenarioBody {
    horizon_days: Option<i64>,
    scenario: Scenario,
}

pub async fn create_scenario(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Json(b): Json<ScenarioBody>) -> R<(StatusCode, Json<Value>)> {
    authorize(&p, &cid)?;
    let h = horizon_of(b.horizon_days)?;
    let s = &b.scenario;
    if !(0..=90).contains(&s.delay_days) || !(0.0..=100.0).contains(&s.revenue_drop_pct) || !(0.0..=300.0).contains(&s.fixed_cost_growth_pct) || s.extra_expense_kop.map_or(false, |v| v <= 0 || v >= 1_000_000_000_000_000) || s.extra_income_kop.map_or(false, |v| v <= 0 || v >= 1_000_000_000_000_000) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_scenario", "Параметры сценария вне допустимых пределов"));
    }
    if s.is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "empty_scenario", "Задайте хотя бы одно изменение"));
    }
    // базовый и сценарный расчёты используют один seed — разница объясняется только сценарием
    let (base, rev) = compute(&st, &cid, h, Scenario::default()).await?;
    let (sc, _) = compute(&st, &cid, h, b.scenario.clone()).await?;
    let (bid, bat) = save_run(&st, &cid, rev, None, &base)?;
    let (sid, sat) = save_run(&st, &cid, rev, Some(&b.scenario), &sc)?;
    audit(&st, &p, "scenario.create", &sid, "ok");
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

pub async fn run_backtest(State(st): State<AppState>, p: Principal, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let (hist, _) = load_history(&st, &cid)?;
    let r = tokio::task::spawn_blocking(move || {
        let balances = forecast::daily_balances(&hist);
        let txs: Vec<Tx> = hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect();
        forecast::backtest(&txs, &balances, 14)
    })
    .await
    .map_err(internal)?
    .ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "not_enough_history", "Для backtest нужно не менее 5 месяцев истории"))?;
    Ok(money(serde_json::to_value(r).map_err(internal)?))
}

// ---------- план-факт ----------
#[derive(Deserialize)]
pub struct PfQuery {
    days: Option<i64>,
}

pub async fn plan_fact(State(st): State<AppState>, p: Principal, Path(cid): Path<String>, Query(q): Query<PfQuery>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let days = q.days.unwrap_or(90).clamp(14, 365);
    let (hist, _) = load_history(&st, &cid)?;
    let events = load_events(&st, &cid)?;
    let txs: Vec<Tx> = hist.into_iter().map(|h| h.0).filter(|t| t.category != "transfers").collect();
    let as_of = txs.iter().map(|t| t.date).max().ok_or_else(|| map_fc_err(ForecastError::NoData))?;
    let from = as_of - Duration::days(days);
    let rules = forecast::detect_rules(&txs, as_of);

    struct Plan {
        date: NaiveDate,
        amount: i64,
        title: String,
        source: &'static str,
        key: Option<String>,
    }
    let mut plans: Vec<Plan> = vec![];
    for r in &rules {
        for d in from.iter_days().take_while(|d| *d <= as_of) {
            let hit = match r.kind {
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
    Ok(money(json!({
        "as_of": as_of, "days": days, "weeks": weeks_json, "items": items, "missed": missed,
        "note": "План по регулярным платежам построен по шаблонам, найденным во всей истории (in-sample); ручные события и счета — по введённым датам."
    })))
}

// ---------- внутренний API ----------
pub async fn internal_summary(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let v = read_run(&st, &cid, None)?;
    let r = &v.0["result"];
    Ok(Json(json!({
        "run_id": v.0["run_id"], "stale": v.0["stale"], "as_of": r["as_of"], "horizon": r["horizon"],
        "risk": {"level": r["risk"]["level"], "shortfall_probability": r["risk"]["shortfall_probability"],
                 "first_break_date": r["risk"]["first_break_date"], "max_deficit_p90": r["risk"]["max_deficit_p90"]},
    })))
}
