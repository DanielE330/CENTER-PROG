//! Тесты внутреннего API ledger: временная SQLite, запросы через `Router::oneshot` (без сети к ledger).
//! Сервис разбора подменён локальной заглушкой на эфемерном порту loopback: она возвращает
//! содержимое загруженного «файла» (JSON) как ответ parser.

use crate::{AppState, build, db};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::Multipart,
    http::{Request, StatusCode},
    routing::{get, post},
};
use chrono::{Duration, Local};
use common::Upstream;
use serde_json::{Value, json};
use tower::ServiceExt;

async fn fake_parse(mut mp: Multipart) -> (StatusCode, Json<Value>) {
    let mut v = Value::Null;
    while let Some(f) = mp.next_field().await.unwrap() {
        if f.name() == Some("file") {
            v = serde_json::from_slice(&f.bytes().await.unwrap()).unwrap_or(Value::Null);
        }
    }
    if v.get("fail").is_some() {
        return (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"detail": {"code": v["fail"]}})));
    }
    (StatusCode::OK, Json(v))
}

async fn fake_taxonomy() -> Json<Value> {
    Json(json!({"groups": {}, "subcategories": {
        "customer_payment": {"group": "revenue", "name": "Оплаты клиентов", "category": "revenue"},
        "supplier_payment": {"group": "purchases", "name": "Поставщики", "category": "suppliers"},
        "other_in": {"group": "other", "name": "Прочее", "category": "other_income"},
    }}))
}

/// Заглушка модели на хвост: положительные суммы -> customer_payment, остальные не распознаёт.
async fn fake_tail(Json(b): Json<Value>) -> Json<Value> {
    let preds: Vec<Value> = b["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            if r["amount_kop"].as_i64().unwrap_or(0) > 0 {
                json!({"subcategory": "customer_payment", "group": "revenue", "category": "revenue", "confidence": 0.9})
            } else {
                Value::Null
            }
        })
        .collect();
    Json(json!({"ready": true, "predictions": preds, "train_size": b["train"].as_array().unwrap().len()}))
}

struct Env {
    app: Router,
    _dir: std::path::PathBuf,
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self._dir);
    }
}

