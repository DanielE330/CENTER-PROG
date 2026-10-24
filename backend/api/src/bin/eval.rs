//! Оценка прогнозной модели на синтетическом датасете (rolling-origin backtest по компаниям).
//!
//! Запуск из корня репозитория:
//!   cargo run --release --bin eval -- [--limit 30] [--step 14] [--mode block|weekday_mean|ewma]
//!                                      [--damp TAU_DAYS] [--compare] [--csv] [--data data/dataset]
//!
//! Нормировка: ошибки каждой компании делятся на медианный недельный оборот
//! (медиана по неделям суммы |операций|), т.е. MAE выражен в «недельных оборотах» компании.
#![allow(dead_code)]

#[path = "../forecast.rs"]
mod forecast;

use chrono::NaiveDate;
use forecast::{Backtest, Params, ResidualMode, Tx};
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

fn run_variant(companies: &[Company], params: &Params, step: i64, threads: usize) -> Vec<CompanyResult> {
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
                let bt = forecast::backtest_with(&c.txs, &c.balances, step, params);
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
    /// (порог, precision, recall) для разных порогов вероятности
    curve: Vec<(f64, f64, f64)>,
    /// (нижняя граница корзины, число, доля фактических разрывов)
    calib: Vec<(f64, usize, f64)>,
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
    for r in res {
        if let Some(bt) = &r.bt {
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
    Agg {
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
    println!("alarm (P(gap in 30d)>=0.5) pooled: precision {} recall {} (tp {} fp {} fn {}); actual gap windows {}", f(a.prec), f(a.rec), a.tp, a.fp, a.fnn, a.gaps);
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
        return;
    }
    let mk = |mode: ResidualMode, keep: f64, tau: f64, shape: f64, rev: f64| Params { revert: rev, horizon: 30, seed: 1, n_paths: paths, residual: mode, drift_keep: keep, drift_tau: tau, shape_keep: shape };
    let nm = |mode: ResidualMode, keep: f64, tau: f64, shape: f64, rev: f64| {
        let mut n = mode.name().to_string();
        if keep < 1.0 { n += &format!("+keep{keep}"); }
        if tau > 0.0 { n += &format!("+tau{tau}"); }
        if rev > 0.0 { n += &format!("+rev{rev}"); }
        if shape < 1.0 { n += &format!("+shape{shape}"); }
        n
    };
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
        let res = run_variant(&companies, p, step, threads);
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
