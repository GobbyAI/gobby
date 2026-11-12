//! Project operations must leave direct terminal input runnable while the daemon waits.

use super::*;
use gobby_client::app::sync_live_chrome;
use gobby_client::app::{ContextMenuKind, ContextMenuState, MenuAction, MenuItem};
use gobby_client::ui::dialogs::{CloseScope, CloseTarget, Dialog};
use gobby_client::ui::keymap::{Keymap, DEFAULT_PREFIX};
use gobby_client::ui::Mode;

#[derive(Clone, Copy, Debug)]
enum HeldOperation {
    Remove,
    RemoveFailure,
    Attach,
    Locate,
    Create,
}

#[tokio::test]
async fn held_worktree_creation_keeps_direct_input_live() {
    let delivered = assert_project_operation_live(HeldOperation::Create).await;
    assert!(
        delivered.is_ok(),
        "creation preserves direct input: {delivered:?}"
    );
}

#[tokio::test]
async fn held_worktree_deletion_keeps_direct_input_live() {
    let delivered = assert_project_operation_live(HeldOperation::Remove).await;
    assert!(
        delivered.is_ok(),
        "deletion preserves direct input: {delivered:?}"
    );
}

#[tokio::test]
async fn held_project_attachment_keeps_direct_input_live() {
    let delivered = assert_project_operation_live(HeldOperation::Attach).await;
    assert!(
        delivered.is_ok(),
        "attachment preserves direct input: {delivered:?}"
    );
}

#[tokio::test]
async fn held_terminal_discovery_keeps_direct_input_live() {
    let delivered = assert_project_operation_live(HeldOperation::Locate).await;
    assert!(
        delivered.is_ok(),
        "discovery preserves direct input: {delivered:?}"
    );
}

#[tokio::test]
async fn removal_failure_after_dismissal_is_visible_and_repeats_are_coalesced() {
    let delivered = assert_project_operation_live(HeldOperation::RemoveFailure).await;
    assert!(
        delivered.is_ok(),
        "refused deletion preserves direct input: {delivered:?}"
    );
}

