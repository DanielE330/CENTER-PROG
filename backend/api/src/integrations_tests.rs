//! Подключение интеграций пользователем (integrations.rs): настоящий роутер api поверх заглушек ledger и integrator
//! (общая заглушка roles_tests). Права по ролям для четырёх маршрутов проверяет таблица `every_route_enforces_…`;
//! здесь содержание: что видит каждая роль, порядок «адрес → сохранение → проверка → статус», пароль не в ответах,
//! синхронизация подключённой 1С.

use crate::roles_tests::{T, code};
use axum::http::StatusCode;
use serde_json::{Value, json};

const SECRET: &str = "Sup3r-Secret-Pass!";

fn conn(secret: bool) -> Value {
    let mut b = json!({"url": "https://msk1.1cfresh.com/a/ea/4269703/", "username": "odata"});
    if secret {
        b["password"] = json!(SECRET);
    }
    b
}

fn base(cid: &str) -> String {
    format!("/api/v1/companies/{cid}/integrations")
}

/// Подключённая 1С в заглушке ledger.
fn connected(t: &T) {
    t.f.lock().unwrap().integ = Some(json!({"kind": "onec", "url": "https://msk1.1cfresh.com/a/ea/4269703", "username": "odata", "password_set": true,
        "status": "ok", "last_check_at": "2026-10-25T09:00:00Z", "last_error": null, "last_sync_at": "2026-10-25T09:05:00Z"}));
}

#[tokio::test]
async fn every_role_reads_statuses_but_only_director_sees_requisites() {
    let t = T::new().await;
    let (cid, toks) = t.team4();
    connected(&t);
    for (i, tok) in toks.iter().enumerate() {
        let (s, v) = t.call("GET", &base(&cid), tok, None).await;
        assert_eq!(s, StatusCode::OK, "роль {i}");
        let onec = &v["items"][0];
        assert_eq!((onec["kind"].as_str(), onec["connected"].as_bool(), onec["source"].as_str(), onec["status"].as_str()), (Some("onec"), Some(true), Some("live"), Some("ok")));
        assert_eq!(onec["last_sync_at"], "2026-10-25T09:05:00Z");
        assert_eq!((v["items"][1]["kind"].as_str(), v["items"][1]["source"].as_str(), v["items"][2]["kind"].as_str(), v["items"][2]["source"].as_str()), (Some("edo"), Some("sim"), Some("bank"), Some("sim")));
        let text = v.to_string();
        if i == 0 {
            assert_eq!((onec["url"].as_str(), onec["username"].as_str(), onec["password_set"].as_bool(), v["can_manage"].as_bool()), (Some("https://msk1.1cfresh.com/a/ea/4269703"), Some("odata"), Some(true), Some(true)));
            assert_eq!(v["key_configured"], true);
        } else {
            assert!(!text.contains("1cfresh") && !text.contains("odata") && onec.get("url").is_none() && onec.get("password_set").is_none(), "роль {i}: {text}");
            assert_eq!((v["can_manage"].as_bool(), v["key_configured"].is_null()), (Some(false), true));
        }
        assert!(!text.contains("password\":") && !text.contains("password_enc"));
    }
}

#[tokio::test]
async fn not_connected_company_shows_simulator() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let (s, v) = t.call("GET", &base(&cid), &dir, None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((v["items"][0]["connected"].as_bool(), v["items"][0]["status"].as_str(), v["items"][0]["source"].as_str()), (Some(false), Some("not_connected"), Some("sim")));
}

