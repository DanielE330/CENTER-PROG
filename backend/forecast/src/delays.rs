//! Задержки оплат по Каплану–Мейеру: кривая «доля ещё не заплативших» по контрагенту со сжатием к общей кривой.
//! Цензурирование: неоплаченный счёт возраста a даёт наблюдение «задержка не меньше a» (событие не наступило).
//! Идея — из проекта «ЦИ1» (ветка gleb, swimshadow), портирована на Rust; задержки считаются в рабочих днях.

use crate::Rng;
use std::collections::HashMap;

/// Сила сжатия: вес кривой контрагента w = n / (n + K).
pub const SHRINK_K: f64 = 5.0;
/// Минимум наблюдений в общей кривой, ниже которого модель не используется (остаётся таблица по умолчанию).
pub const MIN_POOL: usize = 8;
/// Максимальный «хвост» за последней наблюдённой задержкой, рабочих дней.
const TAIL_DAYS: usize = 7;

/// Оценка Каплана–Мейера: после каждого момента `t` доля, чья задержка больше `t`.
#[derive(Clone, Debug, Default)]
pub struct Curve {
    /// (t, S(t)) по возрастанию t; S убывает.
    pub steps: Vec<(i64, f64)>,
    pub n: usize,
}

impl Curve {
    /// `obs`: (задержка в днях, наступило ли событие — оплата); `false` — цензурированное наблюдение.
    pub fn fit(obs: &[(i64, bool)]) -> Curve {
        let mut times: Vec<i64> = obs.iter().filter(|o| o.1).map(|o| o.0.max(0)).collect();
        times.sort_unstable();
        times.dedup();
        let mut s = 1.0;
        let mut steps = Vec::with_capacity(times.len());
        for t in times {
            let at_risk = obs.iter().filter(|o| o.0.max(0) >= t).count() as f64;
            let died = obs.iter().filter(|o| o.1 && o.0.max(0) == t).count() as f64;
            if at_risk > 0.0 {
                s *= 1.0 - died / at_risk;
            }
            steps.push((t, s));
        }
        Curve { steps, n: obs.len() }
    }

    /// S(t): вероятность, что оплата придёт позже, чем через `t` дней.
    pub fn survival(&self, t: i64) -> f64 {
        self.steps.iter().take_while(|(x, _)| *x <= t).last().map_or(1.0, |(_, s)| *s)
    }

    /// Среднее по наблюдённой части кривой (рабочие дни; масса цензурированного хвоста не учитывается).
    pub fn mean(&self) -> f64 {
        let mut prev = 1.0;
        let mut m = 0.0;
        for (t, s) in &self.steps {
            m += (prev - s) * *t as f64;
            prev = *s;
        }
        m
    }

    /// Задержка `d >= age`, условная на «ещё не заплатил к возрасту `age`». None — выборка попала в хвост
    /// за пределами наблюдений (доля цензурированного хвоста не определена).
    fn sample(&self, age: i64, u: f64) -> Option<i64> {
        let before = if age > 0 { self.survival(age - 1) } else { 1.0 };
        if before <= 0.0 {
            return None;
        }
        let target = u * before; // S(d) <= target
        self.steps.iter().find(|(t, s)| *t >= age && *s <= target).map(|(t, _)| *t)
    }

    fn tail(&self) -> i64 {
        self.steps.last().map_or(0, |(t, _)| *t)
    }
}

#[derive(Clone, Debug, Default)]
pub struct DelayModel {
    pub global: Curve,
    pub by_cp: HashMap<String, Curve>,
}

impl DelayModel {
    /// `obs`: (контрагент, задержка, наступило ли событие). Пустой контрагент идёт только в общую кривую.
    /// None — наблюдений мало (`MIN_POOL`), вызывающий использует таблицу по умолчанию.
    pub fn fit(obs: &[(String, i64, bool)]) -> Option<DelayModel> {
        if obs.len() < MIN_POOL || !obs.iter().any(|o| o.2) {
            return None;
        }
        let all: Vec<(i64, bool)> = obs.iter().map(|o| (o.1, o.2)).collect();
        let mut groups: HashMap<&str, Vec<(i64, bool)>> = HashMap::new();
        for o in obs.iter().filter(|o| !o.0.is_empty()) {
            groups.entry(o.0.as_str()).or_default().push((o.1, o.2));
        }
        let by_cp = groups.into_iter().filter(|(_, v)| v.iter().any(|x| x.1)).map(|(k, v)| (k.to_string(), Curve::fit(&v))).collect();
        Some(DelayModel { global: Curve::fit(&all), by_cp })
    }

