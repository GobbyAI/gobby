//! Sidebar states: a live agent that is not active and not needs-you is paused.

use std::collections::BTreeMap;

use gobby_client::app::sidebar_model::{agent_state, build, SidebarInputs};
use gobby_client::app::{Backend, Pane, PaneId};
use gobby_client::daemon::{
    ProjectRow, RosterEntry, RunRow, SidebarRows, TaskRef, TerminalRef, WorktreeRow,
};
use gobby_client::ui::chrome::{Chrome, RowState};
use gobby_client::ui::keybind_help::legend_groups;
use gobby_client::ui::status::state_dot;
use tokio::time::Instant;

const ROUTINE_TURN_END: &str = "waiting";

fn seat(status: &str) -> RosterEntry {
    RosterEntry {
        entry_id: "session:seat".into(),
        lifecycle_status: Some(status.into()),
        ..Default::default()
    }
}

#[test]
fn waiting_seat_is_paused_in_agents_projects_and_task_worktrees() {
    let mut entry = seat(ROUTINE_TURN_END);
    entry.run_id = Some("run-1".into());
    entry.terminal = Some(TerminalRef {
        terminal_id: "terminal-1".into(),
        backend: Backend::Native,
        state: None,
    });
    entry.task = Some(TaskRef {
        id: "task-1".into(),
        reference: Some("#25240".into()),
        ..Default::default()
    });
    let pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch");
    let rows = SidebarRows {
        projects: vec![ProjectRow {
            id: "project-1".into(),
            name: "gobby".into(),
            ..Default::default()
        }],
        worktrees: vec![WorktreeRow {
            id: "worktree-1".into(),
            project_id: "project-1".into(),
            worktree_path: "/isolated/worktree".into(),
            ..Default::default()
        }],
        runs: BTreeMap::from([(
            "project-1".into(),
            vec![RunRow {
                run_id: "run-1".into(),
                status: "running".into(),
                worktree_id: Some("worktree-1".into()),
                ..Default::default()
            }],
        )]),
        ..Default::default()
    };
    let model = build(&SidebarInputs {
        local_machine: "local",
        focused_project: Some("project-1"),
        rows: &rows,
        roster: &[entry],
        panes: &[&pane],
        git_refreshed_at: Instant::now(),
    });

    assert_eq!(model.agents.len(), 1);
    assert_eq!(model.agents[0].state, RowState::Paused);
    let project = model
        .projects
        .iter()
        .find(|p| p.project_id == "project-1")
        .unwrap();
    assert_eq!(project.state, RowState::Paused);
    assert_eq!(project.worktrees[0].task_ref.as_deref(), Some("#25240"));
    assert_eq!(project.worktrees[0].state, RowState::Paused);
}

#[test]
fn help_names_a_child_run_wait_under_active_and_blank_as_nothing_assigned() {
    let chrome = Chrome::dark();
    let legend: Vec<String> = legend_groups(&chrome, u16::MAX)
        .iter()
        .flatten()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect()
        })
        .collect();

    let active = legend
        .iter()
        .find(|row| row.contains("▶ active"))
        .expect("the legend has an active row");
    assert!(
        active.contains("waiting on a child run"),
        "active help must cover a seat waiting on the child run it started: {active:?}"
    );
    let blank = legend
        .iter()
        .find(|row| row.contains("blank"))
        .expect("the legend has a blank row");
    assert!(
        blank.contains("nothing is assigned"),
        "blank help must say nothing is assigned: {blank:?}"
    );
}

/// Alive but not active is one state: a hold, an interrupt and a routine turn
/// end all draw paused, never the blank of a row with nothing assigned.
#[test]
fn pause_interrupt_and_turn_end_all_draw_paused() {
    let palette = Chrome::dark().palette;
    for status in [
        "paused",
        "interrupted",
        "completed",
        "idle",
        ROUTINE_TURN_END,
    ] {
        let state = agent_state(&seat(status));
        assert_eq!(state, RowState::Paused, "{status}");
        assert_ne!(
            state_dot(state, &palette).0,
            state_dot(RowState::Blank, &palette).0,
            "{status} needs a glyph, independent of color"
        );
    }
}

#[test]
fn help_explains_the_state_drawn_for_a_user_interrupt() {
    let chrome = Chrome::dark();
    let interrupted = agent_state(&seat("interrupted"));
    let glyph = state_dot(interrupted, &chrome.palette).0;
    let legend: Vec<String> = legend_groups(&chrome, u16::MAX)
        .iter()
        .flatten()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect()
        })
        .collect();

    assert!(
        legend.iter().any(|row| {
            row.trim_start().starts_with(glyph) && row.to_lowercase().contains("interrupt")
        }),
        "help must explain the daemon's user-interrupted state: {legend:?}"
    );
}
