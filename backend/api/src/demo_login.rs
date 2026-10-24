//! Быстрый вход в демо-аккаунты (`/api/v1/auth/demo`): кнопки «Директор / Редактор / Наблюдатель» на странице входа,
//! чтобы жюри могло зайти без ввода пароля. Включается только `DEMO_LOGIN=1`.
//!
//! Безопасность:
//! - вход возможен ТОЛЬКО в три почты из белого списка (по умолчанию `demo@example.com`, `demo-editor@example.com`,
//!   `demo-viewer@example.com`); клиент присылает лишь короткий идентификатор роли (`director|editor|viewer`),
//!   почту и пароль подставляет сервер;
//! - пароль берётся из окружения (`DEMO_PASSWORD`), в ответ и в журнал не попадает; дальше работает обычный `POST /auth/login`
//!   (те же проверки блокировки, второго фактора и cookie `session`), CSRF-проверка Origin общая (`security_layer`);
//! - число запросов ограничено: на IP (из `X-Forwarded-For`, его перезаписывает шлюз Caddy) и на весь процесс;
//! - у демо-сотрудников, заведённых директором с заданным паролем, перед входом сбрасывается обязательная смена пароля
//!   (внутренний вызов auth `demo/trust`, который сам повторно проверяет белый список и `DEMO_SEED=1`).

use crate::{AppState, http};
use axum::{
    Json, Router,
    extract::{Extension, State},
    http::{HeaderMap, StatusCode},
    response::Response,
    routing::{get, post},
};
use common::{Ctx, R, err, mw::env};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Запросов входа в минуту с одного IP.
const PER_IP_PER_MINUTE: u32 = 30;
/// Запросов входа в минуту на весь процесс (на случай подмены адреса в заголовках).
const GLOBAL_PER_MINUTE: u32 = 300;
const WINDOW: Duration = Duration::from_secs(60);
const MAX_TRACKED_IPS: usize = 10_000;

/// Демо-аккаунт: короткий идентификатор для клиента, роль (для подсказки) и почта из белого списка.
#[derive(Clone, Debug)]
pub struct Account {
    pub id: &'static str,
    pub role: &'static str,
    pub email: String,
}

pub struct DemoLogin {
    pub enabled: bool,
    password: String,
    pub accounts: Vec<Account>,
    limiter: Mutex<HashMap<String, (Instant, u32)>>,
    per_ip: u32,
    global: u32,
}

impl DemoLogin {
    pub fn new(enabled: bool, password: &str, emails: [&str; 3]) -> DemoLogin {
        let account = |id, role, email: &str| Account { id, role, email: email.trim().to_lowercase() };
        DemoLogin {
            enabled,
            password: password.to_string(),
            accounts: vec![
                account("director", "director", emails[0]),
                account("editor", "editor", emails[1]),
                account("viewer", "viewer", emails[2]),
            ],
            limiter: Mutex::new(HashMap::new()),
            per_ip: PER_IP_PER_MINUTE,
            global: GLOBAL_PER_MINUTE,
        }
    }

    /// `DEMO_LOGIN=1` (в docker-compose по умолчанию равно `DEMO_SEED`), `DEMO_PASSWORD` (по умолчанию `demo12345`),
    /// `DEMO_DIRECTOR_EMAIL` / `DEMO_EDITOR_EMAIL` / `DEMO_VIEWER_EMAIL`.
    pub fn from_env() -> DemoLogin {
        let dl = DemoLogin::new(
            env("DEMO_LOGIN", "0") == "1",
            &env("DEMO_PASSWORD", "demo12345"),
            [&env("DEMO_DIRECTOR_EMAIL", "demo@example.com"), &env("DEMO_EDITOR_EMAIL", "demo-editor@example.com"), &env("DEMO_VIEWER_EMAIL", "demo-viewer@example.com")],
        );
        if dl.enabled && !dl.config_ok() {
            eprintln!("demo login: некорректные DEMO_*_EMAIL или пустой DEMO_PASSWORD, быстрый вход выключен");
            return DemoLogin { enabled: false, ..dl };
        }
        if dl.enabled {
            eprintln!("demo login: включён для {} аккаунтов (DEMO_LOGIN=1)", dl.accounts.len());
        }
        dl
    }

