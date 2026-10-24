use axum::Json;
use serde_json::Value;

const MONEY_KEYS: &[&str] = &[
    "opening_balance", "p10", "p50", "p90", "amount", "median_path_deficit", "max_deficit_p90",
    "deficit_given_break_median", "amount_kop", "balance_kop", "mae_model", "mae_last_value", "mae_drift",
    "plan_in", "plan_out", "fact_in", "fact_out", "plan", "fact", "diff", "predicted", "actual",
];

/// Суммы в JSON — десятичные строки (JS не теряет точность).
pub fn stringify_money(v: &mut Value) {
    match v {
        Value::Object(m) => {
            for (k, x) in m.iter_mut() {
                if MONEY_KEYS.contains(&k.as_str()) && x.is_i64() {
                    *x = Value::String(x.as_i64().unwrap().to_string());
                } else {
                    stringify_money(x);
                }
            }
        }
        Value::Array(a) => a.iter_mut().for_each(stringify_money),
        _ => {}
    }
}

pub fn money(mut v: Value) -> Json<Value> {
    stringify_money(&mut v);
    Json(v)
}