    /// Вес кривой контрагента: n / (n + K).
    pub fn weight(&self, cp: &str) -> f64 {
        self.by_cp.get(cp).map_or(0.0, |c| c.n as f64 / (c.n as f64 + SHRINK_K))
    }

    /// Случайная задержка (рабочие дни) для контрагента, условная на возраст неоплаченного счёта `age`.
    /// Кривая контрагента выбирается с вероятностью `weight`, иначе общая.
    pub fn sample(&self, cp: &str, age: i64, rng: &mut Rng) -> i64 {
        let curve = match self.by_cp.get(cp) {
            Some(c) if rng.unit() < self.weight(cp) => c,
            _ => &self.global,
        };
        match curve.sample(age, rng.unit().max(1e-12)) {
            Some(d) => d,
            None => age.max(curve.tail()) + 1 + rng.below(TAIL_DAYS) as i64,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obs(v: &[(i64, bool)]) -> Vec<(i64, bool)> {
        v.to_vec()
    }

    #[test]
    fn km_without_censoring_is_empirical_survival() {
        let c = Curve::fit(&obs(&[(0, true), (2, true), (2, true), (4, true)]));
        assert!((c.survival(0) - 0.75).abs() < 1e-9);
        assert!((c.survival(2) - 0.25).abs() < 1e-9);
        assert!(c.survival(4).abs() < 1e-9);
        assert!((c.mean() - 2.0).abs() < 1e-9);
    }

    #[test]
    fn censored_unpaid_invoices_shift_the_curve_right() {
        let paid = obs(&[(1, true), (2, true), (3, true), (4, true)]);
        let mut with_unpaid = paid.clone();
        with_unpaid.extend([(10, false), (12, false)]);
        let a = Curve::fit(&paid);
        let b = Curve::fit(&with_unpaid);
        assert!(b.survival(4) > a.survival(4), "{} vs {}", b.survival(4), a.survival(4));
    }

    #[test]
    fn conditional_sample_never_below_age() {
        let c = Curve::fit(&obs(&[(0, true), (1, true), (3, true), (5, true), (8, true), (9, true)]));
        let mut rng = Rng::new(7);
        for _ in 0..500 {
            if let Some(d) = c.sample(4, rng.unit().max(1e-12)) {
                assert!(d >= 4);
            }
        }
    }

    #[test]
    fn shrinkage_weight_grows_with_observations() {
        let mut o: Vec<(String, i64, bool)> = (0..10).map(|i| ("общий".to_string(), i % 4, true)).collect();
        o.extend((0..3).map(|_| ("А".to_string(), 9, true)));
        o.extend((0..45).map(|_| ("Б".to_string(), 1, true)));
        let m = DelayModel::fit(&o).unwrap();
        assert!((m.weight("А") - 3.0 / 8.0).abs() < 1e-9);
        assert!((m.weight("Б") - 45.0 / 50.0).abs() < 1e-9);
        assert_eq!(m.weight("нет такого"), 0.0);
    }

    #[test]
    fn counterparty_curve_dominates_with_history_and_is_deterministic() {
        let mut o: Vec<(String, i64, bool)> = (0..20).map(|_| ("быстрый".to_string(), 0, true)).collect();
        o.extend((0..20).map(|_| ("медленный".to_string(), 10, true)));
        let m = DelayModel::fit(&o).unwrap();
        let mean = |cp: &str| {
            let mut rng = Rng::new(1);
            (0..2000).map(|_| m.sample(cp, 0, &mut rng)).sum::<i64>() as f64 / 2000.0
        };
        assert!(mean("медленный") > mean("быстрый") + 5.0);
        let (mut a, mut b) = (Rng::new(3), Rng::new(3));
        assert_eq!(m.sample("медленный", 0, &mut a), m.sample("медленный", 0, &mut b));
    }

    #[test]
    fn too_few_observations_means_no_model() {
        assert!(DelayModel::fit(&[("А".into(), 1, true)]).is_none());
        let all_cens: Vec<_> = (0..20).map(|_| ("А".to_string(), 3, false)).collect();
        assert!(DelayModel::fit(&all_cens).is_none());
    }

    #[test]
    fn overdue_invoice_sample_exceeds_age_even_in_tail() {
        let o: Vec<(String, i64, bool)> = (0..10).map(|i| ("А".to_string(), i, true)).collect();
        let m = DelayModel::fit(&o).unwrap();
        let mut rng = Rng::new(5);
        for _ in 0..200 {
            assert!(m.sample("А", 20, &mut rng) >= 20);
        }
    }
}
