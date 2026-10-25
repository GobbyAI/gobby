//! Native `POST /api/runtime/handshake`: an identified operator's handshake issues the
//! interactive principal and the signed grant that `GET /api/runtime/config` presents.
//! Mirrors `src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`
//! and the route in `src/gobby/servers/routes/runtime_handshake.py`.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::header::CACHE_CONTROL;
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use gobby_core::bootstrap::postgres_database_url_from_bootstrap_file;
use gobby_core::grant::presentation::sign_grant;
use gobby_core::grant::{
    EXPECTED_API_CONTRACT, GRANT_VERSION, GrantBundle, GrantDeployment, GrantPrincipal,
    MACHINE_HEADER, PostgresCapability, PrincipalKind, deployment_token, expected_schema_identity,
};
use gobby_core::machine::read_machine_id_from_home;
use gobby_core::postgres_pool::{Pool, quote_identifier};
use gobby_core::schema::auth_schema_for;
use rand::RngCore;
use rand::rngs::OsRng;
use serde::Deserialize;
use serde_json::json;
use tokio_postgres::Client;
use tokio_postgres::error::SqlState;
use url::Url;
use uuid::Uuid;

use crate::RuntimeState;
use crate::capabilities::capabilities_from_snapshot;
use crate::config::{ActiveState, active_state, json_response};
use crate::grant::{grant_unavailable, internal, read_fence, unix_now};
use crate::secrets::{SecretError, SecretStore};

pub(crate) const GRANT_TTL_SECONDS: i64 = 3600;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HandshakeRequest {
    pub(crate) machine_id: String,
    pub(crate) project_id: String,
    pub(crate) session_id: Option<String>,
    pub(crate) code_overlay_project_id: Option<String>,
}

pub(crate) async fn handshake(
    State(state): State<Arc<RuntimeState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    match issue(&state, &headers, &body).await {
        Ok(reply) | Err(reply) => reply,
    }
}

