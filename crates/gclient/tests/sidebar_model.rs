//! 2.1 sidebar model: `agent_state` over a typed roster entry and its pane,
//! and `build` joining projects, source status, worktrees, sessions, runs,
//! and the roster into the tree the sidebar draws.

use std::collections::BTreeMap;
use std::path::PathBuf;

use gobby_client::app::sidebar_model::{agent_state, build, SidebarInputs, SidebarModel};
use gobby_client::app::{Backend, Pane, PaneId};
use gobby_client::daemon::{
    Attention, Checkout, ProjectRow, RosterEntry, RunRow, SessionRow, SidebarRows, SourceStatus,
    TaskRef, TerminalRef, WorktreeRow,
};
use gobby_client::ui::chrome::RowState;
use serde_json::json;
use tokio::time::Instant;

const LOCAL_MACHINE: &str = "m-local";
const REMOTE_MACHINE: &str = "m-remote";
const PROJECT: &str = "p1";

fn entry(entry_id: &str, terminal_id: Option<&str>) -> RosterEntry {
    RosterEntry {
        entry_id: entry_id.to_string(),
        lifecycle_status: Some("running".to_string()),
        terminal: terminal_id.map(|terminal_id| TerminalRef {
            terminal_id: terminal_id.to_string(),
            backend: Backend::Native,
            state: None,
        }),
        ..Default::default()
    }
}

fn blocked() -> Option<Attention> {
    Some(Attention {
        attention_id: Some("att-1".to_string()),
        kind: Some("actionable".to_string()),
        ..Default::default()
    })
}

fn pane(index: u32, terminal_id: &str, new_output: bool, live: bool) -> Pane {
    let mut pane = Pane::new(PaneId(index), terminal_id, Backend::Native, "epoch");
    pane.new_output = new_output;
    pane.live = live;
    pane
}

fn model(rows: &SidebarRows, roster: &[RosterEntry], panes: &[Pane]) -> SidebarModel {
    let panes: Vec<&Pane> = panes.iter().collect();
    build(&SidebarInputs {
        local_machine: LOCAL_MACHINE,
        focused_project: Some(PROJECT),
        rows,
        roster,
        panes: &panes,
        git_refreshed_at: Instant::now(),
    })
}

#[test]
fn session_row_accepts_a_missing_reasoning_effort() {
    let row: SessionRow = serde_json::from_value(json!({
        "id": "sess-a",
        "status": "active"
    }))
    .expect("an older daemon session row should deserialize");

    assert_eq!(row.reasoning_effort(), None);
}

