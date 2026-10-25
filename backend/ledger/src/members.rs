//! Участники компании и роли (director / admin / editor / viewer). Директор создаётся вместе с компанией; его нельзя ни удалить,
//! ни понизить, но владение можно передать (`transfer`): прежний директор становится администратором. Права ролей проверяет api; ledger хранит членство и защищает инварианты.

use crate::AppState;
use crate::routes::{audit, lock, member_view, not_found, now};
use axum::{
    Json,
    extract::{Path, State},
    http::StatusCode,
};
use common::{Ctx, R, err, internal};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

pub const MAX_MEMBERS: i64 = 50;

fn staff_role(r: &str) -> R<&str> {
    match r {
        "admin" | "editor" | "viewer" => Ok(r),
        _ => Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_role", "Роль сотрудника: admin, editor или viewer")),
    }
}

fn company_exists(c: &rusqlite::Connection, cid: &str) -> R<()> {
    let n: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [cid], |r| r.get(0)).map_err(internal)?;
    if n == 0 { Err(not_found()) } else { Ok(()) }
}

/// GET /internal/v1/members/by-user/{user_id}: в какой компании состоит пользователь и в какой роли.
pub async fn by_user(State(st): State<AppState>, Path(uid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    let m: Option<(String, String)> = c
        .query_row("SELECT company_id, role FROM company_members WHERE user_id=?", [&uid], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()
        .map_err(internal)?;
    let (cid, role) = m.ok_or_else(not_found)?;
    Ok(Json(member_view(&c, &cid, &role)?))
}

pub(crate) fn list_of(c: &rusqlite::Connection, cid: &str) -> R<Vec<Value>> {
    let mut s = c
        .prepare("SELECT user_id, email, role, created_at FROM company_members WHERE company_id=? ORDER BY CASE role WHEN 'director' THEN 0 WHEN 'admin' THEN 1 WHEN 'editor' THEN 2 ELSE 3 END, created_at, user_id")
        .map_err(internal)?;
    let v = s
        .query_map([cid], |r| Ok(json!({"user_id": r.get::<_, String>(0)?, "email": r.get::<_, String>(1)?, "role": r.get::<_, String>(2)?, "created_at": r.get::<_, String>(3)?})))
        .map_err(internal)?
        .filter_map(|x| x.ok())
        .collect();
    Ok(v)
}

pub async fn list_members(State(st): State<AppState>, Path(cid): Path<String>) -> R<Json<Value>> {
    let c = lock(&st);
    company_exists(&c, &cid)?;
    Ok(Json(json!({"items": list_of(&c, &cid)?})))
}

#[derive(Deserialize)]
pub struct MemberIn {
    user_id: String,
    #[serde(default)]
    email: String,
    role: String,
}

pub async fn add_member(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<MemberIn>) -> R<(StatusCode, Json<Value>)> {
    let role = staff_role(&b.role)?;
    let uid = b.user_id.trim();
    if uid.is_empty() || uid.len() > 64 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_user", "Некорректный пользователь"));
    }
    let email = b.email.trim().to_lowercase();
    if email.len() > 254 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_email", "Некорректный email"));
    }
    let created_at = {
        let c = lock(&st);
        company_exists(&c, &cid)?;
        let taken: i64 = c.query_row("SELECT COUNT(*) FROM company_members WHERE user_id=?", [uid], |r| r.get(0)).map_err(internal)?;
        if taken > 0 {
            return Err(err(StatusCode::CONFLICT, "member_exists", "Пользователь уже состоит в компании"));
        }
        let n: i64 = c.query_row("SELECT COUNT(*) FROM company_members WHERE company_id=?", [&cid], |r| r.get(0)).map_err(internal)?;
        if n >= MAX_MEMBERS {
            return Err(err(StatusCode::CONFLICT, "too_many_members", "Достигнут предел числа сотрудников"));
        }
        let t = now();
        c.execute("INSERT INTO company_members(company_id, user_id, email, role, created_at, created_by) VALUES(?,?,?,?,?,?)", params![cid, uid, email, role, t, ctx.actor])
            .map_err(internal)?;
        t
    };
    audit(&st, &ctx, &cid, &format!("member.add:{role}"), uid, "ok");
    Ok((StatusCode::CREATED, Json(json!({"user_id": uid, "email": email, "role": role, "created_at": created_at}))))
}

