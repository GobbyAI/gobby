mod mock_daemon;

use std::time::{Duration, Instant};

use gobby_client::app::run_live_loop;
use gobby_client::app::startup_stages::{ConnectionView, StageState, StartupStages};
use gobby_client::daemon::{
    DaemonError, Generation, LiveDaemon, WorkspaceError, WorkspaceErrorCode,
};
use gobby_client::frame_source::{PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::marks::{self, MarkPalette};
use gobby_client::ui::{render_workspace, splash, status, Chrome};
use gobby_client::Workspace;
use gobby_terminal::protocol::{CellData, FrameData, PaneModes, ServerMessage};
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use tokio::sync::mpsc;
use tokio::time::timeout;

fn waiting_chrome() -> Chrome {
    let mut chrome = Chrome::dark();
    let start = Instant::now();
    chrome.connection = ConnectionView {
        url: "http://127.0.0.1:60887".into(),
        machine: "local".into(),
        daemon_version: None,
        launch_project: None,
        stages: Some(StartupStages::for_test(
            [
                StageState::Running { since: start },
                StageState::Pending,
                StageState::Pending,
                StageState::Pending,
            ],
            start + Duration::from_millis(9800),
        )),
        retry_at: None,
        now: start + Duration::from_millis(9800),
    };
    chrome
}

fn draw(width: u16, height: u16, area: Rect, chrome: &Chrome) -> Terminal<TestBackend> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).expect("test terminal");
    terminal
        .draw(|frame| splash::render_splash(frame, area, chrome))
        .expect("splash draw");
    terminal
}

fn row(terminal: &Terminal<TestBackend>, y: u16) -> String {
    (0..terminal.backend().buffer().area.width)
        .map(|x| terminal.backend().buffer()[(x, y)].symbol())
        .collect()
}

/// The marks drawn straight into an empty buffer at the given origins, so
/// the splash is judged cell for cell.
fn marks_at(
    width: u16,
    height: u16,
    placed: &[(&marks::Mark, (u16, u16))],
    chrome: &Chrome,
) -> Terminal<TestBackend> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).expect("test terminal");
    let palette = MarkPalette::normal(&chrome.palette, chrome.prefs.monochrome);
    terminal
        .draw(|frame| {
            for (mark, origin) in placed {
                marks::render_mark(frame, *origin, mark, &palette);
            }
        })
        .expect("marks draw");
    terminal
}

#[test]
fn goblin_and_wordmark_stand_alone_centred_and_drop_as_the_area_shrinks() {
    let chrome = waiting_chrome();
    let (goblin, wordmark) = (marks::goblin_large(), marks::wordmark());

    // Both: the wordmark 45 columns right of the haloed goblin and 6 rows
    // down.
    let full = draw(120, 40, Rect::new(0, 0, 120, 40), &chrome);
    let both = marks_at(
        120,
        40,
        &[(goblin, (10, 11)), (wordmark, (55, 17))],
        &chrome,
    );
    assert_eq!(full.backend().buffer(), both.backend().buffer());

    // Too narrow for both: the wordmark alone.
    let narrow = draw(60, 20, Rect::new(0, 0, 60, 20), &chrome);
    let alone = marks_at(60, 20, &[(wordmark, (3, 6))], &chrome);
    assert_eq!(narrow.backend().buffer(), alone.backend().buffer());

    // Too narrow for the wordmark: the goblin alone, inside its area.
    let compact = draw(50, 20, Rect::new(5, 1, 43, 19), &chrome);
    let goblin_only = marks_at(50, 20, &[(goblin, (6, 1))], &chrome);
    assert_eq!(compact.backend().buffer(), goblin_only.backend().buffer());

    // Too small for either: nothing at all.
    let tiny = draw(30, 10, Rect::new(0, 0, 30, 10), &chrome);
    let empty = marks_at(30, 10, &[], &chrome);
    assert_eq!(tiny.backend().buffer(), empty.backend().buffer());
}

