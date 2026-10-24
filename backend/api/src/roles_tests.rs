//! Роли и права на уровне api: настоящий роутер (`tenant_routes` + `security_layer`) поверх заглушки внутренних сервисов.
//! Заглушка на эфемерном порту loopback изображает auth (сессии, пользователи, токены) и ledger (компании, участники);
//! все остальные внутренние вызовы (forecast, planfact, advisor, ...) записываются и отвечают пустым успехом.
//! Проверяется то, что принадлежит api: права по ролям для каждого вида маршрутов, бан компании, обязательный пароль
//! директора, сотрудники (создание, роль, удаление, сброс пароля), токены и их права.

use crate::{AppState, Config, demo, http};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Path, Query, Request as AxReq, State},
    http::{Request, StatusCode},
    middleware::{self, Next},
    response::Response,
    routing::{get, post},
};
use common::Upstream;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tower::ServiceExt;

// ---------- заглушка auth + ledger ----------
#[derive(Clone)]
struct User {
    id: String,
    email: String,
    password: String,
    has_email: bool,
    must_change: bool,
    generated: bool,
}

struct Co {
    name: String,
    banned: Option<String>,
}

#[derive(Clone)]
struct Mem {
    cid: String,
    uid: String,
    email: String,
    role: String,
}

#[derive(Default)]
struct Fake {
    users: Vec<User>,
    sessions: HashMap<String, String>,
    /// сырой токен -> (id токена, user_id, название)
    tokens: HashMap<String, (String, String, String)>,
    companies: HashMap<String, Co>,
    members: Vec<Mem>,
    calls: Vec<String>,
    seq: u32,
}

type Shared = Arc<Mutex<Fake>>;
type Resp = (StatusCode, Json<Value>);

fn detail(s: StatusCode, code: &str) -> Resp {
    (s, Json(json!({"detail": {"code": code}})))
}

fn lerr(s: StatusCode, code: &str) -> Resp {
    (s, Json(json!({"error": {"code": code, "message": code}})))
}

fn user_info(u: &User) -> Value {
    json!({"user_id": u.id, "email": u.email, "has_email": u.has_email, "must_change_password": u.must_change, "password_generated": u.generated})
}

async fn record(State(f): State<Shared>, req: AxReq, next: Next) -> Response {
    f.lock().unwrap().calls.push(format!("{} {}", req.method(), req.uri().path()));
    next.run(req).await
}

async fn a_session(State(f): State<Shared>, Json(b): Json<Value>) -> Resp {
    let g = f.lock().unwrap();
    let uid = b["token"].as_str().and_then(|t| g.sessions.get(t));
    match uid.and_then(|id| g.users.iter().find(|u| &u.id == id)) {
        Some(u) => (StatusCode::OK, Json(user_info(u))),
        None => detail(StatusCode::UNAUTHORIZED, "invalid_session"),
    }
}

async fn a_verify(State(f): State<Shared>, Json(b): Json<Value>) -> Resp {
    let g = f.lock().unwrap();
    let uid = b["token"].as_str().and_then(|t| g.tokens.get(t)).map(|t| t.1.clone());
    match uid.and_then(|id| g.users.iter().find(|u| u.id == id)) {
        Some(u) => (StatusCode::OK, Json(json!({"user_id": u.id, "email": u.email}))),
        None => detail(StatusCode::UNAUTHORIZED, "invalid_token"),
    }
}

async fn a_login(State(f): State<Shared>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let u = g.users.iter().find(|u| u.has_email && Some(u.email.as_str()) == b["email"].as_str() && Some(u.password.as_str()) == b["password"].as_str()).cloned();
    let Some(u) = u else { return detail(StatusCode::UNAUTHORIZED, "invalid_credentials") };
    g.seq += 1;
    let tok = format!("login-{}", g.seq);
    g.sessions.insert(tok.clone(), u.id.clone());
    let mut v = user_info(&u);
    v["token"] = json!(tok);
    v["ttl"] = json!(3600);
    (StatusCode::OK, Json(v))
}

async fn a_logout(State(f): State<Shared>, Json(b): Json<Value>) -> StatusCode {
    if let Some(t) = b["token"].as_str() {
        f.lock().unwrap().sessions.remove(t);
    }
    StatusCode::NO_CONTENT
}

async fn a_create(State(f): State<Shared>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let email = b["email"].as_str().unwrap_or_default().trim().to_lowercase();
    if !email.contains('@') {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, "invalid_email");
    }
    if g.users.iter().any(|u| u.email == email) {
        return detail(StatusCode::CONFLICT, "email_taken");
    }
    let generate = b["generate"].as_bool().unwrap_or(false);
    let password = if generate { "GenPass-12345".to_string() } else { b["password"].as_str().unwrap_or_default().to_string() };
    if password.len() < 8 {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, "invalid_password");
    }
    g.seq += 1;
    let id = format!("u{}", g.seq);
    g.users.push(User { id: id.clone(), email: email.clone(), password: password.clone(), has_email: true, must_change: true, generated: generate });
    let mut v = json!({"user_id": id, "email": email});
    if generate {
        v["password"] = json!(password);
    }
    (StatusCode::CREATED, Json(v))
}

async fn a_delete(State(f): State<Shared>, Path(uid): Path<String>) -> Resp {
    let mut g = f.lock().unwrap();
    let before = g.users.len();
    g.users.retain(|u| u.id != uid);
    if g.users.len() == before {
        return detail(StatusCode::NOT_FOUND, "user_not_found");
    }
    g.sessions.retain(|_, v| *v != uid);
    g.tokens.retain(|_, v| v.1 != uid);
    (StatusCode::NO_CONTENT, Json(Value::Null))
}

async fn a_password(State(f): State<Shared>, Path(uid): Path<String>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(u) = g.users.iter_mut().find(|u| u.id == uid) else { return detail(StatusCode::NOT_FOUND, "user_not_found") };
    if !u.must_change && b["current"].as_str() != Some(u.password.as_str()) {
        return detail(StatusCode::FORBIDDEN, "invalid_current_password");
    }
    u.password = b["new"].as_str().unwrap_or_default().to_string();
    u.must_change = false;
    u.generated = false;
    (StatusCode::OK, Json(json!({"ok": true, "must_change_password": false})))
}

async fn a_reset(State(f): State<Shared>, Path(uid): Path<String>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(u) = g.users.iter_mut().find(|u| u.id == uid) else { return detail(StatusCode::NOT_FOUND, "user_not_found") };
    u.password = "ResetPass-1234".into();
    u.must_change = true;
    u.generated = true;
    g.sessions.retain(|_, v| *v != uid);
    (StatusCode::OK, Json(json!({"password": "ResetPass-1234"})))
}

