//! Оценка прогнозной модели на синтетическом датасете (rolling-origin backtest по компаниям).
//!
//! Запуск из корня репозитория:
//!   cargo run --release --bin eval -- [--limit 30] [--step 14] [--mode block|weekday_mean|ewma]
//!                                      [--damp TAU_DAYS] [--compare] [--csv] [--data data/dataset]
//!                                      [--incremental K]  (режим «Обновлять по новым данным»: обучение за K дней до точки)
//!
//! Нормировка: ошибки каждой компании делятся на медианный недельный оборот
//! (медиана по неделям суммы |операций|), т.е. MAE выражен в «недельных оборотах» компании.
#![allow(dead_code)]

use fc_model as forecast;

use chrono::NaiveDate;
use fc_model::{Backtest, Params, ResidualMode, Tx};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

const HS: [usize; 3] = [7, 14, 30];

struct Company {
    id: String,
    txs: Vec<Tx>,
    balances: BTreeMap<NaiveDate, i64>,
    scale: f64,
}

fn parse_kop(s: &str) -> Option<i64> {
    let s = s.trim();
    let (neg, s) = match s.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, s),
    };
    let (int, frac) = s.split_once('.').unwrap_or((s, ""));
    let mut f = frac.to_string();
    while f.len() < 2 {
        f.push('0');
    }
    let v = int.parse::<i64>().ok()? * 100 + f[..2].parse::<i64>().ok()?;
    Some(if neg { -v } else { v })
}

fn load_company(path: &std::path::Path) -> Option<Company> {
    let raw = std::fs::read_to_string(path).ok()?;
    let raw = raw.trim_start_matches('\u{feff}');
    let mut rows: Vec<(Tx, Option<i64>)> = vec![];
    let mut id = String::new();
    for (i, line) in raw.lines().enumerate() {
        if i == 0 || line.trim().is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split(';').collect();
        if f.len() < 9 {
            continue;
        }
        // назначение платежа может содержать ';' — склеиваем середину
        let purpose = f[7..f.len() - 1].join(";");
        let date = NaiveDate::parse_from_str(f[2], "%Y-%m-%d").ok()?;
        let amt = parse_kop(f[3])?;
        let signed = if f[4] == "Поступление" { amt } else { -amt };
        id = f[0].to_string();
        rows.push((
            Tx { date, amount: signed, counterparty: f[6].to_string(), inn: f[5].to_string(), purpose, category: "x".into() },
            parse_kop(f[f.len() - 1]),
        ));
    }
    if rows.is_empty() {
        return None;
    }
    let balances = forecast::daily_balances(&rows);
    let txs: Vec<Tx> = rows.into_iter().map(|r| r.0).collect();
    let scale = weekly_turnover_median(&txs);
    Some(Company { id, txs, balances, scale })
}

fn weekly_turnover_median(txs: &[Tx]) -> f64 {
    let base = NaiveDate::from_ymd_opt(1970, 1, 5).unwrap();
    let mut w: BTreeMap<i64, i64> = BTreeMap::new();
    for t in txs {
        *w.entry((t.date - base).num_days().div_euclid(7)).or_default() += t.amount.abs();
    }
    let mut v: Vec<i64> = w.values().copied().collect();
    v.sort_unstable();
    (v[v.len() / 2].max(1)) as f64
}

#[derive(Clone)]
struct CompanyResult {
    id: String,
    scale: f64,
    bt: Option<Backtest>,
}

/// `lag`: Some(K) — прогноз в режиме «Обновлять по новым данным» (модель обучена за K дней до точки, дальше `advance`).
fn run_variant(companies: &[Company], params: &Params, step: i64, threads: usize, lag: Option<i64>) -> Vec<CompanyResult> {
    let next = Arc::new(AtomicUsize::new(0));
    let out: Arc<Mutex<Vec<Option<CompanyResult>>>> = Arc::new(Mutex::new(vec![None; companies.len()]));
    std::thread::scope(|sc| {
        for _ in 0..threads {
            let next = next.clone();
            let out = out.clone();
            sc.spawn(move || loop {
                let i = next.fetch_add(1, Ordering::SeqCst);
                if i >= companies.len() {
                    break;
                }
                let c = &companies[i];
                let bt = match lag {
                    Some(k) => forecast::backtest_incremental(&c.txs, &c.balances, step, params, k),
                    None => forecast::backtest_with(&c.txs, &c.balances, step, params),
                };
                out.lock().unwrap()[i] = Some(CompanyResult { id: c.id.clone(), scale: c.scale, bt });
            });
        }
    });
    let v = out.lock().unwrap();
    v.iter().map(|x| x.clone().unwrap()).collect()
}

