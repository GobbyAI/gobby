//! A4b outcome-gate tests. The transport produces the real outcome; tests
//! change its generation only to control the ordering at the apply gate.

#[path = "../../../tests/mock_daemon/transport.rs"]
mod mock_daemon;

use super::*;
use crate::app::live_loop::lifecycle::spawn_live_shell;
use crate::app::live_loop::mouse::Placement;
use crate::daemon::WorkspaceSnapshot;
use futures_util::poll;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
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
    workspace.apply_workspace_snapshot(snapshot);
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
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
    assert!(
        requests(&mock, "terminal_kill").is_empty(),
        "adopted terminal remains live"
    );
    mock.shutdown().await;
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
