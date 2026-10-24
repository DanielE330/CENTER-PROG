//! Финансовое ядро: регулярности, остаточный поток, траектории остатка, риск разрыва.
//!
//! Все суммы — целые копейки (i64, знак: + поступление, − списание).
//! Модель возвращает потоки; итоговый баланс собирается здесь, суммированием целых.
//!
//! balance_s(d) = balance_s(d-1) + регулярные/ожидаемые события_s(d) + остаточный поток_s(d)
//! Остаточный поток строится только из операций, НЕ вошедших в найденные регулярности,
//! поэтому зарплата не учитывается дважды.

use chrono::{Datelike, Duration, NaiveDate};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};

mod delays;
pub use delays::DelayModel;

pub const MODEL_VERSION: &str = "recurring-bootstrap/3";
const N_PATHS: usize = 1000;
/// Окно остаточного потока: столько последних валидных рабочих дней (≈120 календарных).
const RESIDUAL_BDAYS: usize = 86;
const RESIDUAL_MAX_CAL_DAYS: i64 = 300;
/// Пропуск выписки: календарный разрыв между соседними датами операций больше стольких дней.
const GAP_MIN_SPAN_DAYS: i64 = 5;
const DEFAULT_PAY_DELAY: [i64; 7] = [0, 0, 1, 2, 3, 5, 7];
const OVERDUE_IN_DELAY: [i64; 6] = [1, 2, 3, 5, 7, 10];
/// Максимальная длина «моста» от конца выписки до сегодняшнего дня; дальше выписка считается слишком старой.
pub const MAX_BRIDGE_DAYS: i64 = 60;

// Риск контрагента по данным ФНС (флаги приходят из ledger; без флагов расчёт не меняется ни на один бит).
/// Дополнительная задержка оплаты (рабочие дни) у клиента с высоким / средним риском.
const HIGH_RISK_EXTRA_DELAY_BD: i64 = 5;
const MEDIUM_RISK_EXTRA_DELAY_BD: i64 = 2;
/// Множитель уверенности в ожидаемом поступлении от клиента с высоким / средним риском.
const HIGH_RISK_CONFIDENCE: f64 = 0.85;
const MEDIUM_RISK_CONFIDENCE: f64 = 0.95;
/// Клиент попадает в предупреждение «крупный клиент в зоне риска», если его доля ожидаемых поступлений не меньше этой.
pub const RISKY_CLIENT_MIN_SHARE: f64 = 0.10;

pub use common::{CounterpartyRisk, Event, Tx};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Scenario {
    /// Сдвиг оплат от клиента (дней). Клиент — `delay_counterparty` или самый крупный по регулярным поступлениям.
    #[serde(default)]
    pub delay_days: i64,
    #[serde(default)]
    pub delay_counterparty: Option<String>,
    /// Падение выручки, %.
    #[serde(default)]
    pub revenue_drop_pct: f64,
    /// Рост регулярных расходов, %.
    #[serde(default)]
    pub fixed_cost_growth_pct: f64,
    /// Внеплановый расход (копейки, положительное число) и дата.
    #[serde(default)]
    pub extra_expense_kop: Option<i64>,
    #[serde(default)]
    pub extra_expense_date: Option<NaiveDate>,
    /// Дополнительное ожидаемое поступление (копейки, положительное число) и дата.
    #[serde(default)]
    pub extra_income_kop: Option<i64>,
    #[serde(default)]
    pub extra_income_date: Option<NaiveDate>,
}