    /// Три разные нормальные почты (не служебный адрес ЭЦП) и непустой пароль.
    fn config_ok(&self) -> bool {
        let mut seen: Vec<&str> = Vec::new();
        for a in &self.accounts {
            let ok = a.email.contains('@') && !a.email.ends_with("@ecp.invalid") && !a.email.contains(char::is_whitespace);
            if !ok || seen.contains(&a.email.as_str()) {
                return false;
            }
            seen.push(&a.email);
        }
        self.password.len() >= 8
    }

    fn account(&self, id: &str) -> Option<&Account> {
        self.accounts.iter().find(|a| a.id == id)
    }

    /// Фиксированное окно в минуту: на каждый IP и общее. `true` — запрос разрешён.
    fn allow(&self, ip: &str, now: Instant) -> bool {
        let mut m = self.limiter.lock().unwrap_or_else(|e| e.into_inner());
        if m.len() > MAX_TRACKED_IPS {
            m.retain(|_, (t, _)| now.duration_since(*t) < WINDOW);
        }
        let mut hit = |key: &str, limit: u32| {
            let e = m.entry(key.to_string()).or_insert((now, 0));
            if now.duration_since(e.0) >= WINDOW {
                *e = (now, 0);
            }
            e.1 += 1;
            e.1 <= limit
        };
        // сначала общий счётчик, потом адрес: запрос сверх лимита всё равно считается
        let global_ok = hit("*", self.global);
        let ip_ok = hit(&format!("ip:{ip}"), self.per_ip);
        global_ok && ip_ok
    }
}

pub fn routes(cfg: Arc<DemoLogin>) -> Router<AppState> {
    Router::new()
        .route("/api/v1/auth/demo", get(status))
        .route("/api/v1/auth/demo/login", post(login))
        .layer(Extension(cfg))
}

/// IP клиента: последний элемент `X-Forwarded-For` (шлюз Caddy перезаписывает заголовок реальным адресом), иначе общий ключ.
fn client_ip(h: &HeaderMap) -> String {
    h.get("x-forwarded-for")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.rsplit(',').next())
        .map(|s| s.trim().chars().take(64).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "-".to_string())
}

/// GET /api/v1/auth/demo -> {enabled, accounts:[{id, role, email}]}; без паролей. Выключено: {enabled:false, accounts:[]}.
async fn status(Extension(dl): Extension<Arc<DemoLogin>>) -> Json<Value> {
    Json(status_json(&dl))
}

fn status_json(dl: &DemoLogin) -> Value {
    if !dl.enabled {
        return json!({"enabled": false, "accounts": []});
    }
    let accounts: Vec<Value> = dl.accounts.iter().map(|a| json!({"id": a.id, "role": a.role, "email": a.email})).collect();
    json!({"enabled": true, "accounts": accounts})
}

#[derive(Deserialize)]
struct LoginIn {
    account: String,
}

