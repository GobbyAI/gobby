//! The session-bound credential an unspawned MCP client presents.
//!
//! The hook client stores the daemon's credential for its session under the CLI
//! process that runs both the hooks and this MCP client. The MCP client reads it back
//! by deriving the same CLI pid from its own parent. A seat on a shared host, which
//! every seat on it shares, is stored under its own session refs instead; the client
//! finds it by the session_id its call names, and the hook gate refuses a call naming
//! any session other than the caller's own. Only a client the shared host launched has
//! hook-gated calls, so only it looks a record up by ref; a gmcp started from a seat's
//! shell never does. A managed run never reads one.

use std::path::Path;
#[cfg(unix)]
use std::process::{Command, Stdio};

use gobby_core::process_identity::current_cli_pid;
use gobby_core::session_credential::{self, SessionCredential, cli_pid_key};

const CODEX_PROCESS_NAME: &str = "codex";
const CODEX_SHARED_HOST_FLAG: &str = "--managed-daemon";

/// The credential stored for this unspawned client's CLI process, if one exists.
pub fn cli_binding() -> Option<SessionCredential> {
    if session_credential::is_managed_environment() {
        return None;
    }
    let home = gobby_core::gobby_home().ok()?;
    session_credential::load(&home, &cli_pid_key(current_cli_pid()?))
}

/// The credential a call presents: its CLI's own record first, else, for a client the
/// shared host launched, the record stored under the session ref the call names.
pub fn binding_for(session_ref: Option<&str>, shared_host: bool) -> Option<SessionCredential> {
    if let Some(binding) = cli_binding() {
        return Some(binding);
    }
    if !shared_host || session_credential::is_managed_environment() {
        return None;
    }
    let reference = session_ref
        .map(str::trim)
        .filter(|reference| !reference.is_empty())?;
    let home = gobby_core::gobby_home().ok()?;
    session_credential::load(&home, &session_ref_key(reference))
}

/// Store key for a shared-host seat's record under one of its own session refs; ghook
/// writes the same key.
pub fn session_ref_key(session_ref: &str) -> String {
    format!("session-ref:{session_ref}")
}

/// Whether this process's parent is Codex's shared app-server, which launched it as an MCP
/// client. An unreadable parent is not the host.
pub fn parent_is_codex_shared_host() -> bool {
    #[cfg(unix)]
    {
        let parent = std::os::unix::process::parent_id().to_string();
        match (ps_field(&parent, "comm="), ps_field(&parent, "args=")) {
            (Some(name), Some(args)) => is_codex_shared_host(&name, &args),
            _ => false,
        }
    }
    #[cfg(not(unix))]
    {
        false
    }
}

/// Whether a process with this name and command line is Codex's shared app-server. The name
/// may be a full executable path; the flag must be its own argument.
pub fn is_codex_shared_host(name: &str, args: &str) -> bool {
    let named_codex = Path::new(name.trim())
        .file_name()
        .is_some_and(|base| base == CODEX_PROCESS_NAME);
    named_codex
        && args
            .split_whitespace()
            .any(|arg| arg == CODEX_SHARED_HOST_FLAG)
}

#[cfg(unix)]
fn ps_field(pid: &str, field: &str) -> Option<String> {
    let output = Command::new("ps")
        .args(["-o", field, "-p", pid])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8(output.stdout).ok()?.trim().to_owned();
    (!value.is_empty()).then_some(value)
}

#[cfg(test)]
#[path = "session_binding/tests.rs"]
mod tests;
