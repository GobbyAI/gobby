//! App shell: project selection, roster, and pane views.

pub mod grid;

use crate::app::{apply_sidebar_snapshot, run_live_loop};
use crate::daemon::LiveDaemon;
use crate::frame_source::AttachLocator;
use crate::persist::load_session;
use crate::startup::initial_project;
use crate::theme::Theme;
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

pub fn run_ready(
    ready: crate::startup::Ready,
    switch: &mut dyn crate::teardown::MouseCaptureSwitch,
) -> anyhow::Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(async move {
            let daemon =
                LiveDaemon::connect(ready.daemon_url, ready.token.unwrap_or_default()).await?;
            let mut workspace = Workspace::live(daemon);
            workspace.set_gobby_home(ready.gobby_home.clone());
            // Without a machine id the sidebar still lists agents; they just
            // sit under an empty machine name until the daemon fills it in.
            workspace.set_local_machine(
                gobby_core::machine::read_local_machine_id().unwrap_or_default(),
            );
            let session = load_session(&ready.gobby_home)?;
            let project = initial_project(ready.project, session.as_ref());
            workspace.set_launch_dir(ready.launch_dir);
            workspace.restore_project(&project).map_err(|error| {
                anyhow::anyhow!("failed to restore the workspace snapshot: {error}")
            })?;
            workspace.set_frame_delivery(ready.frame_delivery);
            let mut chrome = Chrome::new(Theme::new(ready.prefs.theme_kind()));
            chrome.apply_prefs(ready.prefs);
            chrome.keymap = ready.keymap;
            chrome.nested_tmux = ready.nested_tmux;
            if let Some(session) = session {
                apply_sidebar_snapshot(&mut chrome.sidebar, &session.sidebar);
            }
            chrome.status_message = ready.host_notice;
            let mut terminal = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;
            let input = gobby_terminal::raw_input::spawn_input_reader();
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input, switch).await?;
            Ok(())
        })
}