fn median(v: &mut Vec<f64>) -> f64 {
    if v.is_empty() {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v[v.len() / 2]
}
fn mean(v: &[f64]) -> f64 {
    if v.is_empty() { f64::NAN } else { v.iter().sum::<f64>() / v.len() as f64 }
}

#[derive(Default, Clone)]
struct HAgg {
    h: usize,
    companies: usize,
    med_model: f64,
    med_last: f64,
    med_drift: f64,
    mean_model: f64,
    mean_last: f64,
    mean_drift: f64,
    beat_origin: f64,
    beat_company: f64,
    coverage: f64,
}

struct Agg {
    name: String,
    companies_total: usize,
    companies_bt: usize,
    h: Vec<HAgg>,
    prec: Option<f64>,
    rec: Option<f64>,
    tp: usize,
    fp: usize,
    fnn: usize,
    gaps: usize,
    /// разметка разрывов с учётом «спасательных» поступлений владельца: (tp, fp, fn, окон с разрывом)
    adj: (usize, usize, usize, usize),
    /// (порог, precision, recall) для разных порогов вероятности
    curve: Vec<(f64, f64, f64)>,
    /// (нижняя граница корзины, число, доля фактических разрывов)
    calib: Vec<(f64, usize, f64)>,
    /// Brier/ECE/precision-recall/reliability активной вероятности (калиброванной, если Params.calibrate)
    risk_m: Option<Metrics>,
}

fn aggregate(name: &str, res: &[CompanyResult]) -> Agg {
    let mut hs = vec![];
    for &h in &HS {
        let mut m = vec![];
        let mut l = vec![];
        let mut d = vec![];
        let (mut beats, mut n_tot, mut cov, mut bc, mut nc) = (0.0, 0usize, 0.0, 0usize, 0usize);
        for r in res {
            let Some(bt) = &r.bt else { continue };
            let Some(hm) = bt.metrics.iter().find(|x| x.horizon == h) else { continue };
            m.push(hm.mae_model as f64 / r.scale * 100.0);
            l.push(hm.mae_last_value as f64 / r.scale * 100.0);
            d.push(hm.mae_drift as f64 / r.scale * 100.0);
            beats += hm.model_beats_baselines_share * hm.n as f64;
            cov += hm.band_coverage * hm.n as f64;
            n_tot += hm.n;
            nc += 1;
            if hm.mae_model < hm.mae_last_value && hm.mae_model < hm.mae_drift {
                bc += 1;
            }
        }
        // значения выше — в процентах недельного оборота
        let (mm, ml, md) = (mean(&m), mean(&l), mean(&d));
        hs.push(HAgg {
            h,
            companies: nc,
            med_model: median(&mut m.clone()),
            med_last: median(&mut l.clone()),
            med_drift: median(&mut d.clone()),
            mean_model: mm,
            mean_last: ml,
            mean_drift: md,
            beat_origin: if n_tot > 0 { beats / n_tot as f64 } else { f64::NAN },
            beat_company: if nc > 0 { bc as f64 / nc as f64 } else { f64::NAN },
            coverage: if n_tot > 0 { cov / n_tot as f64 } else { f64::NAN },
        });
    }
    let (mut tp, mut fp, mut fnn, mut gaps) = (0, 0, 0, 0);
    let mut adj = (0, 0, 0, 0);
    for r in res {
        if let Some(bt) = &r.bt {
            adj = (adj.0 + bt.alarm_tp_adj, adj.1 + bt.alarm_fp_adj, adj.2 + bt.alarm_fn_adj, adj.3 + bt.actual_gap_windows_adj);
            tp += bt.alarm_tp;
            fp += bt.alarm_fp;
            fnn += bt.alarm_fn;
            gaps += bt.actual_gap_windows;
        }
    }
    let samples: Vec<(f64, bool)> = res.iter().filter_map(|r| r.bt.as_ref()).flat_map(|b| b.risk_samples.iter().copied()).collect();
    let curve = [0.1, 0.2, 0.3, 0.5, 0.7, 0.9]
        .iter()
        .map(|&th| {
            let tp = samples.iter().filter(|(p, g)| *p >= th && *g).count() as f64;
            let fp = samples.iter().filter(|(p, g)| *p >= th && !*g).count() as f64;
            let fnn = samples.iter().filter(|(p, g)| *p < th && *g).count() as f64;
            (th, if tp + fp > 0.0 { tp / (tp + fp) } else { f64::NAN }, if tp + fnn > 0.0 { tp / (tp + fnn) } else { f64::NAN })
        })
        .collect();
    let calib = [0.0, 0.05, 0.2, 0.5, 0.8, 0.95]
        .iter()
        .enumerate()
        .map(|(i, &lo)| {
            let hi = [0.05, 0.2, 0.5, 0.8, 0.95, 1.01][i];
            let b: Vec<&(f64, bool)> = samples.iter().filter(|(p, _)| *p >= lo && *p < hi).collect();
            (lo, b.len(), if b.is_empty() { f64::NAN } else { b.iter().filter(|x| x.1).count() as f64 / b.len() as f64 })
        })
        .collect();
    let risk_m = if samples.is_empty() { None } else { Some(metrics(&samples)) };
    Agg {
        risk_m,
        curve,
        calib,
        name: name.to_string(),
        companies_total: res.len(),
        companies_bt: res.iter().filter(|r| r.bt.is_some()).count(),
        h: hs,
        prec: if tp + fp > 0 { Some(tp as f64 / (tp + fp) as f64) } else { None },
        rec: if tp + fnn > 0 { Some(tp as f64 / (tp + fnn) as f64) } else { None },
        tp,
        fp,
        fnn,
        gaps,
        adj,
    }
}

fn print_agg(a: &Agg) {
    println!("\n=== {} === companies with backtest: {}/{}", a.name, a.companies_bt, a.companies_total);
    println!("NMAE = MAE / median weekly turnover of the company, in % (lower is better); mean and median over companies");
    println!("{:>3} | {:>20} | {:>20} | {:>20} | {:>9} {:>9} | {:>8}", "h", "model med/mean", "last-value med/mean", "drift med/mean", "beat/orig", "beat/comp", "cov p10-90");
    for h in &a.h {
        println!(
            "{:>3} | {:>9.1} /{:>9.1} | {:>9.1} /{:>9.1} | {:>9.1} /{:>9.1} | {:>9.3} {:>9.3} | {:>8.3}",
            h.h, h.med_model, h.mean_model, h.med_last, h.mean_last, h.med_drift, h.mean_drift, h.beat_origin, h.beat_company, h.coverage
        );
    }
    let f = |x: Option<f64>| x.map_or("n/a".to_string(), |v| format!("{v:.3}"));
    let c: Vec<String> = a.curve.iter().map(|(t, p, r)| format!("P>={t}: prec {p:.2} rec {r:.2}")).collect();
    println!("alarm threshold sweep: {}", c.join(" | "));
    let c: Vec<String> = a.calib.iter().map(|(lo, n, f)| format!("[{lo}..): n={n} actual {f:.2}")).collect();
    println!("calibration of P(gap in 30d): {}", c.join(" | "));
    if let Some(m) = &a.risk_m {
        print_metrics("risk probability (active)", m);
    }
    println!("alarm (P(gap in 30d)>=0.5) pooled: precision {} recall {} (tp {} fp {} fn {}); actual gap windows {}", f(a.prec), f(a.rec), a.tp, a.fp, a.fnn, a.gaps);
    let (tpa, fpa, fna, ga) = a.adj;
    let ratio = |n: usize, d: usize| if d > 0 { Some(n as f64 / d as f64) } else { None };
    println!(
        "same alarm, gaps labelled with owner's rescue inflows removed: precision {} recall {} (tp {} fp {} fn {}); actual gap windows {} (hidden by the owner: {})",
        f(ratio(tpa, tpa + fpa)), f(ratio(tpa, tpa + fna)), tpa, fpa, fna, ga, ga.saturating_sub(a.gaps)
    );
}

fn write_csv(dir: &std::path::Path, name: &str, res: &[CompanyResult]) {
    let _ = std::fs::create_dir_all(dir);
    let mut s = String::from("company_id;scale_weekly_turnover_rub;origins;horizon;mae_model_rub;mae_last_rub;mae_drift_rub;nmae_model_pct;nmae_last_pct;nmae_drift_pct;beat_share;coverage_p10_p90;n;alarm_tp;alarm_fp;alarm_fn;gap_windows\n");
    for r in res {
        let Some(bt) = &r.bt else { continue };
        for m in &bt.metrics {
            let n = |x: i64| x as f64 / r.scale * 100.0;
            s += &format!(
                "{};{:.2};{};{};{:.2};{:.2};{:.2};{:.2};{:.2};{:.2};{:.4};{:.4};{};{};{};{};{}\n",
                r.id, r.scale / 100.0, bt.origins, m.horizon, m.mae_model as f64 / 100.0, m.mae_last_value as f64 / 100.0, m.mae_drift as f64 / 100.0,
                n(m.mae_model), n(m.mae_last_value), n(m.mae_drift), m.model_beats_baselines_share, m.band_coverage, m.n, bt.alarm_tp, bt.alarm_fp, bt.alarm_fn, bt.actual_gap_windows
            );
        }
    }
    let path = dir.join(format!("eval_{name}.csv"));
    std::fs::write(&path, s).expect("write csv");
    println!("csv: {}", path.display());
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let get = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    let has = |k: &str| args.iter().any(|a| a == k);
    let data = get("--data").unwrap_or_else(|| "data/dataset".into());
    let limit: usize = get("--limit").and_then(|v| v.parse().ok()).unwrap_or(usize::MAX);
    let step: i64 = get("--step").and_then(|v| v.parse().ok()).unwrap_or(14);
    let threads: usize = get("--threads").and_then(|v| v.parse().ok()).unwrap_or_else(|| std::thread::available_parallelism().map_or(4, |n| n.get()));
    let paths: usize = get("--paths").and_then(|v| v.parse().ok()).unwrap_or(200);

    let mut files: Vec<_> = std::fs::read_dir(format!("{data}/statements"))
        .expect("statements dir")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map_or(false, |e| e == "csv"))
        .collect();
    files.sort();
    files.truncate(limit);
    let companies: Vec<Company> = files.iter().filter_map(|p| load_company(p)).collect();
    println!("loaded {} companies, step {} days, {} paths, {} threads", companies.len(), step, paths, threads);

    if let Some(id) = get("--debug") {
        let c = companies.iter().find(|c| c.id == id).expect("company");
        let first = c.txs.iter().map(|t| t.date).min().unwrap();
        let last = c.txs.iter().map(|t| t.date).max().unwrap();
        let mut o = first + chrono::Duration::days(120);
        while o + chrono::Duration::days(30) <= last {
            let hist: Vec<Tx> = c.txs.iter().filter(|t| t.date <= o).cloned().collect();
            let hb: BTreeMap<NaiveDate, i64> = c.balances.range(..=o).map(|(k, v)| (*k, *v)).collect();
            if let Ok(fc) = forecast::forecast(&hist, &hb, &[], &forecast::Scenario::default(), &Params { n_paths: 300, ..Default::default() }) {
                let fut: Vec<i64> = c.balances.range(o + chrono::Duration::days(1)..=o + chrono::Duration::days(30)).map(|x| *x.1).collect();
                println!("{o} open {:>12.0} p50@30 {:>12.0} p10 {:>12.0} p90 {:>12.0} P {:.2} | actual min {:>12.0} end {:>12.0} rules {}", fc.opening_balance as f64 / 100.0, fc.days[29].p50 as f64 / 100.0, fc.days[29].p10 as f64 / 100.0, fc.days[29].p90 as f64 / 100.0, fc.risk.shortfall_probability, fut.iter().min().copied().unwrap_or(0) as f64 / 100.0, fut.last().copied().unwrap_or(0) as f64 / 100.0, fc.rules.len());
            }
            o += chrono::Duration::days(step);
        }
        return;
    }
    if has("--perf") {
        // один прогноз на истории размером со весь датасет (~220 тыс. операций)
        let mut txs: Vec<Tx> = companies.iter().flat_map(|c| c.txs.iter().cloned()).collect();
        txs.sort_by_key(|t| t.date);
        let bal = companies[0].balances.clone();
        let t0 = std::time::Instant::now();
        let r = forecast::forecast(&txs, &bal, &[], &forecast::Scenario::default(), &Params::default());
        println!("perf: {} ops, forecast (1000 paths, 30d) = {:.3}s, ok={}", txs.len(), t0.elapsed().as_secs_f64(), r.is_ok());
        // режим «Обновлять по новым данным»: обучение без последнего дня, затем дописать день и пересчитать траектории
        let last = txs.last().unwrap().date;
        let (old, day): (Vec<Tx>, Vec<Tx>) = txs.iter().cloned().partition(|t| t.date < last);
        let t0 = std::time::Instant::now();
        let mut f = forecast::fit(&old, &bal).expect("fit");
        let t_fit = t0.elapsed().as_secs_f64();
        let t0 = std::time::Instant::now();
        f.advance(&day, &bal).expect("advance");
        let t_adv = t0.elapsed().as_secs_f64();
        let t0 = std::time::Instant::now();
        let r = forecast::simulate(&f, &[], &forecast::Scenario::default(), &Params::default());
        let t_sim = t0.elapsed().as_secs_f64();
        println!("perf: fit {t_fit:.3}s | advance (+{} ops of {last}) {:.4}s | simulate {t_sim:.3}s, ok={} -> incremental update {:.3}s", day.len(), t_adv, r.is_ok(), t_adv + t_sim);
        return;
    }
    let mk = |mode: ResidualMode, keep: f64, tau: f64, shape: f64, rev: f64| Params { revert: rev, horizon: 30, seed: 1, n_paths: paths, residual: mode, drift_keep: keep, drift_tau: tau, shape_keep: shape, calibrate: true, km_delays: !has("--no-km"), today: None };
    let nm = |mode: ResidualMode, keep: f64, tau: f64, shape: f64, rev: f64| {
        let mut n = mode.name().to_string();
        if keep < 1.0 { n += &format!("+keep{keep}"); }
        if tau > 0.0 { n += &format!("+tau{tau}"); }
        if rev > 0.0 { n += &format!("+rev{rev}"); }
        if shape < 1.0 { n += &format!("+shape{shape}"); }
        n
    };
    if has("--calibrate") {
        calibration_study(&companies, step, paths, threads);
        return;
    }
    let mut variants: Vec<(String, Params)> = vec![];
    if has("--compare") {
        use ResidualMode::*;
        let grid = [
            (Block, 1.0, 0.0, 1.0, 0.0), (WeekdayMean, 1.0, 0.0, 1.0, 0.0), (Ewma, 1.0, 0.0, 1.0, 0.0),
            (Block, 0.5, 0.0, 1.0, 0.0), (Block, 0.0, 0.0, 1.0, 0.0), (Block, 0.0, 0.0, 0.5, 0.0), (Block, 1.0, 15.0, 1.0, 0.0),
            (Block, 0.0, 0.0, 0.5, 0.015), (WeekdayMean, 0.0, 0.0, 0.5, 0.015), (Ewma, 0.0, 0.0, 0.5, 0.015),
        ];
        for (m, k, t, sh, rv) in grid {
            variants.push((nm(m, k, t, sh, rv), mk(m, k, t, sh, rv)));
        }
    } else {
        let d = Params::default();
        let mode = match get("--mode").as_deref() {
            Some("block") => ResidualMode::Block,
            Some("weekday_mean") => ResidualMode::WeekdayMean,
            Some("ewma") => ResidualMode::Ewma,
            _ => d.residual,
        };
        let keep = get("--keep").and_then(|v| v.parse().ok()).unwrap_or(d.drift_keep);
        let tau = get("--tau").and_then(|v| v.parse().ok()).unwrap_or(d.drift_tau);
        let shape = get("--shape").and_then(|v| v.parse().ok()).unwrap_or(d.shape_keep);
        let rev = get("--revert").and_then(|v| v.parse().ok()).unwrap_or(d.revert);
        variants.push((nm(mode, keep, tau, shape, rev), mk(mode, keep, tau, shape, rev)));
    }
    let mut aggs = vec![];
    for (name, p) in &variants {
        let t0 = std::time::Instant::now();
        let lag: Option<i64> = get("--incremental").and_then(|v| v.parse().ok());
        let res = run_variant(&companies, p, step, threads, lag);
        let name = &lag.map_or(name.clone(), |k| format!("{name}+incremental{k}"));
        let a = aggregate(name, &res);
        print_agg(&a);
        println!("({:.1}s)", t0.elapsed().as_secs_f64());
        if has("--csv") {
            write_csv(std::path::Path::new("data/eval"), &name.replace('+', "_"), &res);
        }
        aggs.push(a);
    }
    if aggs.len() > 1 {
        println!("\n=== comparison (NMAE %, mean over companies | median | beat/orig | coverage) ===");
        println!("{:<22} | {:^34} | {:^34} | {:^34}", "variant", "h=7", "h=14", "h=30");
        for a in &aggs {
            let cell = |h: &HAgg| format!("{:>6.1} {:>6.1} {:>6.3} {:>6.3}", h.mean_model, h.med_model, h.beat_origin, h.coverage);
            println!("{:<22} | {:^34} | {:^34} | {:^34}", a.name, cell(&a.h[0]), cell(&a.h[1]), cell(&a.h[2]));
        }
        let l = &aggs[0];
        println!("{:<22} | {:^34} | {:^34} | {:^34}", "[last-value]", fmt_base(&l.h[0], true), fmt_base(&l.h[1], true), fmt_base(&l.h[2], true));
        println!("{:<22} | {:^34} | {:^34} | {:^34}", "[drift]", fmt_base(&l.h[0], false), fmt_base(&l.h[1], false), fmt_base(&l.h[2], false));
    }
}

