//! Input for a direct pane while the daemon is away (#24815). gclient asks the
//! host's control socket for the input grant a pane needs, with the existing
//! `hello` and `grant_input` verbs, so a key is never dropped for lack of a
//! daemon. The socket and its token are owner-only files beside the frame
//! socket, and gclient checks both before it speaks to either.

use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::time::timeout;

/// The control protocol version gclient speaks, matching the host's.
const PROTOCOL_VERSION: u64 = 1;
/// The longest one control exchange may take before its key is reported unsent.
const EXCHANGE_DEADLINE: Duration = Duration::from_secs(5);

/// Status for a key whose grant the host's control socket could not be asked
/// for. The key is not sent, and the pane says so.
pub(super) const CONTROL_UNREACHABLE: &str =
    "terminal control unavailable while the daemon is down; input not sent";

/// The control socket and its token for the host serving `frame_socket`.
pub(super) fn control_paths(frame_socket: &Path) -> (PathBuf, PathBuf) {
    (
        frame_socket.with_file_name("gterm-control.sock"),
        frame_socket.with_file_name("gterm-control.token"),
    )
}

/// Refuses a file another user can read or write, or one this user does not
/// own, so a control token is never read from a shared location.
pub(super) fn verify_owner_only(path: &Path) -> io::Result<()> {
    let metadata = std::fs::metadata(path)?;
    // SAFETY: geteuid has no preconditions and cannot fail.
    let euid = unsafe { libc::geteuid() };
    if metadata.uid() != euid || metadata.mode() & 0o077 != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("{} is not owner-only", path.display()),
        ));
    }
    Ok(())
}

/// The `grant_input` request giving `attachment_id` input on the terminal.
pub(super) fn grant_request(
    id: &str,
    host_terminal_id: &str,
    attachment_id: &str,
    host_attachment_id: u64,
) -> Value {
    json!({
        "attachment_id": attachment_id,
        "host_terminal_id": host_terminal_id,
        "id": id,
        "method": "grant_input",
        "host_attachment_id": host_attachment_id,
    })
}

/// The `revoke_input` request clearing `attachment_id`'s grant on the terminal.
pub(super) fn revoke_request(id: &str, host_terminal_id: &str, attachment_id: &str) -> Value {
    json!({
        "attachment_id": attachment_id,
        "host_terminal_id": host_terminal_id,
        "id": id,
        "method": "revoke_input",
    })
}

#[derive(Debug, PartialEq, Eq)]
pub enum GrantOutcome {
    Granted,
    /// The host's error code, or `not_granted` for a reply without a grant.
    Refused(String),
}

pub(super) fn grant_outcome(reply: &Value) -> GrantOutcome {
    if reply.get("ok").and_then(Value::as_bool) != Some(true) {
        let code = reply
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown_error");
        return GrantOutcome::Refused(code.to_string());
    }
    if reply.get("granted").and_then(Value::as_bool) == Some(true) {
        GrantOutcome::Granted
    } else {
        GrantOutcome::Refused("not_granted".into())
    }
}

/// Asks the host serving `frame_socket` to give `attachment_id` input on the
/// terminal. Each call is its own authenticated connection.
pub(super) async fn grant(
    frame_socket: &Path,
    host_terminal_id: &str,
    attachment_id: &str,
    host_attachment_id: u64,
) -> io::Result<GrantOutcome> {
    let reply = exchange(frame_socket, |id| {
        grant_request(id, host_terminal_id, attachment_id, host_attachment_id)
    })
    .await?;
    Ok(grant_outcome(&reply))
}

/// Clears `attachment_id`'s grant on the terminal, and only that one.
pub(super) async fn revoke(
    frame_socket: &Path,
    host_terminal_id: &str,
    attachment_id: &str,
) -> io::Result<()> {
    let reply = exchange(frame_socket, |id| {
        revoke_request(id, host_terminal_id, attachment_id)
    })
    .await?;
    if reply.get("ok").and_then(Value::as_bool) == Some(true) {
        return Ok(());
    }
    let code = reply
        .get("error")
        .and_then(Value::as_str)
        .unwrap_or("unknown_error");
    Err(io::Error::other(format!("terminal refused revoke: {code}")))
}

/// Verifies both files, then runs `hello` and one request on a fresh
/// connection. The token is read only after its file passes the owner check,
/// and it appears in neither the requests' logs nor any error text.
async fn exchange(frame_socket: &Path, request: impl FnOnce(&str) -> Value) -> io::Result<Value> {
    let (socket, token_path) = control_paths(frame_socket);
    verify_owner_only(&socket)?;
    verify_owner_only(&token_path)?;
    let token = std::fs::read_to_string(&token_path)?.trim().to_string();
    let id = uuid::Uuid::new_v4().to_string();
    let round_trip = async {
        let mut stream = BufReader::new(crate::frame_source::connect_host_control(&socket).await?);
        let hello = json!({
            "control_token": token,
            "id": format!("hello-{id}"),
            "method": "hello",
            "protocol_version": PROTOCOL_VERSION,
        });
        send_line(&mut stream, &hello).await?;
        let reply = read_reply(&mut stream).await?;
        if reply.get("ok").and_then(Value::as_bool) != Some(true) {
            let code = reply
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("unknown_error");
            return Err(io::Error::other(format!(
                "terminal control refused: {code}"
            )));
        }
        send_line(&mut stream, &request(&id)).await?;
        read_reply(&mut stream).await
    };
    timeout(EXCHANGE_DEADLINE, round_trip)
        .await
        .unwrap_or_else(|_| {
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "terminal control did not answer",
            ))
        })
}

async fn send_line(stream: &mut BufReader<UnixStream>, message: &Value) -> io::Result<()> {
    let mut line = message.to_string();
    line.push('\n');
    stream.get_mut().write_all(line.as_bytes()).await
}

async fn read_reply(stream: &mut BufReader<UnixStream>) -> io::Result<Value> {
    let mut line = String::new();
    if stream.read_line(&mut line).await? == 0 {
        return Err(io::ErrorKind::UnexpectedEof.into());
    }
    serde_json::from_str(&line).map_err(io::Error::other)
}

#[cfg(test)]
#[path = "outage_grant/tests.rs"]
mod tests;
