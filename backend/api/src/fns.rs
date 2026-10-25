//! Интеграция с ФНС в продукте: налоговые события на год, единый налоговый счёт (ЕНС) и проверка контрагентов.
//! Сведения ФНС отдаёт сервис `fns`: только открытые реестры (ЕГРЮЛ/ЕГРИП, открытые данные ФНС, ЕФРСБ), ЕНС — из справки,
//! загруженной владельцем. Что проверять и что считать риском, компания задаёт в настройках (`FnsSettings`, раздел `fns` в ledger).
//! Здесь оркестрация: ИНН компании из реквизитов, суммы из ЕНС или оценки
//! по выписке, идемпотентная запись событий в ledger (`expected-events/bulk`, ключ `fns:<вид>:<дата>`), флаги контрагентов в ledger.
//!
//! Маршруты и права (таблица в docs/roles.md):
//! - `POST /api/v1/companies/{cid}/integrations/fns/sync` — edit
//! - `GET  /api/v1/companies/{cid}/fns/ens` — read
//! - `GET  /api/v1/companies/{cid}/fns/counterparties?top=20` — read
//! - `GET  /api/v1/companies/{cid}/fns/settings` — read, `PUT` — admin
//! - `GET  /api/ext/v1/fns/ens`, `GET /api/ext/v1/fns/counterparties` — токен, read
//!
//! Контракты fns (внутренние): `GET /internal/v1/fns/obligations?inn=&regime=&year=`, `GET /internal/v1/fns/ens?inn=`,
//! `GET /internal/v1/fns/counterparty?inn=`, `POST /internal/v1/fns/counterparties/batch {inns}`.
//! Разбор ответов терпим к именам полей (см. функции `parse_*`): суммы — копейки, даты — ГГГГ-ММ-ДД.
//! ИНН в журналы не пишется; в ответах SPA ИНН контрагентов есть (они и так видны в выписке компании).

use crate::AppState;
use crate::http::{Administrator, Editor, Principal, Reader, TokenPrincipal, audit, estimate_tax, estimate_usn, estimate_vat_installment, with_actor};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{Method, StatusCode},
};
use chrono::{Datelike, NaiveDate};
use common::{ApiError, Ctx, R, Snapshot, Tx, Upstream, err, internal};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, HashSet};

const SOURCE_LABEL: &str = "ФНС";
const COUNTERPARTY_NAME: &str = "ФНС России";
const REGIMES: [&str; 5] = ["osno", "usn_income", "usn_income_expense", "eshn", "patent"];
const DEFAULT_TOP: usize = 20;
const MAX_TOP: usize = 50;

fn authorize(p: &Principal, cid: &str) -> R<()> {
    if p.company_id == cid { Ok(()) } else { Err(err(StatusCode::NOT_FOUND, "not_found", "Не найдено")) }
}

fn valid_inn(s: &str) -> bool {
    (s.len() == 10 || s.len() == 12) && s.bytes().all(|b| b.is_ascii_digit())
}

/// ИНН компании из реквизитов; без него запросы в ФНС невозможны.
fn company_inn(p: &Principal) -> R<String> {
    let inn = p.company_inn.trim();
    if valid_inn(inn) {
        Ok(inn.to_string())
    } else {
        Err(err(
            StatusCode::UNPROCESSABLE_ENTITY,
            "inn_required",
            "Укажите ИНН компании: Настройки → Реквизиты компании. Без ИНН данные ФНС запросить нельзя",
        ))
    }
}

// ---------- разбор ответов fns ----------
fn kop_of(v: &Value) -> Option<i64> {
    match v {
        Value::String(s) => s.trim().parse::<i64>().ok(),
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.round() as i64)),
        _ => None,
    }
}

fn first_kop(o: &Value, keys: &[&str]) -> Option<i64> {
    keys.iter().find_map(|k| kop_of(&o[*k]))
}

fn str_of<'a>(o: &'a Value, keys: &[&str]) -> Option<&'a str> {
    keys.iter().find_map(|k| o[*k].as_str()).map(str::trim).filter(|s| !s.is_empty())
}

fn date_of(o: &Value, keys: &[&str]) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(str_of(o, keys)?.get(..10)?, "%Y-%m-%d").ok()
}

/// Источник ответа fns: `egrul`, `data_nalog`, `import` (справка ЕНС), `rules` (календарь по закону).
fn source_of(v: &Value) -> String {
    str_of(v, &["source"]).unwrap_or("fns").to_string()
}

// ---------- настройки компании ----------

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct FnsSources {
    /// ЕГРЮЛ/ЕГРИП: статус, ликвидация, дата регистрации
    pub egrul: bool,
    /// открытые данные ФНС: задолженность перед бюджетом
    pub data_nalog: bool,
    /// ЕФРСБ: банкротство (нужен логин оператора на сервере)
    pub fedresurs: bool,
}

impl Default for FnsSources {
    fn default() -> Self {
        Self { egrul: true, data_nalog: true, fedresurs: true }
    }
}

/// Какие признаки считать риском. Не найденный в ЕГРЮЛ ИНН по умолчанию риском не считается: так бывает с опечаткой в выписке.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct FnsRisk {
    pub bankruptcy: bool,
    pub liquidation: bool,
    pub not_in_registry: bool,
    pub unreliable: bool,
    pub disqualified: bool,
    pub tax_debt: bool,
    pub mass_address: bool,
    /// долг от этой суммы — «крупный» (вес 2), коп.
    pub large_debt_kop: i64,
}

