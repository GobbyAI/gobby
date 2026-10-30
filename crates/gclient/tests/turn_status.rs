//! Sidebar states must preserve deliberate pauses and user interrupts.

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
fn waiting_seat_is_non_held_idle_in_agents_projects_and_task_worktrees() {
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
    assert_eq!(model.agents[0].state, RowState::Idle);
    let project = model
        .projects
        .iter()
        .find(|p| p.project_id == "project-1")
        .unwrap();
    assert_eq!(project.state, RowState::Idle);
    assert_eq!(project.worktrees[0].task_ref.as_deref(), Some("#25240"));
    assert_eq!(project.worktrees[0].state, Some(RowState::Idle));
}

#[test]
fn help_includes_pending_work_in_the_waiting_seats_idle_state() {
    let chrome = Chrome::dark();
    let state = agent_state(&seat(ROUTINE_TURN_END), None);
    let glyph = state_dot(state, &chrome.palette).0;
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
            row.trim_start().starts_with(glyph)
                && row.to_lowercase().contains("wait")
                && !row.contains("nothing pending")
        }),
        "idle help must account for a seat with an armed wait or background work: {legend:?}"
    );
}

#[test]
fn explicit_pause_keeps_a_distinct_state_and_glyph() {
    let palette = Chrome::dark().palette;
    let paused = agent_state(&seat("paused"), None);
    let idle = agent_state(&seat("completed"), None);

    assert_eq!(paused, RowState::Paused);
    assert_ne!(paused, idle);
    assert_ne!(state_dot(paused, &palette).0, state_dot(idle, &palette).0);
}

#[test]
fn user_interrupt_has_a_distinct_state_and_glyph_from_normal_idle() {
    let palette = Chrome::dark().palette;
    let interrupted = agent_state(&seat("interrupted"), None);
    let idle = agent_state(&seat("completed"), None);

    assert_ne!(interrupted, idle, "a user interrupt must remain visible");
    assert_ne!(
        state_dot(interrupted, &palette).0,
        state_dot(idle, &palette).0,
        "the interrupt needs a distinct glyph, independent of color"
    );
}

#[test]
fn help_explains_the_state_drawn_for_a_user_interrupt() {
    let chrome = Chrome::dark();
    let interrupted = agent_state(&seat("interrupted"), None);
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