/// 2.1.1: `attention == null` renders idle or working by the pane's output,
/// only a set `attention` renders blocked, and an entry without a `terminal`
/// never becomes an agent row.
#[test]
fn agent_state_follows_attention_and_terminal() {
    let running = entry("run:a", Some("terminal-a"));
    let quiet = pane(1, "terminal-a", false, true);
    let busy = pane(2, "terminal-a", true, true);
    let detached = pane(3, "terminal-a", true, false);

    assert_eq!(
        agent_state(&running, Some(&quiet)),
        RowState::Working,
        "a running agent is working at the live edge"
    );
    assert_eq!(
        agent_state(&running, Some(&busy)),
        RowState::Working,
        "a running agent with live output is working"
    );
    assert_eq!(
        agent_state(&running, Some(&detached)),
        RowState::Working,
        "a running agent is working even when its pane is detached"
    );
    assert_eq!(
        agent_state(&running, None),
        RowState::Working,
        "a running agent is working without an open pane"
    );

    let mut active = running.clone();
    active.lifecycle_status = Some("active".to_string());
    assert_eq!(agent_state(&active, Some(&quiet)), RowState::Working);
    assert_eq!(agent_state(&active, None), RowState::Working);

    let mut finished = running.clone();
    finished.lifecycle_status = Some("completed".to_string());
    assert_eq!(
        agent_state(&finished, Some(&busy)),
        RowState::Unseen,
        "output after the run ended is unseen, never working"
    );
    assert_eq!(
        agent_state(&finished, Some(&quiet)),
        RowState::Idle,
        "without new output an ended run is idle"
    );

    let mut blocked_entry = running.clone();
    blocked_entry.attention = blocked();
    assert_eq!(
        agent_state(&blocked_entry, Some(&quiet)),
        RowState::Attention,
        "only a set attention renders blocked"
    );

    for status in [
        "paused",
        "awaiting_input",
        "awaiting_approval",
        "awaiting_handoff",
    ] {
        let mut waiting = running.clone();
        waiting.lifecycle_status = Some(status.to_string());
        assert_eq!(
            agent_state(&waiting, Some(&busy)),
            RowState::Paused,
            "{status} pauses the row even while output lands"
        );
        assert_eq!(
            agent_state(&waiting, None),
            RowState::Paused,
            "{status} pauses a row with no pane"
        );
        waiting.attention = blocked();
        assert_eq!(
            agent_state(&waiting, Some(&quiet)),
            RowState::Attention,
            "a prompt outranks {status}"
        );
    }

    let roster = [entry("run:a", Some("terminal-a")), entry("session:s", None)];
    let model = model(&SidebarRows::default(), &roster, &[quiet]);
    let agents: Vec<&str> = model
        .agents
        .iter()
        .map(|agent| agent.entry_id.as_str())
        .collect();
    assert_eq!(
        agents,
        ["run:a"],
        "an entry without a terminal never becomes an agent row"
    );
    assert_eq!(
        model.agents[0].project_id, PROJECT,
        "a terminal the daemon joins to no session or run belongs to the focused project"
    );
}

