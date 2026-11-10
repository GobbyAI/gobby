//! A pane a placement lands in keeps its focus while its terminal resolves.
//! Shares the apply-path fixture of the parent `jobs_apply_tests` module.

use super::*;
use crate::app::live_loop::{actions, control};

#[tokio::test]
async fn taking_control_after_the_focused_terminal_is_reaped_focuses_the_survivor() {
    const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let survivor = workspace.pane_for_terminal("existing").expect("survivor");
    let existing = snapshot.panes[0].id.clone();
    let new_pane = "mock-pane-terminated";
    snapshot.tabs[0].layout = crate::daemon::LayoutNode::Split {
        axis: crate::daemon::LayoutAxis::Horizontal,
        ratio: 0.5,
        children: Box::new([
            crate::daemon::LayoutNode::Pane { pane_id: existing },
            crate::daemon::LayoutNode::Pane {
                pane_id: new_pane.to_owned(),
            },
        ]),
    };
    let mut placed = snapshot.panes[0].clone();
    placed.id = new_pane.to_owned();
    placed.reference = 2;
    placed.terminal_id = Some(NEW.to_owned());
    placed.owns_terminal = true;
    snapshot.panes.push(placed);
    workspace.apply_workspace_snapshot(snapshot);
    mock.enqueue(
        "GET",
        &format!("/api/terminals/{NEW}"),
        200,
        json!({"terminal_id": NEW, "backend": "native", "state": "live"}),
    );
    workspace
        .open_live_terminal_unattached(NEW)
        .await
        .expect("the spawned terminal resolves");
    let terminated = workspace.pane_for_terminal(NEW).expect("the spawned pane");
    workspace.expect_replied_placement(NEW, new_pane);
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);
    workspace.focus = Some(terminated);
    assert_eq!(chrome.focused_pane(), Some(terminated));

    // The terminate relist reaps the client pane before the model drops its row.
    assert_eq!(workspace.drop_terminal_panes(NEW), vec![terminated]);
    assert!(workspace
        .workspace_model()
        .expect("workspace model")
        .pane(new_pane)
        .is_some());
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        chrome.focused_pane(),
        Some(survivor),
        "reaping the focused terminal focuses the survivor"
    );
    assert_eq!(workspace.focus, Some(survivor));
    workspace.pane_mut(survivor).control_request = None;
    workspace.pending_control = None;
    let jobs = LoopJobs::new(&workspace);
    actions::handle_live_action(
        &mut workspace,
        &mut chrome,
        jobs.outcomes(),
        crate::ui::Action::TakeControl,
    )
    .await
    .expect("the survivor's take-control chord is handled");
    let pending = workspace
        .pending_control
        .expect("the chord queues a survivor take-control request");
    assert_eq!(pending.pane_id, survivor);
    assert!(pending.takeover, "the chord requests explicit takeover");
    let (control_tx, _control_rx) = tokio::sync::mpsc::unbounded_channel();
    workspace.start_control_request(&control_tx);
    let request = workspace
        .pane(survivor)
        .control_request
        .expect("take control must target the survivor after termination");
    control::apply_control_outcome(
        &mut workspace,
        &mut chrome,
        &jobs,
        crate::app::live_control::ControlOutcome {
            pane_id: survivor,
            request,
            reply: Ok(json!({
                "granted": true,
                "host_input_granted": true,
                "lease_generation": 1,
            })),
        },
    );
    assert_eq!(chrome.focused_pane(), Some(survivor));
    assert_eq!(workspace.focus, Some(survivor));
    assert_eq!(
        workspace.pane(survivor).control,
        crate::app::ControlState::Held
    );
    let backend = ratatui::backend::TestBackend::new(120, 40);
    let mut terminal = ratatui::Terminal::new(backend).expect("test terminal");
    terminal
        .draw(|frame| {
            chrome.compute_view(&workspace, frame.area());
            crate::ui::render_workspace(frame, &workspace, &chrome);
        })
        .expect("draw the survivor");
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(
        screen.contains("Focused"),
        "the survivor renders its focused badge"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_delayed_placement_follows_its_pane_to_the_current_model_tab() {
    const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let original_tab = snapshot.tabs[0].id.clone();
    let existing = snapshot.panes[0].id.clone();
    let new_pane = "mock-pane-moved";
    let moved_tab = "mock-tab-moved";
    snapshot.tabs[0].layout = crate::daemon::LayoutNode::Split {
        axis: crate::daemon::LayoutAxis::Horizontal,
        ratio: 0.5,
        children: Box::new([
            crate::daemon::LayoutNode::Pane {
                pane_id: existing.clone(),
            },
            crate::daemon::LayoutNode::Pane {
                pane_id: new_pane.to_owned(),
            },
        ]),
    };
    let mut placed = snapshot.panes[0].clone();
    placed.id = new_pane.to_owned();
    placed.reference = 2;
    placed.terminal_id = Some(NEW.to_owned());
    placed.owns_terminal = true;
    placed.label = None;
    snapshot.panes.push(placed);
    workspace.apply_workspace_snapshot(snapshot.clone());
    workspace.expect_replied_placement(NEW, new_pane);

    // Another view moves the pane before this window projects the placement.
    // The placement's old tab remains, and the pane remains in the model.
    snapshot.tabs[0].layout = crate::daemon::LayoutNode::Pane {
        pane_id: existing.clone(),
    };
    snapshot.tabs[0].focused_pane_id = Some(existing);
    let mut destination = snapshot.tabs[0].clone();
    destination.id = moved_tab.to_owned();
    destination.layout = crate::daemon::LayoutNode::Pane {
        pane_id: new_pane.to_owned(),
    };
    destination.focused_pane_id = Some(new_pane.to_owned());
    snapshot.tabs.push(destination);
    snapshot.panes[1].tab_id = moved_tab.to_owned();
    workspace.apply_workspace_snapshot(snapshot);
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);

    let active = chrome.active_tab().expect("the placement has a tab");
    assert!(
        active.layout.pane_ids().contains(&chrome.tab_focus(active)),
        "a pending placement must focus a slot in its rendered layout"
    );
    assert_eq!(active.id, moved_tab, "focus follows the model's pane owner");
    assert_ne!(active.id, original_tab);

    mock.enqueue(
        "GET",
        &format!("/api/terminals/{NEW}"),
        200,
        json!({"terminal_id": NEW, "backend": "native", "state": "live"}),
    );
    workspace
        .open_live_terminal_unattached(NEW)
        .await
        .expect("the placed terminal resolves");
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);
    let pane = workspace
        .pane_for_terminal(NEW)
        .expect("the pane was created");
    let active = chrome.active_tab().expect("the resolved pane has a tab");
    let slot = active
        .slot_for(pane)
        .expect("the created pane is projected");
    assert!(active.layout.pane_ids().contains(&slot));
    assert_eq!(chrome.focused_pane(), Some(pane));
    mock.shutdown().await;
}