#[tokio::test]
async fn put_saves_canonical_address_checks_and_records_status_without_password_in_responses() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let (s, v) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(conn(true))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    {
        let g = t.f.lock().unwrap();
        // в ledger ушёл канонический адрес (без «/» в конце), логин и пароль
        assert_eq!(g.integ_puts.len(), 1);
        assert_eq!((g.integ_puts[0]["url"].as_str(), g.integ_puts[0]["username"].as_str(), g.integ_puts[0]["password"].as_str()), (Some("https://msk1.1cfresh.com/a/ea/4269703"), Some("odata"), Some(SECRET)));
        // проверка шла по сохранённым реквизитам, пароль в integrator не ходил
        assert_eq!(g.onec_tests, vec![json!({"tenant": cid})]);
        assert_eq!((g.integ_status[0]["event"].as_str(), g.integ_status[0]["ok"].as_bool()), (Some("check"), Some(true)));
    }
    assert!(!v.to_string().contains(SECRET), "пароль в ответе: {v}");
    assert_eq!((v["items"][0]["status"].as_str(), v["check"]["ok"].as_bool(), v["check"]["documents"].as_i64()), (Some("ok"), Some(true), Some(3)));
    assert_eq!(v["items"][0]["url"], "https://msk1.1cfresh.com/a/ea/4269703");
}

#[tokio::test]
async fn put_without_password_keeps_the_saved_one_and_never_sends_an_empty_password() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    connected(&t);
    let mut b = conn(false);
    b["password"] = json!("");
    let (s, _) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(b)).await;
    assert_eq!(s, StatusCode::OK);
    assert!(t.f.lock().unwrap().integ_puts[0].get("password").is_none());
}

#[tokio::test]
async fn put_rejects_forbidden_address_before_saving() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    for url in ["https://10.0.0.1/a", "https://localhost/odata"] {
        let (s, v) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(json!({"url": url, "username": "u", "password": SECRET}))).await;
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "url_forbidden"), "{url}");
        assert!(!v.to_string().contains(SECRET));
    }
    let g = t.f.lock().unwrap();
    assert!(g.integ_puts.is_empty() && g.integ.is_none() && g.onec_tests.is_empty());
}

#[tokio::test]
async fn put_validates_form_fields() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let uri = format!("{}/onec", base(&cid));
    for (b, c) in [
        (json!({"username": "u", "password": "p"}), "invalid_url"),
        (json!({"url": "https://x.ru", "username": "  ", "password": "p"}), "invalid_username"),
        (json!({"url": "https://x.ru", "username": "u", "password": "a\nb"}), "invalid_password"),
        (json!({"url": 5, "username": "u"}), "invalid_url"),
    ] {
        let (s, v) = t.call("PUT", &uri, &dir, Some(b)).await;
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, c));
    }
    assert!(t.f.lock().unwrap().integ_puts.is_empty());
}

#[tokio::test]
async fn put_without_server_key_is_a_clear_503() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    t.f.lock().unwrap().integ_key_missing = true;
    let (s, v) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(conn(true))).await;
    assert_eq!((s, code(&v)), (StatusCode::SERVICE_UNAVAILABLE, "integrations_key_missing"));
    assert!(t.f.lock().unwrap().integ.is_none());
    let (_, g) = t.call("GET", &base(&cid), &dir, None).await;
    assert_eq!(g["key_configured"], false);
}

#[tokio::test]
async fn failed_check_is_recorded_as_error_with_a_human_reason() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    t.f.lock().unwrap().onec_result = Some(json!({"ok": false, "code": "bad_credentials", "message": "1С не приняла логин или пароль", "source": "live"}));
    let (s, v) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(conn(true))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!((v["items"][0]["status"].as_str(), v["items"][0]["last_error"].as_str(), v["check"]["code"].as_str()), (Some("error"), Some("1С не приняла логин или пароль"), Some("bad_credentials")));
}