/// 2.1.2: projects carry branch and ahead/behind, worktree children carry
/// task refs, and agents land in the right project with the joined ref,
/// title, and machine id.
#[test]
fn build_joins_projects_worktrees_and_agents() {
    let project = |id: &str, name: &str, display_name: &str, checkout: bool| ProjectRow {
        id: id.to_string(),
        name: name.to_string(),
        display_name: display_name.to_string(),
        checkout: checkout.then(|| Checkout {
            machine_id: LOCAL_MACHINE.to_string(),
            root_path: "/repo".to_string(),
        }),
        ..Default::default()
    };
    let rows = SidebarRows {
        projects: vec![
            project("p-personal", "_personal", "Personal", true),
            project(PROJECT, "gobby", "gobby", true),
            project("p-global", "_global", "_global", false),
        ],
        statuses: BTreeMap::from([(
            PROJECT.to_string(),
            SourceStatus {
                current_branch: Some("0.5.0".to_string()),
                ahead: Some(3),
                behind: Some(1),
                repo_path: Some("/repo".to_string()),
                worktree_count: 1,
            },
        )]),
        worktrees: vec![WorktreeRow {
            id: "wt-1".to_string(),
            project_id: PROJECT.to_string(),
            branch_name: Some("gobby-21986-sidebar".to_string()),
            worktree_path: "/w/1".to_string(),
            status: "active".to_string(),
            workspace_role: "task".to_string(),
            ..Default::default()
        }],
        sessions: BTreeMap::from([(
            PROJECT.to_string(),
            vec![SessionRow {
                id: "sess-a".to_string(),
                reference: Some("#12217".to_string()),
                title: Some("coordinator".to_string()),
                source: Some("claude".to_string()),
                status: "active".to_string(),
                machine_id: Some(REMOTE_MACHINE.to_string()),
                ..Default::default()
            }],
        )]),
        runs: BTreeMap::from([(
            PROJECT.to_string(),
            vec![RunRow {
                run_id: "run-b".to_string(),
                agent_name: Some("codex-worker".to_string()),
                provider: Some("codex".to_string()),
                model: Some("gpt-5-codex".to_string()),
                status: "running".to_string(),
                worktree_id: Some("wt-1".to_string()),
                machine_id: Some(LOCAL_MACHINE.to_string()),
                ..Default::default()
            }],
        )]),
    };

    let mut session_agent = entry("session:sess-a", Some("terminal-a"));
    session_agent.session_id = Some("sess-a".to_string());
    session_agent.attention = blocked();
    let mut run_agent = entry("run:run-b", Some("terminal-b"));
    run_agent.run_id = Some("run-b".to_string());
    run_agent.task = Some(TaskRef {
        id: "task-uuid".to_string(),
        reference: Some("#21986".to_string()),
    });
    let mut bare_session = entry("session:sess-c", None);
    bare_session.session_id = Some("sess-c".to_string());
    let roster = [session_agent, run_agent, bare_session];
    let panes = [
        pane(1, "terminal-a", false, true),
        pane(2, "terminal-b", true, true),
    ];

    let model = model(&rows, &roster, &panes);

    let names: Vec<&str> = model
        .projects
        .iter()
        .map(|project| project.name.as_str())
        .collect();
    assert_eq!(
        names,
        ["gobby", "Personal"],
        "bookkeeping projects are hidden and Personal sorts last"
    );
    let gobby = &model.projects[0];
    assert_eq!(gobby.project_id, PROJECT);
    assert_eq!(gobby.branch.as_deref(), Some("0.5.0"));
    assert_eq!((gobby.ahead, gobby.behind), (Some(3), Some(1)));
    assert_eq!(gobby.root_path, Some(PathBuf::from("/repo")));
    assert_eq!(
        gobby.state,
        RowState::Attention,
        "a project shows its most urgent agent"
    );
    assert_eq!(
        model.projects[1].state,
        RowState::Idle,
        "a project with no agents is idle"
    );

    assert_eq!(
        gobby.worktrees.len(),
        1,
        "one worktree child under the project"
    );
    let worktree = &gobby.worktrees[0];
    assert_eq!(worktree.worktree_id, "wt-1");
    assert_eq!(worktree.branch.as_deref(), Some("gobby-21986-sidebar"));
    assert_eq!(worktree.path, PathBuf::from("/w/1"));
    assert_eq!(worktree.role, "task");
    assert_eq!(
        worktree.task_ref.as_deref(),
        Some("#21986"),
        "the worktree carries the task ref of the agent bound to it"
    );
    assert_eq!(
        worktree.state,
        RowState::Working,
        "the worktree shows the state of the agent running in it"
    );

    let ids: Vec<&str> = model
        .agents
        .iter()
        .map(|agent| agent.entry_id.as_str())
        .collect();
    assert_eq!(
        ids,
        ["session:sess-a", "run:run-b"],
        "agents keep roster order and drop the entry without a terminal"
    );
    let session_row = &model.agents[0];
    assert_eq!(session_row.project_id, PROJECT);
    assert_eq!(session_row.session_ref.as_deref(), Some("#12217"));
    assert_eq!(
        session_row.name, "coordinator",
        "the joined session title names the agent"
    );
    assert_eq!(
        session_row.machine_id, REMOTE_MACHINE,
        "the joined session's machine wins"
    );
    assert_eq!(
        session_row.provider, "claude",
        "the session source stands in for a provider"
    );
    assert_eq!(session_row.state, RowState::Attention);
    assert!(
        session_row.attention.is_some(),
        "the agent row keeps the attention block"
    );
    let run_row = &model.agents[1];
    assert_eq!(run_row.project_id, PROJECT);
    assert_eq!(
        run_row.name, "codex-worker",
        "the joined run's agent name names the agent"
    );
    assert_eq!(run_row.machine_id, LOCAL_MACHINE);
    assert_eq!(run_row.provider, "codex");
    assert_eq!(run_row.model.as_deref(), Some("gpt-5-codex"));
    assert_eq!(run_row.task_ref.as_deref(), Some("#21986"));
    assert_eq!(run_row.worktree_id.as_deref(), Some("wt-1"));
    assert_eq!(run_row.state, RowState::Working);

    assert_eq!(model.local_machine, LOCAL_MACHINE);
    assert_eq!(
        model.machines,
        [LOCAL_MACHINE, REMOTE_MACHINE],
        "machines list every agent host plus this one, sorted"
    );
}

