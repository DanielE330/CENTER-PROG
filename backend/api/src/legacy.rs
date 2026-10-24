//! Разовая миграция команд из версии, где состав хранил auth (user -> владелец компании + роль owner|admin|analyst|observer),
//! в ledger (`company_members`: director|admin|editor|viewer). Идемпотентна: повторный запуск ничего не меняет.
//! Запускается при старте api в фоне с повторами (auth и ledger могут подняться позже).

use crate::AppState;
use axum::http::{Method, StatusCode};
use common::Ctx;
use serde_json::json;
use std::time::Duration;

/// Роль прежней версии -> роль ledger. Владелец-не-создатель (после передачи владения) обрабатывается отдельно.
pub fn map_role(old: &str) -> &'static str {
    match old {
        "admin" => "admin",
        "analyst" | "editor" => "editor",
        _ => "viewer",
    }
}

/// Итог прохода: сколько участников перенесено, пропущено (уже перенесены или не могут быть перенесены) и сколько владений передано.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub moved: usize,
    pub skipped: usize,
    pub transferred: usize,
}

pub async fn migrate(st: &AppState) -> Result<Report, String> {
    let ctx = Ctx { rid: "legacy-team-migration".into(), actor: "system".into() };
    let r = st.auth.json(Method::GET, "/internal/v1/memberships/legacy", &ctx, None).await.map_err(|e| format!("auth: {}", e.0))?;
    if !r.status.is_success() {
        return Err(format!("auth: {}", r.status));
    }
    let rows = r.json()["items"].as_array().cloned().unwrap_or_default();
    let mut rep = Report::default();
    let mut owners: Vec<(String, String)> = vec![]; // (company_id, новый владелец)
    for row in &rows {
        let (uid, tenant, old) = (row["user_id"].as_str().unwrap_or(""), row["tenant_id"].as_str().unwrap_or(""), row["role"].as_str().unwrap_or(""));
        if uid.is_empty() || tenant.is_empty() || uid == tenant {
            rep.skipped += 1;
            continue;
        }
        // компания владельца: ledger создаёт её (и директора) при первом обращении, а в уже мигрированной базе возвращает существующую
        let co = st.ledger.json(Method::POST, "/internal/v1/companies/ensure", &ctx, Some(&json!({"owner_id": tenant, "email": ""}))).await.map_err(|e| format!("ledger: {}", e.0))?;
        let Some(cid) = co.json()["company_id"].as_str().map(str::to_string) else { rep.skipped += 1; continue };
        let role = if old == "owner" { "admin" } else { map_role(old) };
        let j = st
            .ledger
            .json(Method::POST, &format!("/internal/v1/companies/{cid}/members/join"), &ctx, Some(&json!({"user_id": uid, "email": "", "role": role})))
            .await
            .map_err(|e| format!("ledger: {}", e.0))?;
        match j.status {
            s if s.is_success() => {
                rep.moved += 1;
                // владение передаём только тем, кого перенесли сейчас: иначе повтор вернул бы права тому, у кого их уже забрали
                if old == "owner" {
                    owners.push((cid, uid.to_string()));
                }
            }
            StatusCode::CONFLICT => rep.skipped += 1, // уже в команде или есть своя компания с данными
            s => return Err(format!("ledger join: {s}")),
        }
    }
    for (cid, uid) in owners {
        let t = st.ledger.json(Method::POST, &format!("/internal/v1/companies/{cid}/transfer"), &ctx, Some(&json!({"user_id": uid}))).await.map_err(|e| format!("ledger: {}", e.0))?;
        if t.status.is_success() {
            rep.transferred += 1;
        }
    }
    // старая таблица откладывается: удалённых из команды позже повторный проход не воскресит
    let done = st.auth.json(Method::POST, "/internal/v1/memberships/legacy/done", &ctx, Some(&json!({}))).await.map_err(|e| format!("auth: {}", e.0))?;
    if !done.status.is_success() {
        return Err(format!("auth done: {}", done.status));
    }
    Ok(rep)
}

/// Фоновый запуск: до 10 попыток раз в 3 секунды, дальше сдаёмся (повторится при следующем старте).
pub async fn run(st: AppState) {
    for attempt in 1..=10 {
        match migrate(&st).await {
            Ok(r) if r != Report::default() => {
                eprintln!("legacy team migration: moved={} skipped={} transferred={}", r.moved, r.skipped, r.transferred);
                return;
            }
            Ok(_) => return,
            Err(e) => {
                if attempt == 10 {
                    eprintln!("legacy team migration gave up: {e}");
                    return;
                }
                tokio::time::sleep(Duration::from_secs(3)).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn old_roles_map_to_ledger_roles() {
        assert_eq!(map_role("admin"), "admin");
        assert_eq!(map_role("analyst"), "editor");
        assert_eq!(map_role("observer"), "viewer");
        assert_eq!(map_role("owner"), "viewer"); // владелец-не-создатель обрабатывается передачей владения
        assert_eq!(map_role("что-то"), "viewer");
    }
}