async fn env() -> Env {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    tokio::spawn(async move {
        let app = Router::new().route("/internal/v1/parse", post(fake_parse)).route("/internal/v1/taxonomy", get(fake_taxonomy)).route("/internal/v1/tail", post(fake_tail));
        axum::serve(l, app).await.unwrap();
    });
    let dir = std::env::temp_dir().join(format!("ledger-test-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let st = AppState {
        db: db::open(dir.join("ledger.db").to_str().unwrap()),
        parser: Upstream::new("parser", "Сервис разбора", &format!("http://127.0.0.1:{port}"), 10),
    };
    Env { app: build(st), _dir: dir }
}

async fn call(app: &Router, method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
    let b = match body {
        Some(v) => Body::from(serde_json::to_vec(&v).unwrap()),
        None => Body::empty(),
    };
    let req = Request::builder().method(method).uri(uri).header("content-type", "application/json").header("x-actor", "tester").body(b).unwrap();
    finish(app, req).await
}

async fn finish(app: &Router, req: Request<Body>) -> (StatusCode, Value) {
    let res = app.clone().oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = to_bytes(res.into_body(), 10 * 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

async fn upload(app: &Router, cid: &str, filename: &str, content: &Value) -> (StatusCode, Value) {
    let boundary = "XBOUNDARYX";
    let mut body = format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").into_bytes();
    body.extend(serde_json::to_vec(content).unwrap());
    body.extend(format!("\r\n--{boundary}--\r\n").into_bytes());
    let req = Request::builder()
        .method("POST")
        .uri(format!("/internal/v1/companies/{cid}/imports"))
        .header("content-type", format!("multipart/form-data; boundary={boundary}"))
        .body(Body::from(body))
        .unwrap();
    finish(app, req).await
}

async fn company(app: &Router, owner: &str) -> String {
    let (s, v) = call(app, "POST", "/internal/v1/companies/ensure", Some(json!({"owner_id": owner, "email": format!("{owner}@x.ru")}))).await;
    assert_eq!(s, StatusCode::OK);
    v["company_id"].as_str().unwrap().to_string()
}

#[tokio::test]
async fn company_requisites_from_certificate_are_stored_and_not_overwritten() {
    let e = env().await;
    let (_, v) = call(&e.app, "POST", "/internal/v1/companies/ensure", Some(json!({"owner_id": "o1", "name": "Ромашка", "inn": "7707083893", "ogrn": "bad"}))).await;
    assert_eq!((v["inn"].as_str(), v["ogrn"].as_str()), (Some("7707083893"), Some("")));
    // повторный вход: пустое дополняется, заполненное сохраняется
    let (_, v) = call(&e.app, "POST", "/internal/v1/companies/ensure", Some(json!({"owner_id": "o1", "inn": "1234567890", "ogrn": "1027700132195"}))).await;
    assert_eq!((v["inn"].as_str(), v["ogrn"].as_str()), (Some("7707083893"), Some("1027700132195")));
    let cid = v["company_id"].as_str().unwrap();
    let (_, got) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}"), None).await;
    assert_eq!(got["inn"], "7707083893");
}

async fn revision(app: &Router, cid: &str) -> i64 {
    let (s, v) = call(app, "GET", &format!("/internal/v1/companies/{cid}"), None).await;
    assert_eq!(s, StatusCode::OK);
    v["data_revision"].as_i64().unwrap()
}

fn tx(row: i64, date: &str, amount: i64, cp: &str, purpose: &str, cat: &str) -> Value {
    json!({"row_no": row, "date": date, "amount_kop": amount.to_string(), "balance_kop": null, "counterparty": cp,
        "inn": "7700000001", "purpose": purpose, "category": cat, "category_confidence": 0.8})
}

fn parsed(txs: Vec<Value>) -> Value {
    let from = txs.iter().map(|t| t["date"].as_str().unwrap().to_string()).min().unwrap();
    let to = txs.iter().map(|t| t["date"].as_str().unwrap().to_string()).max().unwrap();
    json!({"transactions": txs, "errors": [], "stats": {"date_from": from, "date_to": to, "rows_ok": txs.len()}})
}

/// Загружает и подтверждает импорт; возвращает ответ confirm.
async fn import_confirmed(app: &Router, cid: &str, name: &str, txs: Vec<Value>) -> Value {
    let (s, v) = upload(app, cid, name, &parsed(txs)).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    let iid = v["import_id"].as_str().unwrap();
    let (s, v) = call(app, "POST", &format!("/internal/v1/companies/{cid}/imports/{iid}/confirm"), None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    v
}

fn base_txs() -> Vec<Value> {
    vec![
        tx(1, "2026-03-01", 10_000_000, "ООО Заказчик", "Оплата по договору 1", "revenue"),
        tx(2, "2026-03-02", -2_500_000, "ООО Аренда", "Аренда офиса март", "rent"),
        tx(3, "2026-03-03", -150_000, "Банк", "Комиссия за обслуживание", "bank_fees"),
        tx(4, "2026-03-04", 500_000, "ИП Петров", "Оплата за услуги", "revenue"),
    ]
}

// ---------- импорт ----------

#[tokio::test]
async fn import_preview_does_not_write_until_confirmed() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let (s, v) = upload(&e.app, &cid, "stmt.csv", &parsed(base_txs())).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(v["status"], "preview");
    assert_eq!(v["summary"]["rows_total"], 4);
    assert_eq!(v["summary"]["duplicates_existing"], 0);
    assert_eq!(v["summary"]["categories"]["revenue"], 2);
    // суммы в ответе — строки
    assert_eq!(v["summary"]["sample"][0]["amount_kop"], "10000000");
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    assert_eq!(t["total"], 0);
    assert_eq!(revision(&e.app, &cid).await, 0);

    let iid = v["import_id"].as_str().unwrap();
    let (s, g) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/imports/{iid}"), None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(g["status"], "preview");
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/imports"), None).await;
    assert_eq!(l["items"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn import_confirm_inserts_bumps_revision_and_is_not_repeatable() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let (_, v) = upload(&e.app, &cid, "stmt.csv", &parsed(base_txs())).await;
    let iid = v["import_id"].as_str().unwrap().to_string();
    let uri = format!("/internal/v1/companies/{cid}/imports/{iid}/confirm");
    let (s, c) = call(&e.app, "POST", &uri, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((c["inserted"].as_i64(), c["skipped_duplicates"].as_i64()), (Some(4), Some(0)));
    assert_eq!(c["status"], "confirmed");
    assert_eq!(revision(&e.app, &cid).await, 1);
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    assert_eq!(t["total"], 4);

    let (s, r) = call(&e.app, "POST", &uri, None).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(r["error"]["code"], "already_confirmed");
    assert_eq!(revision(&e.app, &cid).await, 1);

    // снимок для расчётных сервисов
    let (s, snap) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/snapshot"), None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(snap["data_revision"], 1);
    assert_eq!(snap["transactions"].as_array().unwrap().len(), 4);
    assert_eq!(snap["transactions"][0]["amount"], 10_000_000);
}

#[tokio::test]
async fn import_same_file_after_confirm_is_rejected_as_duplicate() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    import_confirmed(&e.app, &cid, "a.csv", base_txs()).await;
    let (s, v) = upload(&e.app, &cid, "a-again.csv", &parsed(base_txs())).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["code"], "duplicate_file");
}

#[tokio::test]
async fn import_overlapping_statement_skips_rows_by_dedup_key() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    import_confirmed(&e.app, &cid, "a.csv", base_txs()).await;
    // другой файл: две уже загруженные операции и одна новая
    let mut second = base_txs()[..2].to_vec();
    second.push(tx(3, "2026-03-05", -700_000, "ООО Поставщик", "Оплата счёта 55", "suppliers"));
    let (s, v) = upload(&e.app, &cid, "b.csv", &parsed(second)).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(v["summary"]["duplicates_existing"], 2);
    let iid = v["import_id"].as_str().unwrap();
    let (s, c) = call(&e.app, "POST", &format!("/internal/v1/companies/{cid}/imports/{iid}/confirm"), None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((c["inserted"].as_i64(), c["skipped_duplicates"].as_i64()), (Some(1), Some(2)));
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    assert_eq!(t["total"], 5);
}

#[tokio::test]
async fn import_identical_rows_inside_one_file_are_kept_separately() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let row = tx(1, "2026-03-01", -100_000, "Кафе", "Обед", "other_expense");
    let c = import_confirmed(&e.app, &cid, "a.csv", vec![row.clone(), row]).await;
    assert_eq!(c["inserted"], 2);
}

#[tokio::test]
async fn import_of_foreign_company_returns_404() {
    let e = env().await;
    let a = company(&e.app, "owner-a").await;
    let b = company(&e.app, "owner-b").await;
    assert_ne!(a, b);
    let (_, v) = upload(&e.app, &a, "a.csv", &parsed(base_txs())).await;
    let iid = v["import_id"].as_str().unwrap();
    let (s, r) = call(&e.app, "GET", &format!("/internal/v1/companies/{b}/imports/{iid}"), None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(r["error"]["code"], "not_found");
    let (s, _) = call(&e.app, "POST", &format!("/internal/v1/companies/{b}/imports/{iid}/confirm"), None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    // у владельца импорт остался в preview, у чужой компании ничего не записалось
    assert_eq!(revision(&e.app, &b).await, 0);
    let (_, g) = call(&e.app, "GET", &format!("/internal/v1/companies/{a}/imports/{iid}"), None).await;
    assert_eq!(g["status"], "preview");
}

#[tokio::test]
async fn import_validation_errors() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let (s, v) = upload(&e.app, &cid, "stmt.pdf", &parsed(base_txs())).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("unsupported_type")));
    let (s, v) = upload(&e.app, &cid, "stmt.csv", &json!({"fail": "empty_file"})).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("parse_failed")));
    let (s, v) = upload(&e.app, &cid, "stmt.csv", &json!({"transactions": [], "errors": [], "stats": {}})).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("no_valid_rows")));
}

// ---------- операции ----------

async fn seeded() -> (Env, String) {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    import_confirmed(&e.app, &cid, "a.csv", base_txs()).await;
    (e, cid)
}

#[tokio::test]
async fn transactions_filters_sort_and_paging() {
    let (e, cid) = seeded().await;
    let get = |q: &'static str| {
        let app = e.app.clone();
        let uri = format!("/internal/v1/companies/{cid}/transactions{q}");
        async move { call(&app, "GET", &uri, None).await.1 }
    };
    assert_eq!(get("").await["total"], 4);
    assert_eq!(get("?category=revenue").await["total"], 2);
    assert_eq!(get("?direction=in").await["total"], 2);
    assert_eq!(get("?direction=out").await["total"], 2);
    assert_eq!(get("?category=revenue&direction=out").await["total"], 0);
    let q = get("?q=%D0%BE%D1%84%D0%B8%D1%81%D0%B0").await; // «офиса»: поиск по назначению (SQLite lower() не меняет регистр кириллицы)
    assert_eq!(q["total"], 1);
    assert_eq!(q["items"][0]["category"], "rent");
    // сортировка по модулю суммы
    let s = get("?sort=amount&order=desc").await;
    assert_eq!(s["items"][0]["amount_kop"], "10000000");
    assert_eq!(s["items"][3]["amount_kop"], "-150000");
    // по умолчанию — по дате, новые сверху
    assert_eq!(get("").await["items"][0]["date"], "2026-03-04");
    assert_eq!(get("?order=asc").await["items"][0]["date"], "2026-03-01");
    // пагинация
    let p = get("?limit=2&offset=3&order=asc").await;
    assert_eq!((p["total"].as_i64(), p["items"].as_array().unwrap().len(), p["limit"].as_i64()), (Some(4), 1, Some(2)));
    assert_eq!(get("?limit=0").await["limit"], 1);
    assert_eq!(get("?limit=9999").await["limit"], 200);
}

