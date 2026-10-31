//! A4b outcome-gate tests. The transport produces the real outcome; tests
//! change its generation only to control the ordering at the apply gate.

#[path = "../../../tests/mock_daemon/transport.rs"]
mod mock_daemon;

use crate as client;
#[path = "../../../tests/mock_daemon/draw.rs"]
mod draw;
#[path = "../../../tests/mock_daemon/host.rs"]
mod host;

use super::*;
use crate::app::live_loop::jobs;
use crate::app::live_loop::lifecycle::spawn_live_shell;
use crate::app::live_loop::mouse::Placement;
use crate::daemon::{Daemon, WorkspaceSnapshot};
use crossterm::event::{KeyCode, KeyModifiers};
use futures_util::poll;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::sync::atomic::Ordering;
use std::task::Poll;
use tokio::time::{sleep, timeout};

const CREATED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const WORKSPACE: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const WAIT: Duration = Duration::from_secs(2);

fn requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body["type"] == kind)
        .collect()
}

async fn fixture(mock: &MockDaemon) -> (Workspace<LiveDaemon>, Chrome, WorkspaceSnapshot) {
    mock.seed_workspace("project-1", &[(&["existing"], "existing")]);
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "existing", "backend": "native", "state": "live"}],
            "next_cursor": null, "snapshot": {"daemon_epoch": "epoch-1", "seq": 1},
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial model and roster");
    let snapshot = workspace
        .daemon()
        .workspace_snapshot(None, WORKSPACE)
        .await
        .expect("snapshot");
    let pane = workspace
        .pane_for_terminal("existing")
        .expect("existing pane");
    let mut chrome = Chrome::dark();
    chrome.focus_project("project-1");
    chrome.open_pane(pane, "local shell");
    (workspace, chrome, snapshot)
}

/// The existing action API must enqueue its work in its first poll. This
/// assertion reaches the real action, and fails before any unimplemented
/// result type or placeholder can manufacture RED evidence.
async fn issue_create(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome, jobs: &LoopJobs) {
    workspace.workspace_model = None;
    let mut action = Box::pin(spawn_live_shell(
        workspace,
        chrome,
        jobs.outcomes(),
        Placement::Tab,
        None,
        None,
    ));
    assert!(
        matches!(poll!(action.as_mut()), Poll::Ready(Ok(()))),
        "terminal_create must be enqueued; the lifecycle action must not await its reply"
    );
}

async fn received(jobs: &mut LoopJobs) -> JobOutcome {
    timeout(WAIT, jobs.rx.recv())
        .await
        .expect("job completes")
        .expect("outcome channel")
}