#[tokio::test]
async fn test_records_only_saved_requisites_and_audits_trial_runs() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    connected(&t);
    let uri = format!("{}/onec/test", base(&cid));
    // без тела и с теми же адресом и логином: проверка сохранённого, результат попадает в статус
    for b in [None, Some(json!({})), Some(json!({"url": "https://msk1.1cfresh.com/a/ea/4269703/", "username": "odata"}))] {
        let (s, v) = t.call("POST", &uri, &dir, b).await;
        assert_eq!((s, v["check"]["ok"].as_bool(), v["recorded"].as_bool()), (StatusCode::OK, Some(true), Some(true)), "{v}");
    }
    assert_eq!(t.f.lock().unwrap().integ_status.len(), 3);
    // пробные реквизиты (другой адрес или введённый пароль): статус не меняется, действие в журнале
    let before = t.calls().iter().filter(|c| c.ends_with("/audit")).count();
    for b in [json!({"url": "https://other.example.org/a", "username": "x", "password": SECRET}), json!({"password": SECRET})] {
        let (s, v) = t.call("POST", &uri, &dir, Some(b)).await;
        assert_eq!((s, v["recorded"].as_bool()), (StatusCode::OK, Some(false)));
        assert!(!v.to_string().contains(SECRET));
    }
    assert_eq!(t.f.lock().unwrap().integ_status.len(), 3);
    assert_eq!(t.calls().iter().filter(|c| c.ends_with("/audit")).count(), before + 2);
    // введённый пароль ушёл только в integrator
    let g = t.f.lock().unwrap();
    assert_eq!(g.onec_tests.last().unwrap()["password"], SECRET);
    assert_eq!(g.onec_tests[0], json!({"tenant": cid}));
}

#[tokio::test]
async fn check_limit_of_integrator_is_passed_to_the_user() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    connected(&t);
    t.f.lock().unwrap().onec_fail = Some((StatusCode::TOO_MANY_REQUESTS, "rate_limited".into()));
    let (s, v) = t.call("POST", &format!("{}/onec/test", base(&cid)), &dir, None).await;
    assert_eq!((s, code(&v)), (StatusCode::TOO_MANY_REQUESTS, "rate_limited"));
    // при сохранении исчерпанный лимит не ломает PUT: подключение записано, проверка отложена
    let (s, v) = t.call("PUT", &format!("{}/onec", base(&cid)), &dir, Some(conn(true))).await;
    assert_eq!(s, StatusCode::OK);
    assert!(v["check"].is_null() && v["items"][0]["status"] == "unchecked");
}

#[tokio::test]
async fn delete_disconnects_and_is_repeatable() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    connected(&t);
    let uri = format!("{}/onec", base(&cid));
    assert_eq!(t.call("DELETE", &uri, &dir, None).await.0, StatusCode::NO_CONTENT);
    assert_eq!(t.call("DELETE", &uri, &dir, None).await.0, StatusCode::NO_CONTENT);
    let (_, v) = t.call("GET", &base(&cid), &dir, None).await;
    assert_eq!(v["items"][0]["status"], "not_connected");
}

#[tokio::test]
async fn only_director_manages_and_foreign_company_is_not_found() {
    let t = T::new().await;
    let (cid, [dir, admin, editor, viewer]) = t.team4();
    for tok in [&admin, &editor, &viewer] {
        for (m, u, b) in [("PUT", "onec", Some(conn(true))), ("POST", "onec/test", None), ("DELETE", "onec", None)] {
            let (s, v) = t.call(m, &format!("{}/{u}", base(&cid)), tok, b).await;
            assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"), "{m} {u}");
        }
    }
    {
        let g = t.f.lock().unwrap();
        assert!(g.integ_puts.is_empty() && g.onec_tests.is_empty() && g.integ.is_none(), "отказ должен наступить до обращения к данным");
    }
    let other_dir = t.user("o@x.ru", "other-pass-1", true);
    let other = t.company("Чужая", &other_dir);
    for (m, u) in [("GET", ""), ("PUT", "/onec"), ("POST", "/onec/test"), ("DELETE", "/onec")] {
        let (s, _) = t.call(m, &format!("{}{u}", base(&other)), &dir, Some(conn(true))).await;
        assert_eq!(s, StatusCode::NOT_FOUND, "{m} {u}");
    }
}

