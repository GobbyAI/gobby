//! A placed shell takes its tab's focus once its terminal resolves. The
//! placement lands before the roster lists the terminal, so until the tab shows
//! the shell's slot, focus stays on the pane it had.

use super::sync_live_chrome;
use crate::app::live_loop::mock_daemon;
use crate::app::Workspace;
use crate::daemon::{Daemon, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::ui::Chrome;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::timeout;

const EXISTING: &str = "existing";
const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const WAIT: Duration = Duration::from_secs(2);

fn roster(ids: &[&str], seq: u64) -> Value {
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
        .collect();
    json!({
        "items": items,
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": seq},
    })
}

#[tokio::test]
async fn a_placed_shell_takes_focus_once_its_terminal_resolves() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&[EXISTING], EXISTING)]);
    mock.enqueue("GET", "/api/terminals?", 200, roster(&[EXISTING], 1));
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial model and roster");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    let existing = mock.pane_for_terminal(EXISTING).expect("existing pane");
    let existing_slot = chrome
        .viewer
        .panes
        .slot(&existing)
        .expect("slot for the existing pane");

    // `place_live_terminal` expects the placement before it sends the split.
    // The new shell is pending until the roster lists its terminal.
    workspace.expect_placement(NEW);
    workspace.pending_spawns.insert(NEW.to_string());
    workspace
        .daemon()
        .workspace_op(WorkspaceOp::PaneSplit {
            pane: existing,
            axis: LayoutAxis::Horizontal,
            terminal_id: Some(NEW.to_string()),
            cwd: None,
            rows: None,
            cols: None,
            node: None,
        })
        .await
        .expect("split the existing pane");
    timeout(WAIT, async {
        loop {
            workspace
                .drain_live_events()
                .await
                .expect("apply live events");
            if workspace
                .workspace_model()
                .is_some_and(|model| model.pane_ref_for_terminal(NEW).is_some())
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("split reaches the model");

    let new_pane = mock
        .pane_for_terminal(NEW)
        .expect("mock pane for the new shell");
    sync_live_chrome(&mut workspace, &mut chrome);
    let tab = chrome.active_tab().expect("daemon tab");
    let new_slot = chrome
        .viewer
        .panes
        .slot(&new_pane)
        .expect("slot for the placed pane");
    assert_eq!(
        chrome.viewer.focus_of(tab),
        existing_slot,
        "focus stays on the existing pane while the placed shell's terminal is pending"
    );

    mock.enqueue("GET", "/api/terminals?", 200, roster(&[EXISTING, NEW], 2));
    workspace
        .fetch_roster()
        .await
        .expect("roster lists the new shell");
    sync_live_chrome(&mut workspace, &mut chrome);
    let tab = chrome.active_tab().expect("daemon tab");
    assert!(
        tab.slots.contains_key(&new_slot),
        "the placed shell's pane resolves"
    );
    assert_eq!(
        chrome.viewer.focus_of(tab),
        new_slot,
        "focus moves to the placed shell once its slot resolves"
    );
    mock.shutdown().await;
}