async fn wait_request(mock: &MockDaemon, kind: &str, count: usize) {
    timeout(WAIT, async {
        while requests(mock, kind).len() < count {
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("mock received request");
}

/// Drive the same queued-work and outcome gates as run_live_loop. Installing
/// a model is followed by another loop iteration; no disposal-specific API
/// or representation is assumed here.
async fn pump_until(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    done: impl Fn(&Workspace<LiveDaemon>, &Chrome) -> bool,
) {
    timeout(WAIT, async {
        loop {
            jobs.drain_pending_refetch(workspace, chrome);
            if done(workspace, chrome) {
                break;
            }
            tokio::select! {
                Some(outcome) = jobs.rx.recv() => {
                    // A request and its reply may both land before this
                    // select wakes. Preserve the awaited disposal outcome
                    // for the test that must inspect and apply it itself.
                    if done(workspace, chrome) {
                        jobs.outcomes().send(outcome).expect("outcome channel");
                        break;
                    }
                    apply_job_outcome(workspace, chrome, jobs, outcome);
                },
                _ = sleep(Duration::from_millis(5)) => {},
            }
        }
    })
    .await
    .expect("loop job pipeline advances");
}

fn stale(mut outcome: JobOutcome, workspace: &Workspace<LiveDaemon>) -> JobOutcome {
    outcome.tag.generation = Generation(workspace.daemon().generation().0.wrapping_sub(1));
    outcome
}

async fn wire_barrier(workspace: &Workspace<LiveDaemon>) {
    // Let already-issued jobs enqueue, then observe a response from the
    // same daemon writer before asserting that it received no disposal.
    tokio::task::yield_now().await;
    workspace
        .daemon()
        .workspace_snapshot(None, WORKSPACE)
        .await
        .expect("wire barrier");
}

async fn key(
    input: &tokio::sync::mpsc::Sender<RawInputEvent>,
    code: KeyCode,
    modifiers: KeyModifiers,
) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("key");
}

async fn observation_window() {
    sleep(Duration::from_millis(800)).await;
}

#[tokio::test]
async fn a_held_terminal_create_keeps_direct_loop_frames_input_and_ticks_live_then_places() {
    let mock = MockDaemon::start("local-token").await;
    let host = host::DirectHost::start("epoch-1").await;
    mock.seed_workspace("project-1", &[(&["existing"], "existing")]);
    let (mut workspace, _home) =
        host::live_workspace_on_direct_host_terminals(&mock, &host, &["existing"]).await;
    workspace.fetch_roster().await.expect("initial roster");
    let mut chrome = Chrome::dark();
    chrome.focus_project("project-1");
    let existing = workspace.pane_for_terminal("existing").expect("existing");
    chrome.open_pane(existing, "local shell");
    chrome.keymap = crate::ui::keymap::Keymap::from_toml(
        "[bindings]\nnew_terminal = \"prefix+i\"\n",
        crate::ui::keymap::HERDR_PREFIX,
    )
    .expect("test keymap");
    // Exercise the legacy terminal_create branch with a real running loop.
    workspace.workspace_model = None;
    let release = mock.hold_ws("terminal_create", |_| true);
    let (backend, draws, frames) = draw::DrawRecorder::new(96, 30);
    let mut terminal = ratatui::Terminal::new(backend).expect("terminal");
    let (input_tx, input_rx) = tokio::sync::mpsc::channel(16);
    // Biased input routes this action before startup can install a new model.
    key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
    key(&input_tx, KeyCode::Char('i'), KeyModifiers::NONE).await;
    let driver = async {
        wait_request(&mock, "terminal_create", 1).await;
        assert!(
            mock.workspace_requests().is_empty(),
            "no placement before create reply"
        );
        host.drain();
        let before = draws.load(Ordering::SeqCst);
        let tag = "held-create-direct-frame";
        use gobby_terminal::protocol::{
            CellData, ClientMessage, FrameData, PaneModes, ServerMessage,
        };
        host.to_client
            .send(ServerMessage::Frame(FrameData {
                cells: tag
                    .chars()
                    .map(|symbol| CellData {
                        symbol: symbol.to_string(),
                        fg: 0x10,
                        bg: 0,
                        modifier: 0,
                        skip: false,
                        hyperlink: None,
                    })
                    .collect(),
                width: tag.len() as u16,
                height: 1,
                cursor: None,
                hyperlinks: Vec::new(),
                graphics: Vec::new(),
                modes: PaneModes::default(),
            }))
            .expect("direct frame");
        key(&input_tx, KeyCode::Char('v'), KeyModifiers::NONE).await;
        observation_window().await;
        let rendered = frames
            .lock()
            .expect("frames")
            .iter()
            .any(|frame| frame.contains(tag));
        let typed = host
            .drain()
            .iter()
            .any(|message| matches!(message, ClientMessage::Input { data } if data == b"v"));
        let tick_draws = draws.load(Ordering::SeqCst) - before;
        release.notify_one();
        mock.enqueue("GET", "/api/terminals?", 200, json!({
            "items": [
                {"terminal_id": "existing", "backend": "native", "state": "live", "attach": host.roster_attach("existing")},
                {"terminal_id": CREATED, "backend": "native", "state": "live"},
            ],
            "next_cursor": null, "snapshot": {"daemon_epoch": "epoch-1", "seq": 2},
        }));
        assert!(
            rendered,
            "held terminal_create must keep direct frame rendering live"
        );
        assert!(typed, "held terminal_create must keep direct input live");
        assert!(
            tick_draws > 1,
            "held terminal_create must keep periodic rendering live"
        );
        wait_request(&mock, "terminal_set_viewport", 1).await;
        timeout(WAIT, async {
            while !requests(&mock, "terminal_set_viewport")
                .iter()
                .any(|op| op["terminal_id"] == CREATED)
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("created pane attaches after reply");
        drop(input_tx);
    };
    let mut switch = crate::teardown::TerminalGuard::recording().0;
    let (result, ()) = tokio::join!(
        crate::app::run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch,
        ),
        driver
    );
    result.expect("live loop");
    let pane = workspace.pane_for_terminal(CREATED).expect("created pane");
    assert!(chrome
        .tabs()
        .tabs
        .iter()
        .any(|tab| tab.slots.values().any(|id| *id == pane)));
    assert_eq!(requests(&mock, "terminal_create").len(), 1);
    mock.shutdown().await;
}

#[tokio::test]
async fn a_stale_created_terminal_already_placed_by_the_current_model_is_kept() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    let release = mock.hold_ws("terminal_create", |_| true);
    issue_create(&mut workspace, &mut chrome, &jobs).await;
    wait_request(&mock, "terminal_create", 1).await;
    snapshot.panes[0].terminal_id = Some(CREATED.to_owned());
    workspace.apply_workspace_snapshot(snapshot);
    release.notify_one();
    let outcome = stale(received(&mut jobs).await, &workspace);
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
    for _ in 0..3 {
        jobs.drain_pending_refetch(&mut workspace, &mut chrome);
    }
    wire_barrier(&workspace).await;
    assert!(
        requests(&mock, "terminal_kill").is_empty(),
        "authoritatively placed terminal survives"
    );
    assert!(
        mock.workspace_requests().is_empty(),
        "stale create adds no placement"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_lost_disposal_reply_reports_once_and_the_next_absent_model_clears_it() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    let release = mock.hold_ws("terminal_create", |_| true);
    issue_create(&mut workspace, &mut chrome, &jobs).await;
    wait_request(&mock, "terminal_create", 1).await;
    workspace.apply_workspace_snapshot(snapshot.clone());
    mock.suppress_ws("terminal_kill");
    release.notify_one();
    let outcome = stale(received(&mut jobs).await, &workspace);
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
    pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
        requests(&mock, "terminal_kill").len() == 1
    })
    .await;
    tokio::time::pause();
    tokio::time::advance(crate::daemon::REQUEST_DEADLINE + Duration::from_secs(1)).await;
    let lost = received(&mut jobs).await;
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, lost);
    tokio::time::resume();
    assert_eq!(
        chrome
            .alert_log
            .iter()
            .filter(|toast| toast.title.contains(CREATED))
            .count(),
        1,
        "lost disposal names its captured terminal exactly once"
    );
    for _ in 0..3 {
        jobs.drain_pending_refetch(&mut workspace, &mut chrome);
    }
    wire_barrier(&workspace).await;
    assert_eq!(
        requests(&mock, "terminal_kill").len(),
        1,
        "uncertain disposal never spins"
    );
    // The server committed the suppressed kill. The next authoritative
    // snapshot and roster contain no created terminal, so no retry is needed.
    snapshot.snapshot.seq += 1;
    workspace.apply_workspace_snapshot(snapshot);
    workspace.roster_ids.retain(|id| id != CREATED);
    jobs.drain_pending_refetch(&mut workspace, &mut chrome);
    wire_barrier(&workspace).await;
    assert_eq!(
        requests(&mock, "terminal_kill").len(),
        1,
        "absent terminal clears without another kill"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_held_terminal_create_keeps_the_window_interactive_and_places_after_create() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    let release = mock.hold_ws("terminal_create", |_| true);
    issue_create(&mut workspace, &mut chrome, &jobs).await;
    wait_request(&mock, "terminal_create", 1).await;
    assert!(
        jobs.rx.try_recv().is_err(),
        "held create has no outcome yet"
    );
    assert!(!workspace.pending_spawns.contains(CREATED));
    assert!(
        mock.workspace_requests().is_empty(),
        "placement cannot precede the create result"
    );
    workspace.apply_workspace_snapshot(snapshot);
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "existing", "backend": "native", "state": "live"},
                {"terminal_id": CREATED, "backend": "native", "state": "live"},
            ],
            "next_cursor": null, "snapshot": {"daemon_epoch": "epoch-1", "seq": 2},
        }),
    );
    release.notify_one();
    let outcome = received(&mut jobs).await;
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
    pump_until(&mut workspace, &mut chrome, &mut jobs, |ws, chrome| {
        ws.pane_for_terminal(CREATED).is_some_and(|pane| {
            chrome
                .tabs()
                .tabs
                .iter()
                .any(|tab| tab.slots.values().any(|id| *id == pane))
        })
    })
    .await;
    assert!(
        requests(&mock, "terminal_kill").is_empty(),
        "current create is placed"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_stale_committed_create_is_compensated_once_and_an_unanswered_create_is_not_replayed() {
    for answered in [true, false] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, snapshot) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        let release = mock.hold_ws("terminal_create", |_| true);
        issue_create(&mut workspace, &mut chrome, &jobs).await;
        wait_request(&mock, "terminal_create", 1).await;
        workspace.apply_workspace_snapshot(snapshot);
        let outcome = if answered {
            release.notify_one();
            stale(received(&mut jobs).await, &workspace)
        } else {
            mock.drop_websockets();
            received(&mut jobs).await
        };
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        if answered {
            pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
                requests(&mock, "terminal_kill").len() == 1
            })
            .await;
            let disposal = received(&mut jobs).await;
            assert_eq!(
                disposal.tag.generation,
                workspace.daemon().generation(),
                "disposal belongs to current generation"
            );
            apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, disposal);
        }
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        assert_eq!(
            requests(&mock, "terminal_create").len(),
            1,
            "create is never replayed"
        );
        assert_eq!(
            requests(&mock, "terminal_kill").len(),
            usize::from(answered)
        );
        assert!(
            mock.workspace_requests().is_empty(),
            "a stale or uncertain create places no pane"
        );
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn a_stale_committed_adoption_step_rolls_back_its_captured_tab() {
    for disposal in ["refused", "lost", "already_absent"] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, _) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        let release = mock.hold_ws("workspace_op", |request| request["op"] == "tab.create");
        let mut action = Box::pin(spawn_live_shell(
            &mut workspace,
            &mut chrome,
            jobs.outcomes(),
            Placement::SplitRight,
            None,
            None,
        ));
        assert!(
            matches!(poll!(action.as_mut()), Poll::Ready(Ok(()))),
            "adoption must enqueue its first step and yield"
        );
        drop(action);
        wait_request(&mock, "workspace_op", 1).await;
        release.notify_one();
        let outcome = received(&mut jobs).await;
        let snapshot = workspace
            .daemon()
            .workspace_snapshot(None, WORKSPACE)
            .await
            .expect("committed model");
        let adopted = snapshot
            .tabs
            .iter()
            .find(|tab| tab.id != mock.tab_for_terminal("existing").unwrap_or_default())
            .or_else(|| snapshot.tabs.last())
            .expect("captured adoption tab")
            .id
            .clone();
        // Remove the initiating project's local view before its success applies.
        chrome.project_tabs.sets.remove("project-1");
        chrome.focus_project("other-project");
        workspace.select_project("other-project");
        let mut snapshot = snapshot;
        if disposal == "already_absent" {
            snapshot.tabs.retain(|tab| tab.id != adopted);
        } else if disposal == "refused" {
            mock.refuse_workspace_op_at("tab.close", 1, "busy", "rollback refused");
        } else {
            // TabClose commits, but its acknowledgement is lost.
            mock.suppress_ws("workspace_op");
        }
        workspace.apply_workspace_snapshot(snapshot.clone());
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        if disposal == "already_absent" {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
            wire_barrier(&workspace).await;
            assert!(
                mock.workspace_requests()
                    .iter()
                    .all(|op| op["op"] != "tab.close"),
                "already-absent captured tab clears without compensation"
            );
            assert!(chrome
                .alert_log
                .iter()
                .all(|toast| !toast.title.contains(&adopted)));
            mock.shutdown().await;
            continue;
        }
        pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
            mock.workspace_requests()
                .iter()
                .any(|op| op["op"] == "tab.close")
        })
        .await;
        let closes: Vec<_> = mock
            .workspace_requests()
            .into_iter()
            .filter(|op| op["op"] == "tab.close")
            .collect();
        assert_eq!(closes.len(), 1);
        assert_eq!(
            closes[0]["tab"], adopted,
            "rollback uses captured identity after project removal"
        );
        if disposal == "lost" {
            tokio::time::pause();
            tokio::time::advance(crate::daemon::REQUEST_DEADLINE + Duration::from_secs(1)).await;
        }
        let failed = received(&mut jobs).await;
        assert_eq!(failed.tag.generation, workspace.daemon().generation());
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, failed);
        if disposal == "lost" {
            tokio::time::resume();
        }
        assert_eq!(
            chrome
                .alert_log
                .iter()
                .filter(|toast| toast.title.contains(&adopted))
                .count(),
            1,
            "failed rollback reports its retained captured tab exactly once"
        );
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        wire_barrier(&workspace).await;
        assert_eq!(
            mock.workspace_requests()
                .iter()
                .filter(|op| op["op"] == "tab.close")
                .count(),
            1,
            "failed rollback never retries before another model install"
        );
        if disposal == "lost" {
            snapshot = workspace
                .daemon()
                .workspace_snapshot(None, WORKSPACE)
                .await
                .expect("post-close model");
            assert!(snapshot.tabs.iter().all(|tab| tab.id != adopted));
        }
        workspace.apply_workspace_snapshot(snapshot.clone());
        if disposal == "refused" {
            pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
                mock.workspace_requests()
                    .iter()
                    .filter(|op| op["op"] == "tab.close")
                    .count()
                    == 2
            })
            .await;
            let retry = mock
                .workspace_requests()
                .into_iter()
                .rfind(|op| op["op"] == "tab.close")
                .expect("retry");
            assert_eq!(
                retry["tab"], adopted,
                "retry retains captured identity across installs"
            );
            let success = received(&mut jobs).await;
            apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, success);
        }
        // Reinstalling cannot resurrect a successfully settled/absent orphan.
        snapshot.snapshot.seq += 1;
        workspace.apply_workspace_snapshot(snapshot);
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        wire_barrier(&workspace).await;
        assert_eq!(
            mock.workspace_requests()
                .iter()
                .filter(|op| op["op"] == "tab.close")
                .count(),
            if disposal == "refused" { 2 } else { 1 },
            "settled/absent tab clears ownership without further rollback"
        );
        assert_eq!(
            chrome
                .alert_log
                .iter()
                .filter(|toast| toast.title.contains(&adopted))
                .count(),
            1
        );
        assert!(
            requests(&mock, "terminal_kill").is_empty(),
            "adopted terminal remains live"
        );
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn held_adoption_uses_its_local_tab_after_switch_or_close() {
    for closed in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, _) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        let old_tab = chrome.active_tab().unwrap().id.clone();
        let release = mock.hold_ws("workspace_op", |request| request["op"] == "tab.create");
        spawn_live_shell(
            &mut workspace,
            &mut chrome,
            jobs.outcomes(),
            Placement::SplitRight,
            None,
            None,
        )
        .await
        .expect("enqueue adoption");
        wait_request(&mock, "workspace_op", 1).await;
        let pane = workspace.pane_for_terminal("existing").unwrap();
        chrome.open_tab(pane, "unrelated");
        let unrelated = chrome.active_tab().unwrap().id.clone();
        if closed {
            chrome.tabs_mut().tabs.retain(|tab| tab.id != old_tab);
        }
        release.notify_one();
        let outcome = received(&mut jobs).await;
        let jobs::JobResult::Adopted { adopted, .. } = &outcome.result else {
            panic!("adoption outcome");
        };
        let adopted = adopted.new_tab.clone().expect("created tab");
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        assert!(
            chrome
                .tabs()
                .tabs
                .iter()
                .any(|tab| tab.id == unrelated && tab.title == "unrelated"),
            "completion preserves the switched-to tab's identity and title"
        );
        if closed {
            assert!(
                jobs.ledger
                    .committed_orphans
                    .iter()
                    .any(|entry| matches!(&entry.orphan, jobs::Orphan::Tab(id) if id == &adopted)),
                "closing the initiating local tab retains cleanup ownership"
            );
            pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
                mock.workspace_requests()
                    .iter()
                    .any(|op| op["op"] == "tab.close")
            })
            .await;
            let closes: Vec<_> = mock
                .workspace_requests()
                .into_iter()
                .filter(|op| op["op"] == "tab.close")
                .collect();
            assert_eq!(closes.len(), 1);
            assert_eq!(
                closes[0]["tab"], adopted,
                "cleanup closes the captured adopted tab"
            );
            assert!(
                requests(&mock, "terminal_kill").is_empty(),
                "cleanup never kills the adopted original terminal"
            );
        } else {
            assert!(
                chrome
                    .tabs()
                    .tabs
                    .iter()
                    .any(|tab| tab.id == adopted && tab.title == "local shell"),
                "the captured local tab receives the daemon identity"
            );
            assert_eq!(
                chrome.active_tab().unwrap().id,
                unrelated,
                "completion preserves newer focus"
            );
        }
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn cleanup_waits_for_a_snapshot_from_the_reconnected_generation() {
    for placed in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        issue_create(&mut workspace, &mut chrome, &jobs).await;
        let outcome = received(&mut jobs).await;
        workspace.apply_workspace_snapshot(snapshot.clone());
        let daemon = workspace.daemon().clone();
        let (before, mut events) = daemon.subscribe();
        mock.drop_websockets();
        timeout(WAIT, async {
            loop {
                if matches!(
                    events.recv().await.expect("daemon event"),
                    crate::daemon::DaemonEvent::Disconnected { .. }
                ) {
                    break;
                }
            }
        })
        .await
        .expect("disconnect observed");
        let after = daemon
            .reconnect(before.generation)
            .await
            .expect("reconnect");
        assert_ne!(
            before.generation, after,
            "the socket actually changed generation"
        );
        jobs.sync_generation(after);
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        wire_barrier(&workspace).await;
        assert!(
            requests(&mock, "terminal_kill").is_empty(),
            "retained pre-reconnect model cannot authorize cleanup"
        );
        if placed {
            snapshot.panes[0].terminal_id = Some(CREATED.to_owned());
        }
        workspace.apply_workspace_snapshot(snapshot);
        if placed {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
            assert!(
                jobs.ledger.committed_orphans.is_empty(),
                "current placement clears cleanup ownership"
            );
        } else {
            pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
                !requests(&mock, "terminal_kill").is_empty()
            })
            .await;
        }
        wire_barrier(&workspace).await;
        assert_eq!(requests(&mock, "terminal_kill").len(), usize::from(!placed));
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn failed_adoption_rollback_remains_owned_until_a_new_snapshot() {
    for lost in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, _) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        mock.refuse_workspace_op_at("pane.split", 1, "busy", "split refused");
        if !lost {
            mock.refuse_workspace_op_at("tab.close", 1, "busy", "rollback refused");
        }
        let release = mock.hold_ws("workspace_op", |request| request["op"] == "tab.close");
        spawn_live_shell(
            &mut workspace,
            &mut chrome,
            jobs.outcomes(),
            Placement::SplitRight,
            None,
            None,
        )
        .await
        .expect("enqueue adoption");
        wait_request(&mock, "workspace_op", 3).await;
        if lost {
            mock.suppress_ws("workspace_op");
        }
        release.notify_one();
        if lost {
            tokio::time::pause();
            tokio::time::advance(crate::daemon::REQUEST_DEADLINE + Duration::from_secs(1)).await;
        }
        let outcome = received(&mut jobs).await;
        if lost {
            tokio::time::resume();
        }
        let jobs::JobResult::Adopted { adopted, .. } = &outcome.result else {
            panic!("adoption outcome");
        };
        let adopted = adopted.new_tab.clone().expect("captured rollback tab");
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        assert!(
            jobs.ledger
                .committed_orphans
                .iter()
                .any(|entry| matches!(&entry.orphan, jobs::Orphan::Tab(id) if id == &adopted)),
            "a failed chain rollback keeps the created tab owned"
        );
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        wire_barrier(&workspace).await;
        assert_eq!(
            mock.workspace_requests()
                .iter()
                .filter(|op| op["op"] == "tab.close")
                .count(),
            1,
            "failure settles until a snapshot is installed"
        );
        assert_eq!(
            chrome
                .alert_log
                .iter()
                .filter(|toast| toast.title.contains(&adopted))
                .count(),
            1
        );
        mock.allow_ws("workspace_op");
        let snapshot = workspace
            .daemon()
            .workspace_snapshot(None, WORKSPACE)
            .await
            .expect("fresh snapshot");
        workspace.apply_workspace_snapshot(snapshot);
        if lost {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
            assert!(
                jobs.ledger.committed_orphans.is_empty(),
                "lost successful close clears ownership"
            );
        } else {
            pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
                mock.workspace_requests()
                    .iter()
                    .filter(|op| op["op"] == "tab.close")
                    .count()
                    == 2
            })
            .await;
        }
        assert!(
            requests(&mock, "terminal_kill").is_empty(),
            "adopted original terminal stays live"
        );
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn a_stale_create_is_disposed_once_in_either_order_and_a_failed_disposal_stays_owned() {
    for install_first in [true, false] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
        let mut jobs = LoopJobs::new(&workspace);
        let release = mock.hold_ws("terminal_create", |_| true);
        issue_create(&mut workspace, &mut chrome, &jobs).await;
        wait_request(&mock, "terminal_create", 1).await;
        if install_first {
            workspace.apply_workspace_snapshot(snapshot.clone());
        }
        release.notify_one();
        let outcome = stale(received(&mut jobs).await, &workspace);
        mock.enqueue_kill_refusal("disposal refused");
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
        if !install_first {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
            assert!(
                requests(&mock, "terminal_kill").is_empty(),
                "no disposal before authoritative model"
            );
            workspace.apply_workspace_snapshot(snapshot.clone());
        }
        pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
            requests(&mock, "terminal_kill").len() == 1
        })
        .await;
        let refusal = received(&mut jobs).await;
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, refusal);
        assert_eq!(
            chrome
                .alert_log
                .iter()
                .filter(|toast| toast.title.contains(CREATED))
                .count(),
            1,
            "failed disposal reports the captured identity once"
        );
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        assert_eq!(
            requests(&mock, "terminal_kill").len(),
            1,
            "no failure retry loop"
        );
        snapshot.snapshot.seq += 1;
        workspace.apply_workspace_snapshot(snapshot);
        pump_until(&mut workspace, &mut chrome, &mut jobs, |_, _| {
            requests(&mock, "terminal_kill").len() == 2
        })
        .await;
        let success = received(&mut jobs).await;
        apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, success);
        for _ in 0..3 {
            jobs.drain_pending_refetch(&mut workspace, &mut chrome);
        }
        assert_eq!(
            requests(&mock, "terminal_kill").len(),
            2,
            "success settles ownership"
        );
        assert!(mock.workspace_requests().is_empty(), "no stale placement");
        mock.shutdown().await;
    }
}