fn fmt_base(h: &HAgg, last: bool) -> String {
    if last { format!("{:>6.1} {:>6.1}", h.mean_last, h.med_last) } else { format!("{:>6.1} {:>6.1}", h.mean_drift, h.med_drift) }
}

// ---------- калибровка вероятности разрыва ----------

/// Выборка для калибровки: компания, номер origin, признаки (первый — logit сырой вероятности), факт.
struct Sample {
    company: usize,
    origin: usize,
    x: Vec<f64>,
    y: bool,
}

fn sigmoid(z: f64) -> f64 {
    1.0 / (1.0 + (-z).exp())
}

/// Решение A x = b гауссом с выбором ведущего элемента.
fn solve(mut a: Vec<Vec<f64>>, mut b: Vec<f64>) -> Vec<f64> {
    let n = b.len();
    for i in 0..n {
        let piv = (i..n).max_by(|&r, &c| a[r][i].abs().partial_cmp(&a[c][i].abs()).unwrap()).unwrap();
        a.swap(i, piv);
        b.swap(i, piv);
        let d = a[i][i];
        if d.abs() < 1e-12 {
            continue;
        }
        for r in i + 1..n {
            let f = a[r][i] / d;
            for c in i..n {
                a[r][c] -= f * a[i][c];
            }
            b[r] -= f * b[i];
        }
    }
    let mut x = vec![0.0; n];
    for i in (0..n).rev() {
        let mut v = b[i];
        for c in i + 1..n {
            v -= a[i][c] * x[c];
        }
        x[i] = if a[i][i].abs() < 1e-12 { 0.0 } else { v / a[i][i] };
    }
    x
}