async fn issue(
    state: &RuntimeState,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Response<Body>, Response<Body>> {
    let request: HandshakeRequest = serde_json::from_slice(body).map_err(|_| invalid_request())?;
    let token = deployment_token(&state.home);
    let fence = read_fence(&state.pool, &token)
        .await
        .map_err(internal)?
        .ok_or_else(grant_unavailable)?;
    let (local_machine, daemon_machine) = local_machine(&state.home).map_err(|error| *error)?;
    let forwarded = headers
        .get(MACHINE_HEADER)
        .and_then(|value| value.to_str().ok())
        .unwrap_or(local_machine.as_str());
    if forwarded.is_empty() || request.machine_id != forwarded {
        return Err(rejection(
            "claims_mismatch",
            "operator machine_id must match the authenticated machine",
        ));
    }
    let Some(project) = admitted_project(&state.pool, &request.project_id).await? else {
        return Err(rejection("claims_mismatch", "project is not admitted"));
    };
    let overlay = overlay_claim(request.code_overlay_project_id.as_deref(), project)
        .map_err(|error| *error)?;
    let session = match request.session_id.as_deref() {
        Some(session) => Some(Uuid::parse_str(session).map_err(|_| credential_failed())?),
        None => None,
    };

    // Everything that can refuse without side effects runs before the principal is issued.
    let active = active_state(&state.pool, &token)
        .await
        .map_err(internal)?
        .ok_or_else(handshake_unavailable)?;
    let database_url =
        postgres_database_url_from_bootstrap_file(&state.home.join("bootstrap.yaml"))
            .map_err(internal)?
            .ok_or_else(handshake_unavailable)?;
    let store = SecretStore::new(state.pool.clone(), state.home.clone());
    let secrets = active_secrets(&store, &token, &active).await?;

    let now = unix_now();
    let valid_until = now + GRANT_TTL_SECONDS;
    let principal = Principal {
        token: &token,
        machine: daemon_machine,
        project,
        session,
        overlay,
        expires_at: UNIX_EPOCH
            + Duration::from_secs(u64::try_from(valid_until).unwrap_or_default()),
    };
    let issued = issue_principal(&state.pool, &store, &principal, random_password()).await?;
    let dsn = interactive_dsn(&database_url, &issued.role_name, &issued.password)
        .map_err(|error| *error)?;
    let capabilities = capabilities_from_snapshot(
        &active.document.active_values,
        &secrets,
        PostgresCapability::Direct {
            dsn,
            role_name: issued.role_name,
            credential_generation: i64::from(issued.generation),
            valid_until,
        },
    );
    let grant = GrantBundle {
        version: GRANT_VERSION,
        api_contract: EXPECTED_API_CONTRACT,
        config_revision: active.revision,
        deployment: GrantDeployment {
            token: token.clone(),
            fencing_epoch: fence.epoch,
        },
        schema_identity: expected_schema_identity(),
        principal: GrantPrincipal {
            kind: PrincipalKind::Interactive,
            machine_id: request.machine_id.clone(),
            project_id: request.project_id.clone(),
            execution_id: None,
            session_id: request.session_id.clone(),
            code_overlay_project_id: overlay.map(|overlay| overlay.to_string()),
        },
        capabilities,
        issued_at: now,
        expires_at: valid_until,
        payload_checksum: String::new(),
        signature: String::new(),
    };
    let grant = sign_grant(&grant, fence.secret.as_bytes()).map_err(internal)?;
    let mut reply = json_response(
        StatusCode::OK,
        json!({
            "grant": grant,
            "deployment_token": token,
            "fencing_epoch": fence.epoch,
        }),
    );
    reply
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(reply)
}

/// The daemon's own machine, as the file and as the UUID the issuer keys on.
fn local_machine(home: &Path) -> Result<(String, Uuid), Box<Response<Body>>> {
    let machine = read_machine_id_from_home(home).map_err(|error| {
        eprintln!("gdaemon runtime: machine id unreadable: {error}");
        grant_unavailable()
    })?;
    let uuid = Uuid::parse_str(&machine).map_err(|_| grant_unavailable())?;
    Ok((machine, uuid))
}

async fn admitted_project(pool: &Pool, project_id: &str) -> Result<Option<Uuid>, Response<Body>> {
    let Ok(project) = Uuid::parse_str(project_id) else {
        return Ok(None);
    };
    let client = pool.get().await.map_err(internal)?;
    let admitted = client
        .query_opt(
            "SELECT 1 FROM projects WHERE id = $1::text::uuid AND deleted_at IS NULL",
            &[&project.to_string()],
        )
        .await
        .map_err(internal)?;
    Ok(admitted.map(|_| project))
}

/// Python's `_normalized_overlay_claim`: a UUID that is not the project itself.
fn overlay_claim(claim: Option<&str>, project: Uuid) -> Result<Option<Uuid>, Box<Response<Body>>> {
    let Some(claim) = claim else {
        return Ok(None);
    };
    let overlay = Uuid::parse_str(claim)
        .map_err(|_| rejection("claims_mismatch", "code_overlay_project_id must be a UUID"))?;
    if overlay == project {
        return Err(Box::new(rejection(
            "claims_mismatch",
            "code_overlay_project_id must differ from project_id",
        )));
    }
    Ok(Some(overlay))
}

pub(crate) async fn active_secrets(
    store: &SecretStore,
    token: &str,
    active: &ActiveState,
) -> Result<BTreeMap<String, Option<String>>, Response<Body>> {
    let mut secrets = BTreeMap::new();
    for (key, binding) in &active.document.active_bindings {
        let plaintext = match &binding.sealed {
            Some(sealed) => {
                let aad = format!("config-binding:{token}:{key}:{}", binding.fingerprint);
                let opened = store
                    .open_sealed(sealed, aad.as_bytes())
                    .await
                    .map_err(|_| grant_unavailable())?;
                Some(String::from_utf8(opened).map_err(internal)?)
            }
            None => None,
        };
        secrets.insert(key.clone(), plaintext);
    }
    Ok(secrets)
}

struct Principal<'a> {
    token: &'a str,
    machine: Uuid,
    project: Uuid,
    session: Option<Uuid>,
    overlay: Option<Uuid>,
    expires_at: SystemTime,
}

