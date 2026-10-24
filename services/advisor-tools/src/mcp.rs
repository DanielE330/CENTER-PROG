//! Model Context Protocol over streamable HTTP, the subset the agent needs:
//! `initialize`, `tools/list`, `tools/call`, `ping`. Answers are plain JSON.

use axum::{
    Json,
    extract::State,
    http::{HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use ci_core::auth::AuthUser;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::AppState;

const PROTOCOL: &str = "2025-06-18";

#[derive(Deserialize)]
pub struct RpcRequest {
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Value,
}

fn ok(id: Value, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

fn err(id: Value, code: i64, message: &str) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}

pub fn tools() -> Value {
    let horizon = json!({ "type": "integer", "minimum": 7, "maximum": 90, "description": "Горизонт прогноза в днях" });
    json!([
        { "name": "get_forecast", "description": "Прогноз остатка по дням: медиана и коридор P10–P90, в копейках.",
          "inputSchema": { "type": "object", "properties": { "horizon": horizon } } },
        { "name": "get_gaps", "description": "Кассовые разрывы: дата начала, вероятность, нехватка P50/P90 и причины (drivers).",
          "inputSchema": { "type": "object", "properties": {} } },
        { "name": "get_plan_fact", "description": "План-факт: что ожидали и что пришло по неделям.",
          "inputSchema": { "type": "object", "properties": {} } },
        { "name": "get_obligations", "description": "Будущие платежи и поступления: счета, налоги, ручные события.",
          "inputSchema": { "type": "object", "properties": { "status": { "type": "string", "enum": ["planned", "overdue", "paid"] } } } },
        { "name": "get_category_summary", "description": "Сумма поступлений и списаний по группам категорий за период.",
          "inputSchema": { "type": "object", "properties": { "days": { "type": "integer", "minimum": 7, "maximum": 365 } } } },
        { "name": "run_scenario", "description": "Посчитать сценарий «что если» без сохранения. transforms: delay_counterparty {counterparty_ref, days}, scale_group {group, factor}, one_off {amount_kop, date, direction}, scale_recurring {group, factor}.",
          "inputSchema": { "type": "object", "required": ["transforms"], "properties": { "transforms": { "type": "array", "items": { "type": "object" } } } } },
        { "name": "propose_obligation", "description": "Предложить пользователю добавить ожидаемый платёж. Применится только после подтверждения.",
          "inputSchema": { "type": "object", "required": ["direction", "amount_rub", "due_date"], "properties": {
              "direction": { "type": "string", "enum": ["inflow", "outflow"] },
              "amount_rub": { "type": "number", "exclusiveMinimum": 0 },
              "due_date": { "type": "string", "format": "date" },
              "note": { "type": "string", "maxLength": 200 } } } },
        { "name": "propose_scenario", "description": "Предложить сохранить сценарий «что если». Применится только после подтверждения.",
          "inputSchema": { "type": "object", "required": ["name", "transforms"], "properties": {
              "name": { "type": "string", "maxLength": 100 }, "transforms": { "type": "array", "items": { "type": "object" } } } } }
    ])
}

pub async fn handle(
    State(state): State<AppState>,
    user: Option<AuthUser>,
    headers: HeaderMap,
    Json(req): Json<RpcRequest>,
) -> Response {
    let Some(id) = req.id.clone() else {
        // Notifications (e.g. notifications/initialized) get no body.
        return StatusCode::ACCEPTED.into_response();
    };
    let body = match req.method.as_str() {
        "initialize" => ok(
            id,
            json!({
                "protocolVersion": PROTOCOL,
                "capabilities": { "tools": { "listChanged": false } },
                "serverInfo": { "name": "ci1-tools", "version": env!("CARGO_PKG_VERSION") }
            }),
        ),
        "ping" => ok(id, json!({})),
        "tools/list" => ok(id, json!({ "tools": tools() })),
        // Listing tools reveals no data: the agent runtime discovers them before any user is
        // known (the connection itself is mTLS from `advisor` only). Calls need the user's token.
        "tools/call" if user.is_none() => err(id, -32001, "user context required"),
        "tools/call" => {
            let Some(user) = user.as_ref() else {
                return StatusCode::UNAUTHORIZED.into_response();
            };
            let name = req.params["name"].as_str().unwrap_or_default().to_owned();
            let args = req.params["arguments"].clone();
            match call_tool(&state, user, &name, &args).await {
                Ok(mut v) => ok(id, {
                    to_rubles(&mut v);
                    json!({ "content": [{ "type": "text", "text": v.to_string() }], "isError": false })
                }),
                Err(e) => {
                    tracing::warn!(tool = %name, error = %e, "tool failed");
                    ok(
                        id,
                        json!({ "content": [{ "type": "text", "text": format!("Ошибка инструмента: {e}") }], "isError": true }),
                    )
                }
            }
        }
        _ => err(id, -32601, "method not found"),
    };
    let mut resp = Json(body).into_response();
    let session = headers.get("mcp-session-id").cloned().unwrap_or_else(|| {
        HeaderValue::from_str(&uuid::Uuid::now_v7().to_string()).unwrap_or(HeaderValue::from_static("ci1"))
    });
    resp.headers_mut().insert("mcp-session-id", session);
    resp
}

/// Models are bad at dividing by 100: every `*_kop` integer reaches the agent as whole
/// rubles in `*_rub`. Storage and the APIs keep kopecks.
fn to_rubles(value: &mut Value) {
    match value {
        Value::Object(map) => {
            let keys: Vec<String> = map.keys().filter(|k| k.ends_with("_kop")).cloned().collect();
            for k in keys {
                if let Some(kop) = map.get(&k).and_then(Value::as_i64) {
                    map.remove(&k);
                    let rub = (kop as f64 / 100.0).round() as i64;
                    map.insert(format!("{}_rub", k.trim_end_matches("_kop")), json!(rub));
                }
            }
            map.values_mut().for_each(to_rubles);
        }
        Value::Array(items) => items.iter_mut().for_each(to_rubles),
        _ => {}
    }
}

fn arg_i64(args: &Value, key: &str, default: i64, min: i64, max: i64) -> i64 {
    args[key].as_i64().unwrap_or(default).clamp(min, max)
}

async fn call_tool(state: &AppState, user: &AuthUser, name: &str, args: &Value) -> anyhow::Result<Value> {
    let s = &state.inner;
    let get = reqwest::Method::GET;
    match name {
        "propose_obligation" => {
            let amount = args["amount_rub"]
                .as_f64()
                .filter(|a| *a > 0.0 && *a < 1e11)
                .ok_or_else(|| anyhow::anyhow!("amount_rub"))?;
            let direction = args["direction"]
                .as_str()
                .filter(|d| matches!(*d, "inflow" | "outflow"))
                .ok_or_else(|| anyhow::anyhow!("direction"))?;
            let due = chrono::NaiveDate::parse_from_str(args["due_date"].as_str().unwrap_or_default(), "%Y-%m-%d")?;
            let note: String = args["note"].as_str().unwrap_or_default().chars().take(200).collect();
            let payload = json!({ "kind": "manual", "direction": direction, "amount_kop": (amount * 100.0).round() as i64,
                                  "due_date": due, "certainty": 0.8, "note": note });
            let verb = if direction == "inflow" {
                "Ожидаемое поступление"
            } else {
                "Плановый платёж"
            };
            let title = format!("{verb} {amount:.0} ₽ на {}", due.format("%d.%m.%Y"));
            let id = s.propose(user, "obligation", &title, payload).await?;
            return Ok(
                json!({ "proposal_id": id, "status": "pending", "message": "Пользователь увидит карточку и решит сам" }),
            );
        }
        "propose_scenario" => {
            let name_arg: String = args["name"].as_str().unwrap_or("Сценарий").chars().take(100).collect();
            let payload = json!({ "name": name_arg, "transforms": args["transforms"] });
            let id = s
                .propose(user, "scenario", &format!("Сценарий: {name_arg}"), payload)
                .await?;
            return Ok(json!({ "proposal_id": id, "status": "pending" }));
        }
        _ => {}
    }
    let token = s.exchange(&user.token).await?;
    match name {
        "get_forecast" => {
            let h = arg_i64(args, "horizon", 30, 7, 90);
            s.call(get, "forecast", &format!("/api/v1/forecast?horizon={h}"), &token, None)
                .await
        }
        "get_gaps" => s.call(get, "forecast", "/api/v1/gaps", &token, None).await,
        "get_plan_fact" => s.call(get, "forecast", "/api/v1/plan-fact", &token, None).await,
        "get_obligations" => {
            let status = args["status"]
                .as_str()
                .filter(|v| matches!(*v, "planned" | "overdue" | "paid"));
            let path = status.map_or("/api/v1/obligations".to_owned(), |v| {
                format!("/api/v1/obligations?status={v}")
            });
            s.call(get, "obligations", &path, &token, None).await
        }
        "get_category_summary" => {
            let d = arg_i64(args, "days", 90, 7, 365);
            s.call(
                get,
                "categorizer",
                &format!("/api/v1/categories/summary?days={d}"),
                &token,
                None,
            )
            .await
        }
        "run_scenario" => {
            let body = json!({ "transforms": args["transforms"] });
            s.call(
                reqwest::Method::POST,
                "forecast",
                "/api/v1/scenarios/preview",
                &token,
                Some(&body),
            )
            .await
        }
        other => anyhow::bail!("unknown tool {other}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_tool_has_object_schema() {
        let tools = tools();
        let list = tools.as_array().unwrap();
        assert_eq!(list.len(), 8);
        assert!(list.iter().all(|t| t["inputSchema"]["type"] == "object"));
    }

    #[test]
    fn kopecks_become_rubles_for_the_agent() {
        let mut v = json!({"gaps": [{"shortfall_p50_kop": 465_443_650, "probability": 0.7}]});
        to_rubles(&mut v);
        assert_eq!(v["gaps"][0]["shortfall_p50_rub"], 4_654_437);
        assert!(v["gaps"][0].get("shortfall_p50_kop").is_none());
    }

    #[test]
    fn write_tools_are_only_proposals() {
        let tools = tools();
        let names: Vec<&str> = tools
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|t| t["name"].as_str())
            .collect();
        assert!(
            names
                .iter()
                .all(|n| n.starts_with("get_") || n.starts_with("run_") || n.starts_with("propose_"))
        );
    }
}