#[tokio::test]
async fn transactions_are_scoped_to_company() {
    let (e, _cid) = seeded().await;
    let other = company(&e.app, "u2").await;
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{other}/transactions"), None).await;
    assert_eq!(t["total"], 0);
}

#[tokio::test]
async fn set_category_marks_manual_and_bumps_revision() {
    let (e, cid) = seeded().await;
    let rev = revision(&e.app, &cid).await;
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions?category=bank_fees"), None).await;
    let id = t["items"][0]["id"].as_i64().unwrap();
    assert_eq!(t["items"][0]["manual"], false);
    let uri = format!("/internal/v1/companies/{cid}/transactions/{id}/category");
    let (s, v) = call(&e.app, "PATCH", &uri, Some(json!({"category": "taxes"}))).await;
    assert_eq!((s, v["category"].as_str()), (StatusCode::OK, Some("taxes")));
    assert_eq!(revision(&e.app, &cid).await, rev + 1);
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions?category=taxes"), None).await;
    assert_eq!(t["total"], 1);
    assert_eq!(t["items"][0]["manual"], true);
    assert_eq!(t["items"][0]["category_confidence"], 1.0);
}

#[tokio::test]
async fn set_category_rejects_unknown_category_and_foreign_company() {
    let (e, cid) = seeded().await;
    let other = company(&e.app, "u2").await;
    let rev = revision(&e.app, &cid).await;
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    let id = t["items"][0]["id"].as_i64().unwrap();
    let (s, v) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/transactions/{id}/category"), Some(json!({"category": "nope"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_category")));
    let (s, _) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{other}/transactions/{id}/category"), Some(json!({"category": "taxes"}))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/transactions/99999/category"), Some(json!({"category": "taxes"}))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(revision(&e.app, &cid).await, rev);
    assert_eq!(revision(&e.app, &other).await, 0);
}

// ---------- ожидаемые события ----------

fn in_days(n: i64) -> String {
    (Local::now().date_naive() + Duration::days(n)).to_string()
}

fn ev(date: String, amount: &str, dir: &str) -> Value {
    json!({"date": date, "amount_kop": amount, "direction": dir, "title": "Платёж", "counterparty": "ООО Тест"})
}

async fn post_event(e: &Env, cid: &str, body: Value) -> (StatusCode, Value) {
    call(&e.app, "POST", &format!("/internal/v1/companies/{cid}/expected-events"), Some(body)).await
}

#[tokio::test]
async fn event_create_stores_signed_amount_defaults_and_bumps_revision() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let (s, v) = post_event(&e, &cid, ev(in_days(10), "123400", "out")).await;
    assert_eq!(s, StatusCode::CREATED);
    assert!(v["id"].as_str().is_some());
    assert_eq!(revision(&e.app, &cid).await, 1);
    let (s, _) = post_event(&e, &cid, ev(in_days(11), "500", "in")).await;
    assert_eq!(s, StatusCode::CREATED);
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/expected-events"), None).await;
    let items = l["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["amount"], "-123400");
    assert_eq!(items[1]["amount"], "500");
    assert_eq!(items[0]["source"], "manual");
    assert_eq!(items[0]["confidence"], 0.9);
}

#[tokio::test]
async fn event_create_validation() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let code = |(s, v): (StatusCode, Value)| (s, v["error"]["code"].as_str().unwrap_or("").to_string());
    let bad = StatusCode::UNPROCESSABLE_ENTITY;
    for amount in ["0", "-5", "abc", "", "1000000000000000"] {
        assert_eq!(code(post_event(&e, &cid, ev(in_days(5), amount, "in")).await), (bad, "invalid_amount".into()), "amount {amount:?}");
    }
    assert_eq!(code(post_event(&e, &cid, ev(in_days(5), "100", "sideways")).await), (bad, "invalid_direction".into()));
    assert_eq!(code(post_event(&e, &cid, ev(in_days(-121), "100", "in")).await), (bad, "invalid_date".into()));
    assert_eq!(code(post_event(&e, &cid, ev(in_days(731), "100", "in")).await), (bad, "invalid_date".into()));
    let mut b = ev(in_days(5), "100", "in");
    b["source"] = json!("telepathy");
    assert_eq!(code(post_event(&e, &cid, b).await), (bad, "invalid_source".into()));
    let mut b = ev(in_days(5), "100", "in");
    b["title"] = json!("   ");
    assert_eq!(code(post_event(&e, &cid, b).await), (bad, "invalid_title".into()));
    let mut b = ev(in_days(5), "100", "in");
    b["confidence"] = json!(1.5);
    assert_eq!(code(post_event(&e, &cid, b).await), (bad, "invalid_confidence".into()));
    // допустимые границы и источники
    for (d, src) in [(-120, "tax"), (730, "contract"), (0, "invoice")] {
        let mut b = ev(in_days(d), "100", "out");
        b["source"] = json!(src);
        assert_eq!(post_event(&e, &cid, b).await.0, StatusCode::CREATED, "{d} {src}");
    }
    assert_eq!(revision(&e.app, &cid).await, 3); // отклонённые запросы ревизию не меняют
}

#[tokio::test]
async fn event_delete_bumps_revision_and_is_company_scoped() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let other = company(&e.app, "u2").await;
    let (_, v) = post_event(&e, &cid, ev(in_days(5), "100", "in")).await;
    let id = v["id"].as_str().unwrap();
    assert_eq!(revision(&e.app, &cid).await, 1);
    let (s, _) = call(&e.app, "DELETE", &format!("/internal/v1/companies/{other}/expected-events/{id}"), None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(revision(&e.app, &cid).await, 1);
    let (s, _) = call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/expected-events/{id}"), None).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    assert_eq!(revision(&e.app, &cid).await, 2);
    let (s, _) = call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/expected-events/{id}"), None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/expected-events"), None).await;
    assert!(l["items"].as_array().unwrap().is_empty());
}