#[tokio::test]
async fn sync_passes_company_to_integrator_and_records_live_result() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    connected(&t);
    t.f.lock().unwrap().obligations = Some(json!({"onec": "live", "sources": {"onec": {"status": "ok", "error": null}, "edo": {"status": "off"}}, "items": []}));
    let (s, v) = t.call("POST", &format!("{}/sync", base(&cid)), &dir, Some(json!({"external_company": "C001"}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["onec"], "live");
    assert!(t.f.lock().unwrap().obl_queries[0].contains(&format!("tenant={cid}")));
    {
        let g = t.f.lock().unwrap();
        assert_eq!((g.integ_status.last().unwrap()["event"].as_str(), g.integ_status.last().unwrap()["ok"].as_bool()), (Some("sync"), Some(true)));
        assert_eq!(g.integ.as_ref().unwrap()["last_sync_at"], "2026-10-25T10:05:00Z");
    }
    // сбой 1С при синхронизации: статус «ошибка» с причиной, ответ 200 (данные ЭДО или пустой список)
    t.f.lock().unwrap().obligations = Some(json!({"onec": "live", "sources": {"onec": {"status": "error", "error": "1С не приняла логин или пароль", "code": "bad_credentials"}, "edo": {"status": "off"}}, "items": []}));
    let (s, v) = t.call("POST", &format!("{}/sync", base(&cid)), &dir, Some(json!({}))).await;
    assert_eq!((s, v["sources"]["onec"]["status"].as_str()), (StatusCode::OK, Some("error")));
    let g = t.f.lock().unwrap();
    assert_eq!((g.integ.as_ref().unwrap()["status"].as_str(), g.integ.as_ref().unwrap()["last_error"].as_str()), (Some("error"), Some("1С не приняла логин или пароль")));
}

#[tokio::test]
async fn sync_of_simulator_does_not_touch_connection_status() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    t.f.lock().unwrap().obligations = Some(json!({"onec": "sim", "sources": {"onec": {"status": "ok"}}, "items": []}));
    let (s, v) = t.call("POST", &format!("{}/sync", base(&cid)), &dir, Some(json!({}))).await;
    assert_eq!((s, v["onec"].as_str()), (StatusCode::OK, Some("sim")));
    assert!(t.f.lock().unwrap().integ_status.is_empty());
}

#[tokio::test]
async fn banned_company_cannot_use_integrations() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    t.ban(&cid, Some("спам"));
    for (m, u) in [("GET", ""), ("PUT", "/onec"), ("DELETE", "/onec")] {
        let (s, v) = t.call(m, &format!("{}{u}", base(&cid)), &dir, Some(conn(true))).await;
        assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "company_banned"));
    }
}

// ---------- Telegram: свой бот компании ----------

const TG: &str = "123456789:AAExampleExampleExampleExample_abc-xyz";

fn tg_connected(t: &T) {
    t.f.lock().unwrap().tg = Some(json!({"kind": "telegram", "bot_username": "My_Company_bot", "token_mask": "123…:…xyz", "token_set": true, "status": "ok",
        "last_check_at": "2026-10-25T09:00:00Z", "last_error": "старая ошибка"}));
}

fn tg_item(v: &Value) -> &Value {
    v["items"].as_array().unwrap().iter().find(|i| i["kind"] == "telegram").unwrap()
}

#[tokio::test]
async fn telegram_status_is_visible_to_all_but_bot_name_and_mask_only_to_director() {
    let t = T::new().await;
    let (cid, toks) = t.team4();
    let (s, v) = t.call("GET", &base(&cid), &toks[0], None).await;
    assert_eq!((s, tg_item(&v)["connected"].as_bool(), tg_item(&v)["source"].as_str(), tg_item(&v)["status"].as_str()), (StatusCode::OK, Some(false), Some("shared"), Some("not_connected")));
    tg_connected(&t);
    for (i, tok) in toks.iter().enumerate() {
        let (_, v) = t.call("GET", &base(&cid), tok, None).await;
        let tg = tg_item(&v);
        assert_eq!((tg["connected"].as_bool(), tg["source"].as_str(), tg["status"].as_str(), tg["last_check_at"].as_str()), (Some(true), Some("own"), Some("ok"), Some("2026-10-25T09:00:00Z")), "роль {i}");
        let text = v.to_string();
        assert!(!text.contains("AAExample") && !text.contains("\"token\""), "роль {i}: {text}");
        if i == 0 {
            assert_eq!((tg["bot_username"].as_str(), tg["token_mask"].as_str()), (Some("My_Company_bot"), Some("123…:…xyz")));
        } else {
            assert!(!text.contains("My_Company_bot") && !text.contains("123…") && !text.contains("старая ошибка"), "роль {i}: {text}");
        }
    }
}

