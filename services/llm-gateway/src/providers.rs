//! LLM providers behind one OpenAI-compatible interface. OpenAI, OpenRouter,
//! Gemini (its OpenAI-compatible endpoint) and the local llama.cpp server share
//! one adapter; Anthropic has its own; `template` answers without any model.

use anyhow::{Context, Result, anyhow};
use serde_json::{Value, json};

use ci_core::client::MtlsClient;

pub enum Provider {
    OpenAiCompatible {
        name: &'static str,
        base_url: String,
        api_key: Option<String>,
        model: String,
        external: bool,
    },
    Anthropic {
        api_key: String,
        model: String,
    },
    Template,
}

pub struct Clients {
    pub external: reqwest::Client,
    /// The local model sits behind an mTLS proxy like every other internal service.
    pub internal: MtlsClient,
}

impl Provider {
    pub fn name(&self) -> &'static str {
        match self {
            Self::OpenAiCompatible { name, .. } => name,
            Self::Anthropic { .. } => "anthropic",
            Self::Template => "template",
        }
    }

    pub fn is_external(&self) -> bool {
        match self {
            Self::OpenAiCompatible { external, .. } => *external,
            Self::Anthropic { .. } => true,
            Self::Template => false,
        }
    }

    pub async fn complete(&self, clients: &Clients, request: &Value) -> Result<Value> {
        match self {
            Self::OpenAiCompatible {
                base_url,
                api_key,
                model,
                external,
                ..
            } => {
                let mut body = request.clone();
                body["model"] = json!(model);
                body["stream"] = json!(false);
                let http = if *external {
                    clients.external.clone()
                } else {
                    clients.internal.get()
                };
                let mut req = http.post(format!("{base_url}/chat/completions")).json(&body);
                if let Some(key) = api_key {
                    req = req.bearer_auth(key);
                }
                let resp = req.send().await.context("provider request")?;
                let status = resp.status();
                if !status.is_success() {
                    return Err(anyhow!("provider returned {status}"));
                }
                Ok(resp.json().await?)
            }
            Self::Anthropic { api_key, model } => anthropic(clients, api_key, model, request).await,
            Self::Template => Ok(template(request)),
        }
    }
}

fn completion(message: Value, model: &str) -> Value {
    json!({
        "id": format!("chatcmpl-{}", uuid::Uuid::now_v7()),
        "object": "chat.completion",
        "created": chrono::Utc::now().timestamp(),
        "model": model,
        "choices": [{ "index": 0, "message": message, "finish_reason":
            if message.get("tool_calls").is_some() { "tool_calls" } else { "stop" } }],
        "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
    })
}

async fn anthropic(clients: &Clients, key: &str, model: &str, request: &Value) -> Result<Value> {
    let mut system = String::new();
    let mut messages: Vec<Value> = Vec::new();
    for m in request["messages"].as_array().cloned().unwrap_or_default() {
        match m["role"].as_str() {
            Some("system") => system.push_str(m["content"].as_str().unwrap_or_default()),
            Some("tool") => messages.push(json!({ "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"].as_str().unwrap_or_default()
            }]})),
            Some("assistant") => {
                let mut blocks = Vec::new();
                if let Some(text) = m["content"].as_str().filter(|t| !t.is_empty()) {
                    blocks.push(json!({ "type": "text", "text": text }));
                }
                for call in m["tool_calls"].as_array().cloned().unwrap_or_default() {
                    let input: Value = serde_json::from_str(call["function"]["arguments"].as_str().unwrap_or("{}")).unwrap_or(json!({}));
                    blocks.push(json!({ "type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": input }));
                }
                messages.push(json!({ "role": "assistant", "content": blocks }));
            }
            _ => messages.push(json!({ "role": "user", "content": m["content"].as_str().unwrap_or_default() })),
        }
    }
    let tools: Vec<Value> = request["tools"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|t| json!({ "name": t["function"]["name"], "description": t["function"]["description"], "input_schema": t["function"]["parameters"] }))
        .collect();
    let mut body = json!({ "model": model, "max_tokens": 1024, "messages": messages });
    if !system.is_empty() {
        body["system"] = json!(system);
    }
    if !tools.is_empty() {
        body["tools"] = json!(tools);
    }
    let resp = clients
        .external
        .post("https://api.anthropic.com/v1/messages")
        .header("x-api-key", key)
        .header("anthropic-version", "2023-06-01")
        .json(&body)
        .send()
        .await?;
    if !resp.status().is_success() {
        return Err(anyhow!("anthropic returned {}", resp.status()));
    }
    let out: Value = resp.json().await?;
    let mut text = String::new();
    let mut calls = Vec::new();
    for block in out["content"].as_array().cloned().unwrap_or_default() {
        match block["type"].as_str() {
            Some("text") => text.push_str(block["text"].as_str().unwrap_or_default()),
            Some("tool_use") => calls.push(json!({
                "id": block["id"], "type": "function",
                "function": { "name": block["name"], "arguments": block["input"].to_string() }
            })),
            _ => {}
        }
    }
    let mut message = json!({ "role": "assistant", "content": text });
    if !calls.is_empty() {
        message["tool_calls"] = json!(calls);
    }
    let mut result = completion(message, model);
    result["usage"] = json!({
        "prompt_tokens": out["usage"]["input_tokens"], "completion_tokens": out["usage"]["output_tokens"],
        "total_tokens": out["usage"]["input_tokens"].as_i64().unwrap_or(0) + out["usage"]["output_tokens"].as_i64().unwrap_or(0)
    });
    Ok(result)
}

