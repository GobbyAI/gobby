//! Native `GET /api/runtime/config`: an interactive principal's runtime grant is
//! presented here, and the machine's effective settings are served on success.

use std::fmt::Debug;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::extract::State;
use axum::http::header::CACHE_CONTROL;
use axum::http::request::Parts;
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode};
use gobby_core::grant::presentation::{
    PresentationContext, Rejection, decode_grant_header, verify_static,
};
use gobby_core::grant::{GRANT_HEADER, GrantBundle, PrincipalKind, deployment_token};
use gobby_core::machine::read_machine_id_from_home;
use gobby_core::postgres_pool::Pool;
use gobby_core::schema::auth_schema_for;
use serde_json::json;

use crate::RuntimeState;
use crate::config::{active_state, json_response, machine_values};

/// Claims a request only when its grant header decodes to an interactive principal's
/// grant. Missing, undecodable, and other-principal requests are forwarded to Python.
pub(crate) fn interactive_gate(parts: &Parts) -> bool {
    parts
        .headers
        .get(GRANT_HEADER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| decode_grant_header(value).ok())
        .is_some_and(|grant| grant.principal.kind == PrincipalKind::Interactive)
}

pub(crate) async fn runtime_config(
    State(state): State<Arc<RuntimeState>>,
    headers: HeaderMap,
) -> Response<Body> {
    let grant = match presented_grant(&headers) {
        Ok(grant) => grant,
        Err(response) => return response,
    };
    let token = deployment_token(&state.home);
    let fence = match read_fence(&state.pool, &token).await {
        Ok(Some(fence)) => fence,
        Ok(None) => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"detail": "grant service unavailable"}),
            );
        }
        Err(error) => return internal(error),
    };
    let context = PresentationContext {
        token: &token,
        fencing_epoch: fence.epoch,
        signing_secret: fence.secret.as_bytes(),
    };
    if let Err(rejection) = verify_static(&grant, &context, unix_now()) {
        return rejection_response(rejection);
    }
    match interactive_revoked(&state, &grant, &token).await {
        Ok(false) => {}
        Ok(true) => return rejection_response(Rejection::Revoked),
        Err(response) => return response,
    }
    match active_state(&state.pool, &token).await {
        Ok(Some(active)) => {
            let body = json!({
                "config_revision": active.revision,
                "settings": machine_values(&active),
            });
            let mut response = json_response(StatusCode::OK, body);
            response
                .headers_mut()
                .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
            response
        }
        Ok(None) => json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"detail": "runtime configuration unavailable"}),
        ),
        Err(error) => internal(error),
    }
}

/// The grant a request presented. The gate has already claimed decodable headers, so a
/// refusal here is defensive and mirrors the Python route's responses.
fn presented_grant(headers: &HeaderMap) -> Result<GrantBundle, Response<Body>> {
    let Some(value) = headers
        .get(GRANT_HEADER)
        .and_then(|value| value.to_str().ok())
    else {
        return Err(json_response(
            StatusCode::UNAUTHORIZED,
            json!({"detail": "Runtime grant required"}),
        ));
    };
    decode_grant_header(value).map_err(|_| {
        json_response(StatusCode::BAD_REQUEST, json!({"detail": "malformed grant"}))
    })
}

/// The deployment's fencing epoch and grant-signing secret, read per request so a
/// bumped epoch is seen at once.
struct Fence {
    epoch: i64,
    secret: String,
}

async fn read_fence(
    pool: &Pool,
    token: &str,
) -> Result<Option<Fence>, Box<dyn std::error::Error + Send + Sync>> {
    let client = pool.get().await?;
    let Some(row) = client
        .query_opt(
            "SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = $1",
            &[&token],
        )
        .await?
    else {
        return Ok(None);
    };
    let epoch: i64 = row.try_get(0)?;
    let secret: String = row.try_get(1)?;
    Ok(Some(Fence { epoch, secret }))
}

/// Python's `interactive_generation_revoked` over the auth schema. A missing binding
/// row counts as revoked, and so does an identifier that cannot name a binding. A
/// machine id that cannot be read is a daemon configuration fault and is unavailable.
async fn interactive_revoked(
    state: &RuntimeState,
    grant: &GrantBundle,
    token: &str,
) -> Result<bool, Response<Body>> {
    if grant.principal.kind != PrincipalKind::Interactive {
        return Ok(false);
    }
    let Some(generation) = grant.credential_generation() else {
        return Ok(false);
    };
    let machine = match read_machine_id_from_home(&state.home) {
        Ok(machine) => machine,
        Err(error) => {
            eprintln!("gdaemon runtime: machine id unreadable: {error}");
            return Err(grant_unavailable());
        }
    };
    let Ok(machine) = uuid::Uuid::parse_str(machine.trim()) else {
        return Err(grant_unavailable());
    };
    let Ok(project) = uuid::Uuid::parse_str(&grant.principal.project_id) else {
        return Ok(true);
    };
    let Ok(generation) = i32::try_from(generation) else {
        return Ok(true);
    };
    let client = state.pool.get().await.map_err(internal)?;
    let schema_row = client
        .query_one("SELECT current_schema()::text", &[])
        .await
        .map_err(internal)?;
    let schema: String = schema_row.try_get(0).map_err(internal)?;
    let auth = auth_schema_for(&schema).replace('"', "\"\"");
    let sql = format!(
        "SELECT revoked_at IS NOT NULL AS revoked FROM \"{auth}\".lookup_interactive_principal($1::text, $2::text::uuid, $3::text::uuid, $4::text::integer)"
    );
    let row = client
        .query_opt(
            sql.as_str(),
            &[
                &token,
                &machine.to_string(),
                &project.to_string(),
                &generation.to_string(),
            ],
        )
        .await
        .map_err(internal)?;
    match row {
        None => Ok(true),
        Some(row) => row.try_get::<_, bool>(0).map_err(internal),
    }
}

fn rejection_response(rejection: Rejection) -> Response<Body> {
    let status =
        StatusCode::from_u16(rejection.status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    json_response(status, rejection.body())
}

fn grant_unavailable() -> Response<Body> {
    json_response(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"detail": "grant service unavailable"}),
    )
}

fn internal(error: impl Debug) -> Response<Body> {
    eprintln!("gdaemon runtime: grant presentation failed: {error:?}");
    json_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        json!({"detail": "Internal server error"}),
    )
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}