impl Principal<'_> {
    fn aad(&self, generation: i32) -> String {
        format!(
            "{}:{}:{}:{generation}",
            self.token, self.machine, self.project
        )
    }
}

struct Issued {
    role_name: String,
    generation: i32,
    password: String,
}

/// Python's `issue_grant_postgres` for an interactive principal: heartbeat, then one
/// transaction that issues or reuses the principal and stores or opens its sealed
/// password. The advisory lock the issuer takes is held until the material is stored.
async fn issue_principal(
    pool: &Pool,
    store: &SecretStore,
    principal: &Principal<'_>,
    fresh: String,
) -> Result<Issued, Response<Body>> {
    let mut client = pool.get().await.map_err(|_| credential_failed())?;
    let auth = auth_schema(&client).await?;
    client
        .execute(
            &format!("SELECT {auth}.heartbeat_daemon($1::text::uuid, interval '2 minutes')"),
            &[&principal.machine.to_string()],
        )
        .await
        .map_err(|_| credential_failed())?;
    let transaction = client
        .transaction()
        .await
        .map_err(|_| credential_failed())?;
    let machine = principal.machine.to_string();
    let project = principal.project.to_string();
    let session = principal.session.map(|session| session.to_string());
    let overlay = principal.overlay.map(|overlay| overlay.to_string());
    let row = transaction
        .query_one(
            &format!(
                "SELECT role_name::text, credential_generation, reused \
                 FROM {auth}.issue_or_reuse_interactive_principal(\
                 $1::text, $2::text::uuid, $3::text::uuid, $4::text::uuid, $5, $6::text, $7::text::uuid)"
            ),
            &[
                &principal.token,
                &machine,
                &project,
                &session,
                &principal.expires_at,
                &fresh,
                &overlay,
            ],
        )
        .await
        .map_err(credential_error)?;
    let role_name: String = row.try_get(0).map_err(internal)?;
    let generation: i32 = row.try_get(1).map_err(internal)?;
    let reused: bool = row.try_get(2).map_err(internal)?;
    let aad = principal.aad(generation);
    let password = if reused {
        let row = transaction
            .query_one(
                &format!(
                    "SELECT ciphertext FROM {auth}.load_interactive_credential_material(\
                     $1::text, $2::text::uuid, $3::text::uuid, $4::integer)"
                ),
                &[&principal.token, &machine, &project, &generation],
            )
            .await
            .map_err(credential_error)?;
        let ciphertext: String = row.try_get(0).map_err(internal)?;
        let opened = store
            .open_sealed(&ciphertext, aad.as_bytes())
            .await
            .map_err(secret_failure)?;
        String::from_utf8(opened).map_err(|_| credential_failed())?
    } else {
        let ciphertext = store
            .seal(fresh.as_bytes(), aad.as_bytes())
            .await
            .map_err(secret_failure)?;
        let stored: bool = transaction
            .query_one(
                &format!(
                    "SELECT {auth}.replace_interactive_credential_material(\
                     $1::text, $2::text::uuid, $3::text::uuid, $4::integer, $5::text, $6::text)"
                ),
                &[
                    &principal.token,
                    &machine,
                    &project,
                    &generation,
                    &ciphertext,
                    &aad,
                ],
            )
            .await
            .map_err(credential_error)?
            .try_get(0)
            .map_err(internal)?;
        if !stored {
            return Err(credential_failed());
        }
        fresh
    };
    transaction.commit().await.map_err(credential_error)?;
    Ok(Issued {
        role_name,
        generation,
        password,
    })
}