/// Логистическая регрессия (Ньютон, L2 на стандартизованных признаках). Возвращает (b, w) для исходных признаков.
/// `cols` — какие признаки использовать.
fn fit_logistic(train: &[&Sample], cols: &[usize], l2: f64) -> (f64, Vec<f64>) {
    let k = cols.len();
    let n = train.len() as f64;
    let mut mu = vec![0.0; k];
    let mut sd = vec![1.0; k];
    for (j, &c) in cols.iter().enumerate() {
        mu[j] = train.iter().map(|s| s.x[c]).sum::<f64>() / n;
        let var = train.iter().map(|s| (s.x[c] - mu[j]).powi(2)).sum::<f64>() / n;
        sd[j] = var.sqrt().max(1e-6);
    }
    let z = |s: &Sample| -> Vec<f64> { cols.iter().enumerate().map(|(j, &c)| (s.x[c] - mu[j]) / sd[j]).collect() };
    let zs: Vec<Vec<f64>> = train.iter().map(|s| z(s)).collect();
    let mut beta = vec![0.0; k + 1];
    for _ in 0..50 {
        let mut g = vec![0.0; k + 1];
        let mut h = vec![vec![0.0; k + 1]; k + 1];
        for (s, zv) in train.iter().zip(&zs) {
            let mut xv = vec![1.0];
            xv.extend(zv.iter().copied());
            let p = sigmoid(xv.iter().zip(&beta).map(|(a, b)| a * b).sum());
            let y = if s.y { 1.0 } else { 0.0 };
            for i in 0..=k {
                g[i] += (p - y) * xv[i];
                for j in 0..=k {
                    h[i][j] += p * (1.0 - p) * xv[i] * xv[j];
                }
            }
        }
        for i in 1..=k {
            g[i] += l2 * beta[i];
            h[i][i] += l2;
        }
        h[0][0] += 1e-6;
        let step = solve(h, g);
        let mut mx: f64 = 0.0;
        for i in 0..=k {
            beta[i] -= step[i];
            mx = mx.max(step[i].abs());
        }
        if mx < 1e-8 {
            break;
        }
    }
    let mut w = vec![0.0; 8];
    let mut b = beta[0];
    for (j, &c) in cols.iter().enumerate() {
        w[c] = beta[j + 1] / sd[j];
        b -= beta[j + 1] * mu[j] / sd[j];
    }
    (b, w)
}