async fn a_email_password(State(f): State<Shared>, Path(uid): Path<String>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(u) = g.users.iter_mut().find(|u| u.id == uid) else { return detail(StatusCode::NOT_FOUND, "user_not_found") };
    if u.has_email {
        return detail(StatusCode::CONFLICT, "email_exists");
    }
    if b["password"].as_str().unwrap_or_default().len() < 8 {
        return detail(StatusCode::UNPROCESSABLE_ENTITY, "invalid_password");
    }
    u.email = b["email"].as_str().unwrap_or_default().to_lowercase();
    u.password = b["password"].as_str().unwrap_or_default().to_string();
    u.has_email = true;
    (StatusCode::OK, Json(json!({"email": u.email, "signatures": []})))
}

async fn a_methods(State(f): State<Shared>, Path(uid): Path<String>) -> Resp {
    let g = f.lock().unwrap();
    match g.users.iter().find(|u| u.id == uid) {
        Some(u) => (StatusCode::OK, Json(json!({"email": u.has_email.then_some(&u.email), "signatures": []}))),
        None => detail(StatusCode::NOT_FOUND, "user_not_found"),
    }
}

fn token_json(raw: &str, t: &(String, String, String)) -> Value {
    json!({"id": t.0, "user_id": t.1, "name": t.2, "prefix": &raw[..raw.len().min(8)], "created_at": 0.0, "last_used": null})
}

async fn a_tok_list(State(f): State<Shared>, Path(uid): Path<String>) -> Resp {
    let g = f.lock().unwrap();
    let items: Vec<Value> = g.tokens.iter().filter(|(_, t)| t.1 == uid).map(|(r, t)| token_json(r, t)).collect();
    (StatusCode::OK, Json(json!({"items": items})))
}

async fn a_tok_create(State(f): State<Shared>, Path(uid): Path<String>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    g.seq += 1;
    let raw = format!("ckt_{:04}xxxxxxxxxxxx", g.seq);
    let t = (format!("t{}", g.seq), uid, b["name"].as_str().unwrap_or_default().to_string());
    let mut v = token_json(&raw, &t);
    v["token"] = json!(raw);
    g.tokens.insert(raw, t);
    (StatusCode::CREATED, Json(v))
}

async fn a_tok_delete(State(f): State<Shared>, Path((uid, tid)): Path<(String, String)>) -> Resp {
    let mut g = f.lock().unwrap();
    let before = g.tokens.len();
    g.tokens.retain(|_, t| !(t.0 == tid && t.1 == uid));
    if g.tokens.len() == before { detail(StatusCode::NOT_FOUND, "token_not_found") } else { (StatusCode::NO_CONTENT, Json(Value::Null)) }
}

async fn a_tokens_of(State(f): State<Shared>, Query(q): Query<HashMap<String, String>>) -> Resp {
    let g = f.lock().unwrap();
    let ids: Vec<&str> = q.get("user_ids").map(|s| s.split(',').collect()).unwrap_or_default();
    let items: Vec<Value> = g.tokens.iter().filter(|(_, t)| ids.contains(&t.1.as_str())).map(|(r, t)| token_json(r, t)).collect();
    (StatusCode::OK, Json(json!({"items": items})))
}

fn view(g: &Fake, cid: &str, role: &str) -> Value {
    let c = &g.companies[cid];
    json!({"company_id": cid, "company_name": c.name, "inn": "", "ogrn": "", "role": role, "banned": c.banned.is_some(), "ban_reason": c.banned})
}

async fn l_ensure(State(f): State<Shared>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let uid = b["owner_id"].as_str().unwrap_or_default().to_string();
    if let Some(m) = g.members.iter().find(|m| m.uid == uid).cloned() {
        return (StatusCode::OK, Json(view(&g, &m.cid, &m.role)));
    }
    g.seq += 1;
    let cid = format!("c{}", g.seq);
    let name = b["name"].as_str().unwrap_or("Новая компания").to_string();
    g.companies.insert(cid.clone(), Co { name, banned: None });
    g.members.push(Mem { cid: cid.clone(), uid, email: b["email"].as_str().unwrap_or_default().into(), role: "director".into() });
    (StatusCode::OK, Json(view(&g, &cid, "director")))
}

async fn l_by_user(State(f): State<Shared>, Path(uid): Path<String>) -> Resp {
    let g = f.lock().unwrap();
    match g.members.iter().find(|m| m.uid == uid) {
        Some(m) => (StatusCode::OK, Json(view(&g, &m.cid, &m.role))),
        None => lerr(StatusCode::NOT_FOUND, "not_found"),
    }
}

fn mem_json(m: &Mem) -> Value {
    json!({"user_id": m.uid, "email": m.email, "role": m.role, "created_at": "2026-01-01T00:00:00Z"})
}

async fn l_members(State(f): State<Shared>, Path(cid): Path<String>) -> Resp {
    let g = f.lock().unwrap();
    let items: Vec<Value> = g.members.iter().filter(|m| m.cid == cid).map(mem_json).collect();
    (StatusCode::OK, Json(json!({"items": items})))
}

async fn l_member_add(State(f): State<Shared>, Path(cid): Path<String>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let uid = b["user_id"].as_str().unwrap_or_default().to_string();
    let role = b["role"].as_str().unwrap_or_default().to_string();
    if role != "editor" && role != "viewer" {
        return lerr(StatusCode::UNPROCESSABLE_ENTITY, "invalid_role");
    }
    if g.members.iter().any(|m| m.uid == uid) {
        return lerr(StatusCode::CONFLICT, "member_exists");
    }
    let m = Mem { cid, uid, email: b["email"].as_str().unwrap_or_default().into(), role };
    let out = mem_json(&m);
    g.members.push(m);
    (StatusCode::CREATED, Json(out))
}

async fn l_member_patch(State(f): State<Shared>, Path((cid, uid)): Path<(String, String)>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(m) = g.members.iter_mut().find(|m| m.cid == cid && m.uid == uid) else { return lerr(StatusCode::NOT_FOUND, "not_found") };
    if m.role == "director" && b.get("role").is_some() {
        return lerr(StatusCode::CONFLICT, "cannot_change_director");
    }
    if let Some(r) = b["role"].as_str() {
        if r != "editor" && r != "viewer" {
            return lerr(StatusCode::UNPROCESSABLE_ENTITY, "invalid_role");
        }
        m.role = r.into();
    }
    if let Some(e) = b["email"].as_str() {
        m.email = e.into();
    }
    (StatusCode::OK, Json(mem_json(m)))
}

async fn l_member_delete(State(f): State<Shared>, Path((cid, uid)): Path<(String, String)>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(m) = g.members.iter().find(|m| m.cid == cid && m.uid == uid) else { return lerr(StatusCode::NOT_FOUND, "not_found") };
    if m.role == "director" {
        return lerr(StatusCode::CONFLICT, "cannot_remove_director");
    }
    g.members.retain(|m| !(m.cid == cid && m.uid == uid));
    (StatusCode::NO_CONTENT, Json(Value::Null))
}

async fn l_company_patch(State(f): State<Shared>, Path(cid): Path<String>, Json(b): Json<Value>) -> Resp {
    let mut g = f.lock().unwrap();
    let Some(c) = g.companies.get_mut(&cid) else { return lerr(StatusCode::NOT_FOUND, "not_found") };
    c.name = b["name"].as_str().unwrap_or("?").into();
    (StatusCode::OK, Json(json!({"company_id": cid, "company_name": c.name})))
}