fn bulk_item(ext: &str, amount: i64) -> Value {
    json!({"external_id": ext, "date": in_days(7), "amount_kop": amount, "title": "Счёт", "counterparty": "ООО Х", "source": "invoice", "confidence": 0.7})
}

#[tokio::test]
async fn bulk_events_are_idempotent_by_external_id() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let other = company(&e.app, "u2").await;
    let uri = format!("/internal/v1/companies/{cid}/expected-events/bulk");
    let body = json!({"events": [bulk_item("inv-1", -100_000), bulk_item("inv-2", 250_000)]});
    let (s, v) = call(&e.app, "POST", &uri, Some(body.clone())).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((v["created"].as_i64(), v["skipped"].as_i64()), (Some(2), Some(0)));
    assert_eq!(v["ids"].as_array().unwrap().len(), 2);
    assert_eq!(revision(&e.app, &cid).await, 1);

    let (s, v) = call(&e.app, "POST", &uri, Some(body)).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((v["created"].as_i64(), v["skipped"].as_i64()), (Some(0), Some(2)));
    assert_eq!(revision(&e.app, &cid).await, 1, "повтор без новых событий не меняет ревизию");

    // частичное пересечение: новое создаётся, старое пропускается
    let (_, v) = call(&e.app, "POST", &uri, Some(json!({"events": [bulk_item("inv-2", 250_000), bulk_item("inv-3", -5)]}))).await;
    assert_eq!((v["created"].as_i64(), v["skipped"].as_i64()), (Some(1), Some(1)));
    assert_eq!(revision(&e.app, &cid).await, 2);
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/expected-events"), None).await;
    assert_eq!(l["items"].as_array().unwrap().len(), 3);

    // тот же external_id у другой компании — независимое событие
    let (_, v) = call(&e.app, "POST", &format!("/internal/v1/companies/{other}/expected-events/bulk"), Some(json!({"events": [bulk_item("inv-1", -100_000)]}))).await;
    assert_eq!(v["created"], 1);
}

#[tokio::test]
async fn bulk_events_validation_and_unknown_company() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let uri = format!("/internal/v1/companies/{cid}/expected-events/bulk");
    let mut zero = bulk_item("z", 0);
    zero["amount_kop"] = json!(0);
    let mut badsrc = bulk_item("s", 100);
    badsrc["source"] = json!("rumor");
    let mut badconf = bulk_item("c", 100);
    badconf["confidence"] = json!(2.0);
    for item in [zero, badsrc, badconf] {
        let (s, v) = call(&e.app, "POST", &uri, Some(json!({"events": [bulk_item("ok", 100), item]}))).await;
        assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_event")));
    }
    // транзакция откатывается целиком
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/expected-events"), None).await;
    assert!(l["items"].as_array().unwrap().is_empty());
    assert_eq!(revision(&e.app, &cid).await, 0);
    let (s, _) = call(&e.app, "POST", "/internal/v1/companies/no-such/expected-events/bulk", Some(json!({"events": [bulk_item("a", 1)]}))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

// ---------- роли: участники компании ----------
async fn ensure_full(app: &Router, owner: &str, name: &str) -> Value {
    let (s, v) = call(app, "POST", "/internal/v1/companies/ensure", Some(json!({"owner_id": owner, "email": format!("{owner}@x.ru"), "name": name}))).await;
    assert_eq!(s, StatusCode::OK);
    v
}

async fn add_member(app: &Router, cid: &str, uid: &str, role: &str) -> (StatusCode, Value) {
    call(app, "POST", &format!("/internal/v1/companies/{cid}/members"), Some(json!({"user_id": uid, "email": format!("{uid}@x.ru"), "role": role}))).await
}

#[tokio::test]
async fn ensure_creates_director_membership() {
    let e = env().await;
    let v = ensure_full(&e.app, "dir1", "Ромашка").await;
    assert_eq!((v["role"].as_str(), v["banned"].as_bool()), (Some("director"), Some(false)));
    let cid = v["company_id"].as_str().unwrap();
    let (s, m) = call(&e.app, "GET", "/internal/v1/members/by-user/dir1", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((m["company_id"].as_str(), m["company_name"].as_str(), m["role"].as_str(), m["banned"].as_bool()), (Some(cid), Some("Ромашка"), Some("director"), Some(false)));
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/members"), None).await;
    assert_eq!(l["items"].as_array().unwrap().len(), 1);
    assert_eq!((l["items"][0]["user_id"].as_str(), l["items"][0]["role"].as_str(), l["items"][0]["email"].as_str()), (Some("dir1"), Some("director"), Some("dir1@x.ru")));
    // повторный ensure идемпотентен
    assert_eq!(ensure_full(&e.app, "dir1", "Другое имя").await["company_id"], cid);
    let (s, _) = call(&e.app, "GET", "/internal/v1/members/by-user/ghost", None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn members_crud_and_ensure_does_not_create_company_for_employee() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "Ромашка").await["company_id"].as_str().unwrap().to_string();
    let (s, v) = add_member(&e.app, &cid, "emp1", "editor").await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    assert_eq!(v["role"], "editor");
    add_member(&e.app, &cid, "emp2", "viewer").await;

    // сотрудник при входе попадает в компанию директора, новая не создаётся
    let v = ensure_full(&e.app, "emp1", "Своя компания").await;
    assert_eq!((v["company_id"].as_str(), v["role"].as_str(), v["company_name"].as_str()), (Some(cid.as_str()), Some("editor"), Some("Ромашка")));
    let (_, all) = call(&e.app, "GET", "/internal/v1/admin/companies", None).await;
    assert_eq!(all["total"], 1);

    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/members"), None).await;
    let roles: Vec<_> = l["items"].as_array().unwrap().iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["director", "editor", "viewer"]);
    add_member(&e.app, &cid, "adm1", "admin").await;
    let (_, l) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/members"), None).await;
    let roles: Vec<_> = l["items"].as_array().unwrap().iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["director", "admin", "editor", "viewer"]);

    let (s, p) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/members/emp1"), Some(json!({"role": "viewer"}))).await;
    assert_eq!((s, p["role"].as_str()), (StatusCode::OK, Some("viewer")));
    assert_eq!(call(&e.app, "GET", "/internal/v1/members/by-user/emp1", None).await.1["role"], "viewer");

    let (s, _) = call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/members/emp2"), None).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    assert_eq!(call(&e.app, "GET", "/internal/v1/members/by-user/emp2", None).await.0, StatusCode::NOT_FOUND);
    assert_eq!(call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/members/emp2"), None).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn director_cannot_be_removed_or_demoted_and_validation() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "К").await["company_id"].as_str().unwrap().to_string();
    let (s, v) = call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/members/dir1"), None).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("cannot_remove_director")));
    for role in ["viewer", "editor", "director"] {
        let (s, v) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/members/dir1"), Some(json!({"role": role}))).await;
        assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("cannot_change_director")), "{role}");
    }
    // второго директора добавить нельзя; неизвестная роль отвергается
    for role in ["director", "owner", "analyst", ""] {
        let (s, v) = add_member(&e.app, &cid, "x1", role).await;
        assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_role")), "{role}");
    }
    assert_eq!(add_member(&e.app, &cid, "emp", "viewer").await.0, StatusCode::CREATED);
    let (s, v) = add_member(&e.app, &cid, "emp", "editor").await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("member_exists")));
    // директор уже состоит в компании: добавить его как сотрудника нельзя
    assert_eq!(add_member(&e.app, &cid, "dir1", "viewer").await.0, StatusCode::CONFLICT);
    let (s, v) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/members/emp"), Some(json!({"role": "director"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_role")));
    // неизвестная компания и чужой участник
    assert_eq!(add_member(&e.app, "nope", "z", "viewer").await.0, StatusCode::NOT_FOUND);
    let other = ensure_full(&e.app, "dir2", "Другая").await["company_id"].as_str().unwrap().to_string();
    assert_eq!(call(&e.app, "PATCH", &format!("/internal/v1/companies/{other}/members/emp"), Some(json!({"role": "editor"}))).await.0, StatusCode::NOT_FOUND);
    assert_eq!(call(&e.app, "DELETE", &format!("/internal/v1/companies/{other}/members/emp"), None).await.0, StatusCode::NOT_FOUND);
    // email директора можно обновить (после установки пароля), роль остаётся
    let (s, v) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/members/dir1"), Some(json!({"email": "New@X.ru"}))).await;
    assert_eq!((s, v["email"].as_str(), v["role"].as_str()), (StatusCode::OK, Some("new@x.ru"), Some("director")));
}