async fn auth_schema(client: &Client) -> Result<String, Response<Body>> {
    let row = client
        .query_one("SELECT current_schema()::text", &[])
        .await
        .map_err(|_| credential_failed())?;
    let schema: String = row.try_get(0).map_err(|_| credential_failed())?;
    quote_identifier(&auth_schema_for(&schema)).map_err(|_| credential_failed())
}

/// Random 32 bytes, base64url without padding, as Python's `secrets.token_urlsafe(32)`.
fn random_password() -> String {
    let mut bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}

/// The bootstrap database URL as libpq keywords, with the issued role and password, as
/// Python's `_interactive_dsn`.
fn interactive_dsn(
    database_url: &str,
    role: &str,
    password: &str,
) -> Result<String, Box<Response<Body>>> {
    let url = Url::parse(database_url).map_err(internal)?;
    let mut parts: Vec<(String, String)> = Vec::new();
    if let Some(host) = url.host_str() {
        parts.push(("host".into(), host.trim_matches(['[', ']']).into()));
    }
    if let Some(port) = url.port() {
        parts.push(("port".into(), port.to_string()));
    }
    let dbname = url.path().trim_start_matches('/');
    if !dbname.is_empty() {
        parts.push(("dbname".into(), dbname.into()));
    }
    parts.extend(
        url.query_pairs()
            .filter(|(key, _)| key != "user" && key != "password")
            .map(|(key, value)| (key.into_owned(), value.into_owned())),
    );
    with_utc_session_timezone(&mut parts);
    parts.push(("user".into(), role.into()));
    parts.push(("password".into(), password.into()));
    Ok(parts
        .iter()
        .map(|(key, value)| {
            let escaped = value.replace('\\', "\\\\").replace('\'', "\\'");
            format!("{key}='{escaped}'")
        })
        .collect::<Vec<_>>()
        .join(" "))
}

/// Python's `conninfo_with_utc_session_timezone`: the session options gain
/// `-ctimezone=UTC` unless a timezone option is already present, matched case-insensitively.
fn with_utc_session_timezone(parts: &mut Vec<(String, String)>) {
    let index = parts.iter().position(|(key, _)| key == "options");
    let options = index.map_or("", |index| parts[index].1.as_str());
    let lowered = options.to_ascii_lowercase();
    if lowered.contains("-ctimezone=") || lowered.contains("-c timezone=") {
        return;
    }
    let joined = [options, "-ctimezone=UTC"]
        .into_iter()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    match index {
        Some(index) => parts[index].1 = joined,
        None => parts.push(("options".into(), joined)),
    }
}

fn rejection(code: &str, message: &str) -> Response<Body> {
    json_response(
        StatusCode::FORBIDDEN,
        json!({"code": code, "error": code, "message": message}),
    )
}

fn credential_failed() -> Response<Body> {
    rejection("credential_issuance_failed", "credential issuance failed")
}

/// Issuer refusals of the requested overlay are claim mismatches; anything else in the
/// issuance transaction is a credential failure, as in Python.
fn credential_error(error: tokio_postgres::Error) -> Response<Body> {
    match error.code() {
        Some(code)
            if *code == SqlState::FOREIGN_KEY_VIOLATION
                || *code == SqlState::INVALID_PARAMETER_VALUE =>
        {
            let message = error
                .as_db_error()
                .map_or_else(|| error.to_string(), |db| db.message().to_owned());
            rejection("claims_mismatch", &message)
        }
        _ => credential_failed(),
    }
}

fn secret_failure(error: SecretError) -> Response<Body> {
    match error {
        SecretError::Unavailable | SecretError::KeyFile(_) => grant_unavailable(),
        _ => credential_failed(),
    }
}

fn handshake_unavailable() -> Response<Body> {
    json_response(
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"detail": "handshake service unavailable"}),
    )
}

fn invalid_request() -> Response<Body> {
    json_response(
        StatusCode::UNPROCESSABLE_ENTITY,
        json!({"detail": "invalid handshake request"}),
    )
}
