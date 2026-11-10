//! Focus memory for a split pane whose terminal has not resolved yet: a
//! reserved pane has no terminal at all, and an open that failed leaves its
//! terminal unresolved. Neither holds focus once it can no longer resolve.

use std::time::Duration;

use serde_json::json;

use super::sync_live_chrome;
use crate::app::live_loop::jobs::{JobKey, JobOutcome, JobResult, JobTag, LoopJobs};
use crate::app::live_loop::jobs_apply::apply_job_outcome;
use crate::app::Workspace;
use crate::daemon::{DaemonError, LiveDaemon};
use crate::ui::Chrome;

fn leaf(pane: &str) -> serde_json::Value {
    json!({"kind": "pane", "pane_id": pane})
}

/// One tab splitting `pane-a` (bound to `terminal-a`) beside `pane-b`, whose
/// terminal is `b_terminal`; `None` is a reserved pane with no terminal yet.
fn split_tab(b_terminal: Option<&str>) -> Workspace<LiveDaemon> {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    apply_split(&mut workspace, b_terminal, 0);
    workspace.select_project("project-1");
    workspace
}

/// Applies the split snapshot at `seq`, with pane-b naming `b_terminal`.
fn apply_split(workspace: &mut Workspace<LiveDaemon>, b_terminal: Option<&str>, seq: u64) {
    workspace.apply_workspace_snapshot(
        serde_json::from_value(json!({
            "workspace": {
                "id": "workspace-1", "machine_id": "machine-1", "ref": 1,
                "name": "test", "focused_project_id": "project-1",
                "focused_tab_id": "tab-1", "default_project_id": "project-1",
                "created_at": "", "updated_at": ""
            },
            "tabs": [{
                "id": "tab-1", "workspace_id": "workspace-1", "ref": 1,
                "title": null, "project_id": "project-1", "worktree_id": null,
                "position": 0, "focused_pane_id": "pane-b",
                "layout": {
                    "kind": "split", "axis": "horizontal", "ratio": 0.5,
                    "children": [leaf("pane-a"), leaf("pane-b")]
                },
                "created_at": "", "updated_at": ""
            }],
            "panes": [
                {
                    "id": "pane-a", "tab_id": "tab-1", "ref": 1,
                    "terminal_id": "terminal-a", "owns_terminal": true,
                    "label": null, "created_at": "", "updated_at": ""
                },
                {
                    "id": "pane-b", "tab_id": "tab-1", "ref": 2,
                    "terminal_id": b_terminal, "owns_terminal": true,
                    "label": null, "created_at": "", "updated_at": ""
                }
            ],
            "snapshot": {"daemon_epoch": "epoch-1", "seq": seq}
        }))
        .expect("workspace snapshot"),
    );
}

fn settle(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome, result: JobResult) {
    let mut jobs = LoopJobs::new(workspace);
    let generation = workspace.daemon().generation();
    let terminal = match &result {
        JobResult::Opened { terminal_id, .. } => terminal_id.clone(),
        _ => unreachable!("only opens settle here"),
    };
    apply_job_outcome(
        workspace,
        chrome,
        &mut jobs,
        JobOutcome {
            tag: JobTag {
                id: 1,
                generation,
                key: JobKey::Terminal(terminal),
            },
            result,
        },
    );
}

/// Binds `pane-a`, so the tab has a resolved slot to fall back to.
fn bind_pane_a(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let row = serde_json::from_value(json!({
        "terminal_id": "terminal-a", "backend": "native", "state": "live"
    }))
    .expect("terminal row");
    settle(
        workspace,
        chrome,
        JobResult::Opened {
            terminal_id: "terminal-a".into(),
            elapsed: Duration::ZERO,
            result: Ok(row),
        },
    );
}