#[tokio::test]
async fn members_limit() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "К").await["company_id"].as_str().unwrap().to_string();
    for i in 1..crate::members::MAX_MEMBERS {
        assert_eq!(add_member(&e.app, &cid, &format!("e{i}"), "viewer").await.0, StatusCode::CREATED);
    }
    let (s, v) = add_member(&e.app, &cid, "one-more", "viewer").await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("too_many_members")));
}

#[test]
fn old_database_gets_director_members_and_ban_columns() {
    let dir = std::env::temp_dir().join(format!("ledger-old-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("old.db");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        // схема до ролей: ни inn/ogrn, ни company_members, ни banned_*
        c.execute_batch(
            "CREATE TABLE companies(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, data_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
             INSERT INTO companies(id, owner_id, name, created_at) VALUES('c1','owner-1','Старая','2026-01-01T00:00:00+00:00');
             INSERT INTO companies(id, owner_id, name, created_at) VALUES('c2','owner-2','Вторая','2026-02-01T00:00:00+00:00');",
        )
        .unwrap();
    }
    let db = db::open(path.to_str().unwrap());
    {
        let c = db.lock().unwrap();
        let rows: Vec<(String, String, String)> = c
            .prepare("SELECT company_id, user_id, role FROM company_members ORDER BY company_id")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .map(|x| x.unwrap())
            .collect();
        assert_eq!(rows, vec![("c1".into(), "owner-1".into(), "director".into()), ("c2".into(), "owner-2".into(), "director".into())]);
        assert_eq!(c.query_row("SELECT COUNT(*) FROM companies WHERE banned_at IS NULL AND ban_reason IS NULL", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    }
    drop(db);
    // повторное открытие ничего не ломает и не дублирует
    let db = db::open(path.to_str().unwrap());
    assert_eq!(db.lock().unwrap().query_row("SELECT COUNT(*) FROM company_members", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn old_director_is_found_by_user_and_email_is_filled_on_ensure() {
    let dir = std::env::temp_dir().join(format!("ledger-old2-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("old.db");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch(
            "CREATE TABLE companies(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, data_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
             INSERT INTO companies(id, owner_id, name, created_at) VALUES('c1','owner-1','Старая','2026-01-01T00:00:00+00:00');",
        )
        .unwrap();
    }
    let st = AppState { db: db::open(path.to_str().unwrap()), parser: Upstream::new("parser", "p", "http://127.0.0.1:1", 1) };
    let app = build(st);
    let (s, m) = call(&app, "GET", "/internal/v1/members/by-user/owner-1", None).await;
    assert_eq!((s, m["company_id"].as_str(), m["role"].as_str()), (StatusCode::OK, Some("c1"), Some("director")));
    let (_, l) = call(&app, "GET", "/internal/v1/companies/c1/members", None).await;
    assert_eq!(l["items"][0]["email"], "");
    ensure_full(&app, "owner-1", "x").await;
    let (_, l) = call(&app, "GET", "/internal/v1/companies/c1/members", None).await;
    assert_eq!(l["items"][0]["email"], "owner-1@x.ru");
    drop(app);
    let _ = std::fs::remove_dir_all(dir);
}

// ---------- оператор: метаданные, блокировка ----------
#[tokio::test]
async fn admin_list_has_aggregates_filters_and_no_company_data() {
    let e = env().await;
    let a = ensure_full(&e.app, "dir1", "Ромашка").await["company_id"].as_str().unwrap().to_string();
    let b = ensure_full(&e.app, "dir2", "Василёк").await["company_id"].as_str().unwrap().to_string();
    add_member(&e.app, &a, "emp1", "editor").await;
    import_confirmed(&e.app, &a, "a.csv", base_txs()).await;
    call(&e.app, "POST", &format!("/internal/v1/companies/{a}/expected-events"), Some(json!({"date": Local::now().date_naive().to_string(), "amount_kop": "1000", "direction": "in", "title": "Секретный платёж"}))).await;

    let (s, l) = call(&e.app, "GET", "/internal/v1/admin/companies", None).await;
    assert_eq!((s, l["total"].as_i64()), (StatusCode::OK, Some(2)));
    let item = l["items"].as_array().unwrap().iter().find(|c| c["id"] == a.as_str()).unwrap();
    assert_eq!((item["name"].as_str(), item["members_count"].as_i64(), item["transactions_count"].as_i64(), item["events_count"].as_i64()), (Some("Ромашка"), Some(2), Some(4), Some(1)));
    assert_eq!((item["last_transaction_date"].as_str(), item["banned"].as_bool(), item["director_email"].as_str()), (Some("2026-03-04"), Some(false), Some("dir1@x.ru")));
    assert!(item["last_import_at"].as_str().is_some() && item["created_at"].as_str().is_some());
    let empty = l["items"].as_array().unwrap().iter().find(|c| c["id"] == b.as_str()).unwrap();
    assert_eq!((empty["transactions_count"].as_i64(), empty["last_transaction_date"].is_null(), empty["last_import_at"].is_null()), (Some(0), true, true));
    // содержимое данных компании в ответах оператора отсутствует
    let raw = l.to_string();
    for secret in ["Секретный", "Заказчик", "Аренда", "Оплата по договору", "purpose", "counterparty"] {
        assert!(!raw.contains(secret), "{secret}");
    }

    // поиск (в том числе кириллица в другом регистре) и пагинация
    let (_, q) = call(&e.app, "GET", "/internal/v1/admin/companies?query=%D1%80%D0%BE%D0%BC%D0%B0%D0%A8", None).await; // «ромаШ»
    assert_eq!((q["total"].as_i64(), q["items"][0]["id"].as_str()), (Some(1), Some(a.as_str())));
    let (_, q) = call(&e.app, "GET", "/internal/v1/admin/companies?query=dir2%40x", None).await;
    assert_eq!(q["items"][0]["id"], b.as_str());
    let (_, q) = call(&e.app, "GET", "/internal/v1/admin/companies?limit=1&offset=1", None).await;
    assert_eq!((q["total"].as_i64(), q["items"].as_array().unwrap().len()), (Some(2), 1));
    assert_eq!(call(&e.app, "GET", "/internal/v1/admin/companies?banned=maybe", None).await.0, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn ban_unban_flow_visible_to_members_lookup() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "Ромашка").await["company_id"].as_str().unwrap().to_string();
    add_member(&e.app, &cid, "emp1", "viewer").await;

    let (s, v) = call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/ban"), Some(json!({"reason": "  Нарушение условий  "}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!((v["banned"].as_bool(), v["ban_reason"].as_str()), (Some(true), Some("Нарушение условий")));
    let banned_at = v["banned_at"].as_str().unwrap().to_string();
    // участники (user_id нужны backoffice для закрытия сессий)
    let ids: Vec<_> = v["members"].as_array().unwrap().iter().map(|m| m["user_id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["dir1", "emp1"]);

    for uid in ["dir1", "emp1"] {
        let (_, m) = call(&e.app, "GET", &format!("/internal/v1/members/by-user/{uid}"), None).await;
        assert_eq!((m["banned"].as_bool(), m["ban_reason"].as_str()), (Some(true), Some("Нарушение условий")), "{uid}");
    }
    assert_eq!(ensure_full(&e.app, "dir1", "x").await["banned"], true);
    let (_, l) = call(&e.app, "GET", "/internal/v1/admin/companies?banned=true", None).await;
    assert_eq!(l["total"], 1);
    assert_eq!(call(&e.app, "GET", "/internal/v1/admin/companies?banned=false", None).await.1["total"], 0);

    // повторная блокировка обновляет причину, момент не меняется
    let (_, v2) = call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/ban"), Some(json!({"reason": "Другая причина"}))).await;
    assert_eq!((v2["banned_at"].as_str(), v2["ban_reason"].as_str()), (Some(banned_at.as_str()), Some("Другая причина")));
    let (_, g) = call(&e.app, "GET", &format!("/internal/v1/admin/companies/{cid}"), None).await;
    assert_eq!(g["banned"], true);

    let (s, v) = call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/unban"), None).await;
    assert_eq!((s, v["banned"].as_bool(), v["ban_reason"].is_null()), (StatusCode::OK, Some(false), true));
    let (_, m) = call(&e.app, "GET", "/internal/v1/members/by-user/emp1", None).await;
    assert_eq!(m["banned"], false);
}

#[tokio::test]
async fn ban_validation_and_unknown_company() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "К").await["company_id"].as_str().unwrap().to_string();
    for reason in ["", "   "] {
        let (s, v) = call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/ban"), Some(json!({"reason": reason}))).await;
        assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_reason")));
    }
    let (s, _) = call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/ban"), Some(json!({"reason": "я".repeat(501)}))).await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(call(&e.app, "POST", "/internal/v1/admin/companies/nope/ban", Some(json!({"reason": "x"}))).await.0, StatusCode::NOT_FOUND);
    assert_eq!(call(&e.app, "POST", "/internal/v1/admin/companies/nope/unban", None).await.0, StatusCode::NOT_FOUND);
    assert_eq!(call(&e.app, "GET", "/internal/v1/admin/companies/nope", None).await.0, StatusCode::NOT_FOUND);
    // блокировка не трогает данные компании
    import_confirmed(&e.app, &cid, "a.csv", base_txs()).await;
    call(&e.app, "POST", &format!("/internal/v1/admin/companies/{cid}/ban"), Some(json!({"reason": "x"}))).await;
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    assert_eq!(t["total"], 4);
}

#[tokio::test]
async fn admin_stats_counts_companies() {
    let e = env().await;
    let a = ensure_full(&e.app, "dir1", "А").await["company_id"].as_str().unwrap().to_string();
    ensure_full(&e.app, "dir2", "Б").await;
    add_member(&e.app, &a, "emp1", "viewer").await;
    call(&e.app, "POST", &format!("/internal/v1/admin/companies/{a}/ban"), Some(json!({"reason": "x"}))).await;
    let (s, v) = call(&e.app, "GET", "/internal/v1/admin/stats", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(
        (v["companies_total"].as_i64(), v["companies_banned"].as_i64(), v["companies_active"].as_i64(), v["new_7d"].as_i64(), v["new_30d"].as_i64(), v["members_total"].as_i64()),
        (Some(2), Some(1), Some(1), Some(2), Some(2), Some(3))
    );
}

#[tokio::test]
async fn audit_log_lists_newest_first_filters_and_isolates_companies() {
    let e = env().await;
    let a = company(&e.app, "oa").await;
    let b = company(&e.app, "ob").await;
    for (cid, action) in [(&a, "team.invite_create"), (&a, "goal.status"), (&a, "team.member_role"), (&b, "team.leave")] {
        let (s, _) = call(&e.app, "POST", &format!("/internal/v1/companies/{cid}/audit"), Some(json!({"action": action, "object_id": "o"}))).await;
        assert_eq!(s, StatusCode::NO_CONTENT);
    }
    let (s, v) = call(&e.app, "GET", &format!("/internal/v1/companies/{a}/audit"), None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["total"], 3);
    assert_eq!(v["items"][0]["action"], "team.member_role"); // новые сверху
    assert_eq!(v["items"][0]["actor"], "tester");
    let (_, f) = call(&e.app, "GET", &format!("/internal/v1/companies/{a}/audit?q=TEAM"), None).await;
    assert_eq!(f["total"], 2);
    let (_, p) = call(&e.app, "GET", &format!("/internal/v1/companies/{a}/audit?limit=1&offset=1"), None).await;
    assert_eq!((p["items"].as_array().unwrap().len(), p["items"][0]["action"].as_str()), (1, Some("goal.status")));
    let (_, other) = call(&e.app, "GET", &format!("/internal/v1/companies/{b}/audit"), None).await;
    assert_eq!(other["total"], 1); // чужие записи не видны
}

// ---------- подкатегории, правила компании, модель на хвост ----------
fn sub_tx(row: i64, cp: &str, amount: i64, sub: &str, method: &str) -> Value {
    let mut t = tx(row, "2026-03-02", amount, cp, "Платёж", "revenue");
    t["subcategory"] = json!(sub);
    t["group"] = json!("revenue");
    t["method"] = json!(method);
    t
}

#[tokio::test]
async fn migration_adds_subcategory_columns_to_an_old_database() {
    let dir = std::env::temp_dir().join(format!("ledger-mig-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("old.db");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch(
            "CREATE TABLE companies(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, data_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
             CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, import_id TEXT NOT NULL, row_no INTEGER NOT NULL, date TEXT NOT NULL,
               amount_kop INTEGER NOT NULL, balance_kop INTEGER, counterparty TEXT NOT NULL, inn TEXT NOT NULL, purpose TEXT NOT NULL, category TEXT NOT NULL,
               category_conf REAL NOT NULL, manual INTEGER NOT NULL DEFAULT 0, dedup_key TEXT NOT NULL, UNIQUE(company_id, dedup_key));",
        )
        .unwrap();
    }
    let db = db::open(path.to_str().unwrap());
    // повторное открытие идемпотентно
    drop(db);
    let db = db::open(path.to_str().unwrap());
    let c = db.lock().unwrap();
    let n: i64 = c.query_row("SELECT COUNT(*) FROM pragma_table_info('transactions') WHERE name IN ('subcategory','grp','method')", [], |r| r.get(0)).unwrap();
    assert_eq!(n, 3);
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn subcategory_correction_becomes_a_company_rule_and_applies_to_similar_rows() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    import_confirmed(
        &e.app,
        &cid,
        "a.csv",
        vec![sub_tx(1, "ИП Иванов", 500_000, "other_in", "fallback"), sub_tx(2, "ИП Иванов", 700_000, "other_in", "fallback"), sub_tx(3, "ООО Другой", 900_000, "other_in", "fallback")],
    )
    .await;
    let rev = revision(&e.app, &cid).await;
    let by_cp = |t: &Value, cp: &str| t["items"].as_array().unwrap().iter().filter(|x| x["counterparty"] == cp).cloned().collect::<Vec<_>>();
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    let id = by_cp(&t, "ИП Иванов")[0]["id"].as_i64().unwrap();
    let uri = format!("/internal/v1/companies/{cid}/transactions/{id}/category");
    let (s, v) = call(&e.app, "PATCH", &uri, Some(json!({"subcategory": "customer_payment"}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!((v["category"].as_str(), v["group"].as_str(), v["applied_to_similar"].as_i64()), (Some("revenue"), Some("revenue"), Some(1)));
    assert_eq!(revision(&e.app, &cid).await, rev + 1);
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    let ivan = by_cp(&t, "ИП Иванов");
    let methods: Vec<&str> = ivan.iter().map(|x| x["method"].as_str().unwrap()).collect();
    assert!(methods.contains(&"manual") && methods.contains(&"tenant_rule"), "{methods:?}");
    assert!(ivan.iter().all(|x| x["subcategory"] == "customer_payment"));
    assert_eq!(by_cp(&t, "ООО Другой")[0]["subcategory"], "other_in");
    // новая выписка: операции этого контрагента размечаются правилом компании ещё до подтверждения
    let (s, up) = upload(&e.app, &cid, "b.csv", &parsed(vec![sub_tx(1, "  ИП ИВАНОВ ", 100_000, "other_in", "fallback")])).await;
    assert_eq!(s, StatusCode::CREATED, "{up}");
    assert_eq!(up["summary"]["sample"][0]["method"], "tenant_rule");
    assert_eq!(up["summary"]["sample"][0]["subcategory"], "customer_payment");
}

#[tokio::test]
async fn subcategory_validation_and_company_scope() {
    let (e, cid) = seeded().await;
    let other = company(&e.app, "u2").await;
    let (_, t) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/transactions"), None).await;
    let id = t["items"][0]["id"].as_i64().unwrap();
    let rev = revision(&e.app, &cid).await;
    let (s, v) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{cid}/transactions/{id}/category"), Some(json!({"subcategory": "nope"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("invalid_subcategory")));
    let (s, _) = call(&e.app, "PATCH", &format!("/internal/v1/companies/{other}/transactions/{id}/category"), Some(json!({"subcategory": "customer_payment"}))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(revision(&e.app, &cid).await, rev);
    let (s, v) = call(&e.app, "GET", &format!("/internal/v1/companies/{cid}/taxonomy"), None).await;
    assert_eq!((s, v["subcategories"]["supplier_payment"]["category"].as_str()), (StatusCode::OK, Some("suppliers")));
}

#[tokio::test]
async fn tail_model_labels_fallback_rows_only_with_enough_training_data() {
    let e = env().await;
    let cid = company(&e.app, "u1").await;
    let unknown = |row| sub_tx(row, "Неизвестный", 123_000, "other_in", "fallback");
    // мало обучающих примеров: хвост не вызывается
    let (_, up) = upload(&e.app, &cid, "a.csv", &parsed(vec![unknown(1)])).await;
    assert_eq!(up["summary"]["sample"][0]["method"], "fallback");
    // 210 размеченных правилами операций
    let train: Vec<Value> = (0..210).map(|i| {
        let mut t = sub_tx(i, &format!("Клиент {i}"), 10_000 + i, "customer_payment", "rule");
        t["date"] = json!(format!("2026-02-{:02}", 1 + i % 27));
        t
    }).collect();
    import_confirmed(&e.app, &cid, "train.csv", train).await;
    let mixed = vec![unknown(1), sub_tx(2, "Налоговая", -300_000, "other_out", "fallback"), sub_tx(3, "Арендодатель", -500_000, "rent", "rule")];
    let (s, up) = upload(&e.app, &cid, "b.csv", &parsed(mixed)).await;
    assert_eq!(s, StatusCode::CREATED, "{up}");
    let sample = up["summary"]["sample"].as_array().unwrap();
    assert_eq!((sample[0]["method"].as_str(), sample[0]["subcategory"].as_str()), (Some("tail"), Some("customer_payment")));
    assert_eq!(sample[0]["category_confidence"], 0.9);
    assert_eq!(sample[1]["method"], "fallback", "модель не уверена -> остаётся как было");
    assert_eq!(sample[2]["method"], "rule", "правила не перезаписываются моделью");
}


// ---------- администратор, приглашения и передача владения ----------
async fn members_of(app: &Router, cid: &str) -> Vec<(String, String)> {
    let (_, l) = call(app, "GET", &format!("/internal/v1/companies/{cid}/members"), None).await;
    l["items"].as_array().unwrap().iter().map(|m| (m["user_id"].as_str().unwrap().to_string(), m["role"].as_str().unwrap().to_string())).collect()
}

fn pair(u: &str, r: &str) -> (String, String) {
    (u.to_string(), r.to_string())
}

#[tokio::test]
async fn join_moves_a_fresh_own_company_but_refuses_one_with_data_or_team() {
    let e = env().await;
    let team = ensure_full(&e.app, "dir1", "Команда").await["company_id"].as_str().unwrap().to_string();
    // у приглашённого уже есть собственная пустая компания (создаётся при первом входе)
    let own = ensure_full(&e.app, "inv1", "Пустая").await["company_id"].as_str().unwrap().to_string();
    let (s, v) = call(&e.app, "POST", &format!("/internal/v1/companies/{team}/members/join"), Some(json!({"user_id": "inv1", "email": "inv1@x.ru", "role": "editor"}))).await;
    assert_eq!((s, v["role"].as_str()), (StatusCode::CREATED, Some("editor")), "{v}");
    assert_eq!(members_of(&e.app, &team).await, [pair("dir1", "director"), pair("inv1", "editor")]);
    assert_eq!(call(&e.app, "GET", &format!("/internal/v1/admin/companies/{own}"), None).await.0, StatusCode::NOT_FOUND, "пустая компания удалена");
    assert_eq!(ensure_full(&e.app, "inv1", "x").await["company_id"], team.as_str());
    // повторно в ту же команду
    let (s, v) = call(&e.app, "POST", &format!("/internal/v1/companies/{team}/members/join"), Some(json!({"user_id": "inv1", "role": "viewer"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("already_member")));
    // своя компания с данными: переехать нельзя, ничего не теряется
    let rich = ensure_full(&e.app, "inv2", "С данными").await["company_id"].as_str().unwrap().to_string();
    import_confirmed(&e.app, &rich, "a.csv", base_txs()).await;
    let (s, v) = call(&e.app, "POST", &format!("/internal/v1/companies/{team}/members/join"), Some(json!({"user_id": "inv2", "role": "viewer"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::CONFLICT, Some("has_company_data")));
    assert_eq!(call(&e.app, "GET", "/internal/v1/members/by-user/inv2", None).await.1["company_id"], rich.as_str());
    // своя компания с командой тоже
    let boss = ensure_full(&e.app, "boss", "С командой").await["company_id"].as_str().unwrap().to_string();
    add_member(&e.app, &boss, "worker", "viewer").await;
    assert_eq!(call(&e.app, "POST", &format!("/internal/v1/companies/{team}/members/join"), Some(json!({"user_id": "boss", "role": "viewer"}))).await.0, StatusCode::CONFLICT);
    let (s, _) = call(&e.app, "POST", &format!("/internal/v1/companies/{team}/members/join"), Some(json!({"user_id": "x", "role": "director"}))).await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn transfer_makes_old_director_an_admin_and_he_can_be_removed_without_regaining_rights() {
    let e = env().await;
    let cid = ensure_full(&e.app, "dir1", "К").await["company_id"].as_str().unwrap().to_string();
    add_member(&e.app, &cid, "emp1", "editor").await;
    let path = format!("/internal/v1/companies/{cid}/transfer");
    assert_eq!(call(&e.app, "POST", &path, Some(json!({"user_id": "ghost"}))).await.0, StatusCode::NOT_FOUND);
    let (s, v) = call(&e.app, "POST", &path, Some(json!({"user_id": "dir1"}))).await;
    assert_eq!((s, v["error"]["code"].as_str()), (StatusCode::UNPROCESSABLE_ENTITY, Some("same_owner")));
    let (s, _) = call(&e.app, "POST", &path, Some(json!({"user_id": "emp1"}))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(members_of(&e.app, &cid).await, [pair("emp1", "director"), pair("dir1", "admin")]);
    assert_eq!(call(&e.app, "GET", "/internal/v1/members/by-user/dir1", None).await.1["role"], "admin");
    // новый директор неизменяем, бывшего можно удалить
    assert_eq!(call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/members/emp1"), None).await.0, StatusCode::CONFLICT);
    assert_eq!(call(&e.app, "DELETE", &format!("/internal/v1/companies/{cid}/members/dir1"), None).await.0, StatusCode::NO_CONTENT);
    // прав директора ему это не возвращает: при входе он получает новую пустую компанию
    let own = ensure_full(&e.app, "dir1", "Новая").await;
    assert_ne!(own["company_id"].as_str(), Some(cid.as_str()));
    assert_eq!(own["role"], "director");
    assert_eq!(members_of(&e.app, &cid).await, [pair("emp1", "director")]);
}

#[test]
fn members_table_with_three_roles_is_rebuilt_to_allow_admin() {
    let dir = std::env::temp_dir().join(format!("ledger-roles3-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("r3.db");
    {
        let c = rusqlite::Connection::open(&path).unwrap();
        c.execute_batch(
            "CREATE TABLE companies(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, data_revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, inn TEXT NOT NULL DEFAULT '', ogrn TEXT NOT NULL DEFAULT '', banned_at TEXT, ban_reason TEXT);
             INSERT INTO companies(id, owner_id, name, created_at) VALUES('c1','o1','К','2026-01-01T00:00:00+00:00');
             CREATE TABLE company_members(company_id TEXT NOT NULL REFERENCES companies(id), user_id TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL CHECK(role IN ('director','editor','viewer')), created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');
             INSERT INTO company_members VALUES('c1','o1','o@x.ru','director','2026-01-01','');
             INSERT INTO company_members VALUES('c1','e1','e@x.ru','editor','2026-01-02','o1');",
        )
        .unwrap();
    }
    let db = db::open(path.to_str().unwrap());
    {
        let c = db.lock().unwrap();
        let rows: i64 = c.query_row("SELECT COUNT(*) FROM company_members", [], |r| r.get(0)).unwrap();
        assert_eq!(rows, 2, "участники сохранены");
        c.execute("UPDATE company_members SET role='admin' WHERE user_id='e1'", []).expect("admin допустим после пересборки");
    }
    drop(db);
    let db = db::open(path.to_str().unwrap());
    assert_eq!(db.lock().unwrap().query_row("SELECT role FROM company_members WHERE user_id='e1'", [], |r| r.get::<_, String>(0)).unwrap(), "admin");
    let _ = std::fs::remove_dir_all(dir);
}