#[test]
fn the_splash_is_the_whole_frame_until_a_failed_first_connect_falls_through() {
    let ws = Workspace::scripted();
    let mut chrome = waiting_chrome();
    // Under System the ground stays the terminal's, so the frame is the
    // marks alone; Dark and Light paint theirs over it (chrome_render).
    chrome.prefs.theme = "system".to_string();
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .expect("splash frame");
    let splash = marks_at(
        120,
        40,
        &[
            (marks::goblin_large(), (10, 11)),
            (marks::wordmark(), (55, 17)),
        ],
        &chrome,
    );
    assert_eq!(terminal.backend().buffer(), splash.backend().buffer());

    // A failed first connect schedules a retry and never finishes its
    // stages: the chrome comes back and the status line says why.
    chrome.connection.retry_at = Some(chrome.connection.now + Duration::from_secs(3));
    terminal
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .expect("retry frame");
    let menu = row(&terminal, 0);
    assert!(menu.starts_with(" Gobby  File "), "{menu}");
    let status = row(&terminal, 39);
    assert!(
        status.contains("× Daemon unreachable · retrying in 3 s"),
        "{status}"
    );
}

#[test]
fn missing_named_workspace_is_not_reported_as_a_daemon_outage() {
    let mut ws = Workspace::scripted();
    ws.observe_daemon_disconnect(
        Generation(0),
        DaemonError::Workspace(WorkspaceError {
            code: WorkspaceErrorCode::NotFound,
            reason: "Workspace 'capture-22745' not found".into(),
        }),
    );
    let mut chrome = Chrome::dark();
    chrome.prefs.status_left.clear();
    chrome.prefs.status_right.clear();
    let mut terminal = Terminal::new(TestBackend::new(120, 1)).expect("test terminal");
    terminal
        .draw(|frame| {
            status::render_status_line(frame, Rect::new(0, 0, 120, 1), &ws, &chrome);
        })
        .expect("draw status");
    let line = row(&terminal, 0);
    assert!(
        line.starts_with(" Workspace 'capture-22745' not found"),
        "status should report the refused workspace name: {line}"
    );
}

#[tokio::test]
async fn panes_keep_their_last_frame_while_the_daemon_is_away() {
    let mut workspace = Workspace::scripted();
    let pane = workspace
        .open_terminal("terminal-1", "native", "epoch-1")
        .expect("pane");
    let frame = FrameData {
        width: 1,
        height: 1,
        cells: vec![CellData {
            symbol: "A".into(),
            fg: 0,
            bg: 0,
            modifier: 0,
            skip: false,
            hyperlink: None,
        }],
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    };
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(frame.clone()));
    workspace
        .replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("frame source");
    workspace.recv_pane_frame(pane).await.expect("first frame");
    let attachment_id = workspace.pane(pane).attachment_id().to_string();

    workspace
        .apply_ws(&json!({
            "type": "terminal_attachment_finalized",
            "attachment_id": attachment_id,
            "reason": "daemon away"
        }))
        .expect("retire attachment");

    assert_eq!(workspace.pane(pane).latest_frame(), Some(&frame));
    assert!(workspace.pane(pane).frame_source().is_none());
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "terminal-1");
    chrome.compute_view(&workspace, Rect::new(0, 0, 80, 24));
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_workspace(frame, &workspace, &chrome);
        })
        .expect("render frozen pane");
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|cell| cell.symbol() == "A"),
        "last frame remains visible after the attachment retires"
    );
}

#[tokio::test]
async fn a_dropped_daemon_toasts_the_url_once_and_clears_on_reconnect() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    let mut chrome = Chrome::dark();
    chrome.connection.url = mock.url().to_string();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        for expected in 1..=2 {
            timeout(Duration::from_secs(5), async {
                loop {
                    let count = mock
                        .requests()
                        .iter()
                        .filter(|request| {
                            request.method == "GET" && request.target.starts_with("/api/projects")
                        })
                        .count();
                    if count >= expected {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("sidebar fetch after handshake");
            if expected == 1 {
                mock.drop_websockets();
            }
        }
        drop(input_tx);
    };
    timeout(Duration::from_secs(12), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("daemon reconnects");
    assert_eq!(chrome.alert_log.len(), 1, "one toast for the outage");
    assert!(chrome.alert_log[0].title.contains(mock.url()));
    assert_eq!(
        chrome.connection.retry_at, None,
        "retry clears on handshake"
    );
    mock.shutdown().await;
}