#[derive(Deserialize)]
pub struct MemberPatch {
    #[serde(default)]
    role: Option<String>,
    /// служебное: email директора после установки пароля (роль при этом не меняется)
    #[serde(default)]
    email: Option<String>,
}

pub async fn patch_member(State(st): State<AppState>, ctx: Ctx, Path((cid, uid)): Path<(String, String)>, Json(b): Json<MemberPatch>) -> R<Json<Value>> {
    if b.role.is_none() && b.email.is_none() {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_role", "Укажите роль"));
    }
    let role: Option<&str>;
    let result = {
        let c = lock(&st);
        let cur: Option<String> = c
            .query_row("SELECT role FROM company_members WHERE company_id=? AND user_id=?", params![cid, uid], |r| r.get(0))
            .optional()
            .map_err(internal)?;
        let cur = cur.ok_or_else(not_found)?;
        if cur == "director" && b.role.is_some() {
            return Err(err(StatusCode::CONFLICT, "cannot_change_director", "Роль директора изменить нельзя"));
        }
        role = b.role.as_deref().map(staff_role).transpose()?;
        let email = match b.email.as_deref() {
            Some(e) => {
                let e = e.trim().to_lowercase();
                if e.is_empty() || e.len() > 254 {
                    return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_email", "Некорректный email"));
                }
                Some(e)
            }
            None => None,
        };
        if let Some(r) = role {
            c.execute("UPDATE company_members SET role=? WHERE company_id=? AND user_id=?", params![r, cid, uid]).map_err(internal)?;
        }
        if let Some(e) = email {
            c.execute("UPDATE company_members SET email=? WHERE company_id=? AND user_id=?", params![e, cid, uid]).map_err(internal)?;
        }
        c.query_row("SELECT user_id, email, role, created_at FROM company_members WHERE user_id=?", [&uid], |r| {
            Ok(json!({"user_id": r.get::<_, String>(0)?, "email": r.get::<_, String>(1)?, "role": r.get::<_, String>(2)?, "created_at": r.get::<_, String>(3)?}))
        })
        .map_err(internal)?
    };
    if let Some(r) = role {
        audit(&st, &ctx, &cid, &format!("member.role:{r}"), &uid, "ok");
    }
    Ok(Json(result))
}

pub async fn delete_member(State(st): State<AppState>, ctx: Ctx, Path((cid, uid)): Path<(String, String)>) -> R<StatusCode> {
    {
        let c = lock(&st);
        let cur: Option<String> = c
            .query_row("SELECT role FROM company_members WHERE company_id=? AND user_id=?", params![cid, uid], |r| r.get(0))
            .optional()
            .map_err(internal)?;
        match cur.as_deref() {
            None => return Err(not_found()),
            Some("director") => return Err(err(StatusCode::CONFLICT, "cannot_remove_director", "Директора удалить нельзя")),
            _ => {}
        }
        c.execute("DELETE FROM company_members WHERE company_id=? AND user_id=?", params![cid, uid]).map_err(internal)?;
    }
    audit(&st, &ctx, &cid, "member.remove", &uid, "ok");
    Ok(StatusCode::NO_CONTENT)
}

/// Компания «чистая»: в ней один участник и нет данных. Такую пользователь оставляет, принимая приглашение в другую команду.
fn is_fresh(c: &rusqlite::Connection, cid: &str) -> R<bool> {
    let n = |sql: &str| -> R<i64> { c.query_row(sql, [cid], |r| r.get(0)).map_err(internal) };
    Ok(n("SELECT COUNT(*) FROM company_members WHERE company_id=?")? == 1
        && n("SELECT COUNT(*) FROM transactions WHERE company_id=?")? == 0
        && n("SELECT COUNT(*) FROM expected_events WHERE company_id=?")? == 0
        && n("SELECT COUNT(*) FROM imports WHERE company_id=?")? == 0)
}

