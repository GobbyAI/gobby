//! Placed-agent-launch 2.1: gclient reconciles daemon-placed terminals in every order.
//!
//! The daemon binds a placed agent's terminal into a pane before exec, but the
//! lifecycle and workspace streams are separate, so `created` can arrive on either
//! side of the binding event. Each test drives the live path end to end:
//! `drain_live_events`, the workspace model, `sync_live_chrome`, then the Chrome.

mod mock_daemon;

use gobby_client::app::{focus_agent, sync_live_chrome};
use gobby_client::daemon::{Daemon, DaemonError, LiveDaemon, WorkspaceOp};
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::timeout;

const AGENT: &str = "agent-1";
const EPOCH: &str = "placed-epoch";

struct Session {
    mock: MockDaemon,
    workspace: Workspace<LiveDaemon>,
    chrome: Chrome,
    _home: tempfile::TempDir,
}

fn roster(ids: &[&str]) -> Value {
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
        .collect();
    json!({
        "items": items,
        "next_cursor": null,
        "snapshot": {"daemon_epoch": EPOCH, "seq": 1},
    })
}

fn agent_row() -> Value {
    json!({"terminal_id": AGENT, "backend": "native", "state": "live"})
}

/// A client subscribed to `project-1`, whose workspace holds `tabs`.
async fn session(tabs: &[(&[&str], &str)], live: &[&str]) -> Session {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", tabs);
    for _ in 0..6 {
        mock.enqueue("GET", "/api/terminals?", 200, roster(live));
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect isolated daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("isolated gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("load seeded workspace");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    Session {
        mock,
        workspace,
        chrome,
        _home: home,
    }
}

impl Session {
    /// The daemon places the agent: a `tab.created` that binds its terminal.
    async fn bind_agent_tab(&mut self) -> String {
        let workspace_id = self
            .workspace
            .workspace_model()
            .expect("attached workspace")
            .workspace
            .id
            .clone();
        let reply = self
            .workspace
            .daemon()
            .workspace_op(WorkspaceOp::TabCreate {
                workspace: workspace_id,
                project_id: "project-1".to_string(),
                worktree_id: None,
                title: Some("agent".to_string()),
                terminal_id: Some(AGENT.to_string()),
                cwd: None,
                node: None,
            })
            .await
            .expect("daemon binds the agent terminal");
        let tab_id = reply
            .result
            .get("tab")
            .and_then(|tab| tab.get("id"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| self.mock.tab_for_terminal(AGENT))
            .expect("bound tab id");
        self.drain_until(|workspace| {
            workspace
                .workspace_model()
                .is_some_and(|model| model.pane_ref_for_terminal(AGENT).is_some())
        })
        .await;
        tab_id
    }

    /// The lifecycle stream reports the agent terminal created.
    async fn created(&mut self, seq: u64) {
        let (_, mut observed) = self.workspace.daemon().subscribe();
        self.mock
            .send_event_and_wait(json!({
                "type": "terminal_event",
                "event": "created",
                "terminal_id": AGENT,
                "backend": "native",
                "daemon_epoch": EPOCH,
                "seq": seq,
                "timestamp": "2026-01-01T00:00:00Z",
            }))
            .await;
        timeout(Duration::from_secs(1), observed.recv())
            .await
            .expect("created delivery")
            .expect("created event");
        self.drain_until(|workspace| workspace.roster_terminal_ids().contains(&AGENT.to_string()))
            .await;
    }

    async fn drain_until(&mut self, done: impl Fn(&Workspace<LiveDaemon>) -> bool) {
        timeout(Duration::from_secs(1), async {
            loop {
                self.workspace
                    .drain_live_events()
                    .await
                    .expect("apply live events");
                if done(&self.workspace) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("live event applied");
    }

    fn sync(&mut self) {
        sync_live_chrome(&mut self.workspace, &mut self.chrome);
    }

    /// Every Chrome slot showing the agent's terminal, as (tab id, is local).
    fn agent_surfaces(&self) -> Vec<(String, bool)> {
        let Some(agent) = self.workspace.pane_for_terminal(AGENT) else {
            return Vec::new();
        };
        self.chrome
            .tabs()
            .tabs
            .iter()
            .flat_map(|tab| {
                tab.slots
                    .values()
                    .filter(move |pane| **pane == agent)
                    .map(move |_| (tab.id.clone(), tab.is_local()))
            })
            .collect()
    }

    fn local_tabs(&self) -> usize {
        self.chrome
            .tabs()
            .tabs
            .iter()
            .filter(|tab| tab.is_local())
            .count()
    }
}

#[tokio::test]
async fn bind_then_created_opens_once_in_pane() {
    let mut s = session(&[(&["existing"], "existing")], &["existing"]).await;
    s.mock
        .enqueue("GET", &format!("/api/terminals/{AGENT}"), 200, agent_row());
    let active = s.chrome.active_tab().map(|tab| tab.id.clone());
    assert!(active.is_some(), "a prior active tab");

    let tab_id = s.bind_agent_tab().await;
    s.sync();
    let panes_after_bind = s.workspace.pane_count();
    s.created(2).await;
    s.sync();

    assert_eq!(
        s.workspace.pane_count(),
        panes_after_bind,
        "a created event for a bound terminal opens nothing new"
    );
    assert_eq!(s.workspace.pane_count(), 2, "existing plus the agent");
    assert_eq!(
        s.agent_surfaces(),
        vec![(tab_id, false)],
        "the agent shows once, in its bound daemon pane"
    );
    assert_eq!(s.local_tabs(), 0, "no local surface");
    assert_eq!(
        s.chrome.active_tab().map(|tab| tab.id.clone()),
        active,
        "an unrequested placement leaves the active tab"
    );
    s.mock.shutdown().await;
}

#[tokio::test]
async fn created_then_bind_moves_into_pane() {
    let mut s = session(&[(&["existing"], "existing")], &["existing"]).await;
    s.mock
        .enqueue("GET", &format!("/api/terminals/{AGENT}"), 200, agent_row());
    let active = s.chrome.active_tab().map(|tab| tab.id.clone());
    assert!(active.is_some(), "a prior active tab");

    s.created(2).await;
    s.sync();
    let unplaced = s
        .workspace
        .pane_for_terminal(AGENT)
        .expect("created opens an unplaced pane");
    let tab_id = s.bind_agent_tab().await;
    s.sync();

    assert_eq!(s.workspace.pane_count(), 2, "existing plus the agent");
    assert_eq!(
        s.workspace.pane_for_terminal(AGENT),
        Some(unplaced),
        "the open terminal pane is reused, not reopened"
    );
    assert_eq!(
        s.agent_surfaces(),
        vec![(tab_id, false)],
        "the agent moved into its bound daemon pane"
    );
    assert_eq!(s.local_tabs(), 0, "no local tab");
    assert_eq!(
        s.chrome.active_tab().map(|tab| tab.id.clone()),
        active,
        "an unrequested placement leaves the active tab"
    );
    s.mock.shutdown().await;
}

#[tokio::test]
async fn reconnect_projects_bound_terminal_once() {
    let mut s = session(
        &[(&["existing"], "existing"), (&[AGENT], AGENT)],
        &["existing", AGENT],
    )
    .await;
    let agent_tab = s.mock.tab_for_terminal(AGENT).expect("seeded agent tab");

    assert_eq!(s.workspace.pane_count(), 2, "one pane per live terminal");
    assert_eq!(
        s.agent_surfaces(),
        vec![(agent_tab.clone(), false)],
        "the snapshot's bound terminal projects into its saved pane"
    );
    assert_eq!(s.local_tabs(), 0, "no local surface");
    let active = s.chrome.active_tab().map(|tab| tab.id.clone());
    assert!(active.is_some(), "a prior active tab");
    let focus = s.chrome.focused_pane();

    s.workspace
        .reconcile_subscribe_first()
        .await
        .expect("reconnect reconciliation");
    s.sync();
    s.created(2).await;
    s.sync();

    assert_eq!(s.workspace.pane_count(), 2, "reconnect opens nothing new");
    assert_eq!(s.agent_surfaces(), vec![(agent_tab, false)]);
    assert_eq!(s.local_tabs(), 0, "no local surface after reconnect");
    assert_eq!(
        s.chrome.active_tab().map(|tab| tab.id.clone()),
        active,
        "the active tab is stable"
    );
    assert_eq!(s.chrome.focused_pane(), focus, "focus is stable");
    s.mock.shutdown().await;
}

/// A placement still waiting on its op when the connection drops never moves
/// the view later: the reply comes on the lost connection, so the loop drops
/// it, and a pane event for that terminal is somebody else's placement.
#[tokio::test]
async fn a_placement_lost_with_the_connection_never_moves_the_view() {
    let mut s = session(&[(&["existing"], "existing")], &["existing", AGENT]).await;
    s.mock
        .enqueue("GET", &format!("/api/terminals/{AGENT}"), 200, agent_row());
    let _held = s.mock.hold_ws("workspace_op", |request| {
        request.get("op") == Some(&json!("tab.create"))
    });
    let (outcomes, _unapplied) = mpsc::unbounded_channel();
    focus_agent(
        &mut s.workspace,
        &mut s.chrome,
        &outcomes,
        &format!("terminal:{AGENT}"),
    )
    .await
    .expect("focus the unplaced agent");
    let placing = |mock: &MockDaemon| {
        mock.workspace_requests().iter().any(|request| {
            request.get("op") == Some(&json!("tab.create"))
                && request["terminal_id"] == json!(AGENT)
        })
    };
    timeout(Duration::from_secs(1), async {
        while !placing(&s.mock) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("the agent was sent to a new tab");

    let generation = s.workspace.daemon().generation();
    s.workspace
        .observe_daemon_disconnect(generation, DaemonError::Unavailable { retry_after: None });
    s.workspace
        .reconcile_subscribe_first()
        .await
        .expect("reconnect reconciliation");
    s.sync();
    let active = s.chrome.active_tab().map(|tab| tab.id.clone());
    assert!(active.is_some(), "an active tab after reconnect");

    let tab_id = s.bind_agent_tab().await;
    s.drain_until(|workspace| {
        workspace
            .workspace_model()
            .is_some_and(|model| model.tab(&tab_id).is_some())
    })
    .await;
    s.sync();

    assert_eq!(
        s.chrome.active_tab().map(|tab| tab.id.clone()),
        active,
        "the lost placement leaves the active tab"
    );
    s.mock.shutdown().await;
}
