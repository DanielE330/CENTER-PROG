//! Интеграция с ФНС (fns.rs): настоящий роутер api поверх заглушек. Ledger и auth изображает общая заглушка roles_tests
//! (снимок, bulk событий с идемпотентностью по external_id, топ контрагентов, флаги), сервис fns — отдельный подставной сервер
//! на эфемерном порту с ответами по контракту из docs/fns-integration-plan.md.

use crate::roles_tests::{T, code};
use axum::{
    Router,
    body::to_bytes,
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

const INN: &str = "7701234567";

#[derive(Default)]
struct FnsFake {
    obligations: Value,
    ens: Value,
    batch: Value,
    import: Value,
    /// все запросы: (метод, путь с query, тело)
    calls: Vec<(String, String, Value)>,
    /// принудительный ответ на любой запрос
    fail: Option<(StatusCode, Value)>,
}

type Fns = Arc<Mutex<FnsFake>>;

async fn fns_handler(State(s): State<Fns>, req: Request) -> Response {
    let (method, uri) = (req.method().to_string(), req.uri().clone());
    let bytes = to_bytes(req.into_body(), 1 << 20).await.unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    let mut g = s.lock().unwrap();
    g.calls.push((method, uri.path_and_query().map(|p| p.to_string()).unwrap_or_default(), body));
    if let Some((st, v)) = g.fail.clone() {
        return (st, axum::Json(v)).into_response();
    }
    let v = match uri.path() {
        "/internal/v1/fns/obligations" => g.obligations.clone(),
        "/internal/v1/fns/ens" => g.ens.clone(),
        "/internal/v1/fns/counterparties/batch" => g.batch.clone(),
        "/internal/v1/fns/ens/import" => g.import.clone(),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    axum::Json(v).into_response()
}

async fn spawn_fns(fake: Fns) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://127.0.0.1:{}", l.local_addr().unwrap().port());
    let app = Router::new().fallback(fns_handler).with_state(fake);
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    base
}

fn obligations() -> Value {
    json!({"source": "fns_sim", "mode": "sim", "items": [
        {"kind": "enp", "date": "2026-10-28", "title": "ЕНП за сентябрь"},
        {"kind": "enp", "date": "2026-11-30"},
        {"kind": "usn_advance", "date": "2027-04-28", "amount_kop": 9_000_000},
        {"kind": "enp", "date": "2026-09-28"},
    ]})
}

fn ens_with_debt() -> Value {
    json!({"source": "fns_sim", "mode": "sim", "balance_kop": -2_500_000, "debt_kop": 2_500_000, "checked_at": "2026-10-24T09:00:00Z",
        "upcoming": [{"kind": "enp", "date": "2026-10-28", "amount_kop": 1_200_000}]})
}

/// Снимок компании: налоговые платежи по месяцам (оценка ЕНП = медиана) и выручка.
fn snapshot() -> Value {
    let tx = |date: &str, amount: i64, cat: &str, cp: &str| json!({"date": date, "amount": amount, "counterparty": cp, "inn": "", "purpose": "", "category": cat, "balance": null});
    json!({"data_revision": 3, "events": [], "transactions": [
        tx("2026-07-28", -300_000, "taxes", "ФНС"), tx("2026-08-28", -500_000, "taxes", "ФНС"), tx("2026-09-28", -400_000, "taxes", "ФНС"),
        tx("2026-09-29", 10_000_000, "revenue", "Клиент"),
    ]})
}

struct Env {
    t: T,
    fns: Fns,
    cid: String,
    /// сессии: director, editor, viewer
    toks: [String; 3],
}

async fn env_with(inn: Option<&str>) -> Env {
    let fns: Fns = Arc::new(Mutex::new(FnsFake { obligations: obligations(), ens: ens_with_debt(), batch: json!({"items": []}), ..Default::default() }));
    let base = spawn_fns(fns.clone()).await;
    let t = T::with_fns(Some(base)).await;
    let (cid, toks) = t.team();
    if let Some(i) = inn {
        t.f.lock().unwrap().inns.insert(cid.clone(), i.to_string());
    }
    t.f.lock().unwrap().snapshot = Some(snapshot());
    Env { t, fns, cid, toks }
}

impl Env {
    fn fns_calls(&self) -> Vec<(String, String, Value)> {
        self.fns.lock().unwrap().calls.clone()
    }
    fn bulk(&self) -> Vec<Value> {
        self.t.f.lock().unwrap().bulk.clone()
    }
    fn uri(&self, tail: &str) -> String {
        format!("/api/v1/companies/{}/{tail}", self.cid)
    }
}

#[tokio::test]
async fn sync_without_inn_is_422_with_a_hint_and_never_calls_fns() {
    let e = env_with(None).await;
    for tok in &e.toks[..2] {
        let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), tok, Some(json!({}))).await;
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "inn_required"), "{v}");
        assert!(v["error"]["message"].as_str().unwrap().contains("Реквизиты компании"));
    }
    for tail in ["fns/ens", "fns/counterparties"] {
        let (s, v) = e.t.call("GET", &e.uri(tail), &e.toks[2], None).await;
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "inn_required"), "{tail}");
    }
    // неверный ИНН (не цифры) тоже не уходит в ФНС
    e.t.f.lock().unwrap().inns.insert(e.cid.clone(), "77-abc".into());
    assert_eq!(e.t.call("GET", &e.uri("fns/ens"), &e.toks[0], None).await.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(e.fns_calls().is_empty());
}