/// Whole rubles from `<name>_rub`, or from `<name>_kop` when the caller did not convert.
fn amount(v: &Value, name: &str) -> i64 {
    v[format!("{name}_rub")]
        .as_i64()
        .unwrap_or_else(|| (v[format!("{name}_kop")].as_i64().unwrap_or(0) + 50) / 100)
}

fn rub(rubles: i64) -> String {
    let s = rubles.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i).is_multiple_of(3) {
            out.push(' ');
        }
        out.push(c);
    }
    format!("{out} ₽")
}

/// Works without a model: first asks for the gaps and the forecast, then turns
/// the tool results into a short explanation with options. Used as the last
/// fallback and in tests, so the agent loop can run end to end anywhere.
pub fn template(request: &Value) -> Value {
    let messages = request["messages"].as_array().cloned().unwrap_or_default();
    let last_user = messages.iter().rposition(|m| m["role"] == "user").unwrap_or(0);
    let tool_results: Vec<&Value> = messages[last_user..].iter().filter(|m| m["role"] == "tool").collect();
    let tool_names: Vec<String> = request["tools"]
        .as_array()
        .map(|t| {
            t.iter()
                .filter_map(|t| t["function"]["name"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();

    // Agent frameworks prefix MCP tool names (Hermes: `mcp__ci1__get_gaps`): match by suffix.
    let find = |suffix: &str| {
        tool_names
            .iter()
            .find(|n| n.as_str() == suffix || n.ends_with(&format!("__{suffix}")))
            .cloned()
    };
    if tool_results.is_empty()
        && let Some(gaps_tool) = find("get_gaps")
    {
        let mut calls = vec![
            json!({ "id": "call_gaps", "type": "function", "function": { "name": gaps_tool, "arguments": "{}" } }),
        ];
        if let Some(forecast_tool) = find("get_forecast") {
            calls.push(json!({ "id": "call_forecast", "type": "function", "function": { "name": forecast_tool, "arguments": "{\"horizon\":30}" } }));
        }
        return completion(
            json!({ "role": "assistant", "content": "", "tool_calls": calls }),
            "template",
        );
    }

    let mut gaps: Vec<Value> = Vec::new();
    let mut min_point: Option<(String, i64)> = None;
    for r in &tool_results {
        let Ok(v) = serde_json::from_str::<Value>(r["content"].as_str().unwrap_or_default()) else {
            continue;
        };
        if let Some(g) = v["gaps"].as_array() {
            gaps.extend(g.iter().cloned());
        }
        if let Some(points) = v["points"].as_array() {
            min_point = points
                .iter()
                .filter_map(|p| Some((p["date"].as_str()?.to_owned(), amount(p, "p50"))))
                .min_by_key(|(_, v)| *v);
        }
    }
    let text = if let Some(g) = gaps.first() {
        let date = g["start_date"].as_str().unwrap_or("?");
        let prob = (g["probability"].as_f64().unwrap_or(0.0) * 100.0).round();
        let p50 = amount(g, "shortfall_p50");
        let p90 = amount(g, "shortfall_p90");
        let drivers: Vec<String> = g["drivers"]
            .as_array()
            .map(|d| {
                d.iter()
                    .take(3)
                    .filter_map(|x| Some(format!("{} ({})", x["label"].as_str()?, rub(amount(x, "amount")))))
                    .collect()
            })
            .unwrap_or_default();
        let why = if drivers.is_empty() {
            String::new()
        } else {
            format!(" В этот период приходятся: {}.", drivers.join(", "))
        };
        format!(
            "Ближайший риск — {date}: не хватит около {} (вероятность {prob}%), в плохом случае до {}.{why}\n\n\
             Что можно сделать:\n1. Попросить предоплату или ускорить оплату у клиентов с открытыми счетами.\n\
             2. Перенести несрочные закупки на дату после {date}.\n\
             3. Заранее согласовать с банком короткий овердрафт на {}.\n\n\
             Это расчёт по шаблону без языковой модели.",
            rub(p50),
            rub(p90),
            rub(p90)
        )
    } else if let Some((date, value)) = min_point {
        format!(
            "Разрывов в ближайшие 30 дней не видно. Самый низкий остаток ожидается {date}: около {}.",
            rub(value)
        )
    } else {
        "Не удалось получить прогноз. Проверьте, что выписка загружена.".to_owned()
    };
    completion(json!({ "role": "assistant", "content": text }), "template")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn template_first_calls_tools() {
        let req = json!({
            "messages": [{"role": "user", "content": "что будет?"}],
            "tools": [{"type": "function", "function": {"name": "get_gaps"}}]
        });
        let out = template(&req);
        assert_eq!(
            out["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "get_gaps"
        );
    }

    #[test]
    fn template_finds_prefixed_mcp_tools() {
        let req = json!({
            "messages": [{"role": "user", "content": "что будет?"}],
            "tools": [{"type": "function", "function": {"name": "mcp__ci1__get_gaps"}}]
        });
        let out = template(&req);
        assert_eq!(
            out["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
            "mcp__ci1__get_gaps"
        );
    }

    #[test]
    fn template_explains_gap_from_tool_result() {
        let gaps = json!({"gaps": [{"start_date": "2026-06-28", "probability": 0.64,
            "shortfall_p50_kop": 41_000_000, "shortfall_p90_kop": 60_000_000,
            "drivers": [{"label": "ЕНП", "amount_kop": 18_000_000}]}]});
        let req = json!({ "messages": [
            {"role": "user", "content": "что будет?"},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "tool_call_id": "call_gaps", "content": gaps.to_string()}
        ]});
        let text = template(&req)["choices"][0]["message"]["content"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(text.contains("2026-06-28") && text.contains("410 000 ₽") && text.contains("ЕНП"));
    }
}