/// Изотоническая регрессия (PAV) по сырой вероятности; возвращает функцию-ступеньку с линейной интерполяцией.
fn fit_isotonic(train: &[&Sample]) -> Vec<(f64, f64)> {
    let mut v: Vec<(f64, f64)> = train.iter().map(|s| (sigmoid(s.x[0]), if s.y { 1.0 } else { 0.0 })).collect();
    v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    // блоки: (сумма y, вес, мин x, макс x)
    let mut blocks: Vec<(f64, f64, f64, f64)> = vec![];
    for (x, y) in v {
        blocks.push((y, 1.0, x, x));
        while blocks.len() > 1 {
            let l = blocks.len();
            if blocks[l - 2].0 / blocks[l - 2].1 >= blocks[l - 1].0 / blocks[l - 1].1 {
                let b = blocks.pop().unwrap();
                let a = blocks.last_mut().unwrap();
                a.0 += b.0;
                a.1 += b.1;
                a.3 = b.3;
            } else {
                break;
            }
        }
    }
    blocks.iter().map(|b| ((b.2 + b.3) / 2.0, b.0 / b.1)).collect()
}

fn iso_apply(f: &[(f64, f64)], p: f64) -> f64 {
    if f.is_empty() {
        return p;
    }
    if p <= f[0].0 {
        return f[0].1;
    }
    for w in f.windows(2) {
        if p <= w[1].0 {
            let t = (p - w[0].0) / (w[1].0 - w[0].0).max(1e-12);
            return w[0].1 + t * (w[1].1 - w[0].1);
        }
    }
    f[f.len() - 1].1
}