/// POST /internal/v1/companies/{cid}/members/join: принять приглашение. Как `add_member`, но пользователь, у которого уже есть
/// собственная пустая компания (создаётся при первом входе), переезжает из неё; с данными или командой переехать нельзя.
pub async fn join_member(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<MemberIn>) -> R<(StatusCode, Json<Value>)> {
    let role = staff_role(&b.role)?;
    let uid = b.user_id.trim().to_string();
    let email = b.email.trim().to_lowercase();
    if uid.is_empty() || uid.len() > 64 || email.len() > 254 {
        return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_user", "Некорректный пользователь"));
    }
    let created_at = {
        let c = lock(&st);
        company_exists(&c, &cid)?;
        let cur: Option<String> = c.query_row("SELECT company_id FROM company_members WHERE user_id=?", [&uid], |r| r.get(0)).optional().map_err(internal)?;
        if let Some(old) = cur {
            if old == cid {
                return Err(err(StatusCode::CONFLICT, "already_member", "Вы уже состоите в этой команде"));
            }
            if !is_fresh(&c, &old)? {
                return Err(err(StatusCode::CONFLICT, "has_company_data", "У вас уже есть своя компания с данными или командой"));
            }
            c.execute("DELETE FROM company_members WHERE company_id=?", [&old]).map_err(internal)?;
            c.execute("DELETE FROM tenant_rules WHERE company_id=?", [&old]).map_err(internal)?;
            c.execute("DELETE FROM integration_connections WHERE company_id=?", [&old]).map_err(internal)?;
            c.execute("DELETE FROM companies WHERE id=?", [&old]).map_err(internal)?;
        }
        let n: i64 = c.query_row("SELECT COUNT(*) FROM company_members WHERE company_id=?", [&cid], |r| r.get(0)).map_err(internal)?;
        if n >= MAX_MEMBERS {
            return Err(err(StatusCode::CONFLICT, "too_many_members", "Достигнут предел числа сотрудников"));
        }
        let t = now();
        c.execute("INSERT INTO company_members(company_id, user_id, email, role, created_at, created_by) VALUES(?,?,?,?,?,?)", params![cid, uid, email, role, t, ctx.actor])
            .map_err(internal)?;
        t
    };
    audit(&st, &ctx, &cid, &format!("member.join:{role}"), &uid, "ok");
    Ok((StatusCode::CREATED, Json(json!({"user_id": uid, "email": email, "role": role, "created_at": created_at}))))
}

#[derive(Deserialize)]
pub struct TransferIn {
    user_id: String,
}

/// POST /internal/v1/companies/{cid}/transfer: директор передаёт владение участнику и сам становится администратором.
pub async fn transfer(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<TransferIn>) -> R<Json<Value>> {
    {
        let mut c = lock(&st);
        company_exists(&c, &cid)?;
        let role_of = |c: &rusqlite::Connection, u: &str| -> R<Option<String>> {
            c.query_row("SELECT role FROM company_members WHERE company_id=? AND user_id=?", params![cid, u], |r| r.get(0)).optional().map_err(internal)
        };
        if role_of(&c, &b.user_id)?.is_none() {
            return Err(not_found());
        }
        let old: String = c.query_row("SELECT user_id FROM company_members WHERE company_id=? AND role='director'", [&cid], |r| r.get(0)).map_err(internal)?;
        if old == b.user_id {
            return Err(err(StatusCode::UNPROCESSABLE_ENTITY, "same_owner", "Этот участник уже директор"));
        }
        let tx = c.transaction().map_err(internal)?;
        tx.execute("UPDATE company_members SET role='admin' WHERE company_id=? AND user_id=?", params![cid, old]).map_err(internal)?;
        tx.execute("UPDATE company_members SET role='director' WHERE company_id=? AND user_id=?", params![cid, b.user_id]).map_err(internal)?;
        tx.commit().map_err(internal)?;
    }
    audit(&st, &ctx, &cid, "member.transfer", &b.user_id, "ok");
    Ok(Json(json!({"director": b.user_id, "members": list_of(&lock(&st), &cid)?})))
}