async fn fallback() -> Json<Value> {
    Json(json!({"items": [], "ok": true}))
}

fn fake_router(f: Shared) -> Router {
    use axum::routing::{delete, patch};
    Router::new()
        .route("/internal/v1/session", post(a_session))
        .route("/internal/v1/tokens/verify", post(a_verify))
        .route("/internal/v1/tokens", get(a_tokens_of))
        .route("/internal/v1/login", post(a_login))
        .route("/internal/v1/logout", post(a_logout))
        .route("/internal/v1/users/create", post(a_create))
        .route("/internal/v1/users/{uid}", delete(a_delete))
        .route("/internal/v1/users/{uid}/password", post(a_password))
        .route("/internal/v1/users/{uid}/reset-password", post(a_reset))
        .route("/internal/v1/users/{uid}/email-password", post(a_email_password))
        .route("/internal/v1/users/{uid}/methods", get(a_methods))
        .route("/internal/v1/users/{uid}/tokens", get(a_tok_list).post(a_tok_create))
        .route("/internal/v1/users/{uid}/tokens/{tid}", delete(a_tok_delete))
        .route("/internal/v1/companies/ensure", post(l_ensure))
        .route("/internal/v1/members/by-user/{uid}", get(l_by_user))
        .route("/internal/v1/companies/{cid}", patch(l_company_patch))
        .route("/internal/v1/companies/{cid}/members", get(l_members).post(l_member_add))
        .route("/internal/v1/companies/{cid}/members/{uid}", patch(l_member_patch).delete(l_member_delete))
        .fallback(fallback)
        .layer(middleware::from_fn_with_state(f.clone(), record))
        .with_state(f)
}

// ---------- окружение теста ----------
struct T {
    app: Router,
    f: Shared,
}

impl T {
    async fn new() -> T {
        let f: Shared = Arc::default();
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://127.0.0.1:{}", l.local_addr().unwrap().port());
        let router = fake_router(f.clone());
        tokio::spawn(async move { axum::serve(l, router).await.unwrap() });
        let up = |n: &'static str| Upstream::new(n, "тест", &base, 10);
        let st = AppState {
            auth: up("auth"),
            ecp: up("auth"),
            gov: up("gov"),
            ledger: up("ledger"),
            forecast: up("forecast"),
            planfact: up("planfact"),
            integrator: up("integrator"),
            advisor: up("advisor"),
            backoffice: up("backoffice"),
            demo: Arc::new(demo::Demo { enabled: false, email: "demo@example.com".into(), external_company: "C001".into(), feed: up("demo_feed") }),
            cfg: Arc::new(Config { cookie_secure: false }),
        };
        let app = crate::tenant_routes().layer(middleware::from_fn(crate::security_layer)).with_state(st);
        T { app, f }
    }

    /// Пользователь с паролем; `member` — (компания, роль), иначе без членства.
    fn user(&self, email: &str, password: &str, has_email: bool) -> String {
        let mut g = self.f.lock().unwrap();
        g.seq += 1;
        let id = format!("u{}", g.seq);
        g.users.push(User { id: id.clone(), email: email.into(), password: password.into(), has_email, must_change: false, generated: false });
        id
    }

    fn session(&self, uid: &str) -> String {
        let mut g = self.f.lock().unwrap();
        g.seq += 1;
        let tok = format!("sess-{}", g.seq);
        g.sessions.insert(tok.clone(), uid.into());
        tok
    }

    fn company(&self, name: &str, director: &str) -> String {
        let mut g = self.f.lock().unwrap();
        g.seq += 1;
        let cid = format!("c{}", g.seq);
        g.companies.insert(cid.clone(), Co { name: name.into(), banned: None });
        let email = g.users.iter().find(|u| u.id == director).map(|u| u.email.clone()).unwrap_or_default();
        g.members.push(Mem { cid: cid.clone(), uid: director.into(), email, role: "director".into() });
        cid
    }

    fn join(&self, cid: &str, uid: &str, role: &str) {
        let mut g = self.f.lock().unwrap();
        let email = g.users.iter().find(|u| u.id == uid).map(|u| u.email.clone()).unwrap_or_default();
        g.members.push(Mem { cid: cid.into(), uid: uid.into(), email, role: role.into() });
    }

    fn set_role(&self, uid: &str, role: &str) {
        self.f.lock().unwrap().members.iter_mut().find(|m| m.uid == uid).unwrap().role = role.into();
    }

    fn ban(&self, cid: &str, reason: Option<&str>) {
        self.f.lock().unwrap().companies.get_mut(cid).unwrap().banned = reason.map(str::to_string);
    }

    fn token_of(&self, uid: &str) -> String {
        let mut g = self.f.lock().unwrap();
        g.seq += 1;
        let raw = format!("ckt_{:04}yyyyyyyyyyyy", g.seq);
        let t = (format!("t{}", g.seq), uid.to_string(), "k".to_string());
        g.tokens.insert(raw.clone(), t);
        raw
    }

    fn calls(&self) -> Vec<String> {
        self.f.lock().unwrap().calls.clone()
    }

    fn companies_count(&self) -> usize {
        self.f.lock().unwrap().companies.len()
    }

    /// Компания с директором и сотрудниками по ролям; возвращает (cid, токены сессий director/editor/viewer).
    fn team(&self) -> (String, [String; 3]) {
        let d = self.user("dir@x.ru", "dir-pass-1", true);
        let cid = self.company("Ромашка", &d);
        let e = self.user("ed@x.ru", "ed-pass-12", true);
        let v = self.user("vi@x.ru", "vi-pass-12", true);
        self.join(&cid, &e, "editor");
        self.join(&cid, &v, "viewer");
        (cid, [self.session(&d), self.session(&e), self.session(&v)])
    }

