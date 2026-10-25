//! Compare a proxied handshake without issuing a second PostgreSQL credential.

use std::collections::BTreeSet;

use axum::http::request::Parts;
use axum::http::{Method, StatusCode};
use gobby_core::grant::presentation::{PresentationContext, Rejection, verify_static};
use gobby_core::grant::{
    GRANT_VERSION, GrantBundle, GrantPrincipal, MACHINE_HEADER, PostgresCapability, PrincipalKind,
    deployment_token,
};
use gobby_core::route_family::ProxiedCheck;
use serde::Deserialize;
use serde_json::Value;

use crate::capabilities::capabilities_from_snapshot;
use crate::config::active_state;
use crate::grant::{read_fence, unix_now};
use crate::handshake::{GRANT_TTL_SECONDS, HandshakeRequest, active_secrets};
use crate::secrets::SecretStore;
use crate::{Caller, RuntimeState};

#[derive(Deserialize)]
struct HandshakeResponse {
    grant: GrantBundle,
    deployment_token: String,
    fencing_epoch: i64,
}

pub(crate) async fn check(
    state: &RuntimeState,
    parts: &Parts,
    request: &[u8],
    status: StatusCode,
    response: &[u8],
) -> ProxiedCheck {
    if status != StatusCode::OK
        || parts.method != Method::POST
        || parts.uri.path() != "/api/runtime/handshake"
        || Caller::of(parts) != Some(Caller::Operator)
    {
        return ProxiedCheck::Skipped;
    }
    match verify(state, parts, request, response).await {
        Ok(result) => result,
        Err(pointer) => ProxiedCheck::Mismatch(pointer.to_owned()),
    }
}

async fn verify(
    state: &RuntimeState,
    parts: &Parts,
    request: &[u8],
    response: &[u8],
) -> Result<ProxiedCheck, &'static str> {
    let token = deployment_token(&state.home);
    // Read one published snapshot before checking the grant. A revision move means
    // its original capabilities can no longer be recomputed from the active row.
    let active = active_state(&state.pool, &token)
        .await
        .map_err(|_| "/grant/config_revision")?
        .ok_or("/grant/config_revision")?;
    let reply: HandshakeResponse = serde_json::from_slice(response).map_err(|_| "/response")?;
    let grant = &reply.grant;
    if active.revision != grant.config_revision {
        return Ok(ProxiedCheck::Skipped);
    }
    let request: HandshakeRequest = serde_json::from_slice(request).map_err(|_| "/request")?;
    let fence = read_fence(&state.pool, &token)
        .await
        .map_err(|_| "/grant/deployment")?
        .ok_or("/grant/deployment")?;
    let context = PresentationContext {
        token: &token,
        fencing_epoch: fence.epoch,
        signing_secret: fence.secret.as_bytes(),
    };
    verify_static(grant, &context, unix_now()).map_err(|rejection| match rejection {
        Rejection::InvalidSignature => "/grant/signature",
        Rejection::InvalidChecksum => "/grant/payload_checksum",
        Rejection::WrongDeployment => "/grant/deployment/token",
        Rejection::WrongSchema => "/grant/schema_identity",
        Rejection::WrongApiContract if grant.version != GRANT_VERSION => "/grant/version",
        Rejection::WrongApiContract => "/grant/api_contract",
        Rejection::StaleEpoch => "/grant/deployment/fencing_epoch",
        Rejection::Expired => "/grant/expires_at",
        _ => "/grant",
    })?;
    if reply.deployment_token != grant.deployment.token {
        return Err("/deployment_token");
    }
    if reply.fencing_epoch != grant.deployment.fencing_epoch {
        return Err("/fencing_epoch");
    }
    let machine = parts
        .headers
        .get(MACHINE_HEADER)
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty())
        .ok_or("/grant/principal/machine_id")?;
    let expected = GrantPrincipal {
        kind: PrincipalKind::Interactive,
        machine_id: machine.to_owned(),
        project_id: request.project_id,
        execution_id: None,
        session_id: request.session_id,
        code_overlay_project_id: request.code_overlay_project_id,
    };
    if let Some(pointer) = first_difference(
        &serde_json::to_value(expected).map_err(|_| "/grant/principal")?,
        &serde_json::to_value(&grant.principal).map_err(|_| "/grant/principal")?,
        "/grant/principal".to_owned(),
    ) {
        return Ok(ProxiedCheck::Mismatch(pointer));
    }
    if grant
        .issued_at
        .checked_add(GRANT_TTL_SECONDS)
        .is_none_or(|limit| grant.expires_at > limit)
    {
        return Err("/grant/expires_at");
    }
    match &grant.capabilities.postgres {
        PostgresCapability::Direct { valid_until, .. } if grant.expires_at <= *valid_until => {}
        PostgresCapability::Direct { .. } => return Err("/grant/expires_at"),
        _ => return Err("/grant/capabilities/postgres/valid_until"),
    }
    let store = SecretStore::new(state.pool.clone(), state.home.clone());
    let secrets = active_secrets(&store, &token, &active)
        .await
        .map_err(|_| "/grant/capabilities")?;
    // Reuse only the issued PostgreSQL capability: recomputing it would create
    // another credential. All other capabilities come from the published state.
    let expected = capabilities_from_snapshot(
        &active.document.active_values,
        &secrets,
        grant.capabilities.postgres.clone(),
    );
    let pointer = first_difference(
        &serde_json::to_value(expected).map_err(|_| "/grant/capabilities")?,
        &serde_json::to_value(&grant.capabilities).map_err(|_| "/grant/capabilities")?,
        "/grant/capabilities".to_owned(),
    );
    Ok(pointer.map_or(ProxiedCheck::Verified, ProxiedCheck::Mismatch))
}

// Mirrors compare's pointer-only JSON traversal. Inputs are typed grant fields,
// so neither arbitrary response keys nor credential values enter the diagnostic.
fn first_difference(expected: &Value, actual: &Value, pointer: String) -> Option<String> {
    match (expected, actual) {
        (Value::Object(expected), Value::Object(actual)) => {
            let keys: BTreeSet<&String> = expected.keys().chain(actual.keys()).collect();
            keys.into_iter().find_map(|key| {
                let escaped = key.replace('~', "~0").replace('/', "~1");
                let pointer = format!("{pointer}/{escaped}");
                match (expected.get(key), actual.get(key)) {
                    (Some(expected), Some(actual)) => first_difference(expected, actual, pointer),
                    _ => Some(pointer),
                }
            })
        }
        (Value::Array(expected), Value::Array(actual)) => (0..expected.len().max(actual.len()))
            .find_map(|index| {
                let pointer = format!("{pointer}/{index}");
                match (expected.get(index), actual.get(index)) {
                    (Some(expected), Some(actual)) => first_difference(expected, actual, pointer),
                    _ => Some(pointer),
                }
            }),
        _ => (expected != actual).then_some(pointer),
    }
}
