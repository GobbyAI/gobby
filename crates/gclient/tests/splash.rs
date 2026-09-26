mod mock_daemon;

use std::time::{Duration, Instant};

use gobby_client::app::run_live_loop;
use gobby_client::app::startup_stages::{ConnectionView, StageState, StartupStages};
use gobby_client::daemon::{
    DaemonError, Generation, LiveDaemon, WorkspaceError, WorkspaceErrorCode,
};
use gobby_client::frame_source::{PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::teardown::TerminalGuard;
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

#[test]
fn group_is_centred_in_the_pane_area_and_never_clips() {
    let chrome = waiting_chrome();
    let full = draw(120, 40, Rect::new(0, 0, 120, 40), &chrome);
    let first_stage = (0..40)
        .map(|y| row(&full, y))
        .find(|line| line.contains("daemon health"))
        .expect("stage row");
    let byte = first_stage.find("daemon health").expect("stage label");
    assert_eq!(first_stage[..byte].chars().count(), 53);
    assert!(first_stage.contains("9.8 s and waiting"));
    assert!(row(&full, 12).chars().any(|ch| ch != ' '));

    let wordmark_only = draw(60, 20, Rect::new(0, 0, 60, 20), &chrome);
    assert!(row(&wordmark_only, 12).contains("daemon health"));
    assert!(row(&wordmark_only, 2).chars().any(|ch| ch != ' '));

    let compact = draw(50, 20, Rect::new(5, 2, 40, 16), &chrome);
    let compact_rows: Vec<String> = (0..20).map(|y| row(&compact, y)).collect();
    assert!(compact_rows
        .iter()
        .any(|line| line.contains("daemon health")));
    for (y, line) in compact_rows.iter().enumerate() {
        for (x, ch) in line.chars().enumerate() {
            if !(5..45).contains(&x) || !(2..18).contains(&y) {
                assert_eq!(ch, ' ', "splash escaped ({x}, {y})");
            }
        }
    }
}

#[test]
fn connecting_note_clears_the_goblin_at_standard_terminal_height() {
    let chrome = waiting_chrome();
    let terminal = draw(120, 40, Rect::new(0, 0, 120, 40), &chrome);
    let note_y = (1..40)
        .find(|&y| row(&terminal, y).contains("Connecting to"))
        .expect("connecting note");
    let goblin_top = (2..note_y)
        .find(|&y| row(&terminal, y).chars().take(50).any(|ch| ch != ' '))
        .expect("goblin mark");
    assert!(
        note_y >= goblin_top + 17,
        "the note at row {note_y} must clear the sixteen-row goblin at row {goblin_top}"
    );
}

#[test]
fn status_segment_names_the_running_stage_and_the_retry_countdown() {
    let ws = Workspace::scripted();
    let mut chrome = waiting_chrome();
    chrome.prefs.status_left.clear();
    chrome.prefs.status_right.clear();
    let mut terminal = Terminal::new(TestBackend::new(120, 1)).expect("test terminal");
    let draw_status = |terminal: &mut Terminal<TestBackend>, chrome: &Chrome| {
        terminal
            .draw(|frame| {
                status::render_status_line(frame, Rect::new(0, 0, 120, 1), &ws, chrome);
            })
            .expect("draw status");
        row(terminal, 0)
    };

    let line = draw_status(&mut terminal, &chrome);
    assert!(
        line.starts_with(" ◐ connecting · daemon health · 9.8 s"),
        "status should name the running stage: {line}"
    );

    chrome.connection.stages = None;
    chrome.connection.retry_at = Some(chrome.connection.now + Duration::from_secs(3));
    let line = draw_status(&mut terminal, &chrome);
    assert!(
        line.starts_with(" Daemon unreachable · retry in 3 s"),
        "status should show the retry deadline: {line}"
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
