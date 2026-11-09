//! The bearer a process presents with a runtime grant.
//!
//! An interactive grant bound to a session is presented with that session's own
//! credential, so the machine key never speaks for a session. The session-less grant of
//! an operator shell keeps the machine key.

use std::path::Path;

use super::{GrantBundle, GrantError, PrincipalKind};
use crate::process_identity::current_cli_pid;
use crate::session_credential::{self, SessionCredential, cli_pid_key};

const NO_CREDENTIAL: &str = "no session credential is bound to this process";
const FOREIGN_CREDENTIAL: &str = "the bound session credential belongs to another session";

/// Test seam: the record a test process presents in place of its CLI's record.
#[cfg(test)]
pub(crate) static TEST_PROCESS_RECORD: std::sync::Mutex<Option<SessionCredential>> =
    std::sync::Mutex::new(None);

/// The bearer that presents `grant`. `machine_token` is the process's machine or managed
/// bearer, used for every request that names no session.
pub fn presentation_bearer(
    grant: Option<&GrantBundle>,
    machine_token: &str,
) -> Result<String, GrantError> {
    let Some(grant) = grant else {
        return Ok(machine_token.to_string());
    };
    let principal = &grant.principal;
    match principal.session_id.as_deref() {
        Some(session_id) if principal.kind == PrincipalKind::Interactive => {
            session_bearer(presented_record(), session_id)
        }
        _ => Ok(machine_token.to_string()),
    }
}

/// The record this process presents: the test seam when a test installs one, else the
/// record its own CLI process wrote.
fn presented_record() -> Option<SessionCredential> {
    #[cfg(test)]
    if let Some(record) = TEST_PROCESS_RECORD
        .lock()
        .ok()
        .and_then(|slot| slot.clone())
    {
        return Some(record);
    }
    process_record(
        crate::gobby_home().ok().as_deref(),
        current_cli_pid(),
        session_credential::is_managed_environment(),
    )
}

/// The process's own session credential. A managed environment never presents one: its
/// bearer is the managed token, so the session record is not its identity.
fn process_record(
    home: Option<&Path>,
    cli_pid: Option<u32>,
    managed: bool,
) -> Option<SessionCredential> {
    if managed {
        return None;
    }
    session_credential::load(home?, &cli_pid_key(cli_pid?))
}

/// Present only the record of `session_id`. A missing or foreign record refuses, and the
/// refusal names the reason without the token.
fn session_bearer(
    record: Option<SessionCredential>,
    session_id: &str,
) -> Result<String, GrantError> {
    match record {
        None => Err(GrantError::SessionCredentialRefused(NO_CREDENTIAL)),
        Some(record) if record.session_id != session_id => {
            Err(GrantError::SessionCredentialRefused(FOREIGN_CREDENTIAL))
        }
        Some(record) => Ok(record.token),
    }
}

#[cfg(test)]
#[path = "bearer/tests.rs"]
mod tests;