#[tokio::test]
async fn a_focused_reserved_pane_with_no_terminal_yet_keeps_focus() {
    let mut workspace = split_tab(None);
    let mut chrome = Chrome::dark();
    bind_pane_a(&mut workspace, &mut chrome);
    sync_live_chrome(&mut workspace, &mut chrome);
    let reserved = chrome.viewer.panes.intern("pane-b");
    chrome.viewer.focus.insert("tab-1".into(), reserved);

    sync_live_chrome(&mut workspace, &mut chrome);

    assert_eq!(
        chrome.viewer.focus.get("tab-1"),
        Some(&reserved),
        "a reserved pane binds its terminal later; its focus is not stale"
    );
}

#[tokio::test]
async fn a_model_backed_pane_keeps_focus_after_leaving_the_unresolved_set() {
    let mut workspace = split_tab(Some("terminal-b"));
    let mut chrome = Chrome::dark();
    bind_pane_a(&mut workspace, &mut chrome);
    sync_live_chrome(&mut workspace, &mut chrome);
    let pending = chrome.viewer.panes.intern("pane-b");
    chrome.viewer.unresolved.clear();
    let tab = chrome
        .tabs()
        .tabs
        .iter()
        .find(|tab| tab.layout.pane_ids().contains(&pending))
        .expect("the split tab holds the pending pane");

    assert!(
        super::awaiting_pane(&workspace, &chrome.viewer, tab, pending),
        "the model still holds this pane and its open has not failed; resolving the terminal must not make its focus stale"
    );
}

#[tokio::test]
async fn a_focused_split_whose_terminal_open_failed_gives_up_focus() {
    let mut workspace = split_tab(Some("terminal-b"));
    let mut chrome = Chrome::dark();
    bind_pane_a(&mut workspace, &mut chrome);
    sync_live_chrome(&mut workspace, &mut chrome);
    let pending = chrome.viewer.panes.intern("pane-b");
    chrome.viewer.focus.insert("tab-1".into(), pending);
    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        chrome.viewer.focus.get("tab-1"),
        Some(&pending),
        "while the open is in flight the pending split keeps focus"
    );

    settle(
        &mut workspace,
        &mut chrome,
        JobResult::Opened {
            terminal_id: "terminal-b".into(),
            elapsed: Duration::ZERO,
            result: Err(DaemonError::NotFound),
        },
    );
    sync_live_chrome(&mut workspace, &mut chrome);

    let survivor = chrome.viewer.panes.intern("pane-a");
    assert_eq!(
        chrome.viewer.focus.get("tab-1"),
        Some(&survivor),
        "a failed open never resolves, so focus falls back to the bound pane"
    );
}

#[tokio::test]
async fn a_failed_open_keeps_the_error_it_failed_with() {
    let mut workspace = split_tab(Some("terminal-b"));
    let mut chrome = Chrome::dark();
    bind_pane_a(&mut workspace, &mut chrome);
    let expected = match workspace.open_live_terminal("terminal-b").await {
        Err(error) => error.to_string(),
        Ok(_) => panic!("an unconnected daemon cannot open a terminal"),
    };

    workspace.open_unresolved_terminals().await;

    assert_eq!(
        workspace.open_failure("terminal-b"),
        Some(expected.as_str()),
        "the sweep keeps the error text of the open it failed"
    );
}

#[tokio::test]
async fn a_failed_open_is_forgotten_once_its_pane_leaves_the_workspace() {
    let mut workspace = split_tab(Some("terminal-b"));
    let mut chrome = Chrome::dark();
    bind_pane_a(&mut workspace, &mut chrome);
    settle(
        &mut workspace,
        &mut chrome,
        JobResult::Opened {
            terminal_id: "terminal-b".into(),
            elapsed: Duration::ZERO,
            result: Err(DaemonError::NotFound),
        },
    );
    assert!(workspace.open_failed("terminal-b"));

    apply_split(&mut workspace, Some("terminal-c"), 1);
    sync_live_chrome(&mut workspace, &mut chrome);

    assert!(
        !workspace.open_failed("terminal-b"),
        "a failure whose terminal left the workspace is not kept"
    );
}