    async fn send(&self, method: &str, uri: &str, cookie: Option<&str>, bearer: Option<&str>, body: Option<Value>) -> (StatusCode, Value) {
        let mut rb = Request::builder().method(method).uri(uri).header("content-type", "application/json");
        if let Some(c) = cookie {
            rb = rb.header("cookie", format!("session={c}"));
        }
        if let Some(b) = bearer {
            rb = rb.header("authorization", format!("Bearer {b}"));
        }
        let body = body.map(|v| Body::from(serde_json::to_vec(&v).unwrap())).unwrap_or_else(Body::empty);
        let res = self.app.clone().oneshot(rb.body(body).unwrap()).await.unwrap();
        let status = res.status();
        let bytes = to_bytes(res.into_body(), 10 * 1024 * 1024).await.unwrap();
        (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }

    async fn call(&self, method: &str, uri: &str, tok: &str, body: Option<Value>) -> (StatusCode, Value) {
        self.send(method, uri, Some(tok), None, body).await
    }
}

fn code(v: &Value) -> &str {
    v["error"]["code"].as_str().unwrap_or("")
}

// ---------- права: таблица «маршрут -> право» ----------
#[derive(Clone, Copy, PartialEq, Debug)]
enum Need {
    Read,
    Edit,
    Manage,
}

impl Need {
    fn allows(self, role: usize) -> bool {
        match self {
            Need::Read => true,
            Need::Edit => role <= 1,
            Need::Manage => role == 0,
        }
    }
}

/// Все публичные тенантские маршруты (без входа, me/logout/set-credentials, внешнего API): метод, путь, нужное право.
fn route_table(c: &str) -> Vec<(&'static str, String, Need)> {
    let co = format!("/api/v1/companies/{c}");
    use Need::*;
    vec![
        ("PATCH", co.clone(), Manage),
        ("GET", format!("{co}/imports"), Read),
        ("POST", format!("{co}/imports"), Edit),
        ("GET", format!("{co}/imports/i1"), Read),
        ("POST", format!("{co}/imports/i1/confirm"), Edit),
        ("GET", format!("{co}/transactions"), Read),
        ("PATCH", format!("{co}/transactions/1/category"), Edit),
        ("GET", format!("{co}/expected-events"), Read),
        ("POST", format!("{co}/expected-events"), Edit),
        ("DELETE", format!("{co}/expected-events/e1"), Edit),
        ("POST", format!("{co}/expected-events/tax-calendar"), Edit),
        ("POST", format!("{co}/integrations/sync"), Edit),
        ("POST", format!("{co}/advisor/ask"), Read),
        ("POST", format!("{co}/advisor/suggestions/apply"), Edit),
        ("GET", format!("{co}/demo/status"), Read),
        ("POST", format!("{co}/demo/advance-week"), Edit),
        ("GET", "/api/v1/gov/key-rate".into(), Read),
        ("POST", format!("{co}/forecasts"), Read),
        ("GET", format!("{co}/forecasts/latest"), Read),
        ("GET", format!("{co}/forecasts/r1"), Read),
        ("GET", format!("{co}/backtest"), Read),
        ("POST", format!("{co}/scenarios"), Read),
        ("GET", format!("{co}/plan-fact"), Read),
        ("GET", "/api/v1/account/methods".into(), Read),
        ("POST", "/api/v1/account/email".into(), Manage),
        ("DELETE", "/api/v1/account/email".into(), Manage),
        ("POST", "/api/v1/account/signatures".into(), Manage),
        ("DELETE", "/api/v1/account/signatures/s1".into(), Manage),
        ("GET", "/api/v1/account/tokens".into(), Read),
        ("GET", "/api/v1/account/tokens?scope=company".into(), Manage),
        ("POST", "/api/v1/account/tokens".into(), Read),
        ("DELETE", "/api/v1/account/tokens/t1".into(), Read),
        ("POST", "/api/v1/account/change-password".into(), Read),
        ("GET", "/api/v1/members".into(), Read),
        ("POST", "/api/v1/members".into(), Manage),
        ("PATCH", "/api/v1/members/u-x".into(), Manage),
        ("DELETE", "/api/v1/members/u-x".into(), Manage),
        ("POST", "/api/v1/members/u-x/reset-password".into(), Manage),
    ]
}

fn sample_body(method: &str, uri: &str) -> Option<Value> {
    match (method, uri) {
        ("GET", _) | ("DELETE", _) => None,
        (_, u) if u.ends_with("/advisor/ask") => Some(json!({"question": "q"})),
        (_, u) if u.ends_with("/suggestions/apply") => Some(json!({"type": "add_expected_payment", "params": {}})),
        (_, u) if u.ends_with("/account/email") => Some(json!({"email": "a@b.co", "password": "password-1", "password_confirm": "password-1"})),
        (_, u) if u.ends_with("/account/signatures") => Some(json!({"challenge": "c", "signature": "s"})),
        (_, u) if u.ends_with("/change-password") => Some(json!({"current": "x", "new": "new-password-1"})),
        (_, u) if u.ends_with("/account/tokens") => Some(json!({"name": "k"})),
        (_, u) if u.ends_with("/api/v1/members") => Some(json!({"email": "n@x.ru", "role": "viewer", "generate": true})),
        (_, u) if u.ends_with("/members/u-x") => Some(json!({"role": "viewer"})),
        (_, u) if u.ends_with("/category") => Some(json!({"category": "rent"})),
        (_, u) if u.ends_with("/expected-events") => Some(json!({"date": "2026-10-01", "amount_kop": "100", "direction": "in", "title": "t"})),
        (_, u) if !u.contains("/companies/") => Some(json!({})),
        (_, u) if u.matches('/').count() == 4 => Some(json!({"name": "Новое название"})),
        _ => Some(json!({})),
    }
}

#[tokio::test]
async fn every_route_enforces_its_permission_for_each_role() {
    let t = T::new().await;
    let (cid, toks) = t.team();
    let roles = ["director", "editor", "viewer"];
    let table = route_table(&cid);
    assert_eq!(table.len(), 38, "таблица маршрутов должна совпадать с docs/roles.md");
    for (method, uri, need) in &table {
        for (i, tok) in toks.iter().enumerate() {
            let before = t.calls().len();
            let (s, v) = t.call(method, uri, tok, sample_body(method, uri)).await;
            if need.allows(i) {
                assert_ne!(code(&v), "forbidden", "{} {uri}: {} должен иметь право {need:?}", method, roles[i]);
            } else {
                assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"), "{method} {uri}: {} не должен иметь права {need:?}", roles[i]);
                // отказ происходит до обращения к данным: допустимы только проверка сессии и членства
                let extra: Vec<String> = t.calls()[before..].to_vec();
                assert!(extra.iter().all(|c| c == "POST /internal/v1/session" || c.starts_with("GET /internal/v1/members/by-user/")), "{method} {uri}: {extra:?}");
            }
        }
    }
    // без сессии ни один маршрут не доступен
    for (method, uri, _) in &table {
        let (s, v) = t.send(method, uri, None, None, sample_body(method, uri)).await;
        assert_eq!((s, code(&v)), (StatusCode::UNAUTHORIZED, "unauthenticated"), "{method} {uri}");
    }
}

#[tokio::test]
async fn viewer_reads_and_computes_but_cannot_change_data() {
    let t = T::new().await;
    let (cid, [_, _, viewer]) = t.team();
    for (m, u) in [("GET", "transactions"), ("GET", "expected-events"), ("GET", "plan-fact"), ("GET", "forecasts/latest")] {
        assert_eq!(t.call(m, &format!("/api/v1/companies/{cid}/{u}"), &viewer, None).await.0, StatusCode::OK, "{u}");
    }
    // вычисляющие POST разрешены
    assert_eq!(t.call("POST", &format!("/api/v1/companies/{cid}/forecasts"), &viewer, Some(json!({"horizon_days": 30}))).await.0, StatusCode::OK);
    assert_eq!(t.call("POST", &format!("/api/v1/companies/{cid}/scenarios"), &viewer, Some(json!({}))).await.0, StatusCode::OK);
    // изменения запрещены, и до ledger дело не доходит
    let n = t.calls().iter().filter(|c| c.contains("/expected-events")).count();
    let (s, v) = t.call("POST", &format!("/api/v1/companies/{cid}/expected-events"), &viewer, Some(json!({"date": "2026-10-01", "amount_kop": "1", "direction": "in", "title": "x"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert_eq!(t.calls().iter().filter(|c| c.contains("/expected-events")).count(), n);
    assert_eq!(v["error"]["message"], "Недостаточно прав для этого действия");
}

#[tokio::test]
async fn editor_edits_data_but_cannot_manage_company_or_staff() {
    let t = T::new().await;
    let (cid, [_, editor, _]) = t.team();
    assert_eq!(t.call("POST", &format!("/api/v1/companies/{cid}/expected-events"), &editor, Some(json!({"date": "2026-10-01", "amount_kop": "1", "direction": "in", "title": "x"}))).await.0, StatusCode::OK);
    assert_eq!(t.call("PATCH", &format!("/api/v1/companies/{cid}/transactions/5/category"), &editor, Some(json!({"category": "rent"}))).await.0, StatusCode::OK);
    for (m, u, b) in [
        ("PATCH", format!("/api/v1/companies/{cid}"), Some(json!({"name": "Хак"}))),
        ("POST", "/api/v1/members".into(), Some(json!({"email": "q@x.ru", "role": "viewer", "generate": true}))),
        ("DELETE", "/api/v1/members/anyone".into(), None),
    ] {
        let (s, v) = t.call(m, &u, &editor, b).await;
        assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"), "{m} {u}");
    }
}

#[tokio::test]
async fn foreign_company_is_not_found_even_with_permission() {
    let t = T::new().await;
    let (_, [_, editor, _]) = t.team();
    let other_dir = t.user("o@x.ru", "other-pass-1", true);
    let other = t.company("Чужая", &other_dir);
    let (s, v) = t.call("GET", &format!("/api/v1/companies/{other}/transactions"), &editor, None).await;
    assert_eq!((s, code(&v)), (StatusCode::NOT_FOUND, "not_found"));
    let (s, _) = t.call("POST", &format!("/api/v1/companies/{other}/expected-events"), &editor, Some(json!({}))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

// ---------- /auth/me ----------
#[tokio::test]
async fn me_reports_role_permissions_and_flags() {
    let t = T::new().await;
    let (cid, toks) = t.team();
    let want = [("director", true, true), ("editor", true, false), ("viewer", false, false)];
    for (tok, (role, edit, manage)) in toks.iter().zip(want) {
        let (s, v) = t.call("GET", "/api/v1/auth/me", tok, None).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!((v["role"].as_str(), v["company_id"].as_str(), v["company_name"].as_str()), (Some(role), Some(cid.as_str()), Some("Ромашка")));
        assert_eq!((v["permissions"]["read"].as_bool(), v["permissions"]["edit"].as_bool(), v["permissions"]["manage"].as_bool()), (Some(true), Some(edit), Some(manage)), "{role}");
        assert_eq!((v["must_change_password"].as_bool(), v["password_required"].as_bool(), v["company_banned"].as_bool(), v["ban_reason"].is_null()), (Some(false), Some(false), Some(false), true));
        assert!(v["email"].as_str().is_some());
    }
}

#[test]
fn role_matrix() {
    use http::{Perm, Role};
    for (r, read, edit, manage) in [(Role::Director, true, true, true), (Role::Editor, true, true, false), (Role::Viewer, true, false, false)] {
        assert_eq!((r.can(Perm::Read), r.can(Perm::Edit), r.can(Perm::Manage)), (read, edit, manage), "{r:?}");
    }
    assert_eq!(Role::parse("что-то"), Role::Viewer, "неизвестная роль получает минимум прав");
    assert_eq!(http::public_email("ecp-ab@ecp.invalid"), None);
    assert_eq!(http::public_email("a@b.co"), Some("a@b.co"));
}

// ---------- бан компании ----------
#[tokio::test]
async fn banned_company_gets_403_for_everyone_and_every_kind_of_route() {
    let t = T::new().await;
    let (cid, toks) = t.team();
    t.ban(&cid, Some("Нарушение условий"));
    for tok in &toks {
        for (m, u, b) in [
            ("GET", format!("/api/v1/companies/{cid}/transactions"), None),
            ("POST", format!("/api/v1/companies/{cid}/forecasts"), Some(json!({}))),
            ("POST", format!("/api/v1/companies/{cid}/expected-events"), Some(json!({}))),
            ("GET", "/api/v1/members".into(), None),
            ("GET", "/api/v1/account/tokens".into(), None),
            ("POST", "/api/v1/members".into(), Some(json!({"email": "n@x.ru", "role": "viewer", "generate": true}))),
        ] {
            let (s, v) = t.call(m, &u, tok, b).await;
            assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "company_banned"), "{m} {u}");
            assert_eq!(v["error"]["message"], "Компания заблокирована оператором: Нарушение условий");
        }
        // me остаётся доступным, чтобы интерфейс показал причину
        let (s, v) = t.call("GET", "/api/v1/auth/me", tok, None).await;
        assert_eq!((s, v["company_banned"].as_bool(), v["ban_reason"].as_str()), (StatusCode::OK, Some(true), Some("Нарушение условий")));
    }
    // токены внешнего API тоже закрыты
    let d = t.f.lock().unwrap().members.iter().find(|m| m.role == "director").unwrap().uid.clone();
    let api_token = t.token_of(&d);
    let (s, v) = t.send("GET", "/api/ext/v1/company", None, Some(&api_token), None).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "company_banned"));
    // разблокировка возвращает доступ сразу
    t.ban(&cid, None);
    assert_eq!(t.call("GET", &format!("/api/v1/companies/{cid}/transactions"), &toks[2], None).await.0, StatusCode::OK);
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some(&api_token), None).await.0, StatusCode::OK);
}

#[tokio::test]
async fn login_into_banned_company_is_refused_and_session_is_closed() {
    let t = T::new().await;
    let (cid, _) = t.team();
    t.ban(&cid, Some("Спам"));
    let (s, v) = t.send("POST", "/api/v1/auth/login", None, None, Some(json!({"email": "vi@x.ru", "password": "vi-pass-12"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "company_banned"));
    assert!(v["error"]["message"].as_str().unwrap().contains("Спам"));
    assert!(t.f.lock().unwrap().sessions.keys().all(|k| !k.starts_with("login-")), "созданная при входе сессия должна быть закрыта");
}

// ---------- пароль директора ----------
#[tokio::test]
async fn ecp_director_without_password_is_locked_until_set_credentials() {
    let t = T::new().await;
    let uid = t.user("ecp-ab12@ecp.invalid", "unused-password", false);
    let cid = t.company("ИП Ромашка", &uid);
    let tok = t.session(&uid);

    let (s, v) = t.call("GET", "/api/v1/auth/me", &tok, None).await;
    assert_eq!((s, v["password_required"].as_bool(), v["role"].as_str(), v["email"].is_null()), (StatusCode::OK, Some(true), Some("director"), true));
    for (m, u, b) in [
        ("GET", format!("/api/v1/companies/{cid}/transactions"), None),
        ("POST", format!("/api/v1/companies/{cid}/expected-events"), Some(json!({}))),
        ("GET", "/api/v1/members".into(), None),
        ("GET", "/api/v1/account/methods".into(), None),
        ("POST", "/api/v1/account/tokens".into(), Some(json!({"name": "k"}))),
        ("POST", "/api/v1/members".into(), Some(json!({"email": "n@x.ru", "role": "viewer", "generate": true}))),
    ] {
        let (s, v) = t.call(m, &u, &tok, b).await;
        assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "password_required"), "{m} {u}");
    }
    // слабый пароль и несовпадение отвергаются
    let (s, v) = t.call("POST", "/api/v1/account/set-credentials", &tok, Some(json!({"email": "dir@x.ru", "password": "abc", "password_confirm": "abc"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_password"));
    let (s, v) = t.call("POST", "/api/v1/account/set-credentials", &tok, Some(json!({"email": "dir@x.ru", "password": "director-pass-1", "password_confirm": "other-pass-12"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch"));
    // установка пароля открывает остальное; email директора в списке участников обновляется
    let (s, v) = t.call("POST", "/api/v1/account/set-credentials", &tok, Some(json!({"email": "Dir@X.ru", "password": "director-pass-1", "password_confirm": "director-pass-1"}))).await;
    assert_eq!((s, v["password_required"].as_bool(), v["email"].as_str()), (StatusCode::OK, Some(false), Some("dir@x.ru")));
    assert_eq!(t.call("GET", &format!("/api/v1/companies/{cid}/transactions"), &tok, None).await.0, StatusCode::OK);
    let (_, m) = t.call("GET", "/api/v1/members", &tok, None).await;
    assert_eq!(m["items"][0]["email"], "dir@x.ru");
    let (_, me) = t.call("GET", "/api/v1/auth/me", &tok, None).await;
    assert_eq!((me["password_required"].as_bool(), me["email"].as_str()), (Some(false), Some("dir@x.ru")));
    // повторно задать нельзя
    let (s, v) = t.call("POST", "/api/v1/account/set-credentials", &tok, Some(json!({"email": "z@x.ru", "password": "director-pass-2"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "credentials_exist"));
}

#[tokio::test]
async fn logout_works_while_password_is_required() {
    let t = T::new().await;
    let uid = t.user("ecp-1@ecp.invalid", "x", false);
    t.company("К", &uid);
    let tok = t.session(&uid);
    let (s, _) = t.call("POST", "/api/v1/auth/logout", &tok, None).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    assert_eq!(t.call("GET", "/api/v1/auth/me", &tok, None).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn director_password_cannot_be_removed() {
    let t = T::new().await;
    let (_, [d, ..]) = t.team();
    let (s, v) = t.call("DELETE", "/api/v1/account/email", &d, None).await;
    assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "password_required"));
}

// ---------- сотрудники ----------
#[tokio::test]
async fn director_creates_employee_with_generated_password_who_joins_the_same_company() {
    let t = T::new().await;
    let (cid, [d, ..]) = t.team();
    let companies_before = t.companies_count();
    let (s, v) = t.call("POST", "/api/v1/members", &d, Some(json!({"email": "New@X.ru", "role": "editor", "generate": true}))).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    assert_eq!((v["email"].as_str(), v["role"].as_str(), v["password"].as_str(), v["must_change_password"].as_bool()), (Some("new@x.ru"), Some("editor"), Some("GenPass-12345"), Some(true)));
    let uid = v["user_id"].as_str().unwrap().to_string();

    // пароль показывается только в ответе создания: список его не содержит
    let (_, l) = t.call("GET", "/api/v1/members", &d, None).await;
    assert!(!l.to_string().contains("GenPass"));
    let roles: Vec<_> = l["items"].as_array().unwrap().iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["director", "editor", "viewer", "editor"]);
    assert_eq!(l["items"][0]["is_me"], true);

    // сотрудник входит по email+паролю, попадает в компанию директора; новая компания не создаётся
    let (s, lv) = t.send("POST", "/api/v1/auth/login", None, None, Some(json!({"email": "new@x.ru", "password": "GenPass-12345"}))).await;
    assert_eq!(s, StatusCode::OK, "{lv}");
    assert_eq!((lv["company_id"].as_str(), lv["role"].as_str(), lv["must_change_password"].as_bool(), lv["password_generated"].as_bool()), (Some(cid.as_str()), Some("editor"), Some(true), Some(true)));
    assert_eq!(t.companies_count(), companies_before);
    let emp = t.f.lock().unwrap().sessions.keys().find(|k| k.starts_with("login-")).unwrap().clone();
    let (_, me) = t.call("GET", "/api/v1/auth/me", &emp, None).await;
    assert_eq!((me["role"].as_str(), me["company_id"].as_str(), me["must_change_password"].as_bool()), (Some("editor"), Some(cid.as_str()), Some(true)));
    assert_eq!(t.call("POST", &format!("/api/v1/companies/{cid}/expected-events"), &emp, Some(json!({}))).await.0, StatusCode::OK);

    // смена пароля сбрасывает флаги
    let (s, v) = t.call("POST", "/api/v1/account/change-password", &emp, Some(json!({"new": "my-own-pass-1", "new_confirm": "my-own-pass-1"}))).await;
    assert_eq!((s, v["must_change_password"].as_bool()), (StatusCode::OK, Some(false)));
    let (_, me) = t.call("GET", "/api/v1/auth/me", &emp, None).await;
    assert_eq!(me["must_change_password"], false);
    assert_eq!(uid, me["user_id"].as_str().unwrap());
}

#[tokio::test]
async fn employee_with_given_password_and_creation_validation() {
    let t = T::new().await;
    let (_, [d, ..]) = t.team();
    let (s, v) = t.call("POST", "/api/v1/members", &d, Some(json!({"email": "a@x.ru", "role": "viewer", "password": "given-pass-1"}))).await;
    assert_eq!((s, v["role"].as_str()), (StatusCode::CREATED, Some("viewer")));
    assert!(v.get("password").is_none(), "заданный директором пароль в ответе не возвращается");
    for (body, st, c) in [
        (json!({"email": "b@x.ru", "role": "director", "generate": true}), 422, "invalid_role"),
        (json!({"email": "b@x.ru", "role": "owner", "generate": true}), 422, "invalid_role"),
        (json!({"email": "b@x.ru", "role": "viewer"}), 422, "invalid_password"),
        (json!({"email": "b@x.ru", "role": "viewer", "generate": true, "password": "given-pass-1"}), 422, "invalid_password"),
        (json!({"email": "b@x.ru", "role": "viewer", "password": "short"}), 422, "invalid_password"),
        (json!({"email": "a@x.ru", "role": "viewer", "generate": true}), 409, "email_taken"),
        (json!({"email": "ecp-1@ecp.invalid", "role": "viewer", "generate": true}), 422, "invalid_email"),
    ] {
        let (s, v) = t.call("POST", "/api/v1/members", &d, Some(body.clone())).await;
        assert_eq!((s.as_u16(), code(&v)), (st, c), "{body}");
    }
}

#[tokio::test]
async fn role_change_takes_effect_immediately() {
    let t = T::new().await;
    let (cid, [d, e, _]) = t.team();
    let uid = t.f.lock().unwrap().members.iter().find(|m| m.role == "editor").unwrap().uid.clone();
    let uri = format!("/api/v1/companies/{cid}/expected-events");
    assert_eq!(t.call("POST", &uri, &e, Some(json!({}))).await.0, StatusCode::OK);
    let (s, v) = t.call("PATCH", &format!("/api/v1/members/{uid}"), &d, Some(json!({"role": "viewer"}))).await;
    assert_eq!((s, v["role"].as_str()), (StatusCode::OK, Some("viewer")));
    let (s, v) = t.call("POST", &uri, &e, Some(json!({}))).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert_eq!(t.call("GET", &uri, &e, None).await.0, StatusCode::OK);
    // роль director назначить нельзя, неизвестную — тоже
    let (s, v) = t.call("PATCH", &format!("/api/v1/members/{uid}"), &d, Some(json!({"role": "owner"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_role"));
}

#[tokio::test]
async fn director_cannot_be_demoted_deleted_or_reset_through_members() {
    let t = T::new().await;
    let (_, [d, ..]) = t.team();
    let did = t.f.lock().unwrap().members.iter().find(|m| m.role == "director").unwrap().uid.clone();
    for role in ["viewer", "editor", "director"] {
        let (s, v) = t.call("PATCH", &format!("/api/v1/members/{did}"), &d, Some(json!({"role": role}))).await;
        assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "cannot_change_director"), "{role}");
    }
    let (s, v) = t.call("DELETE", &format!("/api/v1/members/{did}"), &d, None).await;
    assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "cannot_remove_director"));
    let (s, v) = t.call("POST", &format!("/api/v1/members/{did}/reset-password"), &d, None).await;
    assert_eq!((s, code(&v)), (StatusCode::CONFLICT, "cannot_reset_director"));
    // директор и его пользователь на месте
    assert!(t.f.lock().unwrap().users.iter().any(|u| u.id == did));
    assert_eq!(t.call("GET", "/api/v1/members", &d, None).await.0, StatusCode::OK);
}

#[tokio::test]
async fn deleting_employee_revokes_sessions_and_tokens() {
    let t = T::new().await;
    let (cid, [d, e, _]) = t.team();
    let uid = t.f.lock().unwrap().members.iter().find(|m| m.role == "editor").unwrap().uid.clone();
    let api_token = t.token_of(&uid);
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some(&api_token), None).await.0, StatusCode::OK);

    let (s, _) = t.call("DELETE", &format!("/api/v1/members/{uid}"), &d, None).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    let (s, v) = t.call("GET", &format!("/api/v1/companies/{cid}/transactions"), &e, None).await;
    assert_eq!((s, code(&v)), (StatusCode::UNAUTHORIZED, "unauthenticated"));
    let (s, v) = t.send("GET", "/api/ext/v1/company", None, Some(&api_token), None).await;
    assert_eq!((s, code(&v)), (StatusCode::UNAUTHORIZED, "invalid_token"));
    let (_, l) = t.call("GET", "/api/v1/members", &d, None).await;
    assert_eq!(l["items"].as_array().unwrap().len(), 2);
    // повторное удаление и чужой/несуществующий участник
    assert_eq!(t.call("DELETE", &format!("/api/v1/members/{uid}"), &d, None).await.0, StatusCode::NOT_FOUND);
    assert_eq!(t.call("DELETE", "/api/v1/members/nobody", &d, None).await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn members_of_other_companies_are_unreachable() {
    let t = T::new().await;
    let (_, [d, ..]) = t.team();
    let od = t.user("o@x.ru", "other-pass-1", true);
    let other = t.company("Чужая", &od);
    let ev = t.user("oe@x.ru", "other-pass-2", true);
    t.join(&other, &ev, "editor");
    for (m, u, b) in [
        ("PATCH", format!("/api/v1/members/{ev}"), Some(json!({"role": "viewer"}))),
        ("DELETE", format!("/api/v1/members/{ev}"), None),
        ("POST", format!("/api/v1/members/{ev}/reset-password"), None),
    ] {
        assert_eq!(t.call(m, &u, &d, b).await.0, StatusCode::NOT_FOUND, "{m} {u}");
    }
    assert!(t.f.lock().unwrap().users.iter().any(|u| u.id == ev), "пользователь чужой компании не тронут");
}

#[tokio::test]
async fn reset_password_returns_new_password_once_and_closes_sessions() {
    let t = T::new().await;
    let (_, [d, e, _]) = t.team();
    let uid = t.f.lock().unwrap().members.iter().find(|m| m.role == "editor").unwrap().uid.clone();
    let (s, v) = t.call("POST", &format!("/api/v1/members/{uid}/reset-password"), &d, None).await;
    assert_eq!((s, v["password"].as_str(), v["must_change_password"].as_bool()), (StatusCode::OK, Some("ResetPass-1234"), Some(true)));
    assert_eq!(t.call("GET", "/api/v1/auth/me", &e, None).await.0, StatusCode::UNAUTHORIZED);
    let (s, lv) = t.send("POST", "/api/v1/auth/login", None, None, Some(json!({"email": "ed@x.ru", "password": "ResetPass-1234"}))).await;
    assert_eq!((s, lv["must_change_password"].as_bool(), lv["password_generated"].as_bool()), (StatusCode::OK, Some(true), Some(true)));
}

#[tokio::test]
async fn change_password_validates_current_and_confirmation() {
    let t = T::new().await;
    let (_, [_, e, _]) = t.team();
    let (s, v) = t.call("POST", "/api/v1/account/change-password", &e, Some(json!({"current": "wrong", "new": "new-password-1"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "invalid_current_password"));
    let (s, v) = t.call("POST", "/api/v1/account/change-password", &e, Some(json!({"current": "ed-pass-12", "new": "new-password-1", "new_confirm": "different-1"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::UNPROCESSABLE_ENTITY, "password_mismatch"));
    let (s, _) = t.call("POST", "/api/v1/account/change-password", &e, Some(json!({"current": "ed-pass-12", "new": "new-password-1"}))).await;
    assert_eq!(s, StatusCode::OK);
}

// ---------- токены внешнего API ----------
#[tokio::test]
async fn token_permissions_follow_owner_role() {
    let t = T::new().await;
    let (_, [..]) = t.team();
    let ids: HashMap<String, String> = t.f.lock().unwrap().members.iter().map(|m| (m.role.clone(), m.uid.clone())).collect();
    let (viewer_tok, editor_tok, director_tok) = (t.token_of(&ids["viewer"]), t.token_of(&ids["editor"]), t.token_of(&ids["director"]));
    let ev = json!({"date": "2026-10-01", "amount_kop": "1", "direction": "in", "title": "x"});

    // чтение доступно всем, scope отражает роль
    for (tok, scope) in [(&viewer_tok, "read"), (&editor_tok, "write"), (&director_tok, "write")] {
        let (s, v) = t.send("GET", "/api/ext/v1/company", None, Some(tok), None).await;
        assert_eq!((s, v["scope"].as_str()), (StatusCode::OK, Some(scope)));
        assert_eq!(t.send("GET", "/api/ext/v1/transactions", None, Some(tok), None).await.0, StatusCode::OK);
        assert_eq!(t.send("GET", "/api/ext/v1/plan-fact", None, Some(tok), None).await.0, StatusCode::OK);
        // построение прогноза — вычисляющий POST (read)
        assert_eq!(t.send("POST", "/api/ext/v1/forecast", None, Some(tok), Some(json!({}))).await.0, StatusCode::OK);
    }
    // изменение данных — только write
    let (s, v) = t.send("POST", "/api/ext/v1/expected-events", None, Some(&viewer_tok), Some(ev.clone())).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert_eq!(t.send("POST", "/api/ext/v1/expected-events", None, Some(&editor_tok), Some(ev.clone())).await.0, StatusCode::OK);
    assert_eq!(t.send("POST", "/api/ext/v1/expected-events", None, Some(&director_tok), Some(ev.clone())).await.0, StatusCode::OK);

    // понижение роли действует на токен сразу
    t.set_role(&ids["editor"], "viewer");
    let (s, v) = t.send("POST", "/api/ext/v1/expected-events", None, Some(&editor_tok), Some(ev)).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some(&editor_tok), None).await.1["scope"], "read");
    // неизвестный токен
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some("ckt_nope"), None).await.0, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn director_sees_and_revokes_all_company_tokens_employees_see_only_own() {
    let t = T::new().await;
    let (_, [d, e, v]) = t.team();
    let ids: HashMap<String, String> = t.f.lock().unwrap().members.iter().map(|m| (m.role.clone(), m.uid.clone())).collect();
    // сотрудник сам выпускает токен
    let (s, created) = t.call("POST", "/api/v1/account/tokens", &e, Some(json!({"name": "1С"}))).await;
    assert_eq!(s, StatusCode::CREATED);
    let employee_token = created["token"].as_str().unwrap().to_string();
    let eid = created["id"].as_str().unwrap().to_string();
    t.call("POST", "/api/v1/account/tokens", &v, Some(json!({"name": "BI"}))).await;
    t.call("POST", "/api/v1/account/tokens", &d, Some(json!({"name": "dir"}))).await;

    let (_, own) = t.call("GET", "/api/v1/account/tokens", &e, None).await;
    assert_eq!(own["items"].as_array().unwrap().len(), 1);
    let (s, all) = t.call("GET", "/api/v1/account/tokens?scope=company", &d, None).await;
    assert_eq!(s, StatusCode::OK);
    let items = all["items"].as_array().unwrap();
    assert_eq!(items.len(), 3);
    let mine = items.iter().find(|i| i["id"] == eid.as_str()).unwrap();
    assert_eq!((mine["owner_email"].as_str(), mine["owner_role"].as_str(), mine["mine"].as_bool()), (Some("ed@x.ru"), Some("editor"), Some(false)));
    assert!(!all.to_string().contains(&employee_token), "сам токен после создания не отдаётся");
    let (s, x) = t.call("GET", "/api/v1/account/tokens?scope=company", &e, None).await;
    assert_eq!((s, code(&x)), (StatusCode::FORBIDDEN, "forbidden"));
    assert_eq!(t.call("GET", "/api/v1/account/tokens?scope=other", &d, None).await.0, StatusCode::UNPROCESSABLE_ENTITY);

    // сотрудник не может удалить чужой токен; директор отзывает токен сотрудника
    let dir_token_id = items.iter().find(|i| i["owner_role"] == "director").unwrap()["id"].as_str().unwrap().to_string();
    assert_eq!(t.call("DELETE", &format!("/api/v1/account/tokens/{dir_token_id}"), &e, None).await.0, StatusCode::NOT_FOUND);
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some(&employee_token), None).await.0, StatusCode::OK);
    assert_eq!(t.call("DELETE", &format!("/api/v1/account/tokens/{eid}"), &d, None).await.0, StatusCode::NO_CONTENT);
    assert_eq!(t.send("GET", "/api/ext/v1/company", None, Some(&employee_token), None).await.0, StatusCode::UNAUTHORIZED);
    assert_eq!(t.call("DELETE", &format!("/api/v1/account/tokens/{eid}"), &d, None).await.0, StatusCode::NOT_FOUND);
    let _ = ids;
}

// ---------- вход сотрудника не создаёт компанию ----------
#[tokio::test]
async fn employee_login_never_creates_a_company_but_new_user_becomes_director() {
    let t = T::new().await;
    let (cid, _) = t.team();
    let n = t.companies_count();
    for (email, pw, role) in [("ed@x.ru", "ed-pass-12", "editor"), ("vi@x.ru", "vi-pass-12", "viewer")] {
        let (s, v) = t.send("POST", "/api/v1/auth/login", None, None, Some(json!({"email": email, "password": pw, "company_name": "Моя новая"}))).await;
        assert_eq!((s, v["company_id"].as_str(), v["role"].as_str()), (StatusCode::OK, Some(cid.as_str()), Some(role)));
        assert_eq!(v["permissions"]["edit"], role == "editor");
    }
    assert_eq!(t.companies_count(), n);
    // пользователь без членства получает собственную компанию и роль директора
    t.user("fresh@x.ru", "fresh-pass-1", true);
    let (s, v) = t.send("POST", "/api/v1/auth/login", None, None, Some(json!({"email": "fresh@x.ru", "password": "fresh-pass-1", "company_name": "Новая"}))).await;
    assert_eq!((s, v["role"].as_str(), v["company_name"].as_str()), (StatusCode::OK, Some("director"), Some("Новая")));
    assert_eq!(t.companies_count(), n + 1);
}

#[tokio::test]
async fn session_of_user_without_membership_does_not_create_company() {
    let t = T::new().await;
    let uid = t.user("ghost@x.ru", "ghost-pass-1", true);
    let tok = t.session(&uid);
    let n = t.companies_count();
    let (s, v) = t.call("GET", "/api/v1/auth/me", &tok, None).await;
    assert_eq!((s, code(&v)), (StatusCode::UNAUTHORIZED, "unauthenticated"));
    assert_eq!(t.companies_count(), n);
}

#[tokio::test]
async fn only_director_renames_company() {
    let t = T::new().await;
    let (cid, [d, e, _]) = t.team();
    let uri = format!("/api/v1/companies/{cid}");
    let (s, v) = t.call("PATCH", &uri, &e, Some(json!({"name": "Хак"}))).await;
    assert_eq!((s, code(&v)), (StatusCode::FORBIDDEN, "forbidden"));
    let (s, v) = t.call("PATCH", &uri, &d, Some(json!({"name": "Новое название"}))).await;
    assert_eq!((s, v["company_name"].as_str()), (StatusCode::OK, Some("Новое название")));
}