/// POST /api/v1/auth/demo/login {account: director|editor|viewer} -> как `POST /auth/login` (cookie `session`).
async fn login(State(st): State<AppState>, Extension(dl): Extension<Arc<DemoLogin>>, ctx: Ctx, headers: HeaderMap, Json(b): Json<LoginIn>) -> R<Response> {
    if !dl.enabled {
        return Err(err(StatusCode::NOT_FOUND, "demo_disabled", "Демо-вход выключен"));
    }
    if !dl.allow(&client_ip(&headers), Instant::now()) {
        return Err(err(StatusCode::TOO_MANY_REQUESTS, "demo_rate_limited", "Слишком много попыток демо-входа, подождите минуту"));
    }
    let Some(acc) = dl.account(&b.account) else {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_account", "Неизвестный демо-аккаунт"));
    };
    // у демо-сотрудника, созданного директором, нет смысла требовать смену пароля: сбрасываем флаг (ошибки не важны)
    let _ = st.auth.post("/internal/v1/demo/trust", &ctx, &json!({"email": acc.email})).await;
    let creds: http::Creds = serde_json::from_value(json!({"email": acc.email, "password": dl.password}))
        .map_err(|_| err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "Внутренняя ошибка"))?;
    match http::login(State(st), ctx, Json(creds)).await {
        // неверный пароль у белого аккаунта: сервер не раскрывает подробностей и не подсказывает пароль
        Err(e) if e.0 == StatusCode::UNAUTHORIZED => Err(err(StatusCode::SERVICE_UNAVAILABLE, "demo_account_unavailable", "Демо-аккаунт недоступен: данные для него ещё не загружены")),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, to_bytes},
        http::Request,
        middleware,
        routing::post as fpost,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tower::ServiceExt;

    const PW: &str = "s3cret-demo-pw";
    const EMAILS: [&str; 3] = ["demo@example.com", "demo-editor@example.com", "demo-viewer@example.com"];

    /// Заглушка auth + ledger: `login` принимает только белые почты с PW, пишет полученные тела.
    #[derive(Default)]
    struct Fake {
        logins: Vec<Value>,
        trusts: Vec<Value>,
        users: Vec<(String, String, String)>, // email, password, role
        mfa: bool,
    }
    type Shared = Arc<Mutex<Fake>>;

    async fn a_login(State(f): State<Shared>, Json(b): Json<Value>) -> (StatusCode, Json<Value>) {
        let mut g = f.lock().unwrap();
        g.logins.push(b.clone());
        let u = g.users.iter().find(|u| Some(u.0.as_str()) == b["email"].as_str() && Some(u.1.as_str()) == b["password"].as_str()).cloned();
        match u {
            None => (StatusCode::UNAUTHORIZED, Json(json!({"detail": {"code": "invalid_credentials"}}))),
            Some(_) if g.mfa => (StatusCode::OK, Json(json!({"mfa": "verify", "mfa_token": "t1", "user_id": "u1", "email": b["email"]}))),
            Some(u) => (StatusCode::OK, Json(json!({"user_id": format!("id-{}", u.0), "email": u.0, "token": "tok-1", "ttl": 3600, "must_change_password": false, "password_generated": false, "email_verified": true}))),
        }
    }
    async fn a_trust(State(f): State<Shared>, Json(b): Json<Value>) -> Json<Value> {
        f.lock().unwrap().trusts.push(b);
        Json(json!({"ok": true}))
    }
    async fn l_ensure(State(f): State<Shared>, Json(b): Json<Value>) -> Json<Value> {
        let g = f.lock().unwrap();
        let role = g.users.iter().find(|u| Some(u.0.as_str()) == b["email"].as_str()).map(|u| u.2.clone()).unwrap_or_else(|| "viewer".into());
        Json(json!({"company_id": "c1", "company_name": "Демо", "inn": "", "ogrn": "", "role": role, "banned": false}))
    }
    async fn l_by_user() -> Json<Value> {
        Json(json!({"company_id": "c1", "company_name": "Демо", "role": "viewer", "banned": false}))
    }

    struct T {
        app: Router,
        f: Shared,
        hits: Arc<AtomicUsize>,
    }

    async fn setup(enabled: bool, per_ip: u32) -> T {
        let f: Shared = Arc::new(Mutex::new(Fake {
            users: vec![
                (EMAILS[0].into(), PW.into(), "director".into()),
                (EMAILS[1].into(), PW.into(), "editor".into()),
                (EMAILS[2].into(), PW.into(), "viewer".into()),
                // обычный пользователь с тем же паролем: демо-вход его не касается
                ("real@company.ru".into(), PW.into(), "director".into()),
            ],
            ..Default::default()
        }));
        let hits = Arc::new(AtomicUsize::new(0));
        let h2 = hits.clone();
        let fake = Router::new()
            .route("/internal/v1/login", fpost(a_login))
            .route("/internal/v1/demo/trust", fpost(a_trust))
            .route("/internal/v1/companies/ensure", fpost(l_ensure))
            .route("/internal/v1/members/by-user/{uid}", get(l_by_user))
            .layer(middleware::from_fn(move |req: axum::extract::Request, next: middleware::Next| {
                let h = h2.clone();
                async move {
                    h.fetch_add(1, Ordering::SeqCst);
                    next.run(req).await
                }
            }))
            .with_state(f.clone());
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://127.0.0.1:{}", l.local_addr().unwrap().port());
        tokio::spawn(async move { axum::serve(l, fake).await.unwrap() });
        let up = |n: &'static str| common::Upstream::new(n, "тест", &base, 10);
        let st = AppState {
            auth: up("auth"),
            ecp: up("auth"),
            gov: up("gov"),
            ledger: up("ledger"),
            forecast: up("forecast"),
            planfact: up("planfact"),
            integrator: up("integrator"),
            advisor: up("advisor"),
            notifier: up("notifier"),
            backoffice: up("backoffice"),
            notify: up("notify"),
            demo: Arc::new(crate::demo::Demo { enabled: false, email: "demo@example.com".into(), external_company: "C001".into(), feed: up("demo_feed") }),
            cfg: Arc::new(crate::Config { cookie_secure: false }),
        };
        let mut dl = DemoLogin::new(enabled, PW, EMAILS);
        dl.per_ip = per_ip;
        let app = routes(Arc::new(dl)).layer(middleware::from_fn(crate::security_layer)).with_state(st);
        T { app, f, hits }
    }

    async fn send(t: &T, method: &str, uri: &str, ip: Option<&str>, body: Option<Value>) -> (StatusCode, HeaderMap, String) {
        let mut rb = Request::builder().method(method).uri(uri).header("content-type", "application/json");
        if let Some(ip) = ip {
            rb = rb.header("x-forwarded-for", ip);
        }
        let body = body.map(|v| Body::from(serde_json::to_vec(&v).unwrap())).unwrap_or_else(Body::empty);
        let res = t.app.clone().oneshot(rb.body(body).unwrap()).await.unwrap();
        let (status, headers) = (res.status(), res.headers().clone());
        let text = String::from_utf8(to_bytes(res.into_body(), 1 << 20).await.unwrap().to_vec()).unwrap();
        (status, headers, text)
    }

    #[tokio::test]
    async fn status_lists_accounts_without_passwords() {
        let t = setup(true, 30).await;
        let (s, _, body) = send(&t, "GET", "/api/v1/auth/demo", None, None).await;
        assert_eq!(s, StatusCode::OK);
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["enabled"], true);
        let ids: Vec<&str> = v["accounts"].as_array().unwrap().iter().map(|a| a["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["director", "editor", "viewer"]);
        assert_eq!(v["accounts"][1]["email"], "demo-editor@example.com");
        assert_eq!(v["accounts"][1]["role"], "editor");
        assert!(!body.contains(PW) && !body.contains("password"), "{body}");
    }

    #[tokio::test]
    async fn disabled_reports_nothing_and_refuses_login() {
        let t = setup(false, 30).await;
        let (s, _, body) = send(&t, "GET", "/api/v1/auth/demo", None, None).await;
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!((s, v["enabled"].as_bool(), v["accounts"].as_array().map(Vec::len)), (StatusCode::OK, Some(false), Some(0)));
        let (s, h, body) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"account": "director"}))).await;
        assert_eq!(s, StatusCode::NOT_FOUND);
        assert!(body.contains("demo_disabled") && !h.contains_key("set-cookie"));
        assert_eq!(t.hits.load(Ordering::SeqCst), 0, "ни одного вызова во внутренние сервисы");
    }

    #[tokio::test]
    async fn login_substitutes_server_password_and_leaks_nothing() {
        let t = setup(true, 30).await;
        for (id, email, role) in [("director", EMAILS[0], "director"), ("editor", EMAILS[1], "editor"), ("viewer", EMAILS[2], "viewer")] {
            let (s, h, body) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"account": id}))).await;
            assert_eq!(s, StatusCode::OK, "{body}");
            let v: Value = serde_json::from_str(&body).unwrap();
            assert_eq!((v["email"].as_str(), v["role"].as_str()), (Some(email), Some(role)));
            let cookie = h.get("set-cookie").unwrap().to_str().unwrap();
            assert!(cookie.starts_with("session=tok-1;") && cookie.contains("HttpOnly"), "{cookie}");
            assert!(!body.contains(PW) && !cookie.contains(PW) && !format!("{h:?}").contains(PW));
        }
        let g = t.f.lock().unwrap();
        // в auth ушёл серверный пароль и белая почта, смена пароля сброшена до входа
        assert!(g.logins.iter().all(|b| b["password"] == PW));
        assert_eq!(g.logins.iter().map(|b| b["email"].as_str().unwrap()).collect::<Vec<_>>(), EMAILS);
        assert_eq!(g.trusts.iter().map(|b| b["email"].as_str().unwrap()).collect::<Vec<_>>(), EMAILS);
    }

    #[tokio::test]
    async fn only_whitelisted_accounts_can_be_entered() {
        let t = setup(true, 30).await;
        // клиент не может подсунуть ни почту, ни пароль, ни чужой идентификатор
        for body in [
            json!({"account": "admin"}),
            json!({"account": "real@company.ru"}),
            json!({"account": ""}),
            json!({"account": "Director"}),
        ] {
            let (s, h, text) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(body)).await;
            assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "{text}");
            assert!(text.contains("invalid_account") && !h.contains_key("set-cookie"));
        }
        let (s, _, text) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"email": "real@company.ru", "password": PW}))).await;
        assert!(s.is_client_error(), "{s} {text}");
        let (s, _, _) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"account": "director", "email": "real@company.ru"}))).await;
        assert_eq!(s, StatusCode::OK);
        let g = t.f.lock().unwrap();
        assert!(g.logins.iter().all(|b| EMAILS.contains(&b["email"].as_str().unwrap())), "в auth ушла не белая почта: {:?}", g.logins);
        assert!(g.trusts.iter().all(|b| EMAILS.contains(&b["email"].as_str().unwrap())));
    }

    #[tokio::test]
    async fn missing_demo_account_gives_generic_error_without_password_hint() {
        let t = setup(true, 30).await;
        t.f.lock().unwrap().users.clear();
        let (s, h, body) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"account": "viewer"}))).await;
        assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
        assert!(body.contains("demo_account_unavailable") && !body.contains(PW) && !h.contains_key("set-cookie"));
    }

    #[tokio::test]
    async fn second_factor_is_not_bypassed() {
        let t = setup(true, 30).await;
        t.f.lock().unwrap().mfa = true;
        let (s, h, body) = send(&t, "POST", "/api/v1/auth/demo/login", None, Some(json!({"account": "editor"}))).await;
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!((s, v["mfa"].as_str()), (StatusCode::OK, Some("verify")));
        assert!(!h.contains_key("set-cookie"), "сессия до кода не выдаётся");
    }

    #[tokio::test]
    async fn rate_limit_per_ip_then_recovers() {
        let t = setup(true, 3).await;
        for _ in 0..3 {
            let (s, _, _) = send(&t, "POST", "/api/v1/auth/demo/login", Some("203.0.113.5"), Some(json!({"account": "viewer"}))).await;
            assert_eq!(s, StatusCode::OK);
        }
        let (s, _, body) = send(&t, "POST", "/api/v1/auth/demo/login", Some("203.0.113.5"), Some(json!({"account": "viewer"}))).await;
        assert_eq!(s, StatusCode::TOO_MANY_REQUESTS, "{body}");
        assert!(body.contains("demo_rate_limited"));
        // другой адрес не затронут; GET статуса не ограничивается
        let (s, _, _) = send(&t, "POST", "/api/v1/auth/demo/login", Some("203.0.113.6"), Some(json!({"account": "viewer"}))).await;
        assert_eq!(s, StatusCode::OK);
        let (s, _, _) = send(&t, "GET", "/api/v1/auth/demo", Some("203.0.113.5"), None).await;
        assert_eq!(s, StatusCode::OK);
    }

    #[test]
    fn limiter_window_resets_and_global_cap_applies() {
        let dl = DemoLogin { per_ip: 2, global: 5, ..DemoLogin::new(true, PW, EMAILS) };
        let t0 = Instant::now();
        assert!(dl.allow("a", t0) && dl.allow("a", t0));
        assert!(!dl.allow("a", t0));
        assert!(dl.allow("a", t0 + Duration::from_secs(61)), "окно прошло");
        // общий предел: разные адреса вместе не больше 5 в окне
        let dl = DemoLogin { per_ip: 100, global: 5, ..DemoLogin::new(true, PW, EMAILS) };
        let ok = (0..10).filter(|i| dl.allow(&format!("ip{i}"), t0)).count();
        assert_eq!(ok, 5);
    }

    #[test]
    fn config_rejects_duplicate_or_service_emails_and_weak_password() {
        assert!(DemoLogin::new(true, PW, EMAILS).config_ok());
        assert!(!DemoLogin::new(true, PW, ["a@x.ru", "a@x.ru", "b@x.ru"]).config_ok());
        assert!(!DemoLogin::new(true, PW, ["a@x.ru", "b@ecp.invalid", "c@x.ru"]).config_ok());
        assert!(!DemoLogin::new(true, "short", EMAILS).config_ok());
    }

    #[test]
    fn client_ip_takes_last_forwarded_hop() {
        let mut h = HeaderMap::new();
        assert_eq!(client_ip(&h), "-");
        h.insert("x-forwarded-for", "1.1.1.1, 2.2.2.2".parse().unwrap());
        assert_eq!(client_ip(&h), "2.2.2.2");
    }
}
