//! App shell: project selection, roster, and pane views.

use crate::frame_source::AttachLocator;
use crate::theme::{Theme, ThemeKind};
use crate::Workspace;
use gobby_terminal::protocol::{ClientMessage, PaneLocator, RenderEncoding, PROTOCOL_VERSION};

/// Handshake + user attach used by the live workspace and by Unix frame connect.
pub fn observe_tmux_pane(locator: &AttachLocator) -> (ClientMessage, ClientMessage) {
    let tmux_identity = crate::tmux_identity::current();
    let hello = ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: String::new(),
        cols: 80,
        rows: 24,
        tmux_identity,
    };
    let pane = match (
        locator.pane_id.clone(),
        locator.server_pid,
        locator.server_start_time,
    ) {
        (Some(pane_id), Some(server_pid), Some(server_start_time)) => Some(PaneLocator {
            socket_path: locator.socket_path.clone(),
            server_pid,
            server_start_time,
            pane_id,
        }),
        _ => None,
    };
    let attach = ClientMessage::AttachTerminal {
        host_terminal_id: locator.host_terminal_id.clone(),
        reservation_id: None,
        locator: pane,
    };
    (hello, attach)
}

pub fn run() -> anyhow::Result<()> {
    crate::startup::run()
}

pub fn run_ready(ready: crate::startup::Ready) -> anyhow::Result<()> {
    let mut ws = Workspace::scripted();
    if let Some(project) = ready.project {
        ws.select_project(project);
    }
    let _url = ready.daemon_url;
    let _token = ready.token;
    let _theme = Theme::new(ThemeKind::Dark);
    Ok(())
}
