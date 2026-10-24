use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;

/// Ошибка API: статус, машинный код, сообщение. Тело: {"error":{"code","message"}}.
#[derive(Debug)]
pub struct ApiError(pub StatusCode, pub String, pub String);
pub type R<T> = Result<T, ApiError>;

pub fn err(s: StatusCode, code: impl Into<String>, msg: impl Into<String>) -> ApiError {
    ApiError(s, code.into(), msg.into())
}

pub fn internal<E: std::fmt::Display>(e: E) -> ApiError {
    eprintln!("internal error: {e}"); // без тел запросов и данных
    err(StatusCode::INTERNAL_SERVER_ERROR, "internal", "Внутренняя ошибка")
}

pub fn not_found() -> ApiError {
    err(StatusCode::NOT_FOUND, "not_found", "Не найдено")
}

pub fn no_data() -> ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, "no_data", "Сначала загрузите и подтвердите банковскую выписку")
}

pub fn no_balance() -> ApiError {
    err(StatusCode::UNPROCESSABLE_ENTITY, "no_balance", "В выписке нет остатка на счёте — прогноз невозможен")
}

pub fn error_response(s: StatusCode, code: &str, msg: &str, rid: &str) -> Response {
    (s, Json(json!({"error": {"code": code, "message": msg, "request_id": rid}}))).into_response()
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": {"code": self.1, "message": self.2}}))).into_response()
    }
}
