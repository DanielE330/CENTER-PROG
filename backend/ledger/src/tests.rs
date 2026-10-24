//! Тесты внутреннего API ledger: временная SQLite, запросы через `Router::oneshot` (без сети к ledger).
//! Сервис разбора подменён локальной заглушкой на эфемерном порту loopback: она возвращает
//! содержимое загруженного «файла» (JSON) как ответ parser.

use crate::{AppState, build, db};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::Multipart,
    http::{Request, StatusCode},
    routing::post,
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
        axum::serve(l, Router::new().route("/internal/v1/parse", post(fake_parse))).await.unwrap();
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