struct Metrics {
    n: usize,
    brier: f64,
    ece: f64,
    pr: Vec<(f64, f64, f64)>,
    rel: Vec<(usize, f64, f64)>,
}

fn metrics(pred: &[(f64, bool)]) -> Metrics {
    let n = pred.len();
    let brier = pred.iter().map(|(p, y)| (p - if *y { 1.0 } else { 0.0 }).powi(2)).sum::<f64>() / n as f64;
    let edges = [0.0, 0.05, 0.2, 0.5, 0.8, 0.95, 1.0001];
    let mut ece = 0.0;
    let mut rel = vec![];
    for w in edges.windows(2) {
        let b: Vec<&(f64, bool)> = pred.iter().filter(|(p, _)| *p >= w[0] && *p < w[1]).collect();
        if b.is_empty() {
            rel.push((0, f64::NAN, f64::NAN));
            continue;
        }
        let mp = b.iter().map(|x| x.0).sum::<f64>() / b.len() as f64;
        let fr = b.iter().filter(|x| x.1).count() as f64 / b.len() as f64;
        ece += (mp - fr).abs() * b.len() as f64 / n as f64;
        rel.push((b.len(), mp, fr));
    }
    let pr = [0.5, 0.7, 0.9]
        .iter()
        .map(|&th| {
            let tp = pred.iter().filter(|(p, y)| *p >= th && *y).count() as f64;
            let fp = pred.iter().filter(|(p, y)| *p >= th && !*y).count() as f64;
            let fnn = pred.iter().filter(|(p, y)| *p < th && *y).count() as f64;
            (th, if tp + fp > 0.0 { tp / (tp + fp) } else { f64::NAN }, if tp + fnn > 0.0 { tp / (tp + fnn) } else { f64::NAN })
        })
        .collect();
    Metrics { n, brier, ece, pr, rel }
}

