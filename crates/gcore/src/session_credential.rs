//! The session-bound credential an unspawned session receives at SessionStart.
//!
//! The daemon mints a token that names one session and returns it on the start
//! response. The hook client keeps it in a per-session, owner-only record so later
//! hooks and the MCP client present that token instead of the machine-wide bearer.

use std::borrow::Cow;
use std::fmt;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Response field the daemon uses to hand a start its session-bound token.
pub const SESSION_CREDENTIAL_FIELD: &str = "_gobby_session_credential";

/// Request header that names the session a request acts as.
pub const SESSION_ID_HEADER: &str = "X-Gobby-Session-Id";

/// Request headers that select a session, run or execution. A machine-bearer request
/// may not carry them; a request presenting its session's own credential may.
pub const SESSION_SELECTOR_HEADERS: [&str; 4] = [
    SESSION_ID_HEADER,
    "X-Gobby-Agent-Run-Id",
    "X-Gobby-Managed-Execution-Id",
    "X-Gobby-Terminal-Context",
];

const STORE_DIR: &str = "session-credentials";
const RECORD_NAMESPACE: Uuid = Uuid::from_u128(0x2585_5c0d_e000_4000_8000_0000_2585_5001);

/// Environment variables that mark a spawned, managed agent run.
const MANAGED_ENVIRONMENT_VARS: [&str; 3] = [
    "GOBBY_AGENT_RUN_ID",
    crate::grant::MANAGED_BOOTSTRAP_ENV,
    crate::local_token::AGENT_API_TOKEN_ENV,
];

/// A stored session-bound credential. `Debug` never prints the token.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionCredential {
    pub token: String,
    pub session_id: String,
    pub project_id: String,
    /// The provider's own session id, so a record never serves another conversation.
    #[serde(default)]
    pub provider_session_id: String,
}

impl fmt::Debug for SessionCredential {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SessionCredential")
            .field("token", &"<redacted>")
            .field("session_id", &self.session_id)
            .field("project_id", &self.project_id)
            .field("provider_session_id", &self.provider_session_id)
            .finish()
    }
}

/// Store key for the record a provider's own hooks read back.
pub fn provider_key(provider_session_id: &str) -> String {
    format!("provider:{provider_session_id}")
}

/// Store key for the CLI process that owns a session, stable across CLI restarts of the
/// same terminal identity. The hook client and the MCP client derive the same key.
pub fn cli_pid_key(pid: u32) -> String {
    format!("cli-pid:{pid}")
}

/// Whether this process runs inside a spawned, managed agent run.
pub fn is_managed_environment() -> bool {
    managed_environment_with(|name| {
        std::env::var_os(name).is_some_and(|value| marker_is_set(&value))
    })
}

/// Whether any spawned-run marker is set, reading each one through `is_set`.
fn managed_environment_with(is_set: impl Fn(&str) -> bool) -> bool {
    MANAGED_ENVIRONMENT_VARS.into_iter().any(is_set)
}

/// A marker names a managed run only when it holds text. Blank values count as unset, as the
/// Python proxy's `strip()` check treats them.
fn marker_is_set(value: &std::ffi::OsStr) -> bool {
    !value.to_string_lossy().trim().is_empty()
}

/// Remove the credential field from a hook response body, returning the rest.
///
/// A body without the field, or one that is not a JSON object, is returned borrowed
/// and unchanged.
pub fn extract(body: &str) -> (Cow<'_, str>, Option<SessionCredential>) {
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(body) else {
        return (Cow::Borrowed(body), None);
    };
    let Some(field) = value
        .as_object_mut()
        .and_then(|object| object.remove(SESSION_CREDENTIAL_FIELD))
    else {
        return (Cow::Borrowed(body), None);
    };
    (
        Cow::Owned(value.to_string()),
        serde_json::from_value(field).ok(),
    )
}

/// Write the record under every key, owner-only.
pub fn store(home: &Path, keys: &[String], credential: &SessionCredential) -> anyhow::Result<()> {
    let dir = home.join(STORE_DIR);
    std::fs::create_dir_all(&dir)?;
    restrict_dir(&dir)?;
    let bytes = serde_json::to_vec(credential)?;
    for key in keys {
        write_owner_only(&record_path(home, key), &bytes)?;
    }
    Ok(())
}

/// Read the record stored under one key.
pub fn load(home: &Path, key: &str) -> Option<SessionCredential> {
    let bytes = std::fs::read(record_path(home, key)).ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// The record for one provider session, refused when it names another session.
pub fn bound_for_provider(home: &Path, provider_session_id: &str) -> Option<SessionCredential> {
    load(home, &provider_key(provider_session_id))
        .filter(|credential| credential.provider_session_id == provider_session_id)
}

fn record_path(home: &Path, key: &str) -> PathBuf {
    home.join(STORE_DIR).join(format!(
        "{}.json",
        Uuid::new_v5(&RECORD_NAMESPACE, key.as_bytes())
    ))
}

/// Write through a sibling temp file and rename it into place, so a reader never
/// sees a partial record and a failed write leaves no temp file behind.
fn write_owner_only(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    let written = (|| -> std::io::Result<()> {
        let mut file = owner_only_options()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()
    })();
    if let Err(error) = written {
        let _ = std::fs::remove_file(&tmp);
        return Err(error.into());
    }
    if let Err(error) = std::fs::rename(&tmp, path) {
        let _ = std::fs::remove_file(&tmp);
        return Err(error.into());
    }
    Ok(())
}

fn owner_only_options() -> std::fs::OpenOptions {
    let mut options = std::fs::OpenOptions::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

fn restrict_dir(dir: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(not(unix))]
    let _ = dir;
    Ok(())
}

#[cfg(test)]
#[path = "session_credential/tests.rs"]
mod tests;
