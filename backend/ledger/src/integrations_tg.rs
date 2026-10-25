//! Подключение Telegram-бота компании (`integration_connections`, `kind = telegram`). Директор вводит токен СВОЕГО бота
//! (@BotFather), notifier ведёт этим ботом привязку чатов и рассылки компании; без токена работает общий бот платформы.
//!
//! Токен хранится только зашифрованным тем же способом, что пароль 1С (`integrations::seal`: encrypt-then-MAC, ключ
//! `INTEGRATIONS_KEY`, имитовставка охватывает компанию и вид подключения). Колонки записи: `password_enc` — шифртекст токена,
//! `url` — маска токена (`123…:…xyz`), `username` — имя бота без `@`. Наружу токен не отдаётся ни одним ответом, кроме
//! внутреннего `GET /internal/v1/integrations/telegram/credentials` для сервиса notifier (`x-actor: svc:notifier`, `no-store`);
//! ни в журналы, ни в аудит (там только действие и маска), ни в тексты ошибок он не попадает.

use crate::AppState;
use crate::integrations::{open, seal};
use crate::routes::{audit, lock, not_found, now};
use axum::{
    Json,
    extract::{Path, State},
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use common::{ApiError, Ctx, R, err, internal};
use rusqlite::{OptionalExtension, params};
use serde::Deserialize;
use serde_json::{Value, json};

/// Служебный «актор» сервиса notifier: только он получает расшифрованные токены.
pub const NOTIFIER_ACTOR: &str = "svc:notifier";
pub const KIND: &str = "telegram";

fn aad(cid: &str) -> String {
    format!("{cid}/{KIND}")
}

fn bad(code: &str, msg: &str) -> ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, code, msg)
}

/// Формат токена BotFather: `^\d{6,12}:[A-Za-z0-9_-]{30,50}$`.
pub fn valid_token(t: &str) -> bool {
    let Some((id, secret)) = t.split_once(':') else { return false };
    (6..=12).contains(&id.len())
        && id.bytes().all(|b| b.is_ascii_digit())
        && (30..=50).contains(&secret.len())
        && secret.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Маска для интерфейса и аудита: первые 3 цифры id бота и последние 3 символа токена.
pub fn mask(t: &str) -> String {
    let id: String = t.split(':').next().unwrap_or("").chars().take(3).collect();
    let tail: Vec<char> = t.chars().rev().take(3).collect();
    format!("{id}…:…{}", tail.into_iter().rev().collect::<String>())
}

/// Имя бота (`@name` или `name`): 5–32 символа латиницы, цифр и `_`. Возвращает имя без `@`.
pub fn clean_username(s: &str) -> Option<String> {
    let s = s.trim().trim_start_matches('@');
    ((5..=32).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')).then(|| s.to_string())
}

/// Заменяет в тексте всё, что похоже на токен бота (`цифры:секрет`), на `[token]`: текст ошибки не должен его повторять.
pub fn scrub(text: &str) -> String {
    let cs: Vec<char> = text.chars().collect();
    let (mut out, mut i) = (String::new(), 0);
    while i < cs.len() {
        if cs[i].is_ascii_digit() {
            let s = i;
            while i < cs.len() && cs[i].is_ascii_digit() {
                i += 1;
            }
            if i < cs.len() && cs[i] == ':' && i - s >= 6 {
                let mut k = i + 1;
                while k < cs.len() && (cs[k].is_ascii_alphanumeric() || cs[k] == '_' || cs[k] == '-') {
                    k += 1;
                }
                if k - i > 30 {
                    out.push_str("[token]");
                    i = k;
                    continue;
                }
            }
            out.extend(&cs[s..i]);
        } else {
            out.push(cs[i]);
            i += 1;
        }
    }
    out
}

type Row = (String, String, String, String, Option<String>, Option<String>, String, String);

const COLS: &str = "url, username, password_enc, status, last_check_at, last_error, created_at, updated_at";

fn row(r: &rusqlite::Row) -> rusqlite::Result<Row> {
    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?))
}

