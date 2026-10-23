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

pub const MODEL_VERSION: &str = "baseline-recurring-bootstrap/1";
const N_PATHS: usize = 1000;
const RESIDUAL_WINDOW_DAYS: i64 = 120;
const DEFAULT_PAY_DELAY: [i64; 7] = [0, 0, 1, 2, 3, 5, 7];
const OVERDUE_IN_DELAY: [i64; 6] = [1, 2, 3, 5, 7, 10];

#[derive(Clone, Debug)]
pub struct Tx {
    pub date: NaiveDate,
    pub amount: i64,
    pub counterparty: String,
    pub inn: String,
    pub purpose: String,
    pub category: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct Event {
    pub id: String,
    pub date: NaiveDate,
    pub amount: i64,
    pub title: String,
    pub source: String,
    pub confidence: f64,
    pub counterparty: String,
}

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
}

impl Scenario {
    pub fn is_empty(&self) -> bool {
        self.delay_days == 0
            && self.revenue_drop_pct == 0.0
            && self.fixed_cost_growth_pct == 0.0
            && self.extra_expense_kop.is_none()
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

#[derive(Clone, Debug, Serialize)]
pub struct Warning {
    pub code: String,
    pub severity: &'static str,
    pub date: Option<NaiveDate>,
    pub message: String,
    pub reasons: Vec<PlannedItem>,
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
    pub horizon: usize,
    pub opening_balance: i64,
    pub model_version: &'static str,
    pub seed: u64,
    pub n_paths: usize,
    pub quality: Quality,
    pub days: Vec<DayPoint>,
    pub risk: Risk,
    pub rules: Vec<Rule>,
    pub planned: Vec<PlannedItem>,
}

#[derive(Debug)]
pub enum ForecastError {
    NoData,
    NoBalance,
}

pub struct Params {
    pub horizon: usize,
    pub seed: u64,
    pub n_paths: usize,
}

impl Default for Params {
    fn default() -> Self {
        Params { horizon: 30, seed: 42, n_paths: N_PATHS }
    }
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
    let first_full_week = (first + Duration::days(7)).iso_week();
    let _ = first_full_week;

    let mut rules = vec![];
    for (key, mut g) in groups {
        if g.len() < 4 {
            continue;
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
        if hi - lo + 1 >= 3 {
            let span_months = (hi - lo + 1) as usize;
            let mut cnt_freq: HashMap<usize, usize> = HashMap::new();
            for mi in lo..=hi {
                *cnt_freq.entry(by_month.get(&mi).map_or(0, |v| v.len())).or_default() += 1;
            }
            if let Some((&m, &freq)) = cnt_freq.iter().filter(|(c, _)| **c == 1 || **c == 2).max_by_key(|(_, f)| **f) {
                if freq as f64 / span_months as f64 >= 0.75 && freq >= 3 {
                    let months: Vec<i32> = (lo..=hi).filter(|mi| by_month.get(mi).map_or(0, |v| v.len()) == m).collect();
                    let mut anchors = vec![];
                    let mut amounts = vec![];
                    let mut jitters = vec![];
                    for k in 0..m {
                        let days: Vec<i64> = months.iter().map(|mi| by_month[mi][k].date.day() as i64).collect();
                        let anchor = median_i64(&days);
                        jitters.push(days.iter().map(|d| d - anchor).collect::<Vec<_>>());
                        let mut am: Vec<i64> = months.iter().map(|mi| by_month[mi][k].amount).collect();
                        let n = am.len();
                        am = am.split_off(n.saturating_sub(6));
                        amounts.push(am);
                        anchors.push(anchor as u32);
                    }
                    let all: Vec<i64> = amounts.iter().flatten().copied().collect();
                    if (as_of - last_date).num_days() <= 70 {
                        rules.push(Rule {
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
            continue;
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
                    rules.push(Rule {
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
}

fn expand_rules(rules: &[Rule], txs: &[Tx], as_of: NaiveDate, horizon: usize) -> Vec<Occurrence> {
    let end = as_of + Duration::days(horizon as i64 + 7);
    let mut out = vec![];
    for (ri, r) in rules.iter().enumerate() {
        match r.kind {
            "monthly" => {
                // сколько раз платёж уже был в текущем месяце
                let cur_m = month_idx(as_of);
                let this_month_cnt = txs
                    .iter()
                    .filter(|t| rule_key(t) == r.key && month_idx(t.date) == cur_m)
                    .count();
                let mut mi = cur_m;
                while mi <= month_idx(end) {
                    let (y, m0) = (mi.div_euclid(12), mi.rem_euclid(12) as u32);
                    for (k, &a) in r.anchors.iter().enumerate() {
                        let day = a.min(days_in_month(y, m0 + 1));
                        let d = NaiveDate::from_ymd_opt(y, m0 + 1, day).unwrap();
                        if d > as_of && d <= end {
                            out.push(Occurrence { base: d, rule_ix: Some(ri), anchor_ix: k, fixed_amount: 0, p_include: 1.0, overdue: false, income_delay: false, counterparty: r.counterparty.clone() });
                        } else if mi == cur_m && d <= as_of && k >= this_month_cnt && (as_of - d).num_days() <= 20 {
                            // ожидался в этом месяце, но не пришёл
                            out.push(Occurrence { base: as_of + Duration::days(1), rule_ix: Some(ri), anchor_ix: k, fixed_amount: 0, p_include: if r.direction == "in" { 0.75 } else { 1.0 }, overdue: true, income_delay: r.direction == "in", counterparty: r.counterparty.clone() });
                        }
                    }
                    mi += 1;
                }
            }
            "weekly" => {
                let mut d = as_of + Duration::days(1);
                while d <= end {
                    if d.weekday().num_days_from_monday() == r.anchors[0] {
                        out.push(Occurrence { base: d, rule_ix: Some(ri), anchor_ix: 0, fixed_amount: 0, p_include: 1.0, overdue: false, income_delay: false, counterparty: r.counterparty.clone() });
                    }
                    d += Duration::days(1);
                }
            }
            _ => {}
        }
    }
    out
}

fn expand_events(events: &[Event], as_of: NaiveDate, horizon: usize) -> Vec<Occurrence> {
    let end = as_of + Duration::days(horizon as i64 + 7);
    events
        .iter()
        .filter_map(|e| {
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
            })
        })
        .collect()
}

pub fn forecast(
    txs: &[Tx],
    balances: &BTreeMap<NaiveDate, i64>,
    events: &[Event],
    scenario: &Scenario,
    p: &Params,
) -> Result<ForecastResult, ForecastError> {
    let as_of = txs.iter().map(|t| t.date).max().ok_or(ForecastError::NoData)?;
    let first = txs.iter().map(|t| t.date).min().unwrap();
    let opening = *balances.range(..=as_of).next_back().ok_or(ForecastError::NoBalance)?.1;
    let h = p.horizon;
    let history_days = (as_of - first).num_days() + 1;

    let rules = detect_rules(txs, as_of);
    let rule_keys: HashSet<&str> = rules.iter().map(|r| r.key.as_str()).collect();

    // --- остаточный поток по дням (в окне), с нулевыми днями ---
    let win_start = (as_of - Duration::days(RESIDUAL_WINDOW_DAYS - 1)).max(first);
    let n_win = ((as_of - win_start).num_days() + 1) as usize;
    let mut res_in = vec![0i64; n_win];
    let mut res_out = vec![0i64; n_win];
    for t in txs.iter().filter(|t| t.date >= win_start && !rule_keys.contains(rule_key(t).as_str())) {
        let i = (t.date - win_start).num_days() as usize;
        if t.amount > 0 {
            res_in[i] += t.amount;
        } else {
            res_out[i] += t.amount;
        }
    }

    // --- сценарий ---
    let delay_cp: Option<String> = if scenario.delay_days != 0 {
        scenario.delay_counterparty.clone().or_else(|| {
            rules.iter().filter(|r| r.direction == "in").max_by_key(|r| r.amount).map(|r| r.counterparty.clone())
        })
    } else {
        None
    };
    let in_scale = 1.0 - scenario.revenue_drop_pct / 100.0;
    let out_scale = 1.0 + scenario.fixed_cost_growth_pct / 100.0;

    let mut occs = expand_rules(&rules, txs, as_of, h);
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
    if let (Some(a), Some(d)) = (scenario.extra_expense_kop, scenario.extra_expense_date) {
        occs.push(Occurrence { base: d.max(as_of + Duration::days(1)), rule_ix: None, anchor_ix: 0, fixed_amount: -a.abs(), p_include: 1.0, overdue: false, income_delay: false, counterparty: String::new() });
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
                    let e = events.iter().find(|e| e.amount == o.fixed_amount && e.counterparty == o.counterparty);
                    (o.fixed_amount, e.map_or("Внеплановый расход".to_string(), |e| e.title.clone()), e.map_or("scenario".to_string(), |e| e.source.clone()))
                }
            };
            let amt = scale_amount(amt, in_scale, out_scale, o.rule_ix.is_some());
            let mut date = o.base;
            if delay_cp.as_deref() == Some(o.counterparty.as_str()) && amt > 0 {
                date += Duration::days(scenario.delay_days);
            }
            PlannedItem { date, amount: amt, title, source, overdue: o.overdue }
        })
        .collect();
    planned.sort_by_key(|p| (p.date, p.amount));

    // --- траектории ---
    let mut rng = Rng::new(p.seed);
    let start_wd = (as_of + Duration::days(1)).weekday().num_days_from_monday() as i64;
    let win_start_wd = win_start.weekday().num_days_from_monday() as i64;
    // индексы окна, с которых блок из 7 дней начинается в нужный день недели
    let aligned: Vec<usize> = (0..n_win.saturating_sub(6))
        .filter(|i| (win_start_wd + *i as i64).rem_euclid(7) == start_wd)
        .collect();
    let use_blocks = n_win >= 14 && !aligned.is_empty();
    let mean_in = if n_win > 0 { res_in.iter().sum::<i64>() / n_win as i64 } else { 0 };
    let mean_out = if n_win > 0 { res_out.iter().sum::<i64>() / n_win as i64 } else { 0 };

    let n = p.n_paths;
    let mut by_day: Vec<Vec<i64>> = vec![Vec::with_capacity(n); h];
    let mut min_bal: Vec<i64> = Vec::with_capacity(n);
    let mut first_break: Vec<Option<usize>> = Vec::with_capacity(n);

    for _ in 0..n {
        let mut flows = vec![0i64; h];
        // остаточный поток
        let mut d = 0;
        while d < h {
            if use_blocks {
                let s = aligned[rng.below(aligned.len())];
                for k in 0..7 {
                    if d + k >= h {
                        break;
                    }
                    flows[d + k] += (res_in[s + k] as f64 * in_scale).round() as i64 + res_out[s + k];
                }
                d += 7;
            } else {
                flows[d] += (mean_in as f64 * in_scale).round() as i64 + mean_out;
                d += 1;
            }
        }
        // события
        for o in &occs {
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
            amount = scale_amount(amount, in_scale, out_scale, o.rule_ix.is_some());
            let mut shift = jitter;
            if o.income_delay {
                shift += if o.overdue { OVERDUE_IN_DELAY[rng.below(OVERDUE_IN_DELAY.len())] - 1 } else { DEFAULT_PAY_DELAY[rng.below(DEFAULT_PAY_DELAY.len())] };
            }
            if amount > 0 && delay_cp.as_deref() == Some(o.counterparty.as_str()) {
                shift += scenario.delay_days;
            }
            let idx = ((o.base - as_of).num_days() - 1 + shift).max(0);
            if (idx as usize) < h {
                flows[idx as usize] += amount;
            }
        }
        // баланс
        let mut b = opening;
        let mut mn = opening;
        let mut fb = None;
        for (i, f) in flows.iter().enumerate() {
            b += f;
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
    let shortfall_probability = breaks.len() as f64 / n as f64;
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
        warnings.push(Warning {
            code: "cash_gap".into(),
            severity: if level == "high" { "high" } else if level == "medium" { "medium" } else { "low" },
            date: when,
            message: format!(
                "Вероятность кассового разрыва в ближайшие {} дн.: {:.0}%",
                h,
                shortfall_probability * 100.0
            ),
            reasons,
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
        });
    }

    // --- качество данных ---
    let mut notes = vec![];
    let level_q = if history_days < 60 {
        notes.push("История короче 60 дней: регулярности не определяются, прогноз приблизительный".to_string());
        "low"
    } else if history_days < 180 {
        notes.push("История короче 6 месяцев: сезонность не учтена".to_string());
        "medium"
    } else {
        "good"
    };
    if rules.is_empty() {
        notes.push("Регулярные платежи не найдены".to_string());
    }
    if !use_blocks {
        notes.push("Мало данных для остаточного потока: использовано среднее".to_string());
    }
    if as_of < chrono::Local::now().date_naive() - Duration::days(7) {
        notes.push(format!("Данные выписки актуальны на {as_of}, а не на сегодня"));
    }

    Ok(ForecastResult {
        as_of,
        horizon: h,
        opening_balance: opening,
        model_version: MODEL_VERSION,
        seed: p.seed,
        n_paths: n,
        quality: Quality { level: level_q, history_days, notes },
        days,
        risk: Risk { level, shortfall_probability, first_break_date, possible_break_date, median_path_deficit, max_deficit_p90, deficit_given_break_median, warnings },
        rules,
        planned,
    })
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
    pub note: String,
}

/// Rolling-origin: на каждой точке модель видит только прошлое.
pub fn backtest(txs: &[Tx], balances: &BTreeMap<NaiveDate, i64>, step_days: i64) -> Option<Backtest> {
    let first = txs.iter().map(|t| t.date).min()?;
    let last = txs.iter().map(|t| t.date).max()?;
    let min_hist = 120;
    let max_h = 30;
    let mut origin = first + Duration::days(min_hist);
    let mut errs: HashMap<usize, Vec<(i64, i64, i64)>> = HashMap::new();
    let (mut tp, mut fp, mut fn_, mut gaps, mut n_orig) = (0, 0, 0, 0, 0);
    let bal_at = |d: NaiveDate| balances.range(..=d).next_back().map(|x| *x.1);
    let hs = [7usize, 14, 30];
    while origin + Duration::days(max_h) <= last {
        let hist: Vec<Tx> = txs.iter().filter(|t| t.date <= origin).cloned().collect();
        let hb: BTreeMap<NaiveDate, i64> = balances.range(..=origin).map(|(k, v)| (*k, *v)).collect();
        // as_of = дата последней операции <= origin; приведём к origin через пустые дни
        let fc = forecast(&hist, &hb, &[], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 200 });
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
                    let model = if idx >= 0 && (idx as usize) < fc.days.len() { fc.days[idx as usize].p50 } else { base };
                    errs.entry(h).or_default().push(((model - actual).abs(), (base - actual).abs(), (base + drift * h as i64 - actual).abs()));
                }
            }
            let actual_gap = (1..=30).any(|k| bal_at(origin + Duration::days(k)).map_or(false, |b| b < 0));
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
            let beats = v.iter().filter(|(m, l, d)| m <= l && m <= d).count();
            Some(HorizonMetric {
                horizon: *h,
                mae_model: v.iter().map(|x| x.0).sum::<i64>() / n,
                mae_last_value: v.iter().map(|x| x.1).sum::<i64>() / n,
                mae_drift: v.iter().map(|x| x.2).sum::<i64>() / n,
                model_beats_baselines_share: beats as f64 / v.len() as f64,
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
        let p = Params { horizon: 30, seed: 5, n_paths: 200 };
        let a = forecast(&v, &b, &[], &Scenario::default(), &p).unwrap();
        let c = forecast(&v, &b, &[], &Scenario::default(), &p).unwrap();
        assert_eq!(a.days.iter().map(|x| x.p50).collect::<Vec<_>>(), c.days.iter().map(|x| x.p50).collect::<Vec<_>>());
    }

    #[test]
    fn rent_is_not_double_counted() {
        // без разрыва: на 1 сентября медиана должна упасть примерно на аренду, а не на две
        let (v, b) = sample();
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 300 }).unwrap();
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
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 10, seed: 1, n_paths: 100 }).unwrap();
        assert!(fc.risk.shortfall_probability > 0.99);
        assert_eq!(fc.risk.level, "high");
    }

    #[test]
    fn scenario_client_delay_raises_risk() {
        let (v, mut b) = sample();
        let last = *b.keys().last().unwrap();
        // остаток только чтобы пережить аренду, но не 10-е без клиента
        b.insert(last, 12_000_000);
        let base = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 3, n_paths: 300 }).unwrap();
        let sc = Scenario { delay_days: 20, ..Default::default() };
        let hit = forecast(&v, &b, &[], &sc, &Params { horizon: 30, seed: 3, n_paths: 300 }).unwrap();
        assert!(hit.risk.shortfall_probability >= base.risk.shortfall_probability);
        assert!(hit.risk.max_deficit_p90 >= base.risk.max_deficit_p90);
    }

    #[test]
    fn manual_event_included_once() {
        let (v, b) = sample();
        let ev = Event { id: "1".into(), date: d(2026, 9, 15), amount: -900_000_000, title: "Оборудование".into(), source: "manual".into(), confidence: 1.0, counterparty: String::new() };
        let fc = forecast(&v, &b, &[ev], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 100 }).unwrap();
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
        let fc = forecast(&v, &b, &[], &Scenario::default(), &Params { horizon: 30, seed: 1, n_paths: 50 }).unwrap();
        assert_eq!(fc.quality.level, "low");
    }
}