#[tokio::test]
async fn viewer_cannot_sync_but_can_read_ens_and_counterparties() {
    let e = env_with(Some(INN)).await;
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[2], Some(json!({}))).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert!(e.fns_calls().is_empty(), "отказ по правам раньше обращения к fns");
    assert_eq!(e.t.call("GET", &e.uri("fns/ens"), &e.toks[2], None).await.0, StatusCode::OK);
    assert_eq!(e.t.call("GET", &e.uri("fns/counterparties"), &e.toks[2], None).await.0, StatusCode::OK);
}

#[tokio::test]
async fn sync_creates_tax_events_idempotently_with_source_and_amounts() {
    let e = env_with(Some(INN)).await;
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[1], Some(json!({"year": 2026, "regime": "usn_income"}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    // 3 срока позже конца выписки (28.09) + недоимка ЕНС; прошедший срок пропущен
    assert_eq!((v["created"].as_u64(), v["skipped"].as_u64(), v["estimated"].as_u64(), v["no_amount"].as_u64()), (Some(4), Some(0), Some(1), Some(0)), "{v}");
    assert_eq!((v["source"].as_str(), v["source_label"].as_str(), v["mode"].as_str(), v["year"].as_i64(), v["regime"].as_str(), v["regime_source"].as_str()), (Some("fns_sim"), Some("ФНС"), Some("sim"), Some(2026), Some("usn_income"), Some("request")));
    assert_eq!(v["debt_kop"], "2500000");
    assert!(v["fetched_at"].as_str().is_some());

    let bulk = e.bulk();
    let by_id = |id: &str| bulk.iter().find(|x| x["external_id"] == id).unwrap_or_else(|| panic!("нет {id}: {bulk:?}"));
    // сумма из ЕНС (1 200 000) для срока 28.10; из ФНС (9 000 000) для аванса; оценка по выписке для 30.11 — медиана налогов 400 000
    assert_eq!(by_id("fns:enp:2026-10-28")["amount_kop"], -1_200_000);
    assert_eq!(by_id("fns:usn_advance:2027-04-28")["amount_kop"], -9_000_000);
    let est = by_id("fns:enp:2026-11-30");
    assert_eq!(est["amount_kop"], -400_000);
    assert_eq!(est["confidence"], 0.9);
    assert!(est["title"].as_str().unwrap().contains("оценка по выписке"));
    assert_eq!(by_id("fns:ens_debt:2500000")["amount_kop"], -2_500_000);
    assert_eq!(by_id("fns:ens_debt:2500000")["date"], "2026-09-29");
    for ev in &bulk {
        assert_eq!((ev["source"].as_str(), ev["counterparty"].as_str()), (Some("tax"), Some("ФНС России")));
        assert!(ev["title"].as_str().unwrap().contains("ФНС (имитация)"), "{ev}");
    }
    assert!(bulk.iter().all(|x| x["external_id"] != "fns:enp:2026-09-28"));
    assert_eq!(by_id("fns:enp:2026-10-28")["title"], "ЕНП за сентябрь · ФНС (имитация)", "название из ФНС сохраняется, источник и режим дописываются");

    // запрос к fns: ИНН компании, режим и год
    let first = e.fns_calls().into_iter().find(|c| c.1.starts_with("/internal/v1/fns/obligations")).unwrap();
    assert_eq!(first.1, format!("/internal/v1/fns/obligations?inn={INN}&regime=usn_income&year=2026"));

    // повторная синхронизация ничего не дублирует
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[0], Some(json!({"year": 2026, "regime": "usn_income"}))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((v["created"].as_u64(), v["skipped"].as_u64()), (Some(0), Some(4)));
    assert_eq!(e.bulk().len(), 4);
    assert!(e.t.calls().iter().any(|c| c == "POST /internal/v1/companies/".to_string().as_str() || c.ends_with("/audit")), "действие попадает в журнал");
}

#[tokio::test]
async fn sync_validates_year_and_regime_and_defaults_to_the_statement_year() {
    let e = env_with(Some(INN)).await;
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[0], Some(json!({"regime": "magic"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_regime"));
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[0], Some(json!({"year": 1999}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_year"));
    assert!(e.fns_calls().is_empty());
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[0], Some(json!({}))).await;
    assert_eq!((s, v["year"].as_i64(), v["regime_source"].as_str()), (StatusCode::OK, Some(2026), Some("default")), "{v}");
}

#[tokio::test]
async fn sync_works_without_ens_and_with_live_mode_titles() {
    let e = env_with(Some(INN)).await;
    {
        let mut g = e.fns.lock().unwrap();
        g.obligations = json!({"source": "egrul", "mode": "live", "items": [{"kind": "enp", "date": "2026-11-30"}]});
        g.ens = json!({"error": {"code": "fns_source_unavailable", "message": "нет"}});
    }
    // ЕНС недоступен: ответ fns на /ens — 200 с «ошибкой» в теле не ошибка транспорта, поэтому сумму берём по оценке, недоимки нет
    let (s, v) = e.t.call("POST", &e.uri("integrations/fns/sync"), &e.toks[0], Some(json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!((v["mode"].as_str(), v["created"].as_u64(), v["estimated"].as_u64()), (Some("live"), Some(1), Some(1)));
    assert_eq!(v["debt_kop"], Value::Null);
    let b = e.bulk();
    assert_eq!(b.len(), 1);
    assert_eq!(b[0]["title"], "ЕНП · ФНС · оценка по выписке", "в режиме live пометки «имитация» нет");
}

#[tokio::test]
async fn fns_down_gives_503_with_a_clear_code_on_every_route() {
    // порт без слушателя: сервис недоступен
    let t = T::with_fns(Some("http://127.0.0.1:1".into())).await;
    let (cid, toks) = t.team();
    t.f.lock().unwrap().inns.insert(cid.clone(), INN.into());
    t.f.lock().unwrap().snapshot = Some(snapshot());
    t.f.lock().unwrap().top = Some(json!({"items": [{"inn": "7702000000", "name": "ООО Х", "turnover_kop": 100, "income_kop": 100, "expense_kop": 0, "tx_count": 1, "income_share": 1.0, "risk": null}], "total_income_kop": 100}));
    for (m, tail, tok) in [("POST", "integrations/fns/sync", &toks[1]), ("GET", "fns/ens", &toks[2]), ("GET", "fns/counterparties", &toks[2])] {
        let (s, v) = t.call(m, &format!("/api/v1/companies/{cid}/{tail}"), tok, if m == "POST" { Some(json!({})) } else { None }).await;
        assert_eq!((s, code(&v)), (StatusCode::SERVICE_UNAVAILABLE, "fns_unavailable"), "{tail}: {v}");
        assert!(v["error"]["message"].as_str().unwrap().contains("ФНС") || v["error"]["message"].as_str().unwrap().contains("тест"));
    }
    assert!(t.f.lock().unwrap().bulk.is_empty(), "при недоступном fns события не создаются");
}

#[tokio::test]
async fn fns_errors_are_translated_5xx_to_503_and_4xx_passed_through() {
    let e = env_with(Some(INN)).await;
    e.fns.lock().unwrap().fail = Some((StatusCode::BAD_GATEWAY, json!({"error": {"code": "fns_source_unavailable", "message": "Реестр ФНС не отвечает"}})));
    let (s, v) = e.t.call("GET", &e.uri("fns/ens"), &e.toks[0], None).await;
    assert_eq!((s, code(&v), v["error"]["message"].as_str()), (StatusCode::SERVICE_UNAVAILABLE, "fns_source_unavailable", Some("Реестр ФНС не отвечает")));
    e.fns.lock().unwrap().fail = Some((StatusCode::INTERNAL_SERVER_ERROR, json!({"oops": 1})));
    let (s, v) = e.t.call("GET", &e.uri("fns/ens"), &e.toks[0], None).await;
    assert_eq!((s, code(&v)), (StatusCode::SERVICE_UNAVAILABLE, "fns_unavailable"));
    e.fns.lock().unwrap().fail = Some((StatusCode::UNPROCESSABLE_ENTITY, json!({"detail": {"code": "invalid_inn", "message": "ИНН неверный"}})));
    let (s, v) = e.t.call("GET", &e.uri("fns/ens"), &e.toks[0], None).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_inn"));
    e.fns.lock().unwrap().fail = Some((StatusCode::NOT_FOUND, json!("мусор")));
    let (s, v) = e.t.call("GET", &e.uri("fns/ens"), &e.toks[0], None).await;
    assert_eq!((s, code(&v)), (StatusCode::SERVICE_UNAVAILABLE, "fns_unavailable"));
}

#[tokio::test]
async fn ens_returns_balance_debt_upcoming_and_source() {
    let e = env_with(Some(INN)).await;
    let (s, v) = e.t.call("GET", &e.uri("fns/ens"), &e.toks[2], None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!((v["balance_kop"].as_str(), v["debt_kop"].as_str(), v["has_debt"].as_bool()), (Some("-2500000"), Some("2500000"), Some(true)));
    assert_eq!((v["source"].as_str(), v["source_label"].as_str(), v["mode"].as_str(), v["checked_at"].as_str()), (Some("fns_sim"), Some("ФНС"), Some("sim"), Some("2026-10-24T09:00:00Z")));
    assert_eq!((v["upcoming"][0]["date"].as_str(), v["upcoming"][0]["amount_kop"].as_str()), (Some("2026-10-28"), Some("1200000")));
    assert_eq!(e.fns_calls()[0].1, format!("/internal/v1/fns/ens?inn={INN}"));
    // чужая компания — 404 раньше обращения к fns
    let n = e.fns_calls().len();
    let (s, _) = e.t.call("GET", "/api/v1/companies/c-foreign/fns/ens", &e.toks[0], None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(e.fns_calls().len(), n);
}

#[tokio::test]
async fn ens_import_passes_the_file_to_fns_and_returns_the_snapshot_with_the_import_label() {
    let e = env_with(Some(INN)).await;
    e.fns.lock().unwrap().import = json!({"source": "import", "mode": "live", "format": "knd_1160080", "warnings": ["в файле нет КНД"],
        "balance_kop": -4_500_000, "debt_kop": 4_500_000, "as_of": "2026-10-21", "checked_at": "2026-10-24T10:00:00Z", "upcoming": []});
    let body = json!({"filename": "spravka.xml", "content_b64": "PHg+"});
    // наблюдатель не загружает справки, отказ раньше обращения к fns
    let (s, v) = e.t.call("POST", &e.uri("fns/ens/import"), &e.toks[2], Some(body.clone())).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert!(e.fns_calls().is_empty());
    let (s, v) = e.t.call("POST", &e.uri("fns/ens/import"), &e.toks[1], Some(body)).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!((v["debt_kop"].as_str(), v["format"].as_str(), v["source"].as_str(), v["mode"].as_str()), (Some("4500000"), Some("knd_1160080"), Some("import"), Some("live")));
    assert_eq!(v["warnings"][0], "в файле нет КНД");
    let call = &e.fns_calls()[0];
    assert_eq!(call.1, format!("/internal/v1/fns/ens/import?inn={INN}"));
    assert_eq!((call.2["filename"].as_str(), call.2["content_b64"].as_str()), (Some("spravka.xml"), Some("PHg+")));
    // пустой файл не уходит в fns
    let n = e.fns_calls().len();
    let (s, v) = e.t.call("POST", &e.uri("fns/ens/import"), &e.toks[0], Some(json!({"content_b64": " "}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "fns_import_bad_file"));
    assert_eq!(e.fns_calls().len(), n);
}

#[tokio::test]
async fn ens_import_errors_from_fns_are_passed_through() {
    let e = env_with(Some(INN)).await;
    e.fns.lock().unwrap().fail = Some((StatusCode::UNPROCESSABLE_ENTITY, json!({"detail": {"code": "fns_import_inn_mismatch", "message": "ИНН в справке не совпадает с ИНН компании"}})));
    let (s, v) = e.t.call("POST", &e.uri("fns/ens/import"), &e.toks[0], Some(json!({"content_b64": "PHg+"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "fns_import_inn_mismatch"), "{v}");
}

fn top_rows() -> Value {
    json!({"total_income_kop": 20_000_000, "items": [
        {"inn": "7702000000", "name": "ООО Большой клиент", "turnover_kop": 15_000_000, "income_kop": 15_000_000, "expense_kop": 0, "tx_count": 6, "income_share": 0.75, "last_date": "2026-09-29", "risk": null},
        {"inn": "7703000000", "name": "ООО Поставщик", "turnover_kop": 4_000_000, "income_kop": 0, "expense_kop": 4_000_000, "tx_count": 4, "income_share": 0.0, "last_date": "2026-09-20", "risk": null},
        {"inn": "7704000000", "name": "ООО Не ответили", "turnover_kop": 100, "income_kop": 100, "expense_kop": 0, "tx_count": 1, "income_share": 0.0, "last_date": "2026-09-01", "risk": null},
        {"inn": INN, "name": "Своя компания", "turnover_kop": 50, "income_kop": 50, "expense_kop": 0, "tx_count": 1, "income_share": 0.0, "last_date": "2026-09-01", "risk": null},
    ]})
}

fn batch_reply() -> Value {
    json!({"source": "fns_sim", "mode": "sim", "items": [
        {"inn": "7702000000", "status": "Ликвидируется", "registered_at": "2015-03-02", "mass_address": false, "unreliable": false,
         "tax_debt": {"has": true, "amount_kop": 4_500_000}, "disqualified": false, "bankruptcy": {"flag": false, "source": null},
         "risk_level": "high", "checked_at": "2026-10-24T08:00:00Z", "source": "fns_sim"},
        {"inn": "7703000000", "status": "Действует", "tax_debt": {"has": false, "amount_kop": 0}, "bankruptcy": {"flag": false},
         "risk_level": "low", "checked_at": "2026-10-24T08:00:00Z", "source": "fns_sim"},
    ]})
}

#[tokio::test]
async fn counterparties_are_checked_in_a_batch_saved_to_ledger_and_returned_with_source_and_time() {
    let e = env_with(Some(INN)).await;
    e.t.f.lock().unwrap().top = Some(top_rows());
    e.fns.lock().unwrap().batch = batch_reply();
    let (s, v) = e.t.call("GET", &e.uri("fns/counterparties?top=10"), &e.toks[2], None).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    // пакет: ИНН контрагентов без собственного ИНН компании
    let call = e.fns_calls().into_iter().find(|c| c.1 == "/internal/v1/fns/counterparties/batch").unwrap();
    assert_eq!(call.0, "POST");
    assert_eq!(call.2["inns"], json!(["7702000000", "7703000000", "7704000000"]));
    assert_eq!((v["source"].as_str(), v["source_label"].as_str(), v["mode"].as_str(), v["saved"].as_bool()), (Some("fns_sim"), Some("ФНС"), Some("sim"), Some(true)));
    assert!(v["checked_at"].as_str().is_some());
    let items = v["items"].as_array().unwrap();
    assert_eq!(items.len(), 4);
    let big = &items[0];
    assert_eq!((big["name"].as_str(), big["status"].as_str(), big["risk_level"].as_str(), big["tax_debt_kop"].as_str(), big["has_tax_debt"].as_bool(), big["checked"].as_bool()), (Some("ООО Большой клиент"), Some("liquidating"), Some("high"), Some("4500000"), Some(true), Some(true)));
    assert_eq!((big["turnover_kop"].as_str(), big["income_share"].as_f64(), big["checked_at"].as_str(), big["source"].as_str()), (Some("15000000"), Some(0.75), Some("2026-10-24T08:00:00Z"), Some("fns_sim")));
    assert_eq!((items[1]["risk_level"].as_str(), items[1]["status"].as_str(), items[1]["has_tax_debt"].as_bool()), (Some("low"), Some("active"), Some(false)));
    assert_eq!((items[2]["checked"].as_bool(), items[2]["risk_level"].as_str(), items[2]["status"].as_str()), (Some(false), Some("unknown"), Some("unknown")));
    // флаги ушли в ledger: от них зависит прогноз
    let puts = e.t.f.lock().unwrap().risk_puts.clone();
    assert_eq!(puts.len(), 1);
    let flags = puts[0]["items"].as_array().unwrap();
    assert_eq!(flags.len(), 2);
    assert_eq!((flags[0]["inn"].as_str(), flags[0]["risk_level"].as_str(), flags[0]["status"].as_str(), flags[0]["tax_debt_kop"].as_i64(), flags[0]["bankruptcy_flag"].as_bool(), flags[0]["source"].as_str()), (Some("7702000000"), Some("high"), Some("liquidating"), Some(4_500_000), Some(false), Some("fns_sim")));
}

#[tokio::test]
async fn counterparties_limit_empty_list_and_bad_top() {
    let e = env_with(Some(INN)).await;
    // у компании нет контрагентов с ИНН: ФНС не вызывается, ответ пустой
    let (s, v) = e.t.call("GET", &e.uri("fns/counterparties"), &e.toks[0], None).await;
    assert_eq!((s, v["items"].as_array().map(Vec::len), v["checked_at"].as_str().is_some()), (StatusCode::OK, Some(0), true));
    assert!(e.fns_calls().is_empty());
    for bad in ["0", "51", "x"] {
        let (s, v) = e.t.call("GET", &e.uri(&format!("fns/counterparties?top={bad}")), &e.toks[0], None).await;
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_top"), "{bad}");
    }
    // top по умолчанию — 20: уходит в ledger
    e.t.f.lock().unwrap().top = Some(top_rows());
    e.t.call("GET", &e.uri("fns/counterparties"), &e.toks[0], None).await;
    assert!(e.t.calls().iter().any(|c| c.contains("/counterparties/top")));
}

#[tokio::test]
async fn external_api_exposes_ens_and_counterparties_read_only_by_token() {
    let e = env_with(Some(INN)).await;
    e.t.f.lock().unwrap().top = Some(top_rows());
    e.fns.lock().unwrap().batch = batch_reply();
    // токен наблюдателя (scope read) может читать
    let viewer = e.t.uid_by_role(&e.cid, "viewer");
    let tok = e.t.token_of(&viewer);
    let (s, v) = e.t.send("GET", "/api/ext/v1/fns/ens", None, Some(&tok), None).await;
    assert_eq!((s, v["debt_kop"].as_str(), v["source"].as_str()), (StatusCode::OK, Some("2500000"), Some("fns_sim")), "{v}");
    let (s, v) = e.t.send("GET", "/api/ext/v1/fns/counterparties?top=3", None, Some(&tok), None).await;
    assert_eq!((s, v["items"][0]["risk_level"].as_str()), (StatusCode::OK, Some("high")), "{v}");
    // без токена и с неверным токеном — 401
    assert_eq!(e.t.send("GET", "/api/ext/v1/fns/ens", None, None, None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(e.t.send("GET", "/api/ext/v1/fns/counterparties", None, Some("ckt_nope"), None).await.0, StatusCode::UNAUTHORIZED);
    // записи через внешний API нет: POST не принимается
    assert_eq!(e.t.send("POST", "/api/ext/v1/fns/ens", None, Some(&tok), Some(json!({}))).await.0, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn external_api_without_inn_says_inn_required() {
    let e = env_with(None).await;
    let dir = e.t.uid_by_role(&e.cid, "director");
    let tok = e.t.token_of(&dir);
    let (s, v) = e.t.send("GET", "/api/ext/v1/fns/ens", None, Some(&tok), None).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "inn_required"));
}