impl Default for FnsRisk {
    fn default() -> Self {
        Self { bankruptcy: true, liquidation: true, not_in_registry: false, unreliable: true, disqualified: true, tax_debt: true, mass_address: true, large_debt_kop: 50_000_000 }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct FnsSettings {
    pub sources: FnsSources,
    pub risk: FnsRisk,
    /// показывать контрагентов из выписки
    pub show_statement: bool,
    /// показывать контрагентов из ожидаемых событий (счета и платёжки 1С)
    pub show_expected: bool,
    /// сколько контрагентов проверять (1..=50)
    pub top: usize,
    /// учитывать флаги риска в прогнозе (сдвиг и уверенность поступлений)
    pub use_in_forecast: bool,
    /// проверять контрагентов сразу после синхронизации интеграций
    pub check_after_sync: bool,
}

impl Default for FnsSettings {
    fn default() -> Self {
        Self { sources: FnsSources::default(), risk: FnsRisk::default(), show_statement: true, show_expected: true, top: DEFAULT_TOP, use_in_forecast: true, check_after_sync: true }
    }
}

impl FnsSettings {
    fn validate(&self) -> R<()> {
        let bad = |m: &str| Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_settings", m));
        if !(1..=MAX_TOP).contains(&self.top) {
            return bad("top: число от 1 до 50");
        }
        if !(0..1_000_000_000_000_000).contains(&self.risk.large_debt_kop) {
            return bad("large_debt_kop: неотрицательное число копеек");
        }
        if !self.show_statement && !self.show_expected {
            return bad("Нужно показывать хотя бы один вид контрагентов");
        }
        if !(self.sources.egrul || self.sources.data_nalog || self.sources.fedresurs) {
            return bad("Нужен хотя бы один источник");
        }
        Ok(())
    }
}

/// Настройки компании из ledger; нет записи или запись не разбирается — значения по умолчанию.
pub(crate) async fn load_settings(st: &AppState, ctx: &Ctx, cid: &str) -> R<FnsSettings> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/settings/fns"), ctx).await?;
    Ok(serde_json::from_value(v["value"].clone()).unwrap_or_default())
}

/// Состояние источников в сервисе fns (дата набора задолженности, есть ли логин ЕФРСБ); сервис недоступен — null.
async fn fns_info(st: &AppState, ctx: &Ctx) -> Value {
    match st.fns.json(Method::GET, "/internal/v1/fns/info", ctx, None).await {
        Ok(r) if r.status.is_success() => {
            let v = r.json();
            json!({"fedresurs_configured": v["fedresurs_configured"], "debt_as_of": v["debt_index"]["as_of"], "debt_state": v["debt_index"]["state"]})
        }
        _ => Value::Null,
    }
}

fn settings_json(s: &FnsSettings, updated_at: &Value, info: Value) -> Value {
    json!({"settings": s, "defaults": FnsSettings::default(), "updated_at": updated_at, "info": info, "sources_info": {
            "egrul": "ЕГРЮЛ/ЕГРИП (egrul.nalog.ru): статус, ликвидация, дата регистрации",
            "data_nalog": "Открытые данные ФНС (file.nalog.ru, набор debtam): задолженность перед бюджетом, публикуется раз в год",
            "fedresurs": "ЕФРСБ: признаки банкротства; нужен логин оператора на сервере"}})
}

/// GET /api/v1/companies/{cid}/fns/settings -> {settings, defaults, updated_at}
pub async fn get_settings(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/settings/fns"), &with_actor(&ctx, &p)).await?;
    let s: FnsSettings = serde_json::from_value(v["value"].clone()).unwrap_or_default();
    Ok(Json(settings_json(&s, &v["updated_at"], fns_info(&st, &ctx).await)))
}

/// PUT /api/v1/companies/{cid}/fns/settings {FnsSettings} (admin) -> {settings, defaults, updated_at}
pub async fn put_settings(State(st): State<AppState>, Administrator(p): Administrator, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<Value>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let s: FnsSettings = serde_json::from_value(b).map_err(|e| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_settings", format!("Настройки ФНС: {e}")))?;
    s.validate()?;
    let actx = with_actor(&ctx, &p);
    let r = st.ledger.json(Method::PUT, &format!("/internal/v1/companies/{cid}/settings/fns"), &actx, Some(&json!(s))).await?;
    if !r.status.is_success() {
        return Err(err(StatusCode::BAD_GATEWAY, "settings_not_saved", "Настройки не сохранены"));
    }
    // флаги риска пересчитываются под новые правила сразу (без ИНН компании или без сервиса ФНС — при следующем открытии)
    if !p.company_inn.trim().is_empty() {
        let _ = counterparties_view(&st, &actx, &p, None).await;
    }
    audit(&st, &ctx, &p, "fns.settings", "").await;
    Ok(Json(settings_json(&s, &r.json()["updated_at"], fns_info(&st, &ctx).await)))
}

fn stringify(v: Option<i64>) -> Value {
    v.map_or(Value::Null, |x| json!(x.to_string()))
}

#[derive(Debug, Clone)]
struct Obligation {
    kind: String,
    date: NaiveDate,
    amount: Option<i64>,
    title: Option<String>,
}

fn parse_obligations(v: &Value) -> Vec<Obligation> {
    let arr = ["items", "obligations", "events"].iter().find_map(|k| v[*k].as_array()).cloned().unwrap_or_default();
    let mut seen = HashSet::new();
    arr.iter()
        .filter_map(|it| {
            let kind = str_of(it, &["kind", "type", "code"])?.to_lowercase();
            let date = date_of(it, &["date", "due_date", "due"])?;
            let amount = first_kop(it, &["amount_kop", "amount"]).filter(|a| *a > 0);
            let title = str_of(it, &["title", "name"]).map(|s| s.chars().take(120).collect());
            seen.insert((kind.clone(), date)).then_some(Obligation { kind, date, amount, title })
        })
        .collect()
}

/// Сальдо ЕНС: положительное — переплата (деньги на счёте), отрицательное — долг; `debt_kop` — задолженность (недоимка).
#[derive(Debug, Clone)]
struct Ens {
    balance: Option<i64>,
    debt: i64,
    upcoming: Vec<Obligation>,
    checked_at: Option<String>,
    source: String,
}

fn parse_ens(v: &Value) -> Ens {
    let balance = first_kop(v, &["balance_kop", "saldo_kop", "balance", "saldo"]);
    let debt = first_kop(v, &["debt_kop", "arrears_kop", "debt", "arrears"])
        .or_else(|| first_kop(&v["debt"], &["amount_kop", "amount"]))
        .unwrap_or_else(|| balance.filter(|b| *b < 0).map_or(0, |b| -b))
        .max(0);
    let upcoming = ["upcoming", "payments", "next_payments", "notices"]
        .iter()
        .find_map(|k| v[*k].as_array())
        .map(|a| {
            a.iter()
                .filter_map(|it| {
                    Some(Obligation {
                        kind: str_of(it, &["kind", "type", "code"]).unwrap_or("enp").to_lowercase(),
                        date: date_of(it, &["date", "due_date", "due"])?,
                        amount: first_kop(it, &["amount_kop", "amount"]).filter(|a| *a > 0),
                        title: str_of(it, &["title", "name"]).map(|s| s.chars().take(120).collect()),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    Ens { balance, debt, upcoming, checked_at: str_of(v, &["checked_at", "as_of", "updated_at"]).map(str::to_string), source: source_of(v) }
}

fn ens_json(e: &Ens) -> Value {
    json!({
        "balance_kop": stringify(e.balance), "debt_kop": e.debt.to_string(), "has_debt": e.debt > 0,
        "upcoming": e.upcoming.iter().map(|o| json!({"kind": o.kind, "date": o.date, "amount_kop": stringify(o.amount), "title": o.title})).collect::<Vec<_>>(),
        "checked_at": e.checked_at, "source": e.source, "source_label": SOURCE_LABEL,
    })
}

/// Статус контрагента в едином виде: active | liquidating | liquidated | bankrupt | not_found | unknown.
fn norm_status(s: &str) -> &'static str {
    let s = s.trim().to_lowercase();
    if s.contains("not_found") || s.contains("не найден") {
        "not_found"
    } else if s.contains("банкрот") || s.contains("bankrupt") {
        "bankrupt"
    } else if s.contains("ликвидируется") || s.contains("liquidating") || s.contains("in_liquidation") {
        "liquidating"
    } else if s.contains("ликвидирован") || s.contains("liquidated") || s.contains("excluded") {
        "liquidated"
    } else if s.contains("действ") || s.contains("active") {
        "active"
    } else {
        "unknown"
    }
}

#[derive(Debug, Clone)]
struct Cp {
    inn: String,
    status: &'static str,
    risk_level: &'static str,
    tax_debt: i64,
    bankruptcy: bool,
    unreliable: bool,
    mass_address: bool,
    disqualified: bool,
    registered_at: Option<String>,
    /// дата, на которую опубликованы сведения о задолженности (открытые данные ФНС)
    tax_debt_as_of: Option<String>,
    checked_at: Option<String>,
    source: String,
    /// причины риска по настройкам компании (коды fns: tax_debt_large, liquidated, ...)
    reasons: Vec<String>,
    /// по каким источникам сведения получены (`ok`), выключены (`off`) или недоступны
    sources: Value,
    /// данные из просроченного кэша: источник сейчас недоступен
    stale: bool,
    /// сведения получены хотя бы от одного источника (иначе проверка не состоялась)
    usable: bool,
}

fn flag(v: &Value) -> bool {
    match v {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_i64().unwrap_or(0) != 0,
        Value::String(s) => matches!(s.to_lowercase().as_str(), "true" | "yes" | "да" | "1"),
        _ => false,
    }
}

fn parse_cp(v: &Value, fallback_inn: Option<&str>) -> Option<Cp> {
    let inn = str_of(v, &["inn"]).or(fallback_inn)?.to_string();
    if !valid_inn(&inn) {
        return None;
    }
    let tax_debt = first_kop(v, &["tax_debt_kop", "debt_kop"])
        .or_else(|| first_kop(&v["tax_debt"], &["amount_kop", "amount"]))
        .unwrap_or(0)
        .max(0);
    let bankruptcy = flag(&v["bankruptcy"]["flag"]) || flag(&v["bankruptcy_flag"]) || flag(&v["bankruptcy"]);
    let status = if bankruptcy { "bankrupt" } else { norm_status(str_of(v, &["status"]).unwrap_or("")) };
    let (unreliable, mass_address, disqualified) = (flag(&v["unreliable"]), flag(&v["mass_address"]), flag(&v["disqualified"]));
    // уровень риска из fns; если не передан — по признакам
    let usable = str_of(v, &["source"]) != Some("unavailable") && str_of(v, &["status"]) != Some("invalid_inn");
    let risk_level = match str_of(v, &["risk_level", "risk"]).map(str::to_lowercase).as_deref() {
        Some("high") => "high",
        Some("medium") => "medium",
        Some("low") => "low",
        Some("unknown") => "unknown",
        _ if matches!(status, "bankrupt" | "liquidating" | "liquidated") || tax_debt > 0 && disqualified => "high",
        _ if tax_debt > 0 || unreliable || mass_address || disqualified => "medium",
        _ => "low",
    };
    let source = source_of(v);
    let reasons = v["risk_reasons"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default();
    Some(Cp {
        inn, status, risk_level, tax_debt, bankruptcy, unreliable, mass_address, disqualified,
        registered_at: str_of(v, &["registered_at", "registration_date"]).map(str::to_string),
        tax_debt_as_of: str_of(&v["tax_debt"], &["as_of"]).map(str::to_string),
        checked_at: str_of(v, &["checked_at"]).map(str::to_string),
        source, reasons, sources: v["sources"].clone(), stale: flag(&v["stale"]), usable,
    })
}

/// Результаты пакетной проверки: массив `items`/`results` или объект «ИНН -> сведения».
fn parse_batch(v: &Value) -> Vec<Cp> {
    if let Some(a) = ["items", "results", "counterparties"].iter().find_map(|k| v[*k].as_array()) {
        return a.iter().filter_map(|x| parse_cp(x, None)).collect();
    }
    let obj = ["items", "results", "counterparties"].iter().find_map(|k| v[*k].as_object());
    obj.map(|m| m.iter().filter_map(|(inn, x)| parse_cp(x, Some(inn))).collect()).unwrap_or_default()
}

// ---------- обращение к fns ----------
fn fns_down(msg: &str) -> ApiError {
    err(StatusCode::SERVICE_UNAVAILABLE, "fns_unavailable", msg)
}

/// Запрос к fns: недоступность и ошибки источника -> 503 с понятным кодом, ошибки запроса передаются как есть.
async fn fns_call(up: &Upstream, method: Method, path: &str, ctx: &Ctx, body: Option<&Value>) -> R<Value> {
    let r = up.json(method, path, ctx, body).await?;
    if r.status.is_success() {
        return Ok(r.json());
    }
    let v = r.json();
    let code = v["error"]["code"].as_str().or_else(|| v["detail"]["code"].as_str());
    let msg = v["error"]["message"].as_str().or_else(|| v["detail"]["message"].as_str());
    if r.status.is_server_error() || r.status == StatusCode::TOO_MANY_REQUESTS {
        return Err(err(
            StatusCode::SERVICE_UNAVAILABLE,
            code.filter(|c| c.starts_with("fns_")).unwrap_or("fns_unavailable"),
            msg.unwrap_or("Сервис ФНС временно недоступен, повторите позже"),
        ));
    }
    match (code, msg) {
        (Some(c), m) => Err(err(r.status, c, m.unwrap_or("Ошибка запроса к ФНС"))),
        _ => Err(fns_down("Сервис ФНС вернул неожиданный ответ")),
    }
}

async fn ledger_snapshot(st: &AppState, ctx: &Ctx, cid: &str) -> R<Snapshot> {
    let v = st.ledger.get(&format!("/internal/v1/companies/{cid}/snapshot"), ctx).await?;
    serde_json::from_value(v).map_err(internal)
}

/// Подсказка режима налогообложения из подтверждённых импортов (`stats.tax_regime_guess` parser); не критично, ошибки игнорируются.
async fn regime_from_imports(st: &AppState, ctx: &Ctx, cid: &str) -> Option<String> {
    let list = st.ledger.get(&format!("/internal/v1/companies/{cid}/imports"), ctx).await.ok()?;
    let ids: Vec<String> = list["items"]
        .as_array()?
        .iter()
        .filter(|i| i["status"].as_str() == Some("confirmed"))
        .filter_map(|i| i["import_id"].as_str().map(str::to_string))
        .take(3)
        .collect();
    for id in ids {
        let Ok(v) = st.ledger.get(&format!("/internal/v1/companies/{cid}/imports/{id}"), ctx).await else { continue };
        if let Some(r) = v["summary"]["stats"]["tax_regime_guess"]["regime"].as_str().filter(|r| REGIMES.contains(r)) {
            return Some(r.to_string());
        }
    }
    None
}

// ---------- события из ФНС ----------
fn kind_title(kind: &str) -> &'static str {
    if kind.contains("usn") && (kind.contains("annual") || kind.contains("year")) {
        "УСН за год"
    } else if kind.contains("usn") {
        "Аванс по УСН"
    } else if kind.contains("vat") || kind.contains("ндс") {
        "НДС"
    } else if kind.contains("insurance") || kind.contains("contrib") || kind.contains("взнос") {
        "Страховые взносы"
    } else if kind.contains("ndfl") {
        "НДФЛ"
    } else if kind.contains("enp") || kind.contains("ens") {
        "ЕНП"
    } else {
        "Налоговый платёж"
    }
}

/// Оценка суммы по выписке для вида платежа; None — по выписке оценить нельзя (годовой налог, взносы с дохода).
fn estimate_for(kind: &str, regime: &str, txs: &[Tx], base: NaiveDate) -> Option<i64> {
    if kind.contains("usn") {
        if kind.contains("annual") || kind.contains("year") {
            return None;
        }
        let ie = regime == "usn_income_expense";
        estimate_usn(txs, base, if ie { 15.0 } else { 6.0 }, ie)
    } else if kind.contains("vat") || kind.contains("ндс") {
        estimate_vat_installment(txs, base)
    } else if kind.contains("enp") || kind.contains("ens") {
        estimate_tax(txs, base)
    } else {
        None
    }
}

#[derive(Deserialize, Default)]
pub struct SyncBody {
    /// налоговый год, по умолчанию — год последней операции выписки
    #[serde(default)]
    year: Option<i32>,
    /// режим налогообложения: osno | usn_income | usn_income_expense | eshn | patent; по умолчанию — по выписке
    #[serde(default)]
    regime: Option<String>,
}

fn event_title(base: &str, estimated: bool) -> String {
    let mut t = format!("{base} · {SOURCE_LABEL}");
    if estimated {
        t.push_str(" · оценка по выписке");
    }
    t
}

/// Собирает события для ledger из обязательств fns. Сумма: из fns, иначе из ближайших платежей ЕНС, иначе оценка по выписке.
/// Возвращает (события, превью для ответа, число оценённых, число без суммы).
fn build_events(obls: &[Obligation], ens: Option<&Ens>, regime: &str, txs: &[Tx], base: NaiveDate) -> (Vec<Value>, Vec<Value>, usize, usize) {
    let (mut events, mut preview, mut estimated, mut no_amount) = (vec![], vec![], 0, 0);
    for o in obls.iter().filter(|o| o.date > base) {
        let from_ens = ens.and_then(|e| e.upcoming.iter().find(|u| u.date == o.date && (u.kind == o.kind || u.kind == "enp")).and_then(|u| u.amount));
        let (amount, is_est) = match o.amount.or(from_ens) {
            Some(a) => (a, false),
            None => match estimate_for(&o.kind, regime, txs, base) {
                Some(a) => (a, true),
                None => {
                    no_amount += 1;
                    continue;
                }
            },
        };
        if is_est {
            estimated += 1;
        }
        let name = o.title.clone().unwrap_or_else(|| kind_title(&o.kind).to_string());
        let title = event_title(&name, is_est);
        events.push(json!({
            "external_id": format!("fns:{}:{}", o.kind, o.date), "date": o.date, "amount_kop": -amount, "title": title,
            "counterparty": COUNTERPARTY_NAME, "source": "tax", "confidence": if is_est { 0.9 } else { 1.0 },
        }));
        preview.push(json!({"kind": o.kind, "title": title, "date": o.date, "amount_kop": amount.to_string(), "estimated": is_est}));
    }
    (events, preview, estimated, no_amount)
}

/// POST /api/v1/companies/{cid}/integrations/fns/sync {year?, regime?} -> {created, skipped, estimated, no_amount, year, regime, regime_source, source, fetched_at, items, debt_kop}
/// Налоговые события на год из ФНС (сроки — сервис fns, суммы — ФНС/ЕНС или оценка по выписке) -> ожидаемые события ledger, идемпотентно.
pub async fn sync(State(st): State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>, body: Option<Json<SyncBody>>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let inn = company_inn(&p)?;
    let b = body.map(|b| b.0).unwrap_or_default();
    if let Some(r) = b.regime.as_deref().filter(|r| !REGIMES.contains(r)) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_regime", format!("Режим налогообложения: {} (получено {r})", REGIMES.join(" | "))));
    }
    if b.year.is_some_and(|y| !(2020..=2100).contains(&y)) {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_year", "Год от 2020 до 2100"));
    }
    let actx = with_actor(&ctx, &p);
    let snap = ledger_snapshot(&st, &actx, &cid).await?;
    let txs: Vec<Tx> = snap.transactions.iter().map(|r| r.tx.clone()).collect();
    let base = txs.iter().map(|t| t.date).max().unwrap_or_else(|| chrono::Local::now().date_naive());
    let year = b.year.unwrap_or(base.year());
    let (regime, regime_source) = match b.regime {
        Some(r) => (r, "request"),
        None => match regime_from_imports(&st, &actx, &cid).await {
            Some(r) => (r, "statement"),
            None => ("usn_income".to_string(), "default"),
        },
    };

    let v = fns_call(&st.fns, Method::GET, &format!("/internal/v1/fns/obligations?inn={inn}&regime={regime}&year={year}"), &actx, None).await?;
    let obls = parse_obligations(&v);
    let source = source_of(&v);
    // ЕНС нужен для сумм и недоимки; без него синхронизация всё равно работает (оценка по выписке)
    let ens = match fns_call(&st.fns, Method::GET, &format!("/internal/v1/fns/ens?inn={inn}"), &actx, None).await {
        Ok(e) => Some(parse_ens(&e)),
        Err(_) => None,
    };

    let (mut events, preview, estimated, no_amount) = build_events(&obls, ens.as_ref(), &regime, &txs, base);
    // недоимка на ЕНС — просроченное обязательство; ключ по сумме: повторная синхронизация с той же недоимкой ничего не дублирует
    let mut debt_kop = None;
    if let Some(e) = ens.as_ref().filter(|e| e.debt > 0) {
        debt_kop = Some(e.debt);
        events.push(json!({
            "external_id": format!("fns:ens_debt:{}", e.debt), "date": base, "amount_kop": -e.debt,
            "title": event_title("Задолженность по ЕНС", false), "counterparty": COUNTERPARTY_NAME, "source": "tax", "confidence": 0.95,
        }));
    }
    let (mut created, mut skipped) = (0, 0);
    if !events.is_empty() {
        let r = st.ledger.post(&format!("/internal/v1/companies/{cid}/expected-events/bulk"), &actx, &json!({"events": events})).await?;
        created = r["created"].as_u64().unwrap_or(0);
        skipped = r["skipped"].as_u64().unwrap_or(0);
    }
    audit(&st, &ctx, &p, "integrations.fns.sync", &format!("{year}")).await;
    Ok(Json(json!({
        "created": created, "skipped": skipped, "estimated": estimated, "no_amount": no_amount, "year": year,
        "regime": regime, "regime_source": regime_source, "source": source, "source_label": SOURCE_LABEL,
        "fetched_at": chrono::Utc::now().to_rfc3339(), "items": preview, "debt_kop": stringify(debt_kop),
    })))
}

// ---------- ЕНС ----------
async fn ens_view(st: &AppState, ctx: &Ctx, p: &Principal) -> R<Value> {
    let inn = company_inn(p)?;
    let v = fns_call(&st.fns, Method::GET, &format!("/internal/v1/fns/ens?inn={inn}"), ctx, None).await?;
    Ok(ens_json(&parse_ens(&v)))
}

/// GET /api/v1/companies/{cid}/fns/ens -> {balance_kop, debt_kop, has_debt, upcoming[], checked_at, source, source_label, mode}
pub async fn ens(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    Ok(Json(ens_view(&st, &with_actor(&ctx, &p), &p).await?))
}

/// GET /api/ext/v1/fns/ens (токен, read)
pub async fn ext_ens(State(st): State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx) -> R<Json<Value>> {
    Ok(Json(ens_view(&st, &with_actor(&ctx, &p), &p).await?))
}

#[derive(Deserialize)]
pub struct ImportBody {
    #[serde(default)]
    filename: Option<String>,
    /// содержимое файла справки (XML КНД 1160080 или CSV) в base64: кодировка XML бывает windows-1251
    content_b64: String,
}

/// POST /api/v1/companies/{cid}/fns/ens/import {filename?, content_b64} -> ЕНС из справки + {format, warnings}.
/// Справку владелец выгружает из личного кабинета сам; вход в кабинет по ЭЦП мы не автоматизируем (docs/fns.md).
pub async fn ens_import(State(st): State<AppState>, Editor(p): Editor, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<ImportBody>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let inn = company_inn(&p)?;
    if b.content_b64.trim().is_empty() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "fns_import_bad_file", "Выберите файл справки"));
    }
    let actx = with_actor(&ctx, &p);
    let body = json!({"filename": b.filename.unwrap_or_default().chars().take(200).collect::<String>(), "content_b64": b.content_b64});
    let v = fns_call(&st.fns, Method::POST, &format!("/internal/v1/fns/ens/import?inn={inn}"), &actx, Some(&body)).await?;
    audit(&st, &ctx, &p, "integrations.fns.ens_import", v["format"].as_str().unwrap_or("")).await;
    let mut out = ens_json(&parse_ens(&v));
    out["format"] = v["format"].clone();
    out["warnings"] = v["warnings"].clone();
    Ok(Json(out))
}

// ---------- контрагенты ----------
#[derive(Deserialize)]
pub struct TopQuery {
    #[serde(default)]
    top: Option<String>,
}

/// `top` из запроса; не задан — из настроек компании.
fn parse_top(q: &TopQuery) -> R<Option<usize>> {
    match q.top.as_deref().map(str::trim) {
        None | Some("") => Ok(None),
        Some(s) => s.parse::<usize>().ok().filter(|n| (1..=MAX_TOP).contains(n)).map(Some).ok_or_else(|| err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_top", "top: число от 1 до 50")),
    }
}

/// Контрагент проходит фильтр настроек: из выписки и/или из ожидаемых событий (1С).
fn shown(row: &Value, s: &FnsSettings) -> bool {
    let has = |k: &str| row["origin"].as_array().is_some_and(|a| a.iter().any(|x| x == k));
    let statement = !row["origin"].is_array() || has("statement");
    (s.show_statement && statement) || (s.show_expected && has("expected"))
}

/// Топ контрагентов компании (ledger) -> проверка в fns по настройкам компании (пакетом, кэш на стороне fns) -> флаги в ledger
/// -> ответ с источником, датой сведений и временем проверки. `top` не задан — из настроек.
pub(crate) async fn counterparties_view(st: &AppState, ctx: &Ctx, p: &Principal, top: Option<usize>) -> R<Value> {
    company_inn(p)?; // компания без ИНН не может пользоваться ФНС-функциями: единое сообщение для всех маршрутов
    let cid = &p.company_id;
    let cfg = load_settings(st, ctx, cid).await?;
    let top = top.unwrap_or(cfg.top);
    // при выключенном виде контрагентов берём с запасом, чтобы после фильтра осталось `top`
    let limit = if cfg.show_statement && cfg.show_expected { top } else { (top * 3).min(MAX_TOP * 2) };
    let t = st.ledger.get(&format!("/internal/v1/companies/{cid}/counterparties/top?limit={limit}"), ctx).await?;
    let rows: Vec<Value> = t["items"].as_array().cloned().unwrap_or_default().into_iter().filter(|r| shown(r, &cfg)).take(top).collect();
    let now = chrono::Utc::now().to_rfc3339();
    if rows.is_empty() {
        return Ok(json!({"items": [], "checked_at": now, "source": null, "source_label": SOURCE_LABEL, "exported_from": "fns", "saved": true, "total_income_kop": "0", "settings": cfg}));
    }
    let own = p.company_inn.trim();
    let inns: Vec<String> = rows.iter().filter_map(|r| r["inn"].as_str()).filter(|i| valid_inn(i) && *i != own).map(str::to_string).collect();
    let body = json!({"inns": inns, "settings": {"sources": cfg.sources, "risk": cfg.risk}});
    let v = fns_call(&st.fns, Method::POST, "/internal/v1/fns/counterparties/batch", ctx, Some(&body)).await?;
    let checked: BTreeMap<String, Cp> = parse_batch(&v).into_iter().filter(|c| c.usable).map(|c| (c.inn.clone(), c)).collect();
    // источник — по самим проверкам (у пакета верхнего `source` нет)
    let batch_source = checked.values().next().map_or_else(|| source_of(&v), |c| c.source.clone());
    // дата, на которую ФНС опубликовала сведения о задолженности (одна на весь набор debtam)
    let debt_as_of = checked.values().find_map(|c| c.tax_debt_as_of.clone());

    // флаги в ledger: от них зависит прогноз (ошибка записи не мешает показать результат). Учёт риска выключен —
    // флаги нейтральные: прогноз ведёт себя так, будто проверки не было.
    let flags: Vec<Value> = checked
        .values()
        .map(|c| {
            let at = c.checked_at.clone().unwrap_or_else(|| now.clone());
            if cfg.use_in_forecast {
                json!({"inn": c.inn, "risk_level": c.risk_level, "status": c.status, "tax_debt_kop": c.tax_debt, "bankruptcy_flag": c.bankruptcy, "checked_at": at, "source": c.source})
            } else {
                json!({"inn": c.inn, "risk_level": "low", "status": "", "tax_debt_kop": 0, "bankruptcy_flag": false, "checked_at": at, "source": c.source})
            }
        })
        .collect();
    let saved = flags.is_empty() || st.ledger.json(Method::PUT, &format!("/internal/v1/companies/{cid}/counterparty-risk"), ctx, Some(&json!({"items": flags}))).await.is_ok_and(|r| r.status.is_success());

    let items: Vec<Value> = rows
        .iter()
        .map(|r| {
            let inn = r["inn"].as_str().unwrap_or_default();
            let mut o = Map::new();
            o.insert("inn".into(), json!(inn));
            o.insert("name".into(), r["name"].clone());
            for (k, src) in [("turnover_kop", "turnover_kop"), ("income_kop", "income_kop"), ("expense_kop", "expense_kop")] {
                o.insert(k.into(), stringify(kop_of(&r[src])));
            }
            o.insert("income_share".into(), r["income_share"].clone());
            o.insert("tx_count".into(), r["tx_count"].clone());
            // откуда контрагент: выписка и/или ожидаемые события (счета 1С)
            o.insert("origin".into(), if r["origin"].is_array() { r["origin"].clone() } else { json!(["statement"]) });
            if r["origin"].as_array().is_some_and(|a| a.iter().any(|x| x == "expected")) {
                o.insert("expected_in_kop".into(), stringify(kop_of(&r["expected_in_kop"])));
                o.insert("expected_out_kop".into(), stringify(kop_of(&r["expected_out_kop"])));
                o.insert("expected_share".into(), r["expected_share"].clone());
            }
            match checked.get(inn) {
                Some(c) => {
                    o.insert("checked".into(), json!(true));
                    o.insert("status".into(), json!(c.status));
                    o.insert("risk_level".into(), json!(c.risk_level));
                    o.insert("tax_debt_kop".into(), json!(c.tax_debt.to_string()));
                    o.insert("has_tax_debt".into(), json!(c.tax_debt > 0));
                    o.insert("bankruptcy_flag".into(), json!(c.bankruptcy));
                    o.insert("unreliable".into(), json!(c.unreliable));
                    o.insert("mass_address".into(), json!(c.mass_address));
                    o.insert("disqualified".into(), json!(c.disqualified));
                    o.insert("registered_at".into(), json!(c.registered_at));
                    o.insert("tax_debt_as_of".into(), json!(c.tax_debt_as_of));
                    o.insert("checked_at".into(), json!(c.checked_at.clone().unwrap_or_else(|| now.clone())));
                    o.insert("source".into(), json!(c.source));
                    o.insert("risk_reasons".into(), json!(c.reasons));
                    o.insert("sources".into(), c.sources.clone());
                    o.insert("stale".into(), json!(c.stale));
                }
                None => {
                    o.insert("checked".into(), json!(false));
                    o.insert("status".into(), json!("unknown"));
                    o.insert("risk_level".into(), json!("unknown"));
                    o.insert("checked_at".into(), Value::Null);
                    o.insert("source".into(), Value::Null);
                }
            }
            Value::Object(o)
        })
        .collect();
    Ok(json!({
        "items": items, "checked_at": now, "source": batch_source, "source_label": SOURCE_LABEL, "exported_from": "fns", "debt_as_of": debt_as_of,
        "saved": saved, "settings": cfg,
        "total_income_kop": stringify(kop_of(&t["total_income_kop"])),
    }))
}

/// GET /api/v1/companies/{cid}/fns/counterparties[?top=] -> {items[], checked_at, source, source_label, exported_from, debt_as_of, saved, settings}
pub async fn counterparties(State(st): State<AppState>, Reader(p): Reader, ctx: Ctx, Path(cid): Path<String>, Query(q): Query<TopQuery>) -> R<Json<Value>> {
    authorize(&p, &cid)?;
    let top = parse_top(&q)?;
    Ok(Json(counterparties_view(&st, &with_actor(&ctx, &p), &p, top).await?))
}

/// GET /api/ext/v1/fns/counterparties?top=20 (токен, read)
pub async fn ext_counterparties(State(st): State<AppState>, TokenPrincipal(p): TokenPrincipal, ctx: Ctx, Query(q): Query<TopQuery>) -> R<Json<Value>> {
    let top = parse_top(&q)?;
    Ok(Json(counterparties_view(&st, &with_actor(&ctx, &p), &p, top).await?))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    fn rev(date: &str, amount: i64) -> Tx {
        Tx { date: d(date), amount, counterparty: "Клиент".into(), inn: String::new(), purpose: String::new(), category: "revenue".into() }
    }

    #[test]
    fn obligations_are_parsed_tolerantly_and_deduplicated() {
        let v = json!({"obligations": [
            {"kind": "ENP", "due_date": "2026-11-30", "amount": "150000"},
            {"type": "usn_advance", "date": "2027-04-28", "amount_kop": 9000000, "title": "Аванс УСН за I квартал"},
            {"kind": "enp", "date": "2026-11-30"},
            {"kind": "bad", "date": "не дата"},
            {"date": "2026-12-28"},
        ]});
        let o = parse_obligations(&v);
        assert_eq!(o.len(), 2, "{o:?}");
        assert_eq!((o[0].kind.as_str(), o[0].date, o[0].amount), ("enp", d("2026-11-30"), Some(150_000)));
        assert_eq!((o[1].kind.as_str(), o[1].amount, o[1].title.as_deref()), ("usn_advance", Some(9_000_000), Some("Аванс УСН за I квартал")));
        assert!(parse_obligations(&json!({})).is_empty());
    }

    #[test]
    fn events_take_amount_from_fns_then_ens_then_statement_estimate() {
        let txs = vec![rev("2026-08-10", 10_000_000), rev("2026-09-10", 20_000_000)];
        let base = d("2026-09-30");
        let obls = vec![
            Obligation { kind: "usn_advance".into(), date: d("2026-10-28"), amount: Some(777), title: None },
            Obligation { kind: "enp".into(), date: d("2026-11-30"), amount: None, title: None },
            Obligation { kind: "usn_advance".into(), date: d("2027-04-28"), amount: None, title: None },
            Obligation { kind: "usn_annual".into(), date: d("2027-03-31"), amount: None, title: None },
            Obligation { kind: "enp".into(), date: d("2026-09-28"), amount: Some(5), title: None },
        ];
        let ens = Ens { balance: Some(0), debt: 0, upcoming: vec![Obligation { kind: "enp".into(), date: d("2026-11-30"), amount: Some(123_000), title: None }], checked_at: None, source: "import".into() };
        let (ev, preview, estimated, no_amount) = build_events(&obls, Some(&ens), "usn_income", &txs, base);
        // прошедший срок (до конца выписки) пропущен; годовой налог без суммы — не оценивается
        assert_eq!((ev.len(), estimated, no_amount), (3, 1, 1), "{ev:?}");
        assert_eq!((ev[0]["amount_kop"].as_i64(), ev[0]["external_id"].as_str()), (Some(-777), Some("fns:usn_advance:2026-10-28")));
        assert_eq!(ev[1]["amount_kop"].as_i64(), Some(-123_000), "сумма из ЕНС");
        // 6 % от поступлений за квартал: 30 000 000 × 6 % = 1 800 000
        assert_eq!(ev[2]["amount_kop"].as_i64(), Some(-1_800_000));
        assert_eq!(ev[2]["confidence"].as_f64(), Some(0.9));
        for e in &ev {
            assert_eq!((e["source"].as_str(), e["counterparty"].as_str()), (Some("tax"), Some("ФНС России")));
            assert!(e["title"].as_str().unwrap().contains("· ФНС") && !e["title"].as_str().unwrap().contains("имитац"));
        }
        assert!(ev[2]["title"].as_str().unwrap().contains("оценка по выписке") && !ev[0]["title"].as_str().unwrap().contains("оценка"));
        assert_eq!((preview[2]["estimated"].as_bool(), preview[0]["estimated"].as_bool()), (Some(true), Some(false)));
        let (one, _, _, _) = build_events(&obls[..1], None, "usn_income", &txs, base);
        assert_eq!(one[0]["title"], "Аванс по УСН · ФНС");
    }

    #[test]
    fn ens_is_parsed_with_debt_and_upcoming() {
        let e = parse_ens(&json!({"balance_kop": -2500000, "upcoming": [{"date": "2026-11-30", "amount_kop": 1200000, "kind": "enp"}, {"date": "bad"}], "checked_at": "2026-10-24T09:00:00Z", "source": "import"}));
        assert_eq!((e.balance, e.debt, e.upcoming.len(), e.source.as_str()), (Some(-2_500_000), 2_500_000, 1, "import"));
        let e = parse_ens(&json!({"balance_kop": "700", "debt_kop": "0", "source": "import"}));
        assert_eq!((e.balance, e.debt), (Some(700), 0));
        let j = ens_json(&parse_ens(&json!({"debt": {"amount_kop": 5}})));
        assert_eq!((j["debt_kop"].as_str(), j["has_debt"].as_bool(), j["source_label"].as_str()), (Some("5"), Some(true), Some("ФНС")));
    }

    #[test]
    fn counterparty_is_normalized_and_risk_is_derived_when_missing() {
        let c = parse_cp(&json!({"inn": "7701234567", "status": "Действует", "tax_debt": {"has": true, "amount": 90000}, "unreliable": false, "source": "egrul", "risk_reasons": ["tax_debt"]}), None).unwrap();
        assert_eq!((c.status, c.risk_level, c.tax_debt, c.source.as_str(), c.reasons.as_slice()), ("active", "medium", 90_000, "egrul", ["tax_debt".to_string()].as_slice()));
        let c = parse_cp(&json!({"inn": "7701234568", "status": "Ликвидируется"}), None).unwrap();
        assert_eq!((c.status, c.risk_level), ("liquidating", "high"));
        let c = parse_cp(&json!({"inn": "7701234569", "status": "active", "bankruptcy": {"flag": true, "source": "efrsb"}}), None).unwrap();
        assert_eq!((c.status, c.bankruptcy, c.risk_level), ("bankrupt", true, "high"));
        let c = parse_cp(&json!({"status": "active", "risk_level": "low", "tax_debt_kop": 0}), Some("500100732259")).unwrap();
        assert_eq!((c.inn.as_str(), c.risk_level), ("500100732259", "low"));
        assert!(parse_cp(&json!({"inn": "123"}), None).is_none());
        assert_eq!(parse_batch(&json!({"results": {"7701234567": {"status": "active"}}})).len(), 1);
        assert_eq!(parse_batch(&json!({"items": [{"inn": "7701234567"}, {"inn": "bad"}]})).len(), 1);
    }

    #[test]
    fn top_param_is_validated() {
        let q = |s: Option<&str>| parse_top(&TopQuery { top: s.map(str::to_string) });
        assert_eq!(q(None).unwrap(), None, "из настроек компании");
        assert_eq!(q(Some("5")).unwrap(), Some(5));
        for bad in ["0", "51", "-1", "abc"] {
            assert_eq!(q(Some(bad)).err().map(|e| e.1).as_deref(), Some("invalid_top"), "{bad}");
        }
    }

    #[test]
    fn settings_defaults_partial_json_and_validation() {
        let d = FnsSettings::default();
        assert!(!d.risk.not_in_registry && d.use_in_forecast && d.check_after_sync && d.top == 20);
        let s: FnsSettings = serde_json::from_value(json!({"risk": {"not_in_registry": true}, "top": 5})).unwrap();
        assert!(s.risk.not_in_registry && s.risk.tax_debt && s.top == 5 && s.sources.egrul, "недостающие поля — по умолчанию");
        assert!(serde_json::from_value::<FnsSettings>(json!({"risk": {"bogus": true}})).is_err(), "неизвестные поля отклоняются");
        assert!(s.validate().is_ok());
        for bad in [json!({"top": 0}), json!({"top": 51}), json!({"show_statement": false, "show_expected": false}),
                    json!({"sources": {"egrul": false, "data_nalog": false, "fedresurs": false}}), json!({"risk": {"large_debt_kop": -1}})] {
            let s: FnsSettings = serde_json::from_value(bad.clone()).unwrap();
            assert_eq!(s.validate().err().map(|e| e.1).as_deref(), Some("invalid_settings"), "{bad}");
        }
    }

    #[test]
    fn rows_are_filtered_by_origin_settings() {
        let st = json!({"origin": ["statement"]});
        let onec = json!({"origin": ["expected", "onec"]});
        let both = json!({"origin": ["expected", "onec", "statement"]});
        let only_onec = FnsSettings { show_statement: false, ..Default::default() };
        let only_st = FnsSettings { show_expected: false, ..Default::default() };
        assert_eq!([shown(&st, &only_onec), shown(&onec, &only_onec), shown(&both, &only_onec)], [false, true, true]);
        assert_eq!([shown(&st, &only_st), shown(&onec, &only_st), shown(&json!({}), &only_st)], [true, false, true]);
    }
}