async fn assert_project_operation_live(
    operation: HeldOperation,
) -> Result<(), tokio::time::error::Elapsed> {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, json!([
        {"id":"project-1", "name":"fixture", "checkout":{"machine_id":"m-local", "root_path":"/repo"}}
    ]));
        mock.enqueue(
            "GET",
            "/api/source-control/worktrees?",
            200,
            json!({"worktrees":[{
                "id":"wt-unused", "project_id":"project-1", "branch_name":"fixture",
                "worktree_path":"/fixture-worktree", "status":"active", "workspace_role":"client"
            }]}),
        );
        mock.enqueue(
            "GET",
            "/api/source-control/status?",
            200,
            json!({"current_branch":"0.5.0", "repo_path":"/repo", "worktree_count":1}),
        );
    }
    workspace.fetch_sidebar_rows().await.expect("project rows");
    assert!(
        workspace.sidebar().projects.iter().any(|project| project
            .worktrees
            .iter()
            .any(|tree| tree.worktree_id == "wt-unused")),
        "fixture worktree is visible"
    );
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("attach workspace");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    assert!(chrome.focus_pane(
        workspace
            .pane_for_terminal("terminal-b")
            .expect("direct pane")
    ));
    let held = match operation {
        HeldOperation::Create => {
            chrome.dialog = Some(Dialog::NewWorktree {
                project_id: "project-1".into(),
                branch: "new-fixture".into(),
                base: String::new(),
                cursor: 11,
                base_focused: false,
                error: None,
            });
            chrome.mode = Mode::ProjectDialog;
            mock.enqueue_held(
                "POST",
                "/api/source-control/worktrees",
                409,
                json!({"detail":"fixture creation refused"}),
            )
        }
        HeldOperation::Remove | HeldOperation::RemoveFailure => {
            chrome.dialog = Some(Dialog::RemoveWorktree {
                worktree_id: "wt-unused".into(),
                branch: "fixture".into(),
                path: "/fixture-worktree".into(),
                tabs: 0,
                panes: 0,
                error: None,
            });
            chrome.mode = Mode::ProjectDialog;
            mock.enqueue_held(
                "DELETE",
                "/api/source-control/worktrees/wt-unused",
                if matches!(operation, HeldOperation::RemoveFailure) {
                    409
                } else {
                    200
                },
                if matches!(operation, HeldOperation::RemoveFailure) {
                    json!({"detail":"fixture deletion refused"})
                } else {
                    json!({"ok":true})
                },
            )
        }
        HeldOperation::Locate => {
            chrome.menu = Some(ContextMenuState {
                kind: ContextMenuKind::Global,
                anchor: (2, 2),
                selected: 0,
                item_rects: Vec::new(),
                parent: None,
                items: vec![MenuItem {
                    label: "Focus remote terminal".into(),
                    action: MenuAction::FocusAgent(format!(
                        "{}remote-terminal",
                        gobby_client::ui::sidebar::TERMINAL_ROW
                    )),
                    enabled: true,
                }],
            });
            chrome.mode = Mode::ContextMenu;
            mock.hold_ws("workspace_op", |request| request["op"] == "workspace.list")
        }
        HeldOperation::Attach => {
            chrome.menu = Some(ContextMenuState {
                kind: ContextMenuKind::Global,
                anchor: (2, 2),
                selected: 0,
                item_rects: Vec::new(),
                parent: None,
                items: vec![MenuItem {
                    label: "Focus next project".into(),
                    action: MenuAction::FocusProject("project-2".into()),
                    enabled: true,
                }],
            });
            chrome.mode = Mode::ContextMenu;
            mock.hold_ws("workspace_attach", |request| {
                request["project_id"] == "project-2"
            })
        }
    };
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.draws_continue().await;
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        timeout(
            DRAIN_DEADLINE,
            mock.wait_for_request(|request| match operation {
                HeldOperation::Create => {
                    request.method == "POST" && request.target == "/api/source-control/worktrees"
                }
                HeldOperation::Remove | HeldOperation::RemoveFailure => request.method == "DELETE",
                HeldOperation::Locate => {
                    request.method == "WS"
                        && request.body.as_ref().is_some_and(|body| {
                            body["type"] == "workspace_op" && body["op"] == "workspace.list"
                        })
                }
                HeldOperation::Attach => {
                    request.method == "WS"
                        && request.body.as_ref().is_some_and(|body| {
                            body["type"] == "workspace_attach" && body["project_id"] == "project-2"
                        })
                }
            }),
        )
        .await
        .expect("operation request is issued");
        if matches!(operation, HeldOperation::RemoveFailure) {
            probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        }
        // A pending deletion dialog may be dismissed while its result is outstanding.
        probe.key(KeyCode::Esc, KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('q'), KeyModifiers::NONE).await;
        let delivered = timeout(KEY_BOUND, host.wait_for_input(b"q")).await;
        held.notify_one();
        if matches!(operation, HeldOperation::RemoveFailure) {
            wait_until("dismissed removal error is shown", || {
                probe
                    .frames
                    .lock()
                    .expect("frames")
                    .iter()
                    .any(|frame| frame.contains("Cannot remove worktree wt-unused"))
            })
            .await;
            assert_eq!(
                mock.requests()
                    .iter()
                    .filter(|request| request.method == "DELETE")
                    .count(),
                1,
                "pending removal is coalesced"
            );
        }
        drop(input_tx);
        delivered
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, delivered) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("loop exits cleanly");
    mock.shutdown().await;
    delivered
}

#[tokio::test]
async fn held_project_kill_keeps_direct_input_live() {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.seed_other_workspace("project-2", &[(&["terminal-b"], "terminal-b")]);
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, json!([
        {"id":"project-1", "name":"first", "checkout":{"machine_id":"m-local", "root_path":"/first"}},
        {"id":"project-2", "name":"second", "checkout":{"machine_id":"m-local", "root_path":"/second"}}
    ]));
    }
    workspace.fetch_sidebar_rows().await.expect("project rows");
    assert_eq!(
        workspace.sidebar().projects.len(),
        2,
        "both fixture projects are visible"
    );
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("attach workspace");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::from_toml(
        "[bindings]\nnext_project = \"prefix+f12\"\n",
        DEFAULT_PREFIX,
    )
    .expect("project shortcut");
    sync_live_chrome(&mut workspace, &mut chrome);
    chrome.dialog = Some(Dialog::ConfirmClose {
        target: CloseTarget::Project("project-1".into()),
        title: "Fixture project".into(),
        scope: CloseScope::Panes(2),
    });
    chrome.mode = Mode::ConfirmClose;
    let held = mock.hold_ws("terminal_kill", |_| true);
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        probe.draws_continue().await;
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        let delivered = timeout(KEY_BOUND, async {
            probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            probe.key(KeyCode::F(12), KeyModifiers::NONE).await;
            wait_until("surviving direct pane receives focus", || {
                websocket_requests(&mock, "terminal_focus")
                    .iter()
                    .any(|request| {
                        request["terminal_id"] == "terminal-b" && request["focused"] == true
                    })
            })
            .await;
            probe.key(KeyCode::Char('q'), KeyModifiers::NONE).await;
            host.wait_for_input(b"q").await;
        })
        .await;
        held.notify_one();
        delivered.expect("project kill must not hold a direct key for 1500ms");
        drop(input_tx);
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, ()) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("loop exits cleanly");
    mock.shutdown().await;
}