fn print_metrics(name: &str, m: &Metrics) {
    let pr: Vec<String> = m.pr.iter().map(|(t, p, r)| format!("@{t}: P {p:.2} R {r:.2}")).collect();
    let rel: Vec<String> = m.rel.iter().map(|(n, mp, f)| format!("n={n} {mp:.2}->{f:.2}")).collect();
    println!("{name:<28} n={} Brier {:.4} ECE {:.4} | {} | rel[0-.05,-.2,-.5,-.8,-.95,1]: {}", m.n, m.brier, m.ece, pr.join(" "), rel.join("; "));
}

fn calibration_study(companies: &[Company], step: i64, paths: usize, threads: usize) {
    let params = Params { calibrate: false, horizon: 30, seed: 1, n_paths: paths, ..Default::default() };
    let res = run_variant(companies, &params, step, threads, None);
    let mut samples: Vec<Sample> = vec![];
    for (ci, r) in res.iter().enumerate() {
        let Some(bt) = &r.bt else { continue };
        for ((oi, x), (_, y)) in bt.risk_feats.iter().zip(&bt.risk_samples) {
            samples.push(Sample { company: ci, origin: *oi, x: x.clone(), y: *y });
        }
    }
    println!("calibration samples: {}, positives {}", samples.len(), samples.iter().filter(|s| s.y).count());
    let sets: Vec<(&str, Vec<usize>)> = vec![
        ("platt(raw)", vec![0]),
        ("logit(raw+opening)", vec![0, 1]),
        ("logit(raw+opening+frac_neg)", vec![0, 1, 2]),
        ("logit(raw+opening+hist)", vec![0, 1, 2, 3, 7]),
        ("logit(all)", vec![0, 1, 2, 3, 4, 5, 6, 7]),
    ];
    let l2 = 5.0;
    let folds = 5;
    for (proto, test_of) in [("leave-company-out (5 folds)", 0), ("temporal (train origin<20, test >=23)", 1)] {
        println!("\n##### {proto}");
        let in_test = |s: &Sample| if test_of == 0 { false } else { s.origin >= 23 };
        let all_idx: Vec<&Sample> = if test_of == 0 { samples.iter().collect() } else { samples.iter().filter(|s| in_test(s)).collect() };
        let raw: Vec<(f64, bool)> = all_idx.iter().map(|s| (sigmoid(s.x[0]), s.y)).collect();
        print_metrics("raw (before)", &metrics(&raw));
        // изотоника
        let mut preds: Vec<(f64, bool)> = vec![];
        let mut pl: Vec<Vec<(f64, bool)>> = vec![vec![]; sets.len()];
        let mut pg: Vec<Vec<(f64, bool)>> = vec![vec![]; sets.len()];
        let n_iter = if test_of == 0 { folds } else { 1 };
        for f in 0..n_iter {
            let (train, test): (Vec<&Sample>, Vec<&Sample>) = if test_of == 0 {
                (samples.iter().filter(|s| s.company % folds != f).collect(), samples.iter().filter(|s| s.company % folds == f).collect())
            } else {
                (samples.iter().filter(|s| s.origin < 20).collect(), samples.iter().filter(|s| s.origin >= 23).collect())
            };
            let iso = fit_isotonic(&train);
            preds.extend(test.iter().map(|s| (iso_apply(&iso, sigmoid(s.x[0])), s.y)));
            for (k, (_, cols)) in sets.iter().enumerate() {
                let (b, w) = fit_logistic(&train, cols, l2);
                pl[k].extend(test.iter().map(|s| (forecast::calibrate_with(&s.x, b, &w), s.y)));
                pg[k].extend(test.iter().map(|s| (forecast::guard_risk(forecast::calibrate_with(&s.x, b, &w), &s.x), s.y)));
            }
        }
        print_metrics("isotonic(raw)", &metrics(&preds));
        for (k, (nm, _)) in sets.iter().enumerate() {
            print_metrics(nm, &metrics(&pl[k]));
            print_metrics(&format!("  +guard {nm}"), &metrics(&pg[k]));
        }
    }
    println!("\n##### fit on all data (for embedding)");
    let all: Vec<&Sample> = samples.iter().collect();
    for (nm, cols) in &sets {
        let (b, w) = fit_logistic(&all, cols, l2);
        println!("{nm}: CAL_B = {b:.6}; CAL_W = {:?}", w.iter().map(|x| (x * 1e6).round() / 1e6).collect::<Vec<_>>());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn smp(company: usize, origin: usize, raw: f64, y: bool) -> Sample {
        let mut x = vec![0.0; 8];
        x[0] = (raw / (1.0 - raw)).ln();
        Sample { company, origin, x, y }
    }

    #[test]
    fn isotonic_is_monotone_and_fixes_overconfidence() {
        // сырая вероятность 0.8, факт 10 % -> изотоника ~0.1
        let mut v = vec![];
        for i in 0..200 {
            v.push(smp(0, i, 0.8, i % 10 == 0));
            v.push(smp(0, i, 0.2, i % 50 == 0));
            v.push(smp(0, i, 0.99, i % 10 != 0));
        }
        let refs: Vec<&Sample> = v.iter().collect();
        let f = fit_isotonic(&refs);
        assert!(f.windows(2).all(|w| w[0].1 <= w[1].1 + 1e-12));
        assert!((iso_apply(&f, 0.8) - 0.1).abs() < 0.03);
        assert!(iso_apply(&f, 0.99) > 0.85);
    }

    #[test]
    fn logistic_recovers_platt_scaling_and_improves_brier() {
        let mut v = vec![];
        for i in 0..600 {
            v.push(smp(0, i, 0.6, i % 20 == 0)); // 5 %
            v.push(smp(0, i, 0.98, i % 5 != 0)); // 80 %
        }
        let refs: Vec<&Sample> = v.iter().collect();
        let (b, w) = fit_logistic(&refs, &[0], 0.1);
        let before = metrics(&v.iter().map(|s| (sigmoid(s.x[0]), s.y)).collect::<Vec<_>>());
        let after = metrics(&v.iter().map(|s| (forecast::calibrate_with(&s.x, b, &w), s.y)).collect::<Vec<_>>());
        assert!(after.brier < before.brier * 0.5, "{} vs {}", after.brier, before.brier);
        assert!(after.ece < 0.02);
    }

    #[test]
    fn metrics_perfect_and_worst() {
        let m = metrics(&[(1.0, true), (0.0, false)]);
        assert!(m.brier < 1e-12 && m.ece < 1e-12);
        let m = metrics(&[(1.0, false), (0.0, true)]);
        assert!((m.brier - 1.0).abs() < 1e-12);
    }
}