/// Описание подключения без токена: только маска и имя бота.
pub fn view(r: &Row) -> Value {
    json!({"kind": KIND, "bot_username": r.1, "token_mask": r.0, "token_set": !r.2.is_empty(), "status": r.3,
        "last_check_at": r.4, "last_error": r.5, "created_at": r.6, "updated_at": r.7})
}

fn load(c: &rusqlite::Connection, cid: &str) -> R<Option<Row>> {
    c.query_row(&format!("SELECT {COLS} FROM integration_connections WHERE company_id=? AND kind=?"), params![cid, KIND], row).optional().map_err(internal)
}

/// Для `GET integrations`: описание подключения Telegram или ничего.
pub fn view_of(c: &rusqlite::Connection, cid: &str) -> R<Option<Value>> {
    Ok(load(c, cid)?.as_ref().map(view))
}

fn company_exists(c: &rusqlite::Connection, cid: &str) -> R<()> {
    let n: i64 = c.query_row("SELECT COUNT(*) FROM companies WHERE id=?", [cid], |r| r.get(0)).map_err(internal)?;
    if n == 0 { Err(not_found()) } else { Ok(()) }
}

#[derive(Deserialize)]
pub struct PutBody {
    token: String,
    /// имя бота из getMe (проверку выполнил api через notifier)
    bot_username: String,
}

/// PUT integrations/telegram {token, bot_username} -> описание подключения (без токена). Статус `ok`: токен уже проверен через getMe.
pub async fn put_telegram(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<PutBody>) -> R<Json<Value>> {
    let token = b.token.trim().to_string();
    if !valid_token(&token) {
        return Err(bad("invalid_bot_token", "Токен бота: формат 123456789:AA… (его выдаёт @BotFather)"));
    }
    let username = clean_username(&b.bot_username).ok_or_else(|| bad("invalid_bot_username", "Имя бота: 5–32 символа, латиница, цифры и _"))?;
    let key = st.integrations_key.as_ref().ok_or_else(|| {
        err(StatusCode::SERVICE_UNAVAILABLE, "integrations_key_missing", "На сервере не задан ключ шифрования INTEGRATIONS_KEY: подключения временно недоступны (см. docs/integrator.md)")
    })?;
    let masked = mask(&token);
    let enc = seal(key, &aad(&cid), &token);
    let at = now();
    let out = {
        let c = lock(&st);
        company_exists(&c, &cid)?;
        c.execute(
            "INSERT INTO integration_connections(company_id, kind, url, username, password_enc, status, last_check_at, last_error, last_sync_at, created_at, updated_at, updated_by)
             VALUES(?1,?2,?3,?4,?5,'ok',?6,NULL,NULL,?6,?6,?7)
             ON CONFLICT(company_id, kind) DO UPDATE SET url=excluded.url, username=excluded.username, password_enc=excluded.password_enc,
               status='ok', last_check_at=excluded.last_check_at, last_error=NULL, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            params![cid, KIND, masked, username, enc, at, ctx.actor],
        )
        .map_err(internal)?;
        load(&c, &cid)?.map(|r| view(&r)).unwrap_or(Value::Null)
    };
    audit(&st, &ctx, &cid, "integrations.telegram.connect", &masked, "ok");
    Ok(Json(out))
}

#[derive(Deserialize)]
pub struct StatusBody {
    ok: bool,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    bot_username: Option<String>,
}

/// PUT integrations/telegram/status {ok, error?, bot_username?} -> описание подключения. Текст ошибки очищается от токенов и
/// управляющих символов и обрезается.
pub async fn put_status(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>, Json(b): Json<StatusBody>) -> R<Json<Value>> {
    let error: Option<String> = if b.ok {
        None
    } else {
        Some(scrub(&b.error.unwrap_or_default()).chars().filter(|c| !c.is_control()).take(300).collect::<String>()).filter(|s| !s.is_empty()).or(Some("error".into()))
    };
    let username = b.bot_username.as_deref().and_then(clean_username);
    let status = if b.ok { "ok" } else { "error" };
    let out = {
        let c = lock(&st);
        let n = c
            .execute(
                "UPDATE integration_connections SET status=?3, last_error=?4, last_check_at=?5, username=COALESCE(?6, username) WHERE company_id=?1 AND kind=?2",
                params![cid, KIND, status, error, now(), username],
            )
            .map_err(internal)?;
        if n == 0 {
            return Err(not_found());
        }
        load(&c, &cid)?.map(|r| view(&r)).unwrap_or(Value::Null)
    };
    audit(&st, &ctx, &cid, "integrations.telegram.check", status, status);
    Ok(Json(out))
}