#[tokio::test]
async fn telegram_put_checks_getme_first_then_saves_and_never_returns_the_token() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let (s, v) = t.call("PUT", &format!("{}/telegram", base(&cid)), &dir, Some(json!({"token": format!("  {TG} ")}))).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    {
        let g = t.f.lock().unwrap();
        assert_eq!(g.tg_verifies, vec![json!({"token": TG})]);
        assert_eq!((g.tg_puts[0]["token"].as_str(), g.tg_puts[0]["bot_username"].as_str()), (Some(TG), Some("My_Company_bot")));
        assert_eq!(g.tg_syncs, 1, "notifier сразу подхватывает токен");
    }
    assert!(!v.to_string().contains("AAExample") && !v.to_string().contains(TG), "{v}");
    assert_eq!((tg_item(&v)["connected"].as_bool(), tg_item(&v)["bot_username"].as_str(), tg_item(&v)["token_mask"].as_str(), v["check"]["ok"].as_bool()), (Some(true), Some("My_Company_bot"), Some("123…:…xyz"), Some(true)));
    // порядок: проверка у notifier раньше записи в ledger
    let calls = t.calls();
    let iv = calls.iter().position(|c| c.ends_with("/telegram/verify")).unwrap();
    let ip = calls.iter().position(|c| c.starts_with("PUT") && c.ends_with("/integrations/telegram")).unwrap();
    assert!(iv < ip, "{calls:?}");
}

#[tokio::test]
async fn telegram_put_rejects_bad_format_and_failed_getme_without_saving_or_echoing() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let uri = format!("{}/telegram", base(&cid));
    for b in [json!({}), json!({"token": ""}), json!({"token": "abc"}), json!({"token": 5}), json!({"token": "123456:short"}), json!({"token": format!("{TG}\n")})] {
        let (s, v) = t.call("PUT", &uri, &dir, Some(b.clone())).await;
        // перевод строки в конце обрезается как пробел и токен принимается
        if b["token"].as_str().is_some_and(|x| x.trim() == TG) {
            assert_eq!(s, StatusCode::OK);
            t.f.lock().unwrap().tg = None;
            continue;
        }
        assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_bot_token"), "{b}");
    }
    assert!(t.f.lock().unwrap().tg.is_none());
    let before = t.f.lock().unwrap().tg_puts.len();
    t.f.lock().unwrap().tg_verify_fail = Some((StatusCode::UNPROCESSABLE_ENTITY, "telegram_token_invalid".into(), "Telegram не принял токен".into()));
    let (s, v) = t.call("PUT", &uri, &dir, Some(json!({"token": TG}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "telegram_token_invalid"));
    assert!(!v.to_string().contains("AAExample"));
    t.f.lock().unwrap().tg_verify_fail = Some((StatusCode::TOO_MANY_REQUESTS, "rate_limited".into(), "Не больше 10 проверок в минуту".into()));
    let (s, v) = t.call("PUT", &uri, &dir, Some(json!({"token": TG}))).await;
    assert_eq!((s, code(&v)), (StatusCode::TOO_MANY_REQUESTS, "rate_limited"));
    let g = t.f.lock().unwrap();
    assert_eq!(g.tg_puts.len(), before, "после отказа getMe токен не сохраняется");
    assert!(g.tg.is_none());
}

