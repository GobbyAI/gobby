//! Client-side tmux identity for recursive-view refusal.

use gobby_terminal::protocol::TmuxClientIdentity;
use std::path::PathBuf;
use std::process::Command;

/// Parse `$TMUX` and the current pane, or `None` when not inside tmux.
pub fn current() -> Option<TmuxClientIdentity> {
    let tmux = std::env::var("TMUX").ok()?;
    let socket = tmux.split(',').next()?.to_string();
    if socket.is_empty() {
        return None;
    }
    let pane_id = tmux_display(&socket, "#{pane_id}")?;
    let server_pid = tmux_display(&socket, "#{pid}")?.parse().ok()?;
    let server_start_time = tmux_display(&socket, "#{start_time}")?.parse().ok()?;
    Some(TmuxClientIdentity {
        socket_path: socket,
        server_pid,
        server_start_time,
        pane_id,
    })
}

fn tmux_display(socket: &str, format: &str) -> Option<String> {
    let output = Command::new("tmux")
        .args(["-S", socket, "display-message", "-p", format])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

pub fn socket_from_tmux_env(value: &str) -> Option<PathBuf> {
    value.split(',').next().map(PathBuf::from)
}
