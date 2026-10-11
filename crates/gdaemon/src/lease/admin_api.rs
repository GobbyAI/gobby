//! Native, key-authenticated lease control; used even without a Python backend.

use crate::state::RuntimeState;
use axum::{
    body::Body,
    http::{Method, Request, Response, StatusCode},
};
use serde_json::{Value, json};
use std::time::Duration;

pub fn is_route(path: &str) -> bool {
    matches!(
        path,
        "/api/admin/lease/status" | "/api/admin/lease/promote" | "/api/admin/lease/recover"
    )
}

pub async fn handle(state: &RuntimeState, request: Request<Body>) -> Response<Body> {
    let Some(lease) = &state.lease else {
        return response(
            StatusCode::CONFLICT,
            json!({"detail": "node mode has no singleton lease"}),
        );
    };
    let mut lease = lease.lock().await;
    let result = match (request.method(), request.uri().path()) {
        (&Method::GET, "/api/admin/lease/status") => lease.owner().await.map(|owner| {
            json!({
                "mode": if lease.fence().is_some() { "active" } else { "standby" },
                "held": owner.is_some(),
                "owner_pid": owner.as_ref().map(|owner| owner.pid),
                "owner_application_name": owner.as_ref().map(|owner| &owner.application_name),
                "heartbeat_age_seconds": owner.as_ref().map(|owner| owner.heartbeat_age_seconds),
            })
        }),
        (&Method::POST, path)
            if matches!(
                path,
                "/api/admin/lease/promote" | "/api/admin/lease/recover"
            ) =>
        {
            if lease.fence().is_some() {
                return if path.ends_with("/promote") {
                    response(
                        StatusCode::OK,
                        json!({"promoting": false, "mode": "active"}),
                    )
                } else {
                    response(
                        StatusCode::CONFLICT,
                        json!({"detail": "lease owner is already active"}),
                    )
                };
            }
            if matches!(
                *state.backend.borrow(),
                crate::lifecycle::backend::BackendStatus::Refused { .. }
            ) {
                return response(
                    StatusCode::CONFLICT,
                    json!({"detail": "refused runtime requires a full restart"}),
                );
            }
            if path.ends_with("/recover") {
                let value = request
                    .uri()
                    .query()
                    .unwrap_or_default()
                    .split('&')
                    .find_map(|field| field.strip_prefix("stale_after_seconds="));
                let seconds = match value.unwrap_or("30").parse::<f64>() {
                    Ok(seconds) if seconds.is_finite() && seconds > 0.0 && seconds <= 86400.0 => {
                        seconds
                    }
                    _ => {
                        return response(
                            StatusCode::BAD_REQUEST,
                            json!({"detail": "invalid stale_after_seconds"}),
                        );
                    }
                };
                if let Err(error) = lease
                    .recover_stale_owner(Duration::from_secs_f64(seconds))
                    .await
                {
                    let status = if error.is::<super::standby::FreshLeaseOwner>() {
                        StatusCode::CONFLICT
                    } else {
                        StatusCode::SERVICE_UNAVAILABLE
                    };
                    return response(status, json!({"detail": error.to_string()}));
                }
            }
            match lease.try_acquire().await {
                Ok(true) => Ok(if path.ends_with("/recover") {
                    json!({"promoting": true, "recovered": true})
                } else {
                    json!({"promoting": true})
                }),
                Ok(false) => {
                    let owner = match lease.owner().await {
                        Ok(owner) => owner,
                        Err(error) => {
                            return response(
                                StatusCode::SERVICE_UNAVAILABLE,
                                json!({"detail": error.to_string()}),
                            );
                        }
                    };
                    return response(
                        StatusCode::CONFLICT,
                        json!({"detail": {
                            "message": "active-daemon lease is held",
                            "owner_pid": owner.as_ref().map(|owner| owner.pid),
                            "owner_application_name": owner.as_ref().map(|owner| &owner.application_name),
                            "heartbeat_age_seconds": owner.as_ref().map(|owner| owner.heartbeat_age_seconds),
                        }}),
                    );
                }
                Err(error) => Err(error),
            }
        }
        _ => {
            return response(
                StatusCode::METHOD_NOT_ALLOWED,
                json!({"detail": "method not allowed"}),
            );
        }
    };
    match result {
        Ok(body) => response(StatusCode::OK, body),
        Err(error) => response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"detail": error.to_string()}),
        ),
    }
}

fn response(status: StatusCode, value: Value) -> Response<Body> {
    let mut response = Response::new(Body::from(value.to_string()));
    *response.status_mut() = status;
    response.headers_mut().insert(
        "content-type",
        axum::http::HeaderValue::from_static("application/json"),
    );
    response.headers_mut().insert(
        "x-gobby-served-by",
        axum::http::HeaderValue::from_static("gdaemon:lease"),
    );
    response
}