#[tokio::test]
async fn telegram_put_without_integrations_key_is_503() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    t.f.lock().unwrap().integ_key_missing = true;
    let (s, v) = t.call("PUT", &format!("{}/telegram", base(&cid)), &dir, Some(json!({"token": TG}))).await;
    assert_eq!((s, code(&v)), (StatusCode::SERVICE_UNAVAILABLE, "integrations_key_missing"));
    assert!(!v.to_string().contains("AAExample"));
}

#[tokio::test]
async fn telegram_test_rechecks_saved_token_and_records_the_result() {
    let t = T::new().await;
    let (cid, [dir, ..]) = t.team();
    let uri = format!("{}/telegram/test", base(&cid));
    let (s, v) = t.call("POST", &uri, &dir, None).await;
    assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "telegram_not_connected"));
    assert!(t.f.lock().unwrap().tg_verifies.is_empty());
    tg_connected(&t);
    let (s, v) = t.call("POST", &uri, &dir, None).await;
    assert_eq!((s, v["check"]["ok"].as_bool(), tg_item(&v)["status"].as_str()), (StatusCode::OK, Some(true), Some("ok")));
    {
        let g = t.f.lock().unwrap();
        assert_eq!(g.tg_verifies, vec![json!({})], "токен в запрос проверки не передаётся, notifier берёт его из ledger");
        assert_eq!(g.tg_status.last().unwrap()["ok"], true);
    }
    t.f.lock().unwrap().tg_verify_fail = Some((StatusCode::UNPROCESSABLE_ENTITY, "telegram_token_invalid".into(), "Telegram не принял токен: он отозван".into()));
    let (s, v) = t.call("POST", &uri, &dir, None).await;
    assert_eq!((s, v["check"]["ok"].as_bool(), v["check"]["code"].as_str(), tg_item(&v)["status"].as_str()), (StatusCode::OK, Some(false), Some("telegram_token_invalid"), Some("error")));
    assert_eq!(t.f.lock().unwrap().tg_status.last().unwrap()["error"], "Telegram не принял токен: он отозван");
    t.f.lock().unwrap().tg_verify_fail = Some((StatusCode::TOO_MANY_REQUESTS, "rate_limited".into(), "лимит".into()));
    assert_eq!(t.call("POST", &uri, &dir, None).await.0, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn telegram_delete_removes_the_token_and_non_directors_cannot_touch_it() {
    let t = T::new().await;
    let (cid, toks) = t.team4();
    tg_connected(&t);
    for tok in &toks[1..] {
        for (m, u, b) in [("PUT", "telegram", Some(json!({"token": TG}))), ("POST", "telegram/test", None), ("DELETE", "telegram", None)] {
            let (s, v) = t.call(m, &format!("{}/{u}", base(&cid)), tok, b).await;
            assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"), "{m} {u}");
        }
    }
    assert!(t.f.lock().unwrap().tg.is_some() && t.f.lock().unwrap().tg_verifies.is_empty());
    let (s, _) = t.call("DELETE", &format!("{}/telegram", base(&cid)), &toks[0], None).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    assert!(t.f.lock().unwrap().tg.is_none());
    assert_eq!(t.f.lock().unwrap().tg_syncs, 1);
    let (_, v) = t.call("GET", &base(&cid), &toks[0], None).await;
    assert_eq!((tg_item(&v)["connected"].as_bool(), tg_item(&v)["source"].as_str()), (Some(false), Some("shared")));
}

#[tokio::test]
async fn telegram_of_a_foreign_company_is_not_found() {
    let t = T::new().await;
    let (_, [dir, ..]) = t.team();
    let (s, v) = t.call("PUT", &format!("{}/telegram", base("other-company")), &dir, Some(json!({"token": TG}))).await;
    assert_eq!((s, code(&v)), (StatusCode::NOT_FOUND, "not_found"));
    assert!(t.f.lock().unwrap().tg_verifies.is_empty());
}
