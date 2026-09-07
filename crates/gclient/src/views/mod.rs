//! App shell: project selection, roster, and pane views.

pub mod grid;

use crate::app::run_live_loop;
use crate::daemon::LiveDaemon;
use crate::frame_source::AttachLocator;
use crate::theme::{Theme, ThemeKind};
use crate::ui::Chrome;
use crate::Workspace;
use gobby_terminal::protocol::{ClientMessage, RenderEncoding, PROTOCOL_VERSION};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

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
    let attach = ClientMessage::AttachTerminal {
        host_terminal_id: locator.host_terminal_id.clone(),
        reservation_id: None,
        locator: locator.pane.clone(),
    };
    (hello, attach)
}

pub fn run() -> anyhow::Result<()> {
    crate::startup::run()
}

pub fn run_ready(ready: crate::startup::Ready) -> anyhow::Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(async move {
            let daemon =
                LiveDaemon::connect(ready.daemon_url, ready.token.unwrap_or_default()).await?;
            let mut workspace = Workspace::live(daemon);
            workspace.select_project(ready.project);
            workspace.set_frame_delivery(ready.frame_delivery);
            let mut chrome = Chrome::new(Theme::new(ThemeKind::Dark));
            chrome.status_message = ready.host_notice;
            let mut terminal = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;
            let input = gobby_terminal::raw_input::spawn_input_reader();
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input).await?;
            Ok(())
        })
}