/// DELETE integrations/telegram -> 204 (идемпотентно): токен удаляется, компания возвращается на общий бот платформы.
pub async fn delete_telegram(State(st): State<AppState>, ctx: Ctx, Path(cid): Path<String>) -> R<StatusCode> {
    let (n, masked) = {
        let c = lock(&st);
        company_exists(&c, &cid)?;
        let masked = load(&c, &cid)?.map(|r| r.0).unwrap_or_default();
        (c.execute("DELETE FROM integration_connections WHERE company_id=? AND kind=?", params![cid, KIND]).map_err(internal)?, masked)
    };
    if n > 0 {
        audit(&st, &ctx, &cid, "integrations.telegram.disconnect", &masked, "ok");
    }
    Ok(StatusCode::NO_CONTENT)
}

/// GET /internal/v1/integrations/telegram/credentials -> {items:[{company_id, token, bot_username}], skipped}. Только для notifier
/// (`x-actor: svc:notifier`): единственное место, где токены расшифровываются и отдаются. Ответ не кэшируется. Записи, которые не
/// расшифровываются (сменился ключ или запись подменена), пропускаются и считаются в `skipped`.
pub async fn credentials(State(st): State<AppState>, ctx: Ctx) -> R<Response> {
    if ctx.actor != NOTIFIER_ACTOR {
        return Err(err(StatusCode::FORBIDDEN, "forbidden", "Токены ботов отдаются только сервису notifier"));
    }
    let rows: Vec<(String, String, String)> = {
        let c = lock(&st);
        let mut q = c.prepare("SELECT company_id, username, password_enc FROM integration_connections WHERE kind=? ORDER BY company_id").map_err(internal)?;
        let it = q.query_map([KIND], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).map_err(internal)?;
        it.collect::<Result<_, _>>().map_err(internal)?
    };
    let key = st.integrations_key.as_ref().ok_or_else(|| {
        err(StatusCode::SERVICE_UNAVAILABLE, "integrations_key_missing", "На сервере не задан ключ шифрования INTEGRATIONS_KEY")
    })?;
    let mut items = Vec::new();
    let mut skipped = 0;
    for (cid, username, enc) in rows {
        match open(key, &aad(&cid), &enc) {
            Some(token) => items.push(json!({"company_id": cid, "token": token, "bot_username": username})),
            None => skipped += 1,
        }
    }
    let mut res = Json(json!({"items": items, "skipped": skipped})).into_response();
    res.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(res)
}

#[cfg(test)]
mod unit {
    use super::*;

    const TOKEN: &str = "123456789:AAExampleExampleExampleExample_abc-xyz";

    #[test]
    fn token_format() {
        assert!(valid_token(TOKEN) && valid_token("123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"));
        for bad in ["", "abc", "12345:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "1234567890123:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "123456:short", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAA!A", "12a456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"] {
            assert!(!valid_token(bad), "{bad}");
        }
    }

    #[test]
    fn mask_shows_only_id_prefix_and_tail() {
        let m = mask(TOKEN);
        assert_eq!(m, "123…:…xyz");
        assert!(!m.contains("Example"));
    }

    #[test]
    fn scrub_removes_token_like_runs() {
        let s = scrub(&format!("Unauthorized for https://api.telegram.org/bot{TOKEN}/getMe, retry"));
        assert!(!s.contains("AAExample") && s.contains("[token]") && s.contains("retry"), "{s}");
        assert_eq!(scrub("обычный текст 12:30"), "обычный текст 12:30");
    }

    #[test]
    fn username_is_normalised() {
        assert_eq!(clean_username(" @My_Co_bot ").as_deref(), Some("My_Co_bot"));
        assert_eq!(clean_username("bot"), None);
        assert_eq!(clean_username("bad name_bot"), None);
    }
}