#[test]
fn build_prefers_run_effort_then_falls_back_to_session_effort() {
    let rows = SidebarRows {
        sessions: BTreeMap::from([(
            PROJECT.to_string(),
            vec![
                SessionRow {
                    id: "sess-run".to_string(),
                    reasoning_effort: Some("medium".to_string()),
                    ..Default::default()
                },
                SessionRow {
                    id: "sess-requested".to_string(),
                    reasoning_effort: Some("low".to_string()),
                    ..Default::default()
                },
                SessionRow {
                    id: "sess-interactive".to_string(),
                    reasoning_effort: Some("minimal".to_string()),
                    ..Default::default()
                },
            ],
        )]),
        runs: BTreeMap::from([(
            PROJECT.to_string(),
            vec![
                RunRow {
                    run_id: "run-effective".to_string(),
                    effective_reasoning_effort: Some("high".to_string()),
                    requested_reasoning_effort: Some("max".to_string()),
                    ..Default::default()
                },
                RunRow {
                    run_id: "run-requested".to_string(),
                    requested_reasoning_effort: Some("max".to_string()),
                    ..Default::default()
                },
            ],
        )]),
        ..Default::default()
    };
    let mut effective_agent = entry("run:run-effective", Some("terminal-a"));
    effective_agent.session_id = Some("sess-run".to_string());
    effective_agent.run_id = Some("run-effective".to_string());
    let mut requested_agent = entry("run:run-requested", Some("terminal-b"));
    requested_agent.session_id = Some("sess-requested".to_string());
    requested_agent.run_id = Some("run-requested".to_string());
    let mut interactive_agent = entry("session:sess-interactive", Some("terminal-c"));
    interactive_agent.session_id = Some("sess-interactive".to_string());

    let model = model(
        &rows,
        &[effective_agent, requested_agent, interactive_agent],
        &[
            pane(1, "terminal-a", false, true),
            pane(2, "terminal-b", false, true),
            pane(3, "terminal-c", false, true),
        ],
    );

    assert_eq!(
        model.agents[0].effort.as_deref(),
        Some("high"),
        "a run's effective effort wins over its requested and session efforts"
    );
    assert_eq!(
        model.agents[1].effort.as_deref(),
        Some("max"),
        "a run's requested effort wins over its session effort"
    );
    assert_eq!(
        model.agents[2].effort.as_deref(),
        Some("minimal"),
        "an interactive row falls back to its session effort"
    );
}

/// #22805: a Grok pane's row is named like any provider's. The joined session
/// title wins over the pane's foreground command, and the command ("grok")
/// names only a row the roster has not joined to a session.
#[test]
fn grok_row_takes_the_session_title_over_the_pane_command() {
    let rows = SidebarRows {
        sessions: BTreeMap::from([(
            PROJECT.to_string(),
            vec![SessionRow {
                id: "sess-grok".to_string(),
                title: Some("Stability lane Engineer".to_string()),
                source: Some("grok".to_string()),
                status: "awaiting_approval".to_string(),
                ..Default::default()
            }],
        )]),
        ..Default::default()
    };
    let mut joined = entry("session:sess-grok", Some("terminal-grok"));
    joined.session_id = Some("sess-grok".to_string());
    joined.provider = Some("grok".to_string());
    let unjoined = entry("terminal:terminal-bare", Some("terminal-bare"));
    let mut grok_pane = pane(1, "terminal-grok", false, true);
    grok_pane.command = Some("grok".to_string());
    let mut bare_pane = pane(2, "terminal-bare", false, true);
    bare_pane.command = Some("grok".to_string());

    let model = model(&rows, &[joined, unjoined], &[grok_pane, bare_pane]);

    assert_eq!(
        model.agents[0].name, "Stability lane Engineer",
        "the joined session title names the Grok row"
    );
    assert_eq!(model.agents[0].provider, "grok");
    assert_eq!(
        model.agents[1].name, "grok",
        "a row with no session falls back to the pane command"
    );
}