impl Scenario {
    pub fn is_empty(&self) -> bool {
        self.delay_days == 0
            && self.revenue_drop_pct == 0.0
            && self.fixed_cost_growth_pct == 0.0
            && self.extra_expense_kop.is_none()
            && self.extra_income_kop.is_none()
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Rule {
    pub key: String,
    pub counterparty: String,
    pub category: String,
    pub direction: &'static str,
    /// "monthly" | "weekly"
    pub kind: &'static str,
    /// monthly: дни месяца; weekly: [день недели 0=пн]
    pub anchors: Vec<u32>,
    /// медианная сумма по якорям (подписанная)
    pub amount: i64,
    pub occurrences: usize,
    pub last_date: NaiveDate,
    pub confidence: f64,
    #[serde(skip)]
    amounts: Vec<Vec<i64>>,
    #[serde(skip)]
    jitters: Vec<Vec<i64>>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PlannedItem {
    pub date: NaiveDate,
    pub amount: i64,
    pub title: String,
    pub source: String,
    pub overdue: bool,
    /// уровень риска клиента по данным ФНС ("medium" | "high"), если он повлиял на оценку этого поступления
    #[serde(skip_serializing_if = "Option::is_none")]
    pub risk: Option<&'static str>,
    #[serde(skip)]
    pub cp: String,
}

/// Поступление, исключённое из плана: клиент ликвидируется, ликвидирован или признан банкротом (по данным ФНС).
#[derive(Clone, Debug, Serialize)]
pub struct ExcludedItem {
    pub date: NaiveDate,
    pub amount: i64,
    pub title: String,
    pub counterparty: String,
    /// "liquidation" | "bankruptcy"
    pub reason: &'static str,
    pub source: String,
}

/// Клиент в зоне риска и его доля в ожидаемых поступлениях горизонта.
#[derive(Clone, Debug, Serialize)]
pub struct RiskyClient {
    pub counterparty: String,
    pub inn: String,
    pub risk_level: String,
    pub status: String,
    pub tax_debt_kop: i64,
    /// доля в ожидаемых поступлениях (с учётом исключённых), 0..1
    pub share: f64,
    pub amount: i64,
    /// поступления от клиента исключены из плана
    pub excluded: bool,
}

/// Клиент, к которому применён сценарий задержки оплаты, и почему выбран он.
#[derive(Clone, Debug, Serialize)]
pub struct ScenarioClient {
    pub counterparty: String,
    /// "explicit" — назван в сценарии; "risk" — по умолчанию, крупный клиент в зоне риска; "largest" — по умолчанию, самый крупный
    pub reason: &'static str,
}

#[derive(Clone, Debug, Serialize)]
pub struct DayPoint {
    pub date: NaiveDate,
    pub p10: i64,
    pub p50: i64,
    pub p90: i64,
    pub prob_negative: f64,
    pub prob_broken_by: f64,
}

/// Драйвер разрыва: платёж (или неприход поступления) и его доля в давлении на остаток к дате разрыва.
#[derive(Clone, Debug, Serialize)]
pub struct Driver {
    pub date: NaiveDate,
    pub title: String,
    pub source: String,
    pub amount: i64,
    /// доля в сумме модулей «давящих» статей (расходы до даты разрыва + просроченные поступления), 0..1
    pub share: f64,
    /// true — поступление, которое не пришло в срок
    pub missing_income: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct Warning {
    pub code: String,
    pub severity: &'static str,
    pub date: Option<NaiveDate>,
    pub message: String,
    pub reasons: Vec<PlannedItem>,
    /// уровень разрыва: "warning" — остаток может уйти ниже нуля, "critical" — уход ниже нуля вероятен (>= 50 %) или есть на медианной траектории
    pub tier: Option<&'static str>,
    pub drivers: Vec<Driver>,
    /// для предупреждения `risky_client`
    #[serde(skip_serializing_if = "Option::is_none")]
    pub client: Option<RiskyClient>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Risk {
    pub level: &'static str,
    /// вероятность хотя бы одного отрицательного остатка за горизонт
    pub shortfall_probability: f64,
    /// медиана первой даты разрыва среди траекторий, где он случился
    pub first_break_date: Option<NaiveDate>,
    /// дата, к которой накопленная вероятность разрыва достигает 10 %
    pub possible_break_date: Option<NaiveDate>,
    /// максимальный дефицит на медианной траектории
    pub median_path_deficit: i64,
    /// 90-й перцентиль максимального дефицита по всем траекториям
    pub max_deficit_p90: i64,
    /// медиана максимального дефицита среди траекторий с разрывом
    pub deficit_given_break_median: i64,
    pub warnings: Vec<Warning>,
    /// «сырая» вероятность разрыва по доле траекторий (до калибровки); `shortfall_probability` — после неё
    pub raw_shortfall_probability: f64,
}

#[derive(Clone, Debug, Serialize)]
pub struct Quality {
    pub level: &'static str,
    pub history_days: i64,
    pub notes: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ForecastResult {
    pub as_of: NaiveDate,
    /// запрошенный горизонт в днях от сегодняшнего дня (или от конца выписки, если моста нет); в `days` ещё `bridge_days` точек впереди
    pub horizon: usize,
    /// дни между концом выписки и сегодняшним днём, оценённые моделью (первые `bridge_days` точек `days`)
    pub bridge_days: usize,
    pub opening_balance: i64,
    pub model_version: &'static str,
    pub seed: u64,
    pub n_paths: usize,
    pub quality: Quality,
    pub days: Vec<DayPoint>,
    pub risk: Risk,
    pub rules: Vec<Rule>,
    pub planned: Vec<PlannedItem>,
    /// поступления, исключённые из плана из-за ликвидации или банкротства клиента (по данным ФНС)
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub excluded: Vec<ExcludedItem>,
    /// кому применён сценарий задержки оплаты клиента
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scenario_client: Option<ScenarioClient>,
    /// признаки калибровки риска (см. `risk_features`); в JSON не выводятся
    #[serde(skip)]
    pub risk_feats: Vec<f64>,
}

#[derive(Debug)]
pub enum ForecastError {
    NoData,
    NoBalance,
}

/// Модель остаточного потока (то, что не вошло в регулярные платежи).
#[allow(dead_code)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResidualMode {
    /// Блочный bootstrap недель (выровнены по дню недели), только по рабочим дням.
    Block,
    /// Среднее по дню недели + bootstrap недельных сумм отклонений.
    WeekdayMean,
    /// То же, но уровень по дням недели масштабируется EWMA последних недель.
    Ewma,
}

#[allow(dead_code)]
impl ResidualMode {
    pub fn name(self) -> &'static str {
        match self {
            ResidualMode::Block => "block",
            ResidualMode::WeekdayMean => "weekday_mean",
            ResidualMode::Ewma => "ewma",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Params {
    pub horizon: usize,
    pub seed: u64,
    pub n_paths: usize,
    pub residual: ResidualMode,
    /// Доля статистического дрейфа (регулярные + остаточный поток), сохраняемая в медиане: 1 — весь,
    /// 0 — медиана остаётся на текущем остатке (наивный прогноз), разброс траекторий сохраняется.
    pub drift_keep: f64,
    /// Дополнительное экспоненциальное затухание дрейфа с горизонтом (дней); <= 0 — выключено.
    pub drift_tau: f64,
    /// Доля «циклической» формы (провалы/всплески внутри горизонта относительно линейного тренда), 1 — вся.
    pub shape_keep: f64,
    /// Возврат остатка к типичному уровню: доля разрыва, закрываемая за рабочий день (0 — выключено).
    pub revert: f64,
    /// Калибровать вероятность разрыва (см. `calibrate_risk`); false — отдавать долю траекторий как есть.
    pub calibrate: bool,
    /// Задержки оплат поступлений — по кривым Каплана–Мейера (см. `delays`); false — прежняя таблица `DEFAULT_PAY_DELAY`.
    pub km_delays: bool,
    /// Сегодняшняя дата: если выписка заканчивается раньше, прогноз продлевается «мостом» до этой даты
    /// (не более `MAX_BRIDGE_DAYS`), а `horizon` отсчитывается уже от неё. `None` — без моста (backtest, тесты).
    pub today: Option<NaiveDate>,
}

impl Default for Params {
    fn default() -> Self {
        Params { horizon: 30, seed: 42, n_paths: N_PATHS, residual: ResidualMode::Block, drift_keep: 0.0, drift_tau: 0.0, shape_keep: 0.5, revert: 0.015, calibrate: true, km_delays: true, today: None }
    }
}

// ---------- производственный календарь РФ (приближение) ----------
/// Рабочие субботы (переносы), встречающиеся в данных.
const WORK_SATURDAYS: [(i32, u32, u32); 3] = [(2024, 11, 2), (2024, 12, 28), (2025, 11, 1)];
/// Перенесённые выходные в будни (сверх фиксированных праздников).
const EXTRA_OFF: [(i32, u32, u32); 14] = [
    (2024, 11, 4),
    (2024, 12, 30),
    (2025, 5, 2),
    (2025, 5, 8),
    (2025, 6, 13),
    (2025, 11, 3),
    (2026, 1, 9),
    (2026, 3, 9),
    (2026, 5, 11),
    (2026, 12, 31),
    (2027, 5, 3),
    (2027, 5, 10),
    (2027, 6, 14),
    (2027, 12, 31),
];

pub fn is_business_day(d: NaiveDate) -> bool {
    let key = (d.year(), d.month(), d.day());
    if WORK_SATURDAYS.contains(&key) {
        return true;
    }
    if d.weekday().num_days_from_monday() >= 5 {
        return false;
    }
    if EXTRA_OFF.contains(&key) {
        return false;
    }
    let (m, day) = (d.month(), d.day());
    !((m == 1 && day <= 8) || (m == 2 && day == 23) || (m == 3 && day == 8) || (m == 5 && (day == 1 || day == 9)) || (m == 6 && day == 12) || (m == 11 && day == 4) || (m == 12 && day == 31))
}

/// Ближайший рабочий день не раньше d.
pub fn roll_forward(mut d: NaiveDate) -> NaiveDate {
    while !is_business_day(d) {
        d += Duration::days(1);
    }
    d
}

/// Сдвиг на n рабочих дней (n может быть отрицательным); d предполагается рабочим.
fn bd_add(mut d: NaiveDate, n: i64) -> NaiveDate {
    let (step, mut left) = if n >= 0 { (1, n) } else { (-1, -n) };
    while left > 0 {
        d += Duration::days(step);
        if is_business_day(d) {
            left -= 1;
        }
    }
    d
}

/// Число рабочих дней от a до b (со знаком): рабочие дни в (a, b] при b >= a.
fn bd_diff(a: NaiveDate, b: NaiveDate) -> i64 {
    let (lo, hi, sign) = if b >= a { (a, b, 1) } else { (b, a, -1) };
    let mut c = 0;
    let mut d = lo;
    while d < hi {
        d += Duration::days(1);
        if is_business_day(d) {
            c += 1;
        }
    }
    c * sign
}

/// Пропуски выписки: интервалы (первый..последний пропущенный день, включительно).
/// Кандидат — разрыв между соседними датами операций больше GAP_MIN_SPAN_DAYS календарных дней
/// и не меньше 3 пустых рабочих дней (длинные праздники — не пропуск). Если есть остатки и они
/// «сходятся» (остаток дня = прошлый остаток + операции дня), это тихий период, а не пропуск.
pub fn detect_gaps(txs: &[Tx], balances: Option<&BTreeMap<NaiveDate, i64>>) -> Vec<(NaiveDate, NaiveDate)> {
    let mut net: BTreeMap<NaiveDate, i64> = BTreeMap::new();
    for t in txs {
        *net.entry(t.date).or_default() += t.amount;
    }
    let dates: Vec<NaiveDate> = net.keys().copied().collect();
    gaps_between(&dates, &net, balances)
}

/// Пропуски между соседними датами `dates`; `net` — сумма операций дня (нужна для всех дат, кроме первой).
fn gaps_between(dates: &[NaiveDate], net: &BTreeMap<NaiveDate, i64>, balances: Option<&BTreeMap<NaiveDate, i64>>) -> Vec<(NaiveDate, NaiveDate)> {
    let mut out = vec![];
    for w in dates.windows(2) {
        let (p, n) = (w[0], w[1]);
        if (n - p).num_days() <= GAP_MIN_SPAN_DAYS {
            continue;
        }
        let empty_bdays = bd_diff(p, n - Duration::days(1));
        if empty_bdays < 3 {
            continue;
        }
        if let Some(b) = balances {
            let bp = b.range(..=p).next_back().map(|x| *x.1);
            let bn = b.range(..=n).next_back().map(|x| *x.1);
            if let (Some(bp), Some(bn)) = (bp, bn) {
                if (bn - bp - net[&n]).abs() <= 100 {
                    continue;
                }
            }
        }
        out.push((p + Duration::days(1), n - Duration::days(1)));
    }
    out
}

// ---------- RNG ----------
pub struct Rng(u64);
impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E3779B97F4A7C15) ^ 0xD1B54A32D192ED03)
    }
    pub fn next(&mut self) -> u64 {
        // splitmix64
        self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
    pub fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }
}

// ---------- helpers ----------
fn median_i64(v: &[i64]) -> i64 {
    if v.is_empty() {
        return 0;
    }
    let mut s = v.to_vec();
    s.sort_unstable();
    s[s.len() / 2]
}

fn quantile(sorted: &[i64], q: f64) -> i64 {
    if sorted.is_empty() {
        return 0;
    }
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx]
}

fn days_in_month(y: i32, m: u32) -> u32 {
    let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
    (NaiveDate::from_ymd_opt(ny, nm, 1).unwrap() - NaiveDate::from_ymd_opt(y, m, 1).unwrap()).num_days() as u32
}

fn month_idx(d: NaiveDate) -> i32 {
    d.year() * 12 + d.month0() as i32
}

fn rule_key(t: &Tx) -> String {
    let dir = if t.amount > 0 { "in" } else { "out" };
    let id = if !t.inn.is_empty() {
        t.inn.clone()
    } else if !t.counterparty.is_empty() {
        t.counterparty.to_lowercase()
    } else {
        let p: String = t.purpose.to_lowercase().chars().filter(|c| !c.is_ascii_digit()).take(40).collect();
        format!("p:{}", p.trim())
    };
    format!("{dir}:{id}")
}

/// Ключ второго уровня: контрагент + «основа» назначения платежа (первые 4 слова без цифр).
/// Нужен, когда один контрагент (например, банк) получает разные регулярные платежи:
/// зарплата-аванс, зарплата-расчёт, кредит, комиссия.
fn rule_key2(t: &Tx) -> String {
    let stem: Vec<String> = t
        .purpose
        .to_lowercase()
        .split(|c: char| !c.is_alphabetic())
        .filter(|w| w.chars().count() > 2)
        .take(4)
        .map(|w| w.to_string())
        .collect();
    format!("{}#{}", rule_key(t), stem.join(" "))
}

/// Остатки на конец дня: берётся остаток последней по порядку операции дня.
pub fn daily_balances(txs: &[(Tx, Option<i64>)]) -> BTreeMap<NaiveDate, i64> {
    let mut m = BTreeMap::new();
    for (t, b) in txs {
        if let Some(b) = b {
            m.insert(t.date, *b);
        }
    }
    m
}

// ---------- регулярности ----------
pub fn detect_rules(txs: &[Tx], as_of: NaiveDate) -> Vec<Rule> {
    let first = match txs.iter().map(|t| t.date).min() {
        Some(d) => d,
        None => return vec![],
    };
    let mut groups: HashMap<String, Vec<&Tx>> = HashMap::new();
    for t in txs {
        groups.entry(rule_key(t)).or_default().push(t);
    }
    let first_full_month = month_idx(first) + 1; // первый месяц истории может быть неполным
    let last_full_month = month_idx(as_of) - 1;
    let mut gap_months: HashSet<i32> = HashSet::new();
    for (gs, ge) in detect_gaps(txs, None) {
        for mi in month_idx(gs)..=month_idx(ge) {
            gap_months.insert(mi);
        }
    }

    let analyze = |key: String, mut g: Vec<&Tx>| -> Option<Rule> {
        let mut found: Option<Rule> = None;
        if g.len() < 4 {
            return None;
        }
        g.sort_by_key(|t| t.date);
        let last_date = g.last().unwrap().date;
        let direction = if g[0].amount > 0 { "in" } else { "out" };
        let counterparty = g[0].counterparty.clone();
        let category = g[0].category.clone();

        // ---- monthly ----
        let mut by_month: BTreeMap<i32, Vec<&Tx>> = BTreeMap::new();
        for t in &g {
            by_month.entry(month_idx(t.date)).or_default().push(t);
        }
        let lo = by_month.keys().next().copied().unwrap().max(first_full_month);
        let hi = by_month.keys().next_back().copied().unwrap().min(last_full_month);
        let mut done = false;
        let eligible: Vec<i32> = (lo..=hi).filter(|mi| !gap_months.contains(mi)).collect();
        if eligible.len() >= 3 {
            let span_months = eligible.len();
            let mut cnt_freq: HashMap<usize, usize> = HashMap::new();
            for mi in &eligible {
                *cnt_freq.entry(by_month.get(mi).map_or(0, |v| v.len())).or_default() += 1;
            }
            if let Some((&m, &freq)) = cnt_freq.iter().filter(|(c, _)| **c == 1 || **c == 2).max_by_key(|(_, f)| **f) {
                if freq as f64 / span_months as f64 >= 0.75 && freq >= 3 {
                    let months: Vec<i32> = eligible.iter().copied().filter(|mi| by_month.get(mi).map_or(0, |v| v.len()) == m).collect();
                    let mut anchors = vec![];
                    let mut amounts = vec![];
                    let mut jitters = vec![];
                    for k in 0..m {
                        let days: Vec<i64> = months.iter().map(|mi| by_month[mi][k].date.day() as i64).collect();
                        let anchor = median_i64(&days);
                        // сдвиг — в рабочих днях относительно номинальной даты (перенесённой с выходного)
                        jitters.push(
                            months
                                .iter()
                                .map(|mi| {
                                    let (y, m0) = (mi.div_euclid(12), mi.rem_euclid(12) as u32);
                                    let nominal = roll_forward(NaiveDate::from_ymd_opt(y, m0 + 1, (anchor as u32).min(days_in_month(y, m0 + 1))).unwrap());
                                    bd_diff(nominal, by_month[mi][k].date)
                                })
                                .collect::<Vec<_>>(),
                        );
                        let mut am: Vec<i64> = months.iter().map(|mi| by_month[mi][k].amount).collect();
                        let n = am.len();
                        am = am.split_off(n.saturating_sub(6));
                        amounts.push(am);
                        anchors.push(anchor as u32);
                    }
                    let all: Vec<i64> = amounts.iter().flatten().copied().collect();
                    if (as_of - last_date).num_days() <= 70 {
                        found = Some(Rule {
                            key: key.clone(),
                            counterparty: counterparty.clone(),
                            category: category.clone(),
                            direction,
                            kind: "monthly",
                            anchors,
                            amount: median_i64(&all),
                            occurrences: g.len(),
                            last_date,
                            confidence: (freq as f64 / span_months as f64).min(1.0),
                            amounts,
                            jitters,
                        });
                    }
                    done = true;
                }
            }
        }
        if done {
            return found;
        }

        // ---- weekly ----
        let mut by_week: BTreeMap<i64, Vec<&Tx>> = BTreeMap::new();
        for t in &g {
            let wk = (t.date - NaiveDate::from_ymd_opt(1970, 1, 5).unwrap()).num_days().div_euclid(7); // недели с понедельника
            by_week.entry(wk).or_default().push(t);
        }
        let as_of_wk = (as_of - NaiveDate::from_ymd_opt(1970, 1, 5).unwrap()).num_days().div_euclid(7);
        let first_wk = (first - NaiveDate::from_ymd_opt(1970, 1, 5).unwrap()).num_days().div_euclid(7) + 1;
        let wlo = by_week.keys().next().copied().unwrap().max(first_wk);
        let whi = by_week.keys().next_back().copied().unwrap().min(as_of_wk - 1);
        if whi - wlo + 1 >= 6 {
            let span = (whi - wlo + 1) as usize;
            let weeks: Vec<i64> = (wlo..=whi).filter(|w| by_week.get(w).map_or(0, |v| v.len()) == 1).collect();
            if weeks.len() as f64 / span as f64 >= 0.75 {
                let wd: Vec<i64> = weeks.iter().map(|w| by_week[w][0].date.weekday().num_days_from_monday() as i64).collect();
                let anchor = median_i64(&wd);
                let jit: Vec<i64> = wd.iter().map(|d| d - anchor).collect();
                let mut am: Vec<i64> = weeks.iter().map(|w| by_week[w][0].amount).collect();
                let n = am.len();
                am = am.split_off(n.saturating_sub(6));
                if (as_of - last_date).num_days() <= 21 {
                    found = Some(Rule {
                        key,
                        counterparty,
                        category,
                        direction,
                        kind: "weekly",
                        anchors: vec![anchor as u32],
                        amount: median_i64(&am),
                        occurrences: g.len(),
                        last_date,
                        confidence: weeks.len() as f64 / span as f64,
                        amounts: vec![am],
                        jitters: vec![jit],
                    });
                }
            }
        }
            found
    };

    let mut rules = vec![];
    let mut keys: Vec<String> = groups.keys().cloned().collect();
    keys.sort();
    for key in keys {
        let g = groups.remove(&key).unwrap();
        if let Some(r) = analyze(key, g.clone()) {
            rules.push(r);
            continue;
        }
        // разные регулярные платежи одному контрагенту: пробуем разделить по назначению
        let mut sub: HashMap<String, Vec<&Tx>> = HashMap::new();
        for t in &g {
            sub.entry(rule_key2(t)).or_default().push(t);
        }
        if sub.len() > 1 {
            let mut sk: Vec<String> = sub.keys().cloned().collect();
            sk.sort();
            for k2 in sk {
                if let Some(r) = analyze(k2.clone(), sub.remove(&k2).unwrap()) {
                    rules.push(r);
                }
            }
        }
    }
    rules.sort_by(|a, b| b.amount.abs().cmp(&a.amount.abs()).then(a.key.cmp(&b.key)));
    rules
}

/// Одно предстоящее появление платежа.
struct Occurrence {
    base: NaiveDate,
    rule_ix: Option<usize>,
    anchor_ix: usize,
    fixed_amount: i64,
    p_include: f64,
    overdue: bool,
    income_delay: bool,
    counterparty: String,
    /// для просроченных: сколько рабочих дней платёж уже ждём
    age: i64,
    /// индекс ожидаемого события в `events` (название и источник в плане берутся из него, а не по совпадению суммы)
    event_ix: Option<usize>,
}

/// Сколько операций с каждым ключом (`rule_key` и `rule_key2`) было в месяце `as_of`.
fn month_counts(txs: &[Tx], as_of: NaiveDate) -> HashMap<String, usize> {
    let cur_m = month_idx(as_of);
    let mut cur_counts: HashMap<String, usize> = HashMap::new();
    for t in txs.iter().filter(|t| month_idx(t.date) == cur_m) {
        *cur_counts.entry(rule_key(t)).or_default() += 1;
        *cur_counts.entry(rule_key2(t)).or_default() += 1;
    }
    cur_counts
}

fn expand_rules(rules: &[Rule], cur_counts: &HashMap<String, usize>, as_of: NaiveDate, horizon: usize) -> Vec<Occurrence> {
    let end = as_of + Duration::days(horizon as i64 + 7);
    let mut out = vec![];
    let cur_m = month_idx(as_of);
    for (ri, r) in rules.iter().enumerate() {
        match r.kind {
            "monthly" => {
                // сколько раз платёж уже был в текущем месяце
                let this_month_cnt = cur_counts.get(&r.key).copied().unwrap_or(0);
                let mut mi = cur_m;
                while mi <= month_idx(end) {
                    let (y, m0) = (mi.div_euclid(12), mi.rem_euclid(12) as u32);
                    for (k, &a) in r.anchors.iter().enumerate() {
                        let day = a.min(days_in_month(y, m0 + 1));
                        let d = roll_forward(NaiveDate::from_ymd_opt(y, m0 + 1, day).unwrap());
                        if d > as_of && d <= end {
                            out.push(Occurrence { base: d, rule_ix: Some(ri), anchor_ix: k, fixed_amount: 0, p_include: 1.0, overdue: false, income_delay: false, counterparty: r.counterparty.clone(), age: 0, event_ix: None });
                        } else if mi == cur_m && d <= as_of && k >= this_month_cnt && (as_of - d).num_days() <= 20 {
                            // ожидался в этом месяце, но не пришёл
                            out.push(Occurrence { base: as_of + Duration::days(1), rule_ix: Some(ri), anchor_ix: k, fixed_amount: 0, p_include: if r.direction == "in" { 0.75 } else { 1.0 }, overdue: true, income_delay: r.direction == "in", counterparty: r.counterparty.clone(), age: bd_diff(d, as_of).max(1), event_ix: None });
                        }
                    }
                    mi += 1;
                }
            }
            "weekly" => {
                let mut d = as_of + Duration::days(1);
                while d <= end {
                    if d.weekday().num_days_from_monday() == r.anchors[0] {
                        out.push(Occurrence { base: d, rule_ix: Some(ri), anchor_ix: 0, fixed_amount: 0, p_include: 1.0, overdue: false, income_delay: false, counterparty: r.counterparty.clone(), age: 0, event_ix: None });
                    }
                    d += Duration::days(1);
                }
            }
            _ => {}
        }
    }
    out
}

/// Наблюдения задержек оплат: «опоздания» регулярных поступлений относительно самого раннего платежа якоря (оплата наступила)
/// и просроченные ожидаемые поступления (цензурированные: ждём уже `age` дней).
fn fit_delays(rules: &[Rule], occs: &[Occurrence]) -> Option<DelayModel> {
    let mut obs: Vec<(String, i64, bool)> = vec![];
    for r in rules.iter().filter(|r| r.direction == "in") {
        for jt in &r.jitters {
            // самый ранний платёж якоря считаем «в срок», остальные — опоздание относительно него
            let lo = jt.iter().copied().min().unwrap_or(0);
            obs.extend(jt.iter().map(|j| (r.counterparty.clone(), *j - lo, true)));
        }
    }
    obs.extend(occs.iter().filter(|o| o.overdue && o.income_delay).map(|o| (o.counterparty.clone(), o.age, false)));
    DelayModel::fit(&obs)
}

fn expand_events(events: &[Event], as_of: NaiveDate, horizon: usize) -> Vec<Occurrence> {
    let end = as_of + Duration::days(horizon as i64 + 7);
    events
        .iter()
        .enumerate()
        .filter_map(|(i, e)| {
            if e.date > end {
                return None;
            }
            let overdue = e.date <= as_of;
            Some(Occurrence {
                base: if overdue { as_of + Duration::days(1) } else { e.date },
                rule_ix: None,
                anchor_ix: 0,
                fixed_amount: e.amount,
                p_include: e.confidence.clamp(0.0, 1.0),
                overdue,
                income_delay: e.amount > 0 && e.source != "tax",
                counterparty: e.counterparty.clone(),
                age: if overdue { bd_diff(e.date, as_of).max(1) } else { 0 },
                event_ix: Some(i),
            })
        })
        .collect()
}

/// Нормализованное имя контрагента для сопоставления событий и регулярных платежей с флагами по ИНН.
fn norm_name(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

/// Значимые флаги контрагента, по которым меняется расчёт.
#[derive(Clone, Debug)]
struct RiskInfo {
    /// "low" | "medium" | "high"
    level: &'static str,
    /// причина исключения поступлений из плана: "liquidation" | "bankruptcy"
    excluded: Option<&'static str>,
    inn: String,
    status: String,
    tax_debt_kop: i64,
}

fn index_risks(risks: &[CounterpartyRisk]) -> HashMap<String, RiskInfo> {
    let mut m = HashMap::new();
    for r in risks {
        let level = match r.risk_level.as_str() {
            "high" => "high",
            "medium" => "medium",
            _ => "low",
        };
        let status = r.status.to_lowercase();
        let excluded = if r.bankruptcy_flag || status == "bankrupt" {
            Some("bankruptcy")
        } else if status == "liquidating" || status == "liquidated" {
            Some("liquidation")
        } else {
            None
        };
        if level == "low" && excluded.is_none() {
            continue;
        }
        for n in &r.names {
            let k = norm_name(n);
            if !k.is_empty() {
                m.insert(k, RiskInfo { level, excluded, inn: r.inn.clone(), status: status.clone(), tax_debt_kop: r.tax_debt_kop });
            }
        }
    }
    m
}

pub fn forecast(
    txs: &[Tx],
    balances: &BTreeMap<NaiveDate, i64>,
    events: &[Event],
    scenario: &Scenario,
    p: &Params,
) -> Result<ForecastResult, ForecastError> {
    forecast_with_risk(txs, balances, events, scenario, p, &[])
}

/// Прогноз с учётом риска контрагентов (флаги ФНС из ledger). Пустой список флагов даёт ровно тот же результат, что и `forecast`.
/// Клиент с высоким риском: ниже уверенность и больше задержка оплаты (кривая Каплана–Мейера + добавка); с ликвидацией
/// или банкротством: ожидаемые поступления исключаются из плана (список `excluded`); крупный клиент в зоне риска даёт предупреждение `risky_client`.
pub fn forecast_with_risk(
    txs: &[Tx],
    balances: &BTreeMap<NaiveDate, i64>,
    events: &[Event],
    scenario: &Scenario,
    p: &Params,
    risks: &[CounterpartyRisk],
) -> Result<ForecastResult, ForecastError> {
    simulate_with_risk(&fit(txs, balances)?, events, scenario, p, risks)
}

/// Обученная по истории часть модели: всё, что зависит только от операций и остатков. Не зависит от событий,
/// сценария, горизонта и сегодняшней даты, поэтому её можно хранить и переиспользовать (режим «Обновлять по новым
/// данным»): `simulate` по ней даёт тот же результат, что и `forecast`, а `advance` дописывает новые операции без
/// повторного поиска регулярностей.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fitted {
    pub model_version: String,
    /// последняя дата выписки
    pub as_of: NaiveDate,
    first: NaiveDate,
    opening: i64,
    #[serde(with = "rules_state")]
    rules: Vec<Rule>,
    gaps: Vec<(NaiveDate, NaiveDate)>,
    /// окно остаточного потока: потоки по дням начиная с `win_start` и признак валидного рабочего дня
    win_start: NaiveDate,
    res_in: Vec<i64>,
    res_out: Vec<i64>,
    valid: Vec<bool>,
    n_valid: usize,
    balances: BTreeMap<NaiveDate, i64>,
    /// суммы |операций| по неделям (масштаб для калибровки риска)
    weekly: BTreeMap<i64, i64>,
    /// месяц `as_of` и счётчики ключей операций в нём (что из регулярного уже прошло в этом месяце)
    cur_month: i32,
    cur_counts: HashMap<String, usize>,
    /// число операций, на которых модель обучена или которые дописаны
    pub n_ops: usize,
}

/// Почему новые операции нельзя дописать к обученной модели и нужен полный пересчёт.
#[derive(Debug, PartialEq, Eq)]
pub enum NeedsFull {
    /// операция датирована не позже конца уже учтённой выписки (импорт задним числом)
    Backdated,
    NoBalance,
}

/// Rule в кэше обученной модели: вместе с выборками сумм и сдвигов, которых нет в ответе API.
mod rules_state {
    use super::{NaiveDate, Rule};
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    #[derive(Serialize, Deserialize)]
    struct S {
        key: String,
        counterparty: String,
        category: String,
        direction: String,
        kind: String,
        anchors: Vec<u32>,
        amount: i64,
        occurrences: usize,
        last_date: NaiveDate,
        confidence: f64,
        amounts: Vec<Vec<i64>>,
        jitters: Vec<Vec<i64>>,
    }

    #[allow(clippy::ptr_arg)]
    pub fn serialize<Z: Serializer>(rules: &Vec<Rule>, s: Z) -> Result<Z::Ok, Z::Error> {
        rules
            .iter()
            .map(|r| S {
                key: r.key.clone(),
                counterparty: r.counterparty.clone(),
                category: r.category.clone(),
                direction: r.direction.into(),
                kind: r.kind.into(),
                anchors: r.anchors.clone(),
                amount: r.amount,
                occurrences: r.occurrences,
                last_date: r.last_date,
                confidence: r.confidence,
                amounts: r.amounts.clone(),
                jitters: r.jitters.clone(),
            })
            .collect::<Vec<_>>()
            .serialize(s)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<Rule>, D::Error> {
        Ok(Vec::<S>::deserialize(d)?
            .into_iter()
            .map(|s| Rule {
                key: s.key,
                counterparty: s.counterparty,
                category: s.category,
                direction: if s.direction == "in" { "in" } else { "out" },
                kind: if s.kind == "weekly" { "weekly" } else { "monthly" },
                anchors: s.anchors,
                amount: s.amount,
                occurrences: s.occurrences,
                last_date: s.last_date,
                confidence: s.confidence,
                amounts: s.amounts,
                jitters: s.jitters,
            })
            .collect())
    }
}

fn week_idx(d: NaiveDate) -> i64 {
    (d - NaiveDate::from_ymd_opt(1970, 1, 5).unwrap()).num_days().div_euclid(7)
}

impl Rule {
    /// Новая операция по найденной регулярности: сумма попадает в выборку ближайшего якоря (последние 6),
    /// медианная сумма пересчитывается. Якоря и сдвиги не меняются — их уточняет полный пересчёт.
    fn observe(&mut self, t: &Tx) {
        let k = if self.kind == "monthly" {
            let day = t.date.day() as i64;
            (0..self.anchors.len())
                .min_by_key(|&k| {
                    let d = (day - self.anchors[k] as i64).abs();
                    d.min(31 - d)
                })
                .unwrap_or(0)
        } else {
            0
        };
        if let Some(am) = self.amounts.get_mut(k) {
            am.push(t.amount);
            if am.len() > 6 {
                am.remove(0);
            }
        }
        let all: Vec<i64> = self.amounts.iter().flatten().copied().collect();
        self.amount = median_i64(&all);
        self.occurrences += 1;
        self.last_date = self.last_date.max(t.date);
    }
}

/// Обучение по истории: регулярности, пропуски выписки, окно остаточного потока, масштаб оборота.
pub fn fit(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>) -> Result<Fitted, ForecastError> {
    let as_of = txs.iter().map(|t| t.date).max().ok_or(ForecastError::NoData)?;
    let first = txs.iter().map(|t| t.date).min().unwrap();
    let opening = *balances.range(..=as_of).next_back().ok_or(ForecastError::NoBalance)?.1;

    let rules = detect_rules(txs, as_of);
    let rule_keys: HashSet<&str> = rules.iter().map(|r| r.key.as_str()).collect();

    // --- пропуски выписки и остаточный поток по валидным рабочим дням ---
    let gaps = detect_gaps(txs, Some(balances));
    let in_gap = |d: NaiveDate| gaps.iter().any(|(s, e)| d >= *s && d <= *e);
    // идём назад от as_of, пока не наберём RESIDUAL_BDAYS валидных рабочих дней
    let mut win_start = as_of;
    let mut n_valid = 0usize;
    {
        let mut d = as_of;
        while d >= first && (as_of - d).num_days() < RESIDUAL_MAX_CAL_DAYS && n_valid < RESIDUAL_BDAYS {
            win_start = d;
            if is_business_day(d) && !in_gap(d) {
                n_valid += 1;
            }
            d -= Duration::days(1);
        }
    }
    let n_win = ((as_of - win_start).num_days() + 1) as usize;
    let mut res_in = vec![0i64; n_win];
    let mut res_out = vec![0i64; n_win];
    let valid: Vec<bool> = (0..n_win).map(|i| { let d = win_start + Duration::days(i as i64); is_business_day(d) && !in_gap(d) }).collect();
    for t in txs.iter().filter(|t| t.date >= win_start && !(rule_keys.contains(rule_key(t).as_str()) || rule_keys.contains(rule_key2(t).as_str()))) {
        let i = (t.date - win_start).num_days() as usize;
        if !valid[i] {
            // операция в день, считавшийся нерабочим (неточность календаря) — всё равно учитываем в сумме, но день валидным не делаем
            continue;
        }
        if t.amount > 0 {
            res_in[i] += t.amount;
        } else {
            res_out[i] += t.amount;
        }
    }
    let mut weekly: BTreeMap<i64, i64> = BTreeMap::new();
    for t in txs {
        *weekly.entry(week_idx(t.date)).or_default() += t.amount.abs();
    }
    Ok(Fitted {
        model_version: MODEL_VERSION.into(),
        as_of,
        first,
        opening,
        rules,
        gaps,
        win_start,
        res_in,
        res_out,
        valid,
        n_valid,
        balances: balances.clone(),
        weekly,
        cur_month: month_idx(as_of),
        cur_counts: month_counts(txs, as_of),
        n_ops: txs.len(),
    })
}

impl Fitted {
    /// Дописывает операции после `as_of` без повторного обучения: сдвигает конец выписки и остаток, окно
    /// остаточного потока и пропуски на новом участке; операции по уже найденным регулярностям уточняют их суммы.
    /// Новые регулярности не ищутся — это делает полный пересчёт. Возвращает число добавленных операций.
    pub fn advance(&mut self, txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>) -> Result<usize, NeedsFull> {
        let Some(new_as_of) = txs.iter().map(|t| t.date).max() else { return Ok(0) };
        if txs.iter().any(|t| t.date <= self.as_of) {
            return Err(NeedsFull::Backdated);
        }
        let mut bal = self.balances.clone();
        bal.extend(balances.range(self.as_of + Duration::days(1)..).map(|(d, b)| (*d, *b)));
        let opening = *bal.range(..=new_as_of).next_back().ok_or(NeedsFull::NoBalance)?.1;

        // пропуски выписки на добавленном участке: от последнего учтённого дня до новых дат
        let mut net: BTreeMap<NaiveDate, i64> = BTreeMap::new();
        for t in txs {
            *net.entry(t.date).or_default() += t.amount;
        }
        let dates: Vec<NaiveDate> = std::iter::once(self.as_of).chain(net.keys().copied()).collect();
        let new_gaps = gaps_between(&dates, &net, Some(&bal));

        let mut d = self.as_of + Duration::days(1);
        while d <= new_as_of {
            let ok = is_business_day(d) && !new_gaps.iter().any(|(s, e)| d >= *s && d <= *e);
            self.valid.push(ok);
            self.res_in.push(0);
            self.res_out.push(0);
            self.n_valid += ok as usize;
            d += Duration::days(1);
        }
        for t in txs {
            let (k1, k2) = (rule_key(t), rule_key2(t));
            let ri = self.rules.iter().position(|r| r.key == k1).or_else(|| self.rules.iter().position(|r| r.key == k2));
            let i = (t.date - self.win_start).num_days() as usize;
            match ri {
                Some(ri) => self.rules[ri].observe(t),
                None if self.valid[i] && t.amount > 0 => self.res_in[i] += t.amount,
                None if self.valid[i] => self.res_out[i] += t.amount,
                None => {}
            }
            *self.weekly.entry(week_idx(t.date)).or_default() += t.amount.abs();
        }
        // окно снова — последние RESIDUAL_BDAYS валидных рабочих дней, но не длиннее RESIDUAL_MAX_CAL_DAYS (как в `fit`)
        while !self.valid.is_empty()
            && (self.n_valid > RESIDUAL_BDAYS
                || (self.n_valid == RESIDUAL_BDAYS && !self.valid[0])
                || (new_as_of - self.win_start).num_days() >= RESIDUAL_MAX_CAL_DAYS)
        {
            self.n_valid -= self.valid.remove(0) as usize;
            self.res_in.remove(0);
            self.res_out.remove(0);
            self.win_start += Duration::days(1);
        }
        let m = month_idx(new_as_of);
        if m != self.cur_month {
            self.cur_month = m;
            self.cur_counts.clear();
        }
        for (k, v) in month_counts(txs, new_as_of) {
            *self.cur_counts.entry(k).or_default() += v;
        }
        self.gaps.extend(new_gaps);
        self.balances = bal;
        self.as_of = new_as_of;
        self.opening = opening;
        self.n_ops += txs.len();
        Ok(txs.len())
    }

    /// Медианный недельный оборот (сумма |операций| по неделям), коп.; масштаб для безразмерных признаков.
    fn turnover(&self) -> f64 {
        let mut v: Vec<i64> = self.weekly.values().copied().collect();
        v.sort_unstable();
        v.get(v.len() / 2).copied().unwrap_or(1).max(1) as f64
    }
}

/// Прогноз по обученной модели: события, сценарий, траектории, риск и объяснения.
pub fn simulate(f: &Fitted, events: &[Event], scenario: &Scenario, p: &Params) -> Result<ForecastResult, ForecastError> {
    simulate_with_risk(f, events, scenario, p, &[])
}

/// `simulate` с флагами риска контрагентов (см. `forecast_with_risk`): риск влияет только на события и сценарий,
/// поэтому работает и поверх сохранённой обученной модели (режим «Обновлять по новым данным»).
pub fn simulate_with_risk(f: &Fitted, events: &[Event], scenario: &Scenario, p: &Params, risks: &[CounterpartyRisk]) -> Result<ForecastResult, ForecastError> {
    let (as_of, first, opening) = (f.as_of, f.first, f.opening);
    let bridge = p.today.map_or(0, |t| (t - as_of).num_days().clamp(0, MAX_BRIDGE_DAYS) as usize);
    let h = p.horizon + bridge;
    let history_days = (as_of - first).num_days() + 1;
    let rules = f.rules.clone();
    let balances = &f.balances;
    let gaps = &f.gaps;
    let (win_start, n_valid, n_win) = (f.win_start, f.n_valid, f.valid.len());
    let (res_in, res_out, valid) = (&f.res_in, &f.res_out, &f.valid);
    let gap_days_total: i64 = gaps.iter().map(|(s, e)| (*e - *s).num_days() + 1).sum();

    // --- сценарий ---
    // Явно названный контрагент сдвигается в любом направлении (перенос своего платежа);
    // без названия сдвигается самый крупный клиент (только поступления).
    let explicit_cp = scenario.delay_counterparty.as_deref().map_or(false, |c| !c.trim().is_empty());
    let risk_ix = index_risks(risks);
    let mut scenario_client: Option<ScenarioClient> = None;
    let delay_cp: Option<String> = if scenario.delay_days != 0 {
        let picked = scenario.delay_counterparty.clone().filter(|c| !c.trim().is_empty()).map(|c| (c, "explicit")).or_else(|| {
            // по умолчанию: самый крупный клиент; при флагах ФНС — крупный клиент в зоне риска (высокий риск, доля не меньше порога)
            let in_total: i64 = rules.iter().filter(|r| r.direction == "in").map(|r| r.amount).sum();
            let risky = rules
                .iter()
                .filter(|r| r.direction == "in" && in_total > 0 && r.amount as f64 / in_total as f64 >= RISKY_CLIENT_MIN_SHARE)
                .filter(|r| risk_ix.get(&norm_name(&r.counterparty)).is_some_and(|x| x.level == "high" && x.excluded.is_none()))
                .max_by_key(|r| r.amount);
            match risky {
                Some(r) => Some((r.counterparty.clone(), "risk")),
                None => rules.iter().filter(|r| r.direction == "in").max_by_key(|r| r.amount).map(|r| (r.counterparty.clone(), "largest")),
            }
        });
        picked.map(|(cp, reason)| {
            scenario_client = Some(ScenarioClient { counterparty: cp.clone(), reason });
            cp
        })
    } else {
        None
    };
    let in_scale = 1.0 - scenario.revenue_drop_pct / 100.0;
    let out_scale = 1.0 + scenario.fixed_cost_growth_pct / 100.0;

    let mut occs = expand_rules(&rules, &f.cur_counts, as_of, h);
    let ev_occs = expand_events(events, as_of, h);
    // Ожидаемое событие (счёт, ручной ввод, налог) замещает предположение регулярности о том же платеже
    occs.retain(|o| match o.rule_ix {
        Some(ri) => {
            let r = &rules[ri];
            !ev_occs.iter().any(|e| {
                e.fixed_amount.signum() == r.amount.signum()
                    && (e.base - o.base).num_days().abs() <= 7
                    && ((!e.counterparty.is_empty() && e.counterparty == r.counterparty)
                        || (e.fixed_amount - r.amount).abs() * 10 <= r.amount.abs() * 3)
            })
        }
        None => true,
    });
    occs.extend(ev_occs);
    // ликвидация и банкротство клиента (ФНС): ожидаемые от него поступления исключаются из плана с пометкой
    let mut excluded: Vec<ExcludedItem> = vec![];
    if !risk_ix.is_empty() {
        let horizon_end = as_of + Duration::days(h as i64);
        let mut keep = Vec::with_capacity(occs.len());
        for o in occs.drain(..) {
            let income = match o.rule_ix {
                Some(ri) => rules[ri].direction == "in",
                None => o.fixed_amount > 0,
            };
            let reason = risk_ix.get(&norm_name(&o.counterparty)).and_then(|r| r.excluded).filter(|_| income);
            let Some(reason) = reason else {
                keep.push(o);
                continue;
            };
            if o.base <= horizon_end {
                let (amount, title, source) = match o.rule_ix {
                    Some(ri) => (median_i64(&rules[ri].amounts[o.anchor_ix]), o.counterparty.clone(), "recurring".to_string()),
                    None => {
                        let e = o.event_ix.and_then(|i| events.get(i));
                        (o.fixed_amount, e.map_or_else(|| o.counterparty.clone(), |e| e.title.clone()), e.map_or("manual".to_string(), |e| e.source.clone()))
                    }
                };
                excluded.push(ExcludedItem { date: roll_forward(o.base), amount, title, counterparty: o.counterparty.clone(), reason, source });
            }
        }
        occs = keep;
        excluded.sort_by_key(|x| (x.date, x.amount));
    }
    let km: Option<DelayModel> = if p.km_delays { fit_delays(&rules, &occs) } else { None };
    if let (Some(a), Some(d)) = (scenario.extra_expense_kop, scenario.extra_expense_date) {
        occs.push(Occurrence { base: d.max(as_of + Duration::days(1)), rule_ix: None, anchor_ix: 0, fixed_amount: -a.abs(), p_include: 1.0, overdue: false, income_delay: false, counterparty: String::new(), age: 0, event_ix: None });
    }
    if let (Some(a), Some(d)) = (scenario.extra_income_kop, scenario.extra_income_date) {
        occs.push(Occurrence { base: d.max(as_of + Duration::days(1)), rule_ix: None, anchor_ix: 0, fixed_amount: a.abs(), p_include: 1.0, overdue: false, income_delay: false, counterparty: "\u{0}scenario-income".into(), age: 0, event_ix: None });
    }

    // риск клиента: ниже уверенность в поступлении и больше задержка оплаты (только поступления)
    let mut extra_delay = vec![0i64; occs.len()];
    if !risk_ix.is_empty() {
        for (i, o) in occs.iter_mut().enumerate() {
            let income = match o.rule_ix {
                Some(ri) => rules[ri].direction == "in",
                None => o.fixed_amount > 0,
            };
            if let (true, Some(r)) = (income, risk_ix.get(&norm_name(&o.counterparty))) {
                let (conf, extra) = match r.level {
                    "high" => (HIGH_RISK_CONFIDENCE, HIGH_RISK_EXTRA_DELAY_BD),
                    "medium" => (MEDIUM_RISK_CONFIDENCE, MEDIUM_RISK_EXTRA_DELAY_BD),
                    _ => (1.0, 0),
                };
                o.p_include *= conf;
                extra_delay[i] = extra;
            }
        }
    }

    // --- плановый календарь (детерминированный, для объяснений) ---
    let mut planned: Vec<PlannedItem> = occs
        .iter()
        .filter(|o| o.base <= as_of + Duration::days(h as i64))
        .map(|o| {
            let (amt, title, source) = match o.rule_ix {
                Some(ri) => {
                    let r = &rules[ri];
                    (median_i64(&r.amounts[o.anchor_ix]), if r.counterparty.is_empty() { r.key.clone() } else { r.counterparty.clone() }, "recurring".to_string())
                }
                None => {
                    let e = o.event_ix.and_then(|i| events.get(i));
                    (o.fixed_amount, e.map_or(if o.fixed_amount > 0 { "Дополнительное поступление".to_string() } else { "Внеплановый расход".to_string() }, |e| e.title.clone()), e.map_or("scenario".to_string(), |e| e.source.clone()))
                }
            };
            let amt = scale_amount(amt, in_scale, out_scale, o.rule_ix.is_some());
            let mut date = roll_forward(o.base);
            if delay_cp.as_deref() == Some(o.counterparty.as_str()) && (amt > 0 || explicit_cp) {
                date = roll_forward(date + Duration::days(scenario.delay_days));
            }
            let risk = if amt > 0 { risk_ix.get(&norm_name(&o.counterparty)).map(|r| r.level).filter(|l| *l != "low") } else { None };
            PlannedItem { date, amount: amt, title, source, overdue: o.overdue, risk, cp: o.counterparty.clone() }
        })
        .collect();
    planned.sort_by_key(|p| (p.date, p.amount));

    // --- траектории ---
    let mut rng = Rng::new(p.seed);
    let first_fc = as_of + Duration::days(1);
    let start_wd = first_fc.weekday().num_days_from_monday() as i64;
    let win_start_wd = win_start.weekday().num_days_from_monday() as i64;
    let wd_of = |i: usize| ((win_start_wd + i as i64).rem_euclid(7)) as usize;
    // индексы окна, с которых блок из 7 дней начинается в нужный день недели
    let aligned: Vec<usize> = (0..n_win.saturating_sub(6)).filter(|i| (win_start_wd + *i as i64).rem_euclid(7) == start_wd).collect();
    // пулы валидных дней по дню недели (подстановка вместо праздников/пропусков в источнике)
    let mut pools: Vec<Vec<usize>> = vec![vec![]; 7];
    for i in 0..n_win {
        if valid[i] {
            pools[wd_of(i)].push(i);
        }
    }
    let use_blocks = n_valid >= 14 && !aligned.is_empty();
    let nv = n_valid.max(1) as i64;
    let mean_in = (0..n_win).filter(|i| valid[*i]).map(|i| res_in[i]).sum::<i64>() / nv;
    let mean_out = (0..n_win).filter(|i| valid[*i]).map(|i| res_out[i]).sum::<i64>() / nv;
    // профиль по дням недели (для WeekdayMean/Ewma)
    let mut prof_in = [0f64; 7];
    let mut prof_out = [0f64; 7];
    for wd in 0..7 {
        if !pools[wd].is_empty() {
            prof_in[wd] = pools[wd].iter().map(|&i| res_in[i] as f64).sum::<f64>() / pools[wd].len() as f64;
            prof_out[wd] = pools[wd].iter().map(|&i| res_out[i] as f64).sum::<f64>() / pools[wd].len() as f64;
        }
    }
    // EWMA-уровень: последние недели против среднего по окну
    let (mut lvl_in, mut lvl_out) = (1.0f64, 1.0f64);
    if p.residual == ResidualMode::Ewma && n_valid >= 20 {
        let weeks = n_win / 7;
        let (mut e_in, mut e_out, mut wsum) = (0.0, 0.0, 0.0);
        let (mut a_in, mut a_out) = (0.0, 0.0);
        for k in 0..weeks {
            // k = 0 — последняя неделя
            let hi = n_win - k * 7;
            let lo = hi - 7;
            let (wi, wo): (f64, f64) = (lo..hi).filter(|i| valid[*i]).fold((0.0, 0.0), |a, i| (a.0 + res_in[i] as f64, a.1 + res_out[i] as f64));
            let w = 0.5f64.powf(k as f64 / 3.0);
            e_in += w * wi;
            e_out += w * wo;
            wsum += w;
            a_in += wi;
            a_out += wo;
        }
        if wsum > 0.0 && weeks > 0 {
            let (m_in, m_out) = (e_in / wsum, e_out / wsum);
            let (g_in, g_out) = (a_in / weeks as f64, a_out / weeks as f64);
            if g_in > 0.0 { lvl_in = (m_in / g_in).clamp(0.5, 2.0); }
            if g_out < 0.0 { lvl_out = (m_out / g_out).clamp(0.5, 2.0); }
        }
    }
    let lambda = |t: usize| -> f64 { p.drift_keep.clamp(0.0, 1.0) * if p.drift_tau > 0.0 { (-(t as f64 + 1.0) / p.drift_tau).exp() } else { 1.0 } };
    let fc_bday: Vec<bool> = (0..h).map(|t| is_business_day(first_fc + Duration::days(t as i64))).collect();
    let fc_wd: Vec<usize> = (0..h).map(|t| (start_wd as usize + t) % 7).collect();

    let n = p.n_paths;
    let mut by_day: Vec<Vec<i64>> = vec![Vec::with_capacity(n); h];
    let mut min_bal: Vec<i64> = Vec::with_capacity(n);
    let mut first_break: Vec<Option<usize>> = Vec::with_capacity(n);

    let mut paths: Vec<i64> = Vec::with_capacity(n * h);
    let mut stat_paths: Vec<i64> = Vec::with_capacity(n * h);
    for _ in 0..n {
        let mut flows = vec![0i64; h];
        let mut stat0 = vec![0i64; h]; // статистический дрейф без сценарных масштабов
        // остаточный поток: только в рабочие дни, выходные несут нулевой поток
        let mut d = 0;
        while d < h {
            let blk = (h - d).min(7);
            if use_blocks {
                let s = aligned[rng.below(aligned.len())];
                match p.residual {
                    ResidualMode::Block => {
                        for k in 0..blk {
                            let t = d + k;
                            if !fc_bday[t] {
                                continue;
                            }
                            let src = if valid[s + k] {
                                s + k
                            } else {
                                let pool = &pools[fc_wd[t]];
                                if pool.is_empty() { continue; }
                                pool[rng.below(pool.len())]
                            };
                            let (xi, xo) = (res_in[src], res_out[src]);
                            flows[t] += (xi as f64 * in_scale).round() as i64 + xo;
                            stat0[t] += xi + xo;
                        }
                    }
                    ResidualMode::WeekdayMean | ResidualMode::Ewma => {
                        // отклонение недельной суммы источника от профиля, равномерно по рабочим дням блока
                        let (mut dev_in, mut dev_out) = (0.0, 0.0);
                        for k in 0..7 {
                            if valid[s + k] {
                                dev_in += res_in[s + k] as f64 - prof_in[wd_of(s + k)];
                                dev_out += res_out[s + k] as f64 - prof_out[wd_of(s + k)];
                            }
                        }
                        let nb = (0..blk).filter(|k| fc_bday[d + k]).count().max(1) as f64;
                        for k in 0..blk {
                            let t = d + k;
                            if !fc_bday[t] {
                                continue;
                            }
                            let xi = prof_in[fc_wd[t]] * lvl_in + dev_in / nb;
                            let xo = prof_out[fc_wd[t]] * lvl_out + dev_out / nb;
                            flows[t] += (xi * in_scale).round() as i64 + xo.round() as i64;
                            stat0[t] += (xi + xo).round() as i64;
                        }
                    }
                }
                d += 7;
            } else {
                for k in 0..blk {
                    if fc_bday[d + k] {
                        flows[d + k] += (mean_in as f64 * in_scale).round() as i64 + mean_out;
                        stat0[d + k] += mean_in + mean_out;
                    }
                }
                d += 7;
            }
        }
        // события
        for (oi, o) in occs.iter().enumerate() {
            if o.p_include < 1.0 && rng.unit() > o.p_include {
                continue;
            }
            let (mut amount, jitter) = match o.rule_ix {
                Some(ri) => {
                    let r = &rules[ri];
                    let am = &r.amounts[o.anchor_ix];
                    let jt = &r.jitters[o.anchor_ix];
                    (am[rng.below(am.len())], if o.overdue { 0 } else { jt[rng.below(jt.len())] })
                }
                None => (o.fixed_amount, 0),
            };
            let raw_amount = amount;
            amount = scale_amount(amount, in_scale, out_scale, o.rule_ix.is_some());
            let mut shift = jitter;
            let mut km_sampled = false;
            if o.income_delay {
                shift += match &km {
                    Some(m) if o.rule_ix.is_none() || o.overdue => {
                        km_sampled = true;
                        let d = m.sample(&o.counterparty, if o.overdue { o.age } else { 0 }, &mut rng);
                        if o.overdue { d - o.age } else { d }
                    }
                    _ if o.overdue => OVERDUE_IN_DELAY[rng.below(OVERDUE_IN_DELAY.len())] - 1,
                    _ => DEFAULT_PAY_DELAY[rng.below(DEFAULT_PAY_DELAY.len())],
                };
            }
            if extra_delay[oi] > 0 {
                // клиент в зоне риска: добавка к задержке; у регулярного поступления своей задержки оплаты нет, берём её из кривой Каплана–Мейера
                shift += extra_delay[oi];
                if let (false, Some(m)) = (km_sampled, &km) {
                    shift += m.sample(&o.counterparty, 0, &mut rng);
                }
            }
            // сдвиги (джиттер, задержка оплаты) — в рабочих днях от номинальной даты, перенесённой с выходного
            let mut date = bd_add(roll_forward(o.base), shift);
            if (amount > 0 || explicit_cp) && delay_cp.as_deref() == Some(o.counterparty.as_str()) {
                date = roll_forward(date + Duration::days(scenario.delay_days));
            }
            let idx = (date - first_fc).num_days().max(0);
            if (idx as usize) < h {
                flows[idx as usize] += amount;
                if o.rule_ix.is_some() {
                    stat0[idx as usize] += raw_amount;
                }
            }
        }
        let mut st = 0i64;
        for i in 0..h {
            st += stat0[i];
            paths.push(flows[i]);
            stat_paths.push(st);
        }
    }
    // сжатие статистического дрейфа к нулю: сдвигаем все траектории на (1-λ)·медиана накопленного дрейфа
    let mut shift = vec![0i64; h];
    if h > 0 && (p.drift_keep < 1.0 || p.drift_tau > 0.0 || p.shape_keep < 1.0) {
        let med: Vec<f64> = (0..h)
            .map(|t| {
                let mut col: Vec<i64> = (0..n).map(|k| stat_paths[k * h + t]).collect();
                col.sort_unstable();
                col[n / 2] as f64
            })
            .collect();
        let end = med[h - 1];
        // доля пройденных рабочих дней: выходные не несут потока и не двигают тренд
        let total_bd = fc_bday.iter().filter(|b| **b).count().max(1) as f64;
        let mut done_bd = 0.0;
        for t in 0..h {
            if fc_bday[t] {
                done_bd += 1.0;
            }
            let lin = end * done_bd / total_bd; // линейный дрейф
            let shape = med[t] - lin; // форма внутри горизонта
            shift[t] = ((1.0 - lambda(t)) * lin + (1.0 - p.shape_keep.clamp(0.0, 1.0)) * shape).round() as i64;
        }
    }
    // типичный уровень остатка (медиана за последние ~120 дней) — к нему тянет возврат к среднему
    let typical: Option<i64> = {
        let mut v: Vec<i64> = balances.range(as_of - Duration::days(120)..=as_of).map(|(_, b)| *b).collect();
        if v.len() >= 20 && p.revert > 0.0 {
            v.sort_unstable();
            Some(v[v.len() / 2])
        } else {
            None
        }
    };
    for k in 0..n {
        let mut mn = opening;
        let mut fb = None;
        let mut b = opening;
        for i in 0..h {
            let prev_shift = if i == 0 { 0 } else { shift[i - 1] };
            b += paths[k * h + i] - (shift[i] - prev_shift);
            if let (Some(lv), true) = (typical, fc_bday[i]) {
                b += (p.revert * (lv - b) as f64).round() as i64;
            }
            by_day[i].push(b);
            if b < mn {
                mn = b;
            }
            if b < 0 && fb.is_none() {
                fb = Some(i);
            }
        }
        min_bal.push(mn);
        first_break.push(fb);
    }

    // --- агрегаты ---
    let mut days = Vec::with_capacity(h);
    let mut broken_cum = vec![0usize; h];
    for fb in first_break.iter().flatten() {
        broken_cum[*fb] += 1;
    }
    let mut acc = 0;
    let mut median_path = vec![];
    for i in 0..h {
        let v = &mut by_day[i];
        v.sort_unstable();
        let neg = v.partition_point(|x| *x < 0);
        acc += broken_cum[i];
        median_path.push(quantile(v, 0.5));
        days.push(DayPoint {
            date: as_of + Duration::days(i as i64 + 1),
            p10: quantile(v, 0.10),
            p50: quantile(v, 0.50),
            p90: quantile(v, 0.90),
            prob_negative: neg as f64 / n as f64,
            prob_broken_by: acc as f64 / n as f64,
        });
    }
    let breaks: Vec<usize> = first_break.iter().flatten().copied().collect();
    let raw_probability = breaks.len() as f64 / n as f64;
    let first_break_date = if breaks.is_empty() {
        None
    } else {
        let mut b = breaks.clone();
        b.sort_unstable();
        Some(as_of + Duration::days(b[b.len() / 2] as i64 + 1))
    };
    let possible_break_date = days.iter().find(|d| d.prob_broken_by >= 0.10).map(|d| d.date);
    let mut deficits: Vec<i64> = min_bal.iter().map(|m| (-m).max(0)).collect();
    deficits.sort_unstable();
    let max_deficit_p90 = quantile(&deficits, 0.90);
    let mut given: Vec<i64> = deficits.iter().copied().filter(|d| *d > 0).collect();
    given.sort_unstable();
    let deficit_given_break_median = median_i64(&given);
    let median_path_deficit = (-median_path.iter().copied().min().unwrap_or(0)).max(0);

    let scale = f.turnover();
    let risk_feats = risk_features(raw_probability, n, opening, scale, balances, as_of, &days);
    let shortfall_probability = if p.calibrate && opening >= 0 { calibrate_risk(&risk_feats) } else { raw_probability };

    let level = if shortfall_probability >= 0.5 {
        "high"
    } else if shortfall_probability >= 0.2 {
        "medium"
    } else if shortfall_probability >= 0.05 {
        "low"
    } else {
        "none"
    };

    // --- предупреждения ---
    let mut warnings = vec![];
    if level != "none" {
        let when = possible_break_date.or(first_break_date);
        let reasons: Vec<PlannedItem> = {
            let upto = when.unwrap_or(as_of + Duration::days(h as i64));
            let mut r: Vec<PlannedItem> = planned.iter().filter(|x| x.amount < 0 && x.date <= upto).cloned().collect();
            r.sort_by_key(|x| x.amount);
            r.truncate(4);
            r
        };
        let upto = when.unwrap_or(as_of + Duration::days(h as i64));
        let pressing: Vec<&PlannedItem> = planned.iter().filter(|x| (x.amount < 0 && x.date <= upto) || (x.amount > 0 && x.overdue)).collect();
        let total: i64 = pressing.iter().map(|x| x.amount.abs()).sum();
        let mut drivers: Vec<Driver> = pressing
            .iter()
            .map(|x| Driver { date: x.date, title: x.title.clone(), source: x.source.clone(), amount: x.amount, share: if total > 0 { x.amount.abs() as f64 / total as f64 } else { 0.0 }, missing_income: x.amount > 0 })
            .collect();
        drivers.sort_by(|a, b| b.share.partial_cmp(&a.share).unwrap_or(std::cmp::Ordering::Equal).then(a.date.cmp(&b.date)));
        drivers.truncate(5);
        warnings.push(Warning {
            code: "cash_gap".into(),
            severity: if level == "high" { "high" } else if level == "medium" { "medium" } else { "low" },
            date: when,
            message: format!(
                "Вероятность кассового разрыва в ближайшие {} дн.: {:.0}%",
                p.horizon,
                shortfall_probability * 100.0
            ),
            reasons,
            tier: Some(if shortfall_probability >= 0.5 || median_path_deficit > 0 { "critical" } else { "warning" }),
            drivers,
            client: None,
        });
    }
    for it in planned.iter().filter(|x| x.overdue) {
        warnings.push(Warning {
            code: if it.amount > 0 { "overdue_income" } else { "overdue_expense" }.into(),
            severity: "medium",
            date: Some(it.date),
            message: if it.amount > 0 {
                format!("Ожидаемое поступление «{}» не пришло в срок", it.title)
            } else {
                format!("Платёж «{}» ожидался, но не прошёл", it.title)
            },
            reasons: vec![it.clone()],
            tier: None,
            drivers: vec![],
            client: None,
        });
    }

    // крупный клиент в зоне риска (высокий риск или исключён из плана): доля его поступлений в ожидаемых
    let mut risk_notes: Vec<String> = vec![];
    if !risk_ix.is_empty() {
        let mut agg: BTreeMap<String, (String, i64, NaiveDate)> = BTreeMap::new();
        for x in planned.iter().filter(|x| x.amount > 0 && !x.cp.is_empty()) {
            let e = agg.entry(norm_name(&x.cp)).or_insert((x.cp.clone(), 0, x.date));
            e.1 += x.amount;
            e.2 = e.2.min(x.date);
        }
        for x in &excluded {
            let e = agg.entry(norm_name(&x.counterparty)).or_insert((x.counterparty.clone(), 0, x.date));
            e.1 += x.amount;
            e.2 = e.2.min(x.date);
        }
        let total_in: i64 = planned.iter().filter(|x| x.amount > 0).map(|x| x.amount).sum::<i64>() + excluded.iter().map(|x| x.amount).sum::<i64>();
        let mut clients: Vec<(RiskyClient, NaiveDate)> = vec![];
        for (k, (name, amount, first)) in &agg {
            let Some(r) = risk_ix.get(k) else { continue };
            if total_in <= 0 || !(r.level == "high" || r.excluded.is_some()) {
                continue;
            }
            let share = *amount as f64 / total_in as f64;
            if share >= RISKY_CLIENT_MIN_SHARE {
                clients.push((RiskyClient { counterparty: name.clone(), inn: r.inn.clone(), risk_level: r.level.to_string(), status: r.status.clone(), tax_debt_kop: r.tax_debt_kop, share, amount: *amount, excluded: r.excluded.is_some() }, *first));
            }
        }
        clients.sort_by(|a, b| b.0.share.partial_cmp(&a.0.share).unwrap_or(std::cmp::Ordering::Equal).then(a.0.counterparty.cmp(&b.0.counterparty)));
        for (c, first) in clients.into_iter().take(3) {
            let pct = c.share * 100.0;
            warnings.push(Warning {
                code: "risky_client".into(),
                severity: if c.excluded || c.share >= 0.3 { "high" } else { "medium" },
                date: Some(first),
                message: if c.excluded {
                    format!("Поступления от «{}» исключены из плана (ликвидация или банкротство по данным ФНС): {:.0}% ожидаемых поступлений", c.counterparty, pct)
                } else {
                    format!("Крупный клиент «{}» в зоне риска: на него приходится {:.0}% ожидаемых поступлений, оплата может задержаться", c.counterparty, pct)
                },
                reasons: planned.iter().filter(|x| x.amount > 0 && norm_name(&x.cp) == norm_name(&c.counterparty)).take(3).cloned().collect(),
                tier: None,
                drivers: vec![],
                client: Some(c),
            });
        }
        let mut seen = HashSet::new();
        for x in &excluded {
            if seen.insert(norm_name(&x.counterparty)) {
                risk_notes.push(format!("Поступления от «{}» исключены из плана: по данным ФНС контрагент ликвидируется, ликвидирован или в банкротстве", x.counterparty));
            }
        }
    }

    // --- качество данных ---
    let mut notes = vec![];
    let recent_gap = gaps.iter().any(|(_, e)| (as_of - *e).num_days() <= 180);
    let mut level_q = if history_days < 60 {
        notes.push("История короче 60 дней: регулярности не определяются, прогноз приблизительный".to_string());
        "low"
    } else if history_days < 180 {
        notes.push("История короче 6 месяцев: сезонность не учтена".to_string());
        "medium"
    } else {
        "good"
    };
    if !gaps.is_empty() {
        notes.push(format!(
            "В выписке найдено пропусков: {} (всего {} дн.); эти дни исключены из остаточного потока, остаток после пропуска «скачет»",
            gaps.len(),
            gap_days_total
        ));
        if recent_gap && level_q == "good" {
            level_q = "medium";
        }
    }
    if balances.range(..=as_of).any(|(_, b)| *b < 0) {
        notes.push("В истории есть отрицательный остаток (овердрафт)".to_string());
    }
    if rules.is_empty() {
        notes.push("Регулярные платежи не найдены".to_string());
    }
    if !use_blocks {
        notes.push("Мало данных для остаточного потока: использовано среднее".to_string());
    }
    notes.extend(risk_notes);
    if bridge > 0 {
        notes.push(format!("Выписка заканчивается {as_of}: {bridge} дн. до сегодняшнего дня оценены моделью, загрузите свежую выписку"));
    }
    if p.today.is_some_and(|t| (t - as_of).num_days() > MAX_BRIDGE_DAYS) {
        notes.push(format!("Данные выписки актуальны на {as_of}, это более {MAX_BRIDGE_DAYS} дн. назад: прогноз ненадёжен"));
    }

    Ok(ForecastResult {
        as_of,
        horizon: p.horizon,
        bridge_days: bridge,
        opening_balance: opening,
        model_version: MODEL_VERSION,
        seed: p.seed,
        n_paths: n,
        quality: Quality { level: level_q, history_days, notes },
        days,
        risk: Risk { level, shortfall_probability, first_break_date, possible_break_date, median_path_deficit, max_deficit_p90, deficit_given_break_median, warnings, raw_shortfall_probability: raw_probability },
        rules,
        planned,
        excluded,
        scenario_client,
        risk_feats,
    })
}

// ---------- калибровка вероятности разрыва ----------
/// Названия признаков калибровки (порядок = порядок в `risk_features`).
pub const RISK_FEATURE_NAMES: [&str; 8] = [
    "logit(raw)",
    "asinh(opening/scale)",
    "frac_neg_120d",
    "asinh(min_120d/scale)",
    "asinh(min p10 path/scale)",
    "asinh(min p50 path/scale)",
    "asinh(spread p90-p10 @h/scale)",
    "asinh(median_120d/scale)",
];

fn asinh_s(x: f64) -> f64 {
    x.asinh()
}

/// Безразмерные признаки для калибровки P(разрыв): «сырая» доля траекторий и положение остатка
/// относительно типичного масштаба компании (медианного недельного оборота).
pub fn risk_features(raw: f64, n_paths: usize, opening: i64, scale: f64, balances: &BTreeMap<NaiveDate, i64>, as_of: NaiveDate, days: &[DayPoint]) -> Vec<f64> {
    let eps = 0.5 / n_paths.max(1) as f64;
    let rp = raw.clamp(eps, 1.0 - eps);
    let hist: Vec<i64> = balances.range(as_of - Duration::days(120)..=as_of).map(|(_, b)| *b).collect();
    let (frac_neg, hmin, hmed) = if hist.is_empty() {
        (0.0, opening as f64, opening as f64)
    } else {
        let mut v = hist.clone();
        v.sort_unstable();
        (hist.iter().filter(|b| **b < 0).count() as f64 / hist.len() as f64, v[0] as f64, v[v.len() / 2] as f64)
    };
    let min_p10 = days.iter().map(|d| d.p10).min().unwrap_or(opening) as f64;
    let min_p50 = days.iter().map(|d| d.p50).min().unwrap_or(opening) as f64;
    let spread = days.last().map_or(0.0, |d| (d.p90 - d.p10) as f64);
    vec![
        (rp / (1.0 - rp)).ln(),
        asinh_s(opening as f64 / scale),
        frac_neg,
        asinh_s(hmin / scale),
        asinh_s(min_p10 / scale),
        asinh_s(min_p50 / scale),
        asinh_s(spread / scale),
        asinh_s(hmed / scale),
    ]
}

/// Логистическая калибровка: коэффициенты подобраны офлайн на rolling-origin backtest датасета
/// (`eval --calibrate`), проверены leave-company-out.
/// Используются только logit(raw) и признаки истории (не зависят от сценария), поэтому калиброванная вероятность
/// монотонна по сырой: сценарий, повышающий сырой риск, не может понизить итоговый.
const CAL_B: f64 = -3.553637;
const CAL_W: [f64; 8] = [0.203115, -1.207358, 6.198203, -1.72879, 0.0, 0.0, 0.0, 1.008703];

pub fn calibrate_with(feats: &[f64], b: f64, w: &[f64]) -> f64 {
    let z: f64 = b + feats.iter().zip(w).map(|(x, c)| x * c).sum::<f64>();
    1.0 / (1.0 + (-z).exp())
}

/// Калибровка + страховка от неправдоподобно низкой оценки при «глубоком» медианном дефиците.
/// Если медианная траектория уходит ниже нуля больше чем на 0.5 недельного оборота, это уже не колебание около нуля
/// (на нём училась калибровка), а структурный провал (крупный известный платёж, сценарий): доверяем сырой доле траекторий,
/// с плавным переходом до полного доверия при дефиците 2 недельных оборота.
pub fn guard_risk(cal: f64, feats: &[f64]) -> f64 {
    let raw = 1.0 / (1.0 + (-feats[0]).exp());
    let deficit = -feats[5].sinh(); // в недельных оборотах
    let g = ((deficit - 0.5) / 1.5).clamp(0.0, 1.0);
    cal.max(raw * g)
}

pub fn calibrate_risk(feats: &[f64]) -> f64 {
    guard_risk(calibrate_with(feats, CAL_B, &CAL_W), feats)
}

fn scale_amount(a: i64, in_scale: f64, out_scale: f64, is_rule: bool) -> i64 {
    if a > 0 {
        (a as f64 * in_scale).round() as i64
    } else if is_rule {
        (a as f64 * out_scale).round() as i64
    } else {
        a
    }
}

// ---------- backtest ----------
#[derive(Serialize, Clone, Debug)]
pub struct HorizonMetric {
    pub horizon: usize,
    pub mae_model: i64,
    pub mae_last_value: i64,
    pub mae_drift: i64,
    pub model_beats_baselines_share: f64,
    /// доля фактических остатков внутри полосы [p10, p90]
    pub band_coverage: f64,
    pub n: usize,
}

#[derive(Serialize, Clone, Debug)]
pub struct Backtest {
    pub origins: usize,
    pub metrics: Vec<HorizonMetric>,
    /// предупреждения (P>=0.5) против фактического ухода в минус в окне 30 дн.
    pub alarm_precision: Option<f64>,
    pub alarm_recall: Option<f64>,
    pub actual_gap_windows: usize,
    pub alarm_tp: usize,
    pub alarm_fp: usize,
    pub alarm_fn: usize,
    /// То же, но разрыв размечен с учётом «спасательных» поступлений: переводов собственных денег, без которых остаток ушёл бы в минус.
    /// Владелец закрывает такие дыры сам, и в остатке разрыва не видно, но предпринимателю о нём надо было предупредить.
    pub actual_gap_windows_adj: usize,
    pub alarm_tp_adj: usize,
    pub alarm_fp_adj: usize,
    pub alarm_fn_adj: usize,
    pub alarm_precision_adj: Option<f64>,
    pub alarm_recall_adj: Option<f64>,
    /// (прогнозная вероятность разрыва, был ли разрыв) по каждой контрольной дате; для калибровки
    #[serde(skip)]
    #[allow(dead_code)]
    pub risk_samples: Vec<(f64, bool)>,
    /// признаки калибровки (`risk_features`) на тех же контрольных датах и порядковый номер origin
    #[serde(skip)]
    #[allow(dead_code)]
    pub risk_feats: Vec<(usize, Vec<f64>)>,
    /// Прогноз на 14 дней вперёд, сделанный на каждой контрольной дате, против факта на целевую дату.
    pub series_14: Vec<SeriesPoint>,
    pub note: String,
}

#[derive(Serialize, Clone, Debug)]
pub struct SeriesPoint {
    pub date: NaiveDate,
    pub predicted: i64,
    pub actual: i64,
}

/// Поступления собственных денег (категория `transfers`), которые не дали остатку уйти в минус: остаток дня без них < 0.
/// Возвращает сумму таких поступлений по датам. В данных организаторов это 408 из 417 переводов от владельца.
pub fn rescue_inflows(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>) -> BTreeMap<NaiveDate, i64> {
    let mut out: BTreeMap<NaiveDate, i64> = BTreeMap::new();
    for t in txs {
        if t.category == "transfers" && t.amount > 0 && balances.get(&t.date).is_some_and(|b| *b - t.amount < 0) {
            *out.entry(t.date).or_insert(0) += t.amount;
        }
    }
    out
}

/// Был ли разрыв в окне `origin+1..=origin+horizon`, если вычесть спасательные поступления после `origin`.
pub fn adjusted_gap(balances: &BTreeMap<NaiveDate, i64>, rescue: &BTreeMap<NaiveDate, i64>, origin: NaiveDate, horizon: i64) -> bool {
    (1..=horizon).any(|k| {
        let day = origin + Duration::days(k);
        balances.range(..=day).next_back().is_some_and(|(_, b)| {
            let rescued: i64 = rescue.range((origin + Duration::days(1))..=day).map(|x| *x.1).sum();
            *b - rescued < 0
        })
    })
}

/// Rolling-origin: на каждой точке модель видит только прошлое.
pub fn backtest(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>, step_days: i64) -> Option<Backtest> {
    backtest_with(txs, balances, step_days, &Params { horizon: 30, seed: 1, n_paths: 200, ..Default::default() })
}

#[allow(dead_code)]
pub fn backtest_with(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>, step_days: i64, params: &Params) -> Option<Backtest> {
    backtest_by(txs, balances, step_days, params, |_, hist, hb, p| forecast(hist, hb, &[], &Scenario::default(), p))
}

/// Тот же backtest, но прогноз в каждой точке строится в режиме «Обновлять по новым данным»: модель обучена
/// за `lag_days` дней до точки, затем операции дописываются по дням через `Fitted::advance`. Если дописать нельзя,
/// точка считается полным пересчётом (как сделал бы сервис).
pub fn backtest_incremental(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>, step_days: i64, params: &Params, lag_days: i64) -> Option<Backtest> {
    backtest_by(txs, balances, step_days, params, |origin, hist, hb, p| {
        let cut = origin - Duration::days(lag_days);
        let old: Vec<Tx> = hist.iter().filter(|t| t.date <= cut).cloned().collect();
        let ob: BTreeMap<NaiveDate, i64> = hb.range(..=cut).map(|(k, v)| (*k, *v)).collect();
        let Ok(mut f) = fit(&old, &ob) else { return forecast(hist, hb, &[], &Scenario::default(), p) };
        let mut by_day: BTreeMap<NaiveDate, Vec<Tx>> = BTreeMap::new();
        for t in hist.iter().filter(|t| t.date > cut) {
            by_day.entry(t.date).or_default().push(t.clone());
        }
        for day in by_day.values() {
            if f.advance(day, hb).is_err() {
                return forecast(hist, hb, &[], &Scenario::default(), p);
            }
        }
        simulate(&f, &[], &Scenario::default(), p)
    })
}

fn backtest_by<F>(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>, step_days: i64, params: &Params, run: F) -> Option<Backtest>
where
    F: Fn(NaiveDate, &[Tx], &BTreeMap<NaiveDate, i64>, &Params) -> Result<ForecastResult, ForecastError>,
{
    let first = txs.iter().map(|t| t.date).min()?;
    let last = txs.iter().map(|t| t.date).max()?;
    let min_hist = 120;
    let max_h = 30;
    let mut origin = first + Duration::days(min_hist);
    let mut errs: HashMap<usize, Vec<(i64, i64, i64, bool)>> = HashMap::new();
    let (mut tp, mut fp, mut fn_, mut gaps, mut n_orig) = (0, 0, 0, 0, 0);
    let (mut tp_a, mut fp_a, mut fn_a, mut gaps_a) = (0, 0, 0, 0);
    let rescue = rescue_inflows(txs, balances);
    let mut series_14: Vec<SeriesPoint> = vec![];
    let mut risk_samples: Vec<(f64, bool)> = vec![];
    let mut risk_feats: Vec<(usize, Vec<f64>)> = vec![];
    let bal_at = |d: NaiveDate| balances.range(..=d).next_back().map(|x| *x.1);
    let hs = [7usize, 14, 30];
    while origin + Duration::days(max_h) <= last {
        let hist: Vec<Tx> = txs.iter().filter(|t| t.date <= origin).cloned().collect();
        let hb: BTreeMap<NaiveDate, i64> = balances.range(..=origin).map(|(k, v)| (*k, *v)).collect();
        // as_of = дата последней операции <= origin; приведём к origin через пустые дни
        let fc = run(origin, &hist, &hb, &Params { horizon: 30, ..params.clone() });
        if let Ok(fc) = fc {
            n_orig += 1;
            let base = fc.opening_balance;
            let gap_days = (origin - fc.as_of).num_days();
            let drift_start = origin - Duration::days(28);
            let drift = match (bal_at(drift_start), bal_at(origin)) {
                (Some(a), Some(b)) => (b - a) / 28,
                _ => 0,
            };
            for &h in &hs {
                let target = origin + Duration::days(h as i64);
                if let Some(actual) = bal_at(target) {
                    let idx = h as i64 - gap_days - 1;
                    let in_range = idx >= 0 && (idx as usize) < fc.days.len();
                    let model = if in_range { fc.days[idx as usize].p50 } else { base };
                    let covered = if in_range { let dp = &fc.days[idx as usize]; actual >= dp.p10 && actual <= dp.p90 } else { actual == base };
                    if h == 14 {
                        series_14.push(SeriesPoint { date: target, predicted: model, actual });
                    }
                    errs.entry(h).or_default().push(((model - actual).abs(), (base - actual).abs(), (base + drift * h as i64 - actual).abs(), covered));
                }
            }
            let actual_gap = (1..=30).any(|k| bal_at(origin + Duration::days(k)).map_or(false, |b| b < 0));
            risk_samples.push((fc.risk.shortfall_probability, actual_gap));
            risk_feats.push((n_orig - 1, fc.risk_feats.clone()));
            let alarm = fc.risk.shortfall_probability >= 0.5;
            if actual_gap {
                gaps += 1;
            }
            match (alarm, actual_gap) {
                (true, true) => tp += 1,
                (true, false) => fp += 1,
                (false, true) => fn_ += 1,
                _ => {}
            }
            let actual_adj = adjusted_gap(balances, &rescue, origin, 30);
            if actual_adj {
                gaps_a += 1;
            }
            match (alarm, actual_adj) {
                (true, true) => tp_a += 1,
                (true, false) => fp_a += 1,
                (false, true) => fn_a += 1,
                _ => {}
            }
        }
        origin += Duration::days(step_days);
    }
    if n_orig == 0 {
        return None;
    }
    let metrics = hs
        .iter()
        .filter_map(|h| {
            let v = errs.get(h)?;
            let n = v.len() as i64;
            let beats = v.iter().filter(|(m, l, d, _)| m < l && m < d).count();
            let cov = v.iter().filter(|x| x.3).count();
            Some(HorizonMetric {
                horizon: *h,
                mae_model: v.iter().map(|x| x.0).sum::<i64>() / n,
                mae_last_value: v.iter().map(|x| x.1).sum::<i64>() / n,
                mae_drift: v.iter().map(|x| x.2).sum::<i64>() / n,
                model_beats_baselines_share: beats as f64 / v.len() as f64,
                band_coverage: cov as f64 / v.len() as f64,
                n: v.len(),
            })
        })
        .collect();
    Some(Backtest {
        origins: n_orig,
        metrics,
        alarm_precision: if tp + fp > 0 { Some(tp as f64 / (tp + fp) as f64) } else { None },
        alarm_recall: if tp + fn_ > 0 { Some(tp as f64 / (tp + fn_) as f64) } else { None },
        actual_gap_windows: gaps,
        alarm_tp: tp,
        alarm_fp: fp,
        alarm_fn: fn_,
        actual_gap_windows_adj: gaps_a,
        alarm_tp_adj: tp_a,
        alarm_fp_adj: fp_a,
        alarm_fn_adj: fn_a,
        alarm_precision_adj: if tp_a + fp_a > 0 { Some(tp_a as f64 / (tp_a + fp_a) as f64) } else { None },
        alarm_recall_adj: if tp_a + fn_a > 0 { Some(tp_a as f64 / (tp_a + fn_a) as f64) } else { None },
        risk_samples,
        risk_feats,
        series_14,
        note: "Rolling-origin: модель обучается только на данных до точки прогноза; регулярности пересчитываются на каждом срезе. MAE в копейках.".into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).unwrap()
    }
    fn tx(date: NaiveDate, amount: i64, cp: &str) -> Tx {
        Tx { date, amount, counterparty: cp.into(), inn: String::new(), purpose: String::new(), category: "x".into() }
    }

    /// 8 месяцев: аренда 1-го (−100 000 ₽), клиент 10-го (+300 000 ₽), розница по будням.
    fn sample() -> (Vec<Tx>, BTreeMap<NaiveDate, i64>) {
        let mut v = vec![];
        let mut day = d(2026, 1, 1);
        let end = d(2026, 8, 31);
        while day <= end {
            if day.day() == 1 {
                v.push(tx(day, -10_000_000, "Арендодатель"));
            }
            if day.day() == 10 {
                v.push(tx(day, 30_000_000, "Клиент"));
            }
            if day.weekday().num_days_from_monday() < 5 {
                v.push(tx(day, 100_000 + (day.ordinal() as i64 % 5) * 10_000, "Розница"));
            }
            day += Duration::days(1);
        }
        let mut bal = 5_000_000i64;
        let mut b = BTreeMap::new();
        for t in &v {
            bal += t.amount;
            b.insert(t.date, bal);
        }
        (v, b)
    }

    #[test]
    fn rescue_inflow_hides_a_gap_that_adjusted_labels_reveal() {
        let mut bal: BTreeMap<NaiveDate, i64> = BTreeMap::new();
        for k in 0..=10 {
            bal.insert(d(2026, 3, 1) + Duration::days(k), 5_000_000);
        }
        // 5 марта: крупный платёж увёл бы остаток в −2 000 000, владелец перевёл 8 000 000 своих денег в тот же день
        let rescue_tx = Tx { date: d(2026, 3, 5), amount: 8_000_000, counterparty: "Свой счёт".into(), inn: String::new(), purpose: String::new(), category: "transfers".into() };
        let planned = Tx { date: d(2026, 3, 6), amount: 100_000, counterparty: "Свой счёт".into(), inn: String::new(), purpose: String::new(), category: "transfers".into() };
        let mut b = bal.clone();
        for k in 4..=10 {
            b.insert(d(2026, 3, 1) + Duration::days(k), 6_000_000); // остаток после перевода: −2 000 000 + 8 000 000
        }
        b.insert(d(2026, 3, 6), 6_100_000);
        let rescue = rescue_inflows(&[rescue_tx.clone(), planned.clone()], &b);
        assert_eq!(rescue.get(&d(2026, 3, 5)), Some(&8_000_000));
        assert!(!rescue.contains_key(&d(2026, 3, 6))); // плановый перевод при нормальном остатке спасательным не считается
        assert!(!(1..=7).any(|k| b[&(d(2026, 3, 1) + Duration::days(k))] < 0)); // в остатке разрыва не видно
        assert!(adjusted_gap(&b, &rescue, d(2026, 3, 1), 9)); // а без вмешательства владельца он есть
        assert!(!adjusted_gap(&b, &rescue, d(2026, 3, 5), 5)); // после вмешательства окно чистое
    }

    #[test]
    fn detects_monthly_rules_and_ignores_daily_retail() {
        let (v, _) = sample();
        let rules = detect_rules(&v, d(2026, 8, 31));
        let keys: Vec<_> = rules.iter().map(|r| r.counterparty.as_str()).collect();
        assert!(keys.contains(&"Арендодатель") && keys.contains(&"Клиент"));
        assert!(!keys.contains(&"Розница"));
        let rent = rules.iter().find(|r| r.counterparty == "Арендодатель").unwrap();
        assert_eq!(rent.anchors, vec![1]);
        assert_eq!(rent.amount, -10_000_000);
    }

    #[test]
    fn forecast_is_deterministic_for_same_seed() {
        let (v, b) = sample();
        let p = Params { horizon: 30, seed: 5, n_paths: 200, ..Default::default() };
        let a = forecast(&v, &b, &[], &Scenario::default(), &p).unwrap();
        let c = forecast(&v, &b, &[], &Scenario::default(), &p).unwrap();
        assert_eq!(a.days.iter().map(|x| x.p50).collect::<Vec<_>>(), c.days.iter().map(|x| x.p50).collect::<Vec<_>>());
    }

    #[test]
    fn rent_is_not_double_counted() {
        // без разрыва: на 1 сентября медиана должна упасть примерно на аренду, а не на две
        let (v, b) = sample();
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 300, ..Default::default() }).unwrap();
        let before = fc.days[0].p50; // 1 сентября
        let sep1 = fc.days.iter().find(|x| x.date == d(2026, 9, 1)).unwrap().p50;
        assert!((before - sep1).abs() < 15_000_000, "rent applied once");
        assert!(fc.planned.iter().filter(|p| p.title == "Арендодатель").count() == 1);
    }

    #[test]
    fn negative_opening_balance_means_certain_gap() {
        let (v, mut b) = sample();
        let last = *b.keys().last().unwrap();
        b.insert(last, -50_000_000);
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 10, seed: 1, n_paths: 100, ..Default::default() }).unwrap();
        assert!(fc.risk.shortfall_probability > 0.99);
        assert_eq!(fc.risk.level, "high");
    }

    #[test]
    fn scenario_client_delay_raises_risk() {
        let (v, mut b) = sample();
        let last = *b.keys().last().unwrap();
        // остаток только чтобы пережить аренду, но не 10-е без клиента
        b.insert(last, 12_000_000);
        let base = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 300, ..Default::default() }).unwrap();
        let sc = Scenario { delay_days: 20, ..Default::default() };
        let hit = forecast(&v, &b, &[], &sc, &Params { horizon: 30, seed: 3, n_paths: 300, ..Default::default() }).unwrap();
        assert!(hit.risk.shortfall_probability >= base.risk.shortfall_probability);
        assert!(hit.risk.max_deficit_p90 >= base.risk.max_deficit_p90);
    }

    #[test]
    fn manual_event_included_once() {
        let (v, b) = sample();
        let ev = Event { id: "1".into(), date: d(2026, 9, 15), amount: -900_000_000, title: "Оборудование".into(), source: "manual".into(), confidence: 1.0, counterparty: String::new() };
        let fc = forecast(&v, &b, &[ev], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 100, ..Default::default() }).unwrap();
        assert_eq!(fc.planned.iter().filter(|p| p.title == "Оборудование").count(), 1);
        assert!(fc.risk.shortfall_probability > 0.9);
    }

    #[test]
    fn empty_and_no_balance_are_errors_not_zero_risk() {
        assert!(matches!(forecast(&[], &BTreeMap::new(), &[], &Scenario::default(), &Params::default()), Err(ForecastError::NoData)));
        let (v, _) = sample();
        assert!(matches!(forecast(&v, &BTreeMap::new(), &[], &Scenario::default(), &Params::default()), Err(ForecastError::NoBalance)));
    }

    #[test]
    fn short_history_is_flagged_low_quality() {
        let v: Vec<Tx> = (0..20).map(|i| tx(d(2026, 8, 1) + Duration::days(i), 100_000, "A")).collect();
        let mut b = BTreeMap::new();
        b.insert(d(2026, 8, 20), 1_000_000);
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 50, ..Default::default() }).unwrap();
        assert_eq!(fc.quality.level, "low");
    }

    #[test]
    fn calendar_rolls_weekends_and_holidays() {
        assert!(!is_business_day(d(2026, 1, 3)));
        assert_eq!(roll_forward(d(2026, 1, 3)), d(2026, 1, 12)); // новогодние каникулы + 9 января
        assert!(is_business_day(d(2025, 11, 1))); // рабочая суббота
        assert!(!is_business_day(d(2025, 11, 3)));
        assert_eq!(roll_forward(d(2026, 11, 28)), d(2026, 11, 30));
        assert_eq!(bd_add(d(2026, 11, 27), 1), d(2026, 11, 30));
        assert_eq!(bd_diff(d(2026, 11, 27), d(2026, 11, 30)), 1);
    }

    #[test]
    fn payment_anchored_to_weekend_moves_to_next_business_day() {
        // налог 28-го (переносится на рабочий день), история до 30.10.2026; 28.11.2026 — суббота
        let mut v = vec![];
        let mut b = BTreeMap::new();
        let mut bal = 50_000_000i64;
        let mut day = d(2026, 1, 1);
        while day <= d(2026, 10, 30) {
            let mut today = 0;
            if is_business_day(day) {
                today += 1_000_000;
                v.push(tx(day, 1_000_000, "Розница"));
            }
            if roll_forward(d(day.year(), day.month(), 28)) == day {
                v.push(tx(day, -5_000_000, "Казначейство"));
                today -= 5_000_000;
            }
            bal += today;
            if today != 0 {
                b.insert(day, bal);
            }
            day += Duration::days(1);
        }
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 40, n_paths: 100, ..Default::default() }).unwrap();
        assert!(fc.planned.iter().all(|p| is_business_day(p.date)), "planned items fall on business days");
        let tax = fc.planned.iter().find(|p| p.title == "Казначейство" && p.date.month() == 11).expect("November tax planned");
        assert_eq!(tax.date, d(2026, 11, 30));
        // выходные не несут потока: медиана не меняется в субботу/воскресенье
        let sat = fc.days.iter().find(|x| x.date == d(2026, 11, 14)).unwrap().p50;
        let sun = fc.days.iter().find(|x| x.date == d(2026, 11, 15)).unwrap().p50;
        assert_eq!(sat, sun);
    }

    #[test]
    fn gap_with_balance_jump_is_detected_but_holidays_and_quiet_periods_are_not() {
        let mut v = vec![];
        let mut b = BTreeMap::new();
        let mut bal = 1_000_000i64;
        let mut day = d(2025, 10, 1);
        while day <= d(2026, 3, 31) {
            let hole = day >= d(2026, 2, 2) && day <= d(2026, 2, 27); // пропущено 4 недели
            if is_business_day(day) {
                if hole {
                    bal += 700_000; // операции были, но в выписке их нет
                } else {
                    v.push(tx(day, 10_000, "A"));
                    bal += 10_000;
                    b.insert(day, bal);
                }
            }
            day += Duration::days(1);
        }
        let gaps = detect_gaps(&v, Some(&b));
        assert_eq!(gaps.len(), 1, "{gaps:?}");
        assert!(gaps[0].0 >= d(2026, 1, 31) && gaps[0].1 <= d(2026, 3, 1));
        // без остатков — только по разрыву в датах (праздники 30.12–09.01 не считаются)
        let by_dates = detect_gaps(&v, None);
        assert_eq!(by_dates.len(), 1);
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 14, n_paths: 100, ..Default::default() }).unwrap();
        assert!(fc.quality.notes.iter().any(|n| n.contains("пропуск")), "{:?}", fc.quality.notes);
    }

    #[test]
    fn quiet_period_with_consistent_balance_is_not_a_gap() {
        let mut v = vec![tx(d(2026, 3, 2), 100, "A")];
        let mut b = BTreeMap::new();
        b.insert(d(2026, 3, 2), 1_000_100);
        v.push(tx(d(2026, 3, 23), -50, "A"));
        b.insert(d(2026, 3, 23), 1_000_050); // баланс сходится: просто тихие три недели
        assert!(detect_gaps(&v, Some(&b)).is_empty());
        assert_eq!(detect_gaps(&v, None).len(), 1);
    }

    #[test]
    fn very_short_history_and_overdraft_do_not_panic() {
        let v = vec![tx(d(2026, 9, 28), 500_000, "A"), tx(d(2026, 9, 29), -900_000, "B"), tx(d(2026, 9, 30), 100_000, "A")];
        let mut b = BTreeMap::new();
        b.insert(d(2026, 9, 30), -300_000);
        for h in [1usize, 7, 30] {
            let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: h, n_paths: 50, ..Default::default() }).unwrap();
            assert_eq!(fc.quality.level, "low");
            assert_eq!(fc.days.len(), h);
            assert!(fc.risk.shortfall_probability > 0.5, "overdraft opening means high risk");
            assert!(fc.quality.notes.iter().any(|n| n.contains("овердрафт")));
        }
    }

    #[test]
    fn bridge_extends_horizon_to_today_and_is_capped() {
        let (v, b) = sample();
        let as_of = v.iter().map(|t| t.date).max().unwrap();
        let mk = |today: Option<NaiveDate>| forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 14, seed: 1, n_paths: 50, today, ..Default::default() }).unwrap();
        let none = mk(None);
        assert_eq!((none.bridge_days, none.horizon), (0, 14));
        let br = mk(Some(as_of + Duration::days(10)));
        assert_eq!((br.bridge_days, br.horizon, br.days.len()), (10, 14, 24));
        assert_eq!(br.days[0].date, as_of + Duration::days(1));
        assert!(br.quality.notes.iter().any(|n| n.contains("оценены моделью")));
        let old = mk(Some(as_of + Duration::days(400)));
        assert_eq!(old.bridge_days as i64, MAX_BRIDGE_DAYS);
        let same = mk(Some(as_of));
        assert_eq!(same.bridge_days, 0);
    }

    #[test]
    fn calibration_is_monotone_in_raw_and_lowers_mid_range_for_healthy_history() {
        // здоровая история: остаток выше нуля, дней в минусе нет
        let mk = |raw: f64| vec![(raw / (1.0 - raw)).ln(), 2.0f64, 0.0, 1.5, 0.0, 0.0, 0.0, 2.0];
        let mut prev = 0.0;
        for r in [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99] {
            let c = calibrate_risk(&mk(r));
            assert!(c >= prev, "monotone: {c} < {prev} at raw {r}");
            prev = c;
        }
        // исторически «завышенная» середина: 0.5 сырых -> заметно меньше
        assert!(calibrate_risk(&mk(0.5)) < 0.25);
    }

    #[test]
    fn habitual_overdraft_history_raises_calibrated_risk() {
        let mk = |frac_neg: f64, hmin: f64| vec![0.0f64, 0.3, frac_neg, hmin, 0.0, 0.0, 0.0, 0.5];
        let healthy = calibrate_risk(&mk(0.0, 1.0));
        let pinned = calibrate_risk(&mk(0.4, -0.5));
        assert!(pinned > healthy * 3.0, "{pinned} vs {healthy}");
    }

    #[test]
    fn deep_median_deficit_keeps_raw_probability() {
        // медиана уходит на 3 недельных оборота ниже нуля: калибровка (обученная на колебаниях около нуля) не должна «успокаивать»
        let mut f = vec![(0.97f64 / 0.03).ln(), 3.0, 0.0, 3.0, 0.0, (-3.0f64).asinh(), 0.0, 3.0];
        let p = calibrate_risk(&f);
        assert!(p > 0.95, "{p}");
        f[5] = 0.0;
        assert!(calibrate_risk(&f) < 0.5);
    }

    #[test]
    fn result_exposes_raw_and_calibrated_probability() {
        let (v, mut b) = sample();
        let last = *b.keys().last().unwrap();
        b.insert(last, 12_000_000);
        let on = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 200, ..Default::default() }).unwrap();
        let off = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 200, calibrate: false, ..Default::default() }).unwrap();
        assert_eq!(on.risk.raw_shortfall_probability, off.risk.shortfall_probability);
        assert_eq!(off.risk.raw_shortfall_probability, off.risk.shortfall_probability);
        assert_eq!(on.risk_feats.len(), RISK_FEATURE_NAMES.len());
        assert!((0.0..=1.0).contains(&on.risk.shortfall_probability));
        let j = serde_json::to_value(&on.risk).unwrap();
        assert!(j.get("shortfall_probability").is_some() && j.get("level").is_some() && j.get("raw_shortfall_probability").is_some());
    }

    #[test]
    fn negative_opening_keeps_raw_probability_even_when_calibrating() {
        let (v, mut b) = sample();
        let last = *b.keys().last().unwrap();
        b.insert(last, -1_000);
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 5, seed: 1, n_paths: 50, ..Default::default() }).unwrap();
        assert_eq!(fc.risk.shortfall_probability, fc.risk.raw_shortfall_probability);
    }

    /// Клиент платит то 3-го, то 12-го числа: «опоздания» относительно привычного дня дают кривую задержек.
    fn late_payer() -> (Vec<Tx>, BTreeMap<NaiveDate, i64>) {
        let mut v = vec![];
        let mut day = d(2025, 1, 1);
        while day < d(2026, 3, 1) {
            let late = day.month() % 2 == 0;
            if day.day() == if late { 12 } else { 3 } {
                v.push(tx(day, 30_000_000, "Клиент"));
            }
            if day.day() == 1 {
                v.push(tx(day, -10_000_000, "Арендодатель"));
            }
            if day.weekday().num_days_from_monday() < 5 {
                v.push(tx(day, 100_000, "Розница"));
            }
            day += Duration::days(1);
        }
        let mut bal = 5_000_000i64;
        let mut b = BTreeMap::new();
        for t in &v {
            bal += t.amount;
            b.insert(t.date, bal);
        }
        (v, b)
    }

    #[test]
    fn km_delay_model_is_fitted_from_rule_lateness() {
        let (v, _) = late_payer();
        let as_of = v.iter().map(|t| t.date).max().unwrap();
        let rules = detect_rules(&v, as_of);
        let m = fit_delays(&rules, &[]).expect("enough observations");
        assert!(m.global.n >= 8);
        assert!(m.global.mean() > 0.0);
        assert!(m.weight("Клиент") > 0.5);
    }

    #[test]
    fn km_and_legacy_delays_are_deterministic_and_both_run_with_events() {
        let (v, b) = late_payer();
        let ev = Event { id: "1".into(), date: d(2026, 3, 6), amount: 50_000_000, title: "Счёт".into(), source: "invoice".into(), confidence: 1.0, counterparty: "Клиент".into() };
        let run = |km: bool| forecast(&v, &b, &[ev.clone()], &Scenario::default(), &Params { horizon: 14, seed: 9, n_paths: 100, km_delays: km, ..Default::default() }).unwrap();
        let (a1, a2, legacy) = (run(true), run(true), run(false));
        assert_eq!(a1.days.iter().map(|x| x.p50).collect::<Vec<_>>(), a2.days.iter().map(|x| x.p50).collect::<Vec<_>>());
        assert_eq!(a1.seed, legacy.seed);
        assert!(legacy.days.len() == a1.days.len());
    }

    #[test]
    fn cash_gap_warning_carries_tier_and_driver_shares() {
        let (v, b) = sample();
        let ev = Event { id: "1".into(), date: d(2026, 9, 15), amount: -900_000_000, title: "Оборудование".into(), source: "manual".into(), confidence: 1.0, counterparty: String::new() };
        let as_of = v.iter().map(|t| t.date).max().unwrap();
        let ev = Event { date: as_of + Duration::days(5), ..ev };
        let fc = forecast(&v, &b, &[ev], &Scenario::default(), &Params { horizon: 14, seed: 1, n_paths: 100, ..Default::default() }).unwrap();
        let w = fc.risk.warnings.iter().find(|w| w.code == "cash_gap").expect("gap warning");
        assert_eq!(w.tier, Some("critical"));
        assert!(!w.drivers.is_empty());
        assert_eq!(w.drivers[0].title, "Оборудование");
        let sum: f64 = w.drivers.iter().map(|x| x.share).sum();
        assert!(sum > 0.5 && sum <= 1.0 + 1e-9, "{sum}");
        assert!(w.drivers.windows(2).all(|p| p[0].share >= p[1].share));
    }

    #[test]
    fn simulate_on_fitted_equals_forecast_and_survives_serde() {
        let (v, b) = sample();
        let p = Params { horizon: 30, seed: 7, n_paths: 200, ..Default::default() };
        let ev = Event { id: "e".into(), date: d(2026, 9, 12), amount: -4_000_000, title: "Ремонт".into(), counterparty: String::new(), source: "manual".into(), confidence: 1.0 };
        let full = forecast(&v, &b, std::slice::from_ref(&ev), &Scenario::default(), &p).unwrap();
        let f: Fitted = serde_json::from_str(&serde_json::to_string(&fit(&v, &b).unwrap()).unwrap()).unwrap();
        let cached = simulate(&f, &[ev], &Scenario::default(), &p).unwrap();
        assert_eq!(serde_json::to_value(&full).unwrap(), serde_json::to_value(&cached).unwrap());
    }

    #[test]
    fn advance_by_days_rebuilds_the_same_residual_window_as_a_full_fit() {
        let (v, b) = sample();
        let cut = d(2026, 8, 14);
        let old: Vec<Tx> = v.iter().filter(|t| t.date <= cut).cloned().collect();
        let mut f = fit(&old, &b.range(..=cut).map(|(k, x)| (*k, *x)).collect()).unwrap();
        let mut day = cut + Duration::days(1);
        while day <= d(2026, 8, 31) {
            let batch: Vec<Tx> = v.iter().filter(|t| t.date == day).cloned().collect();
            f.advance(&batch, &b).unwrap();
            day += Duration::days(1);
        }
        let full = fit(&v, &b).unwrap();
        assert_eq!((f.as_of, f.opening, f.n_ops), (full.as_of, full.opening, full.n_ops));
        assert_eq!((f.win_start, f.n_valid), (full.win_start, full.n_valid));
        assert_eq!((&f.res_in, &f.res_out, &f.valid), (&full.res_in, &full.res_out, &full.valid));
        assert_eq!((&f.weekly, &f.cur_counts, f.cur_month), (&full.weekly, &full.cur_counts, full.cur_month));
        let keys = |x: &Fitted| x.rules.iter().map(|r| r.key.clone()).collect::<Vec<_>>();
        assert_eq!(keys(&f), keys(&full));
        // клиент заплатил 10 августа: сумма попала в выборку правила
        let client = f.rules.iter().find(|r| r.counterparty == "Клиент").unwrap();
        assert_eq!(client.last_date, d(2026, 8, 10));
        let p = Params { horizon: 30, seed: 1, n_paths: 100, ..Default::default() };
        assert_eq!(simulate(&f, &[], &Scenario::default(), &p).unwrap().as_of, d(2026, 8, 31));
    }

    #[test]
    fn advance_refuses_backdated_operations_and_ignores_empty_batches() {
        let (v, b) = sample();
        let mut f = fit(&v, &b).unwrap();
        assert_eq!(f.advance(&[], &b), Ok(0));
        let late = tx(d(2026, 8, 31), 50_000, "Розница");
        assert_eq!(f.advance(&[late], &b), Err(NeedsFull::Backdated));
        let no_bal = tx(d(2026, 9, 1), 50_000, "Розница");
        assert_eq!(f.advance(std::slice::from_ref(&no_bal), &BTreeMap::new()).map(|_| ()), Ok(()));
        assert_eq!(f.opening, b[&d(2026, 8, 31)]); // остатка за 1 сентября нет — берём последний известный
    }

    // ---------- риск контрагента (ФНС) ----------
    fn flag(inn: &str, name: &str, level: &str, status: &str, bankruptcy: bool) -> CounterpartyRisk {
        CounterpartyRisk { inn: inn.into(), names: vec![name.into()], risk_level: level.into(), status: status.into(), tax_debt_kop: 1_500_000, bankruptcy_flag: bankruptcy, checked_at: "2026-10-20T10:00:00Z".into(), source: "fns_sim".into() }
    }

    /// Два клиента: «Клиент» (+300 000 ₽ 10-го) и «Клиент Б» (+120 000 ₽ 20-го), аренда и розница.
    fn two_clients() -> (Vec<Tx>, BTreeMap<NaiveDate, i64>) {
        let (mut v, _) = sample();
        let mut day = d(2026, 1, 20);
        while day <= d(2026, 8, 31) {
            if day.day() == 20 {
                v.push(tx(day, 12_000_000, "Клиент Б"));
            }
            day += Duration::days(1);
        }
        v.sort_by_key(|t| t.date);
        let mut bal = 5_000_000i64;
        let mut b = BTreeMap::new();
        for t in &v {
            bal += t.amount;
            b.insert(t.date, bal);
        }
        (v, b)
    }

    fn run_risk(v: &[Tx], b: &BTreeMap<NaiveDate, i64>, sc: &Scenario, risks: &[CounterpartyRisk]) -> ForecastResult {
        forecast_with_risk(v, b, &[], sc, &Params { horizon: 30, seed: 3, n_paths: 300, ..Default::default() }, risks).unwrap()
    }

    #[test]
    fn no_risk_flags_means_identical_result() {
        let (v, b) = two_clients();
        let p = Params { horizon: 30, seed: 3, n_paths: 300, ..Default::default() };
        let a = serde_json::to_string(&forecast(&v, &b, &[], &Scenario::default(), &p).unwrap()).unwrap();
        let c = serde_json::to_string(&forecast_with_risk(&v, &b, &[], &Scenario::default(), &p, &[]).unwrap()).unwrap();
        assert_eq!(a, c);
        // «нейтральные» флаги (низкий риск, действует) тоже не меняют расчёт
        let neutral = [flag("7701234567", "Клиент", "low", "active", false), flag("7702000000", "Клиент Б", "unknown", "", false)];
        let n = serde_json::to_string(&forecast_with_risk(&v, &b, &[], &Scenario::default(), &p, &neutral).unwrap()).unwrap();
        assert_eq!(a, n);
        assert!(!a.contains("\"excluded\"") && !a.contains("risky_client"));
    }

    #[test]
    fn high_risk_client_pays_later_and_is_marked() {
        let (v, b) = two_clients();
        let base = run_risk(&v, &b, &Scenario::default(), &[]);
        let risky = run_risk(&v, &b, &Scenario::default(), &[flag("7701234567", "Клиент", "high", "active", false)]);
        // тот же клиент, те же суммы, но деньги приходят позже: сумма медиан остатка по горизонту ниже
        let sum = |r: &ForecastResult| r.days.iter().map(|x| x.p50).sum::<i64>();
        assert!(sum(&risky) < sum(&base), "{} vs {}", sum(&risky), sum(&base));
        assert!(risky.risk.shortfall_probability >= base.risk.shortfall_probability);
        assert!(risky.planned.iter().any(|x| x.amount > 0 && x.risk == Some("high") && x.cp == "Клиент"));
        assert!(base.planned.iter().all(|x| x.risk.is_none()));
        // детерминизм
        let again = run_risk(&v, &b, &Scenario::default(), &[flag("7701234567", "Клиент", "high", "active", false)]);
        assert_eq!(risky.days.iter().map(|x| x.p50).collect::<Vec<_>>(), again.days.iter().map(|x| x.p50).collect::<Vec<_>>());
        // средний риск действует слабее высокого
        let medium = run_risk(&v, &b, &Scenario::default(), &[flag("7701234567", "Клиент", "medium", "active", false)]);
        assert!(sum(&medium) <= sum(&base) && sum(&medium) >= sum(&risky), "{} {} {}", sum(&base), sum(&medium), sum(&risky));
    }

    #[test]
    fn high_risk_big_client_raises_a_risky_client_warning_with_share() {
        let (v, b) = two_clients();
        let r = run_risk(&v, &b, &Scenario::default(), &[flag("7701234567", "ООО  КЛИЕНТ".trim_start_matches("ООО  "), "high", "active", false)]);
        let w = r.risk.warnings.iter().find(|w| w.code == "risky_client").expect("предупреждение");
        let c = w.client.as_ref().unwrap();
        assert_eq!((c.counterparty.as_str(), c.inn.as_str(), c.risk_level.as_str(), c.excluded), ("Клиент", "7701234567", "high", false));
        // 300 000 из 420 000 ₽ ожидаемых поступлений за месяц
        assert!(c.share > 0.55 && c.share < 0.85, "{}", c.share);
        assert!(w.message.contains("Клиент") && w.message.contains('%'));
        // небольшой клиент ниже порога доли не даёт предупреждения
        let small = run_risk(&v, &b, &Scenario::default(), &[flag("7702000000", "Клиент Б", "high", "active", false)]);
        let share_b = small.risk.warnings.iter().find(|w| w.code == "risky_client").map(|w| w.client.as_ref().unwrap().share);
        assert!(share_b.is_some_and(|s| s >= RISKY_CLIENT_MIN_SHARE && s < 0.5), "{share_b:?}");
    }

    #[test]
    fn liquidated_or_bankrupt_client_is_excluded_from_the_plan_with_a_note() {
        let (v, b) = two_clients();
        let base = run_risk(&v, &b, &Scenario::default(), &[]);
        assert!(base.planned.iter().any(|x| x.amount == 30_000_000));
        for (status, bankruptcy, reason) in [("liquidating", false, "liquidation"), ("liquidated", false, "liquidation"), ("active", true, "bankruptcy"), ("bankrupt", false, "bankruptcy")] {
            let r = run_risk(&v, &b, &Scenario::default(), &[flag("7701234567", "Клиент", "high", status, bankruptcy)]);
            assert!(r.planned.iter().all(|x| x.amount != 30_000_000), "{status}: поступление осталось в плане");
            assert!(!r.excluded.is_empty() && r.excluded.iter().all(|x| x.counterparty == "Клиент" && x.reason == reason && x.amount == 30_000_000), "{status}: {:?}", r.excluded);
            assert!(r.quality.notes.iter().any(|n| n.contains("исключены из плана")));
            let w = r.risk.warnings.iter().find(|w| w.code == "risky_client").unwrap();
            assert!(w.client.as_ref().unwrap().excluded && w.severity == "high");
            // без поступления остаток ниже
            assert!(r.days.iter().map(|x| x.p50).sum::<i64>() < base.days.iter().map(|x| x.p50).sum::<i64>());
        }
        // поступление ожидаемого события тоже исключается
        let ev = Event { id: "1".into(), date: d(2026, 9, 12), amount: 50_000_000, title: "Счёт 17".into(), source: "invoice".into(), confidence: 1.0, counterparty: "Клиент".into() };
        let r = forecast_with_risk(&v, &b, &[ev], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 100, ..Default::default() }, &[flag("7701234567", "Клиент", "high", "liquidating", false)]).unwrap();
        assert!(r.excluded.iter().any(|x| x.title == "Счёт 17" && x.source == "invoice"));
        assert!(r.planned.iter().all(|x| x.title != "Счёт 17"));
        // исходящие платежи ликвидируемому контрагенту не исключаются
        let out = forecast_with_risk(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 100, ..Default::default() }, &[flag("7703000000", "Арендодатель", "high", "liquidating", false)]).unwrap();
        assert!(out.excluded.is_empty() && out.planned.iter().any(|x| x.amount == -10_000_000));
    }

    #[test]
    fn default_scenario_client_prefers_the_risky_big_client() {
        let (v, b) = two_clients();
        let sc = Scenario { delay_days: 14, ..Default::default() };
        // без флагов — самый крупный клиент (как раньше)
        let plain = run_risk(&v, &b, &sc, &[]);
        let c = plain.scenario_client.as_ref().unwrap();
        assert_eq!((c.counterparty.as_str(), c.reason), ("Клиент", "largest"));
        // меньший клиент в зоне риска и с долей выше порога — сценарий берёт его
        let risky = run_risk(&v, &b, &sc, &[flag("7702000000", "Клиент Б", "high", "active", false)]);
        let c = risky.scenario_client.as_ref().unwrap();
        assert_eq!((c.counterparty.as_str(), c.reason), ("Клиент Б", "risk"));
        // явно названный клиент важнее
        let named = run_risk(&v, &b, &Scenario { delay_days: 14, delay_counterparty: Some("Клиент".into()), ..Default::default() }, &[flag("7702000000", "Клиент Б", "high", "active", false)]);
        assert_eq!(named.scenario_client.as_ref().unwrap().reason, "explicit");
        // без задержки в сценарии клиент не выбирается
        assert!(run_risk(&v, &b, &Scenario::default(), &[]).scenario_client.is_none());
    }

    #[test]
    fn equal_invoices_of_one_client_keep_their_own_titles() {
        // два счёта одному клиенту на одну сумму (BS-0905 и BS-1005 из 1С): в плане у каждого своё название
        let (v, b) = sample();
        let ev = |id: &str, date: NaiveDate, title: &str| Event { id: id.into(), date, amount: 45_000_000, title: title.into(), source: "onec".into(), confidence: 0.65, counterparty: "ДОНАГРО ООО".into() };
        let events = [ev("1", d(2026, 8, 25), "Счёт BS-0905"), ev("2", d(2026, 8, 28), "Счёт BS-1005")];
        let r = forecast(&v, &b, &events, &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 50, ..Default::default() }).unwrap();
        let mut titles: Vec<&str> = r.planned.iter().filter(|p| p.source == "onec").map(|p| p.title.as_str()).collect();
        titles.sort_unstable();
        assert_eq!(titles, ["Счёт BS-0905", "Счёт BS-1005"]);
    }
}
