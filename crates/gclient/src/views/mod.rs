//! App shell: project selection, roster, and pane views.

pub mod grid;

use crate::app::run_live_loop;
use crate::app::startup_stages::{ConnectionView, StartupStages};
use crate::daemon::LiveDaemon;
use crate::frame_source::AttachLocator;
use crate::theme::Theme;
use crate::ui::status::Toast;
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
                LiveDaemon::unconnected(&ready.daemon_url, ready.token.unwrap_or_default())?;
            let mut workspace = Workspace::live(daemon);
            workspace.set_gobby_home(ready.gobby_home.clone());
            // Without a machine id the sidebar still lists agents; they just
            // sit under an empty machine name until the daemon fills it in.
            let machine = gobby_core::machine::read_local_machine_id().unwrap_or_default();
            workspace.set_local_machine(machine.clone());
            let mut attach = ready.attach;
            if attach.workspace.is_none() {
                attach.project_id = ready.project.clone();
            }
            workspace.set_attach_target(attach);
            workspace.set_in_pane(ready.in_pane);
            workspace.set_launch_dir(ready.launch_dir);
            workspace.set_frame_delivery(ready.frame_delivery);
            let mut chrome = Chrome::new(Theme::new(ready.prefs.theme_kind()));
            chrome.apply_prefs(ready.prefs);
            chrome.keymap = ready.keymap;
            chrome.nested = ready.nested;
            if let Some(notice) = ready.host_notice {
                chrome.notify(Toast::info(notice));
            }
            let mut terminal = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;
            let now = std::time::Instant::now();
            chrome.connection = ConnectionView {
                url: ready.daemon_url,
                machine,
                launch_project: ready.project,
                stages: Some(StartupStages::begin(now)),
                now,
                ..ConnectionView::default()
            };
            let input =
                gobby_terminal::raw_input::spawn_input_reader(chrome.host_color_query.clone());
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input, switch).await?;
            Ok(())
        })
}