#[tokio::test]
async fn a_placement_missing_its_model_row_waits_then_follows_its_owner() {
    const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let original_tab = snapshot.tabs[0].id.clone();
    let new_pane = "mock-pane-not-yet-known";
    let moved_tab = "mock-tab-later-owner";
    workspace.apply_workspace_snapshot(snapshot.clone());
    workspace.hold_placed_panes(vec![(original_tab.clone(), new_pane.to_owned())]);

    crate::app::sync_live_chrome(&mut workspace, &mut chrome);

    assert!(
        workspace
            .placed_panes
            .contains(&(original_tab, new_pane.to_owned())),
        "a placement waits while its pane is absent from the model"
    );
    let mut destination = snapshot.tabs[0].clone();
    destination.id = moved_tab.to_owned();
    destination.layout = crate::daemon::LayoutNode::Pane {
        pane_id: new_pane.to_owned(),
    };
    destination.focused_pane_id = Some(new_pane.to_owned());
    snapshot.tabs.push(destination);
    let mut placed = snapshot.panes[0].clone();
    placed.id = new_pane.to_owned();
    placed.reference = 2;
    placed.tab_id = moved_tab.to_owned();
    placed.terminal_id = Some(NEW.to_owned());
    placed.owns_terminal = true;
    snapshot.panes.push(placed);
    workspace.apply_workspace_snapshot(snapshot);

    crate::app::sync_live_chrome(&mut workspace, &mut chrome);

    let active = chrome.active_tab().expect("the placement activates its owner");
    assert_eq!(active.id, moved_tab);
    assert!(active.layout.pane_ids().contains(&chrome.tab_focus(active)));
    assert!(workspace.placed_panes.is_empty());
    mock.shutdown().await;
}

#[tokio::test]
async fn a_placed_pane_keeps_focus_while_its_terminal_resolves() {
    const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    // The daemon split the shown tab: the new pane is in the model before its
    // terminal resolves into a pane.
    let existing = snapshot.panes[0].id.clone();
    snapshot.tabs[0].layout = crate::daemon::LayoutNode::Split {
        axis: crate::daemon::LayoutAxis::Horizontal,
        ratio: 0.5,
        children: Box::new([
            crate::daemon::LayoutNode::Pane { pane_id: existing },
            crate::daemon::LayoutNode::Pane {
                pane_id: "mock-pane-new".to_owned(),
            },
        ]),
    };
    let mut placed = snapshot.panes[0].clone();
    placed.id = "mock-pane-new".to_owned();
    placed.reference = 2;
    placed.terminal_id = Some(NEW.to_owned());
    placed.owns_terminal = true;
    placed.label = None;
    snapshot.panes.push(placed);
    workspace.apply_workspace_snapshot(snapshot);
    let shell = jobs::OwnedShell {
        terminal_id: NEW.to_owned(),
        pane_id: "mock-pane-new".to_owned(),
        draft: None,
        snapshot: None,
        tab_order_warning: None,
        roster: None,
    };
    chain_apply::apply_shell(&mut workspace, &mut chrome, &mut jobs, true, Ok(shell));
    mock.enqueue(
        "GET",
        &format!("/api/terminals/{NEW}"),
        200,
        json!({"terminal_id": NEW, "backend": "native", "state": "live"}),
    );
    workspace
        .open_live_terminal_unattached(NEW)
        .await
        .expect("the placed terminal resolves");
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        chrome.focused_pane(),
        workspace.pane_for_terminal(NEW),
        "the placed pane takes focus once it resolves"
    );
    mock.shutdown().await;
}
