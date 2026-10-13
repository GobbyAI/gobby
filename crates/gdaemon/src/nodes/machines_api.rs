//! Caller-scoped machine inventory, including this process's live channels.
use super::{NativeRoutes, registry::Registry};
use axum::{
    Router,
    body::Body,
    extract::{Path, State},
    http::{HeaderMap, Request, StatusCode},
    response::Response,
    routing::get,
};
use deadpool_postgres::tokio_postgres::Row;
use gobby_core::postgres_pool::Pool;
use serde_json::{Value, json};
use std::time::SystemTime;
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use tower_service::Service;
use uuid::Uuid;

const COLUMNS: &str = "id, ref, hostname, os, label, tailscale_name, first_seen, \
    last_seen, last_heartbeat_at, node_version";

pub fn is_route(path: &str) -> bool {
    path == "/api/machines" || path.starts_with("/api/machines/")
}

pub async fn handle(native: NativeRoutes, request: Request<Body>) -> Response {
    let mut router = Router::new()
        .route("/api/machines", get(list))
        .route("/api/machines/{id}", get(item))
        .with_state(native);
    match router.call(request).await {
        Ok(response) => response,
        Err(error) => match error {},
    }
}

fn sources(native: &NativeRoutes) -> (Option<&Pool>, Option<&Registry>) {
    match native {
        NativeRoutes::Standalone(pool) => (pool.as_deref(), None),
        NativeRoutes::Hub(nodes) => (Some(&nodes.db), Some(&nodes.registry)),
    }
}

fn caller(headers: &HeaderMap) -> Option<Uuid> {
    headers
        .get("x-gobby-user-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse().ok())
}

async fn list(State(native): State<NativeRoutes>, headers: HeaderMap) -> Response {
    let Some(user) = caller(&headers) else {
        return missing_auth();
    };
    let (Some(pool), registry) = sources(&native) else {
        return unavailable();
    };
    let result = async {
        let client = pool.get().await?;
        let rows = client
            .query(
                &format!(
                    "SELECT {COLUMNS} FROM machines WHERE owner_user_id=$1 ORDER BY first_seen, id"
                ),
                &[&user],
            )
            .await?;
        rows.iter()
            .map(|row| machine(row, registry))
            .collect::<anyhow::Result<Vec<_>>>()
    }
    .await;
    match result {
        Ok(machines) => crate::front_door::health::json_response(
            StatusCode::OK,
            json!({"machines": machines}),
            false,
        ),
        Err(_) => unavailable(),
    }
}

async fn item(
    State(native): State<NativeRoutes>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let Some(user) = caller(&headers) else {
        return missing_auth();
    };
    let (Some(pool), registry) = sources(&native) else {
        return unavailable();
    };
    let Ok(id) = id.parse::<Uuid>() else {
        return not_found();
    };
    let result = async {
        let client = pool.get().await?;
        client
            .query_opt(
                &format!("SELECT {COLUMNS} FROM machines WHERE owner_user_id=$1 AND id=$2"),
                &[&user, &id],
            )
            .await?
            .as_ref()
            .map(|row| machine(row, registry))
            .transpose()
    }
    .await;
    match result {
        Ok(Some(machine)) => {
            crate::front_door::health::json_response(StatusCode::OK, machine, false)
        }
        Ok(None) => not_found(),
        Err(_) => unavailable(),
    }
}

fn machine(row: &Row, registry: Option<&Registry>) -> anyhow::Result<Value> {
    let id: Uuid = row.try_get("id")?;
    Ok(json!({
        "id": id.to_string(),
        "ref": row.try_get::<_, Option<i32>>("ref")?,
        "hostname": row.try_get::<_, Option<String>>("hostname")?,
        "os": row.try_get::<_, Option<String>>("os")?,
        "label": row.try_get::<_, Option<String>>("label")?,
        "tailscale_name": row.try_get::<_, Option<String>>("tailscale_name")?,
        "first_seen": timestamp(row, "first_seen")?,
        "last_seen": timestamp(row, "last_seen")?,
        "last_heartbeat_at": timestamp(row, "last_heartbeat_at")?,
        "node_version": row.try_get::<_, Option<String>>("node_version")?,
        "connected": registry.is_some_and(|registry| registry.is_connected(id)),
    }))
}

fn timestamp(row: &Row, column: &str) -> anyhow::Result<Option<String>> {
    row.try_get::<_, Option<SystemTime>>(column)?
        .map(|value| OffsetDateTime::from(value).format(&Rfc3339))
        .transpose()
        .map_err(Into::into)
}

fn error(status: StatusCode, code: &str, message: &str) -> Response {
    crate::front_door::health::json_response(status, json!({"error": message, "code": code}), false)
}

fn unavailable() -> Response {
    error(
        StatusCode::SERVICE_UNAVAILABLE,
        "machines_unavailable",
        "Machine inventory is unavailable",
    )
}

fn missing_auth() -> Response {
    error(
        StatusCode::UNAUTHORIZED,
        "missing_auth",
        "A valid API key is required",
    )
}

fn not_found() -> Response {
    error(
        StatusCode::NOT_FOUND,
        "machine_not_found",
        "Machine not found",
    )
}
