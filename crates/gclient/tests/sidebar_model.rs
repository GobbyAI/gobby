//! 2.1 sidebar model: `agent_state` over a typed roster entry and its pane,
//! and `build` joining projects, source status, worktrees, sessions, runs,
//! and the roster into the tree the sidebar draws.

use std::collections::BTreeMap;
use std::path::PathBuf;

use gobby_client::app::sidebar_model::{
    agent_state, build, provider_label, rollup, AgentEntry, SandboxState, SidebarInputs,
    SidebarModel,
};
use gobby_client::app::{Backend, Pane, PaneId};
use gobby_client::daemon::{
    Attention, Checkout, ProjectRow, RosterEntry, RunRow, RunSandbox, SessionRow, SidebarRows,
    SourceStatus, TaskRef, TerminalRef, WorktreeRow,
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

    let mut paused = running.clone();
    paused.lifecycle_status = Some("paused".to_string());
    assert_eq!(
        agent_state(&paused, Some(&busy)),
        RowState::Paused,
        "a paused run is held even while output lands"
    );
    assert_eq!(
        agent_state(&paused, None),
        RowState::Paused,
        "a paused run is held with no pane"
    );
    paused.attention = blocked();
    assert_eq!(
        agent_state(&paused, Some(&quiet)),
        RowState::Attention,
        "a prompt outranks a pause"
    );

    // An agent awaiting input, approval or a handoff sits at its prompt:
    // it reads by its pane and is never held.
    for status in ["awaiting_input", "awaiting_approval", "awaiting_handoff"] {
        let mut waiting = running.clone();
        waiting.lifecycle_status = Some(status.to_string());
        assert_eq!(
            agent_state(&waiting, Some(&quiet)),
            RowState::Idle,
            "{status} is idle at its prompt"
        );
        assert_eq!(
            agent_state(&waiting, Some(&busy)),
            RowState::Unseen,
            "{status} with new output is unseen"
        );
        assert_eq!(
            agent_state(&waiting, None),
            RowState::Idle,
            "{status} with no pane is idle"
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

/// A tab, project or machine takes its most urgent member: needs you, then
/// gone, then active, then idle. Output unseen, held and no state yet roll
/// up as idle, and a container with no members is idle.
#[test]
fn rollup_ranks_needs_you_gone_held_active_idle() {
    let every = [
        RowState::Idle,
        RowState::Unseen,
        RowState::Unknown,
        RowState::Working,
        RowState::Paused,
        RowState::Orphaned,
        RowState::Attention,
    ];
    // Each prefix rolls up to its last, most urgent member.
    assert_eq!(rollup(every), RowState::Attention);
    assert_eq!(rollup(every[..6].iter().copied()), RowState::Orphaned);
    assert_eq!(rollup(every[..5].iter().copied()), RowState::Paused);
    assert_eq!(rollup(every[..4].iter().copied()), RowState::Working);
    // Output unseen and no state yet count as idle.
    assert_eq!(rollup(every[..3].iter().copied()), RowState::Idle);
    assert_eq!(
        rollup([RowState::Paused, RowState::Working]),
        RowState::Paused
    );
    assert_eq!(rollup(Vec::<RowState>::new()), RowState::Idle);
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
        ..Default::default()
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
        Some(RowState::Working),
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

/// #23049 option A: a row locks only when Gobby launched it under SRT, from
/// the run's enforced record or a managed session's launch contract. Every
/// other row, with or without records, is unrestricted; there is no unknown state.
#[test]
fn build_resolves_sandbox_state_from_session_and_run_records() {
    let session = |id: &str, enabled: Option<bool>| SessionRow {
        id: id.to_string(),
        sandbox_enabled: enabled,
        ..Default::default()
    };
    let run = |id: &str, enforced: Option<bool>| RunRow {
        run_id: id.to_string(),
        sandbox: enforced.map(|enforced| RunSandbox {
            enforced: Some(enforced),
        }),
        ..Default::default()
    };
    let rows = SidebarRows {
        sessions: BTreeMap::from([(
            PROJECT.to_string(),
            vec![
                session("sess-srt", Some(true)),
                session("sess-conflict", Some(false)),
                session("sess-direct", Some(false)),
                session("sess-none", None),
                session("sess-native", Some(false)),
            ],
        )]),
        runs: BTreeMap::from([(
            PROJECT.to_string(),
            vec![
                run("run-srt", Some(true)),
                run("run-conflict", Some(true)),
                run("run-native", Some(false)),
            ],
        )]),
        ..Default::default()
    };
    let agent = |entry_id: &str, terminal: &str, session: &str, run: Option<&str>| {
        let mut agent = entry(entry_id, Some(terminal));
        agent.session_id = Some(session.to_string());
        agent.run_id = run.map(str::to_string);
        agent
    };
    let model = model(
        &rows,
        &[
            agent("run:run-srt", "terminal-a", "sess-srt", Some("run-srt")),
            agent(
                "run:run-conflict",
                "terminal-b",
                "sess-conflict",
                Some("run-conflict"),
            ),
            agent("session:sess-direct", "terminal-c", "sess-direct", None),
            agent("session:sess-none", "terminal-d", "sess-none", None),
            agent(
                "run:run-native",
                "terminal-e",
                "sess-native",
                Some("run-native"),
            ),
        ],
        &[
            pane(1, "terminal-a", false, true),
            pane(2, "terminal-b", false, true),
            pane(3, "terminal-c", false, true),
            pane(4, "terminal-d", false, true),
            pane(5, "terminal-e", false, true),
        ],
    );

    let states: Vec<_> = model.agents.iter().map(|agent| agent.sandbox).collect();
    assert_eq!(
        states,
        [
            SandboxState::Sandboxed,
            SandboxState::Sandboxed,
            SandboxState::Unrestricted,
            SandboxState::Unrestricted,
            SandboxState::Unrestricted,
        ],
        "an enforced SRT run locks whatever its session says; a direct launch, \
         no record, and a spawned run without SRT stay unrestricted"
    );
    assert_eq!(
        SandboxState::resolve(Some(true), None, true),
        SandboxState::Sandboxed,
        "a managed session's launch contract alone locks"
    );
    assert_eq!(
        SandboxState::resolve(Some(true), None, false),
        SandboxState::Unrestricted,
        "a hand-opened seat never locks on its own session claim"
    );
    assert_eq!(
        SandboxState::resolve(None, None, false),
        SandboxState::Unrestricted,
        "a bare shell with no records is unrestricted"
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

/// 3.1.3: a run row carries its agent definition name and its task's title as
/// fields of their own, and a session with no run labels its definition with
/// the provider label its provisional session title carries.
#[test]
fn build_carries_definition_name_and_task_title() {
    let rows = SidebarRows {
        runs: BTreeMap::from([(
            PROJECT.to_string(),
            vec![RunRow {
                run_id: "run-b".to_string(),
                agent_name: Some("backend-developer".to_string()),
                provider: Some("codex".to_string()),
                status: "running".to_string(),
                ..Default::default()
            }],
        )]),
        ..Default::default()
    };
    let run_agent: RosterEntry = serde_json::from_value(json!({
        "entry_id": "run:run-b",
        "run_id": "run-b",
        "lifecycle_status": "running",
        "task": {"id": "task-uuid", "ref": "#22744", "stage": null, "title": "Agent data model"},
        "terminal": {"terminal_id": "terminal-b", "backend": "native"},
        "context_percent": 42,
        "tokens_used": 128000
    }))
    .expect("a roster run entry with a task title should deserialize");
    let mut claude_code = entry("session:sess-cc", Some("terminal-cc"));
    claude_code.session_id = Some("sess-cc".to_string());
    claude_code.provider = Some("claude_code".to_string());
    let mut claude = entry("session:sess-c", Some("terminal-c"));
    claude.session_id = Some("sess-c".to_string());
    claude.provider = Some("claude".to_string());

    let model = model(&rows, &[run_agent, claude_code, claude], &[]);

    let run_row = &model.agents[0];
    assert_eq!(
        run_row.agent_definition_name.as_deref(),
        Some("backend-developer")
    );
    assert_eq!(run_row.task_title.as_deref(), Some("Agent data model"));
    assert_eq!(run_row.task_ref.as_deref(), Some("#22744"));
    assert_eq!(run_row.context_percent, Some(42));
    assert_eq!(run_row.tokens_used, Some(128_000));
    assert_eq!(run_row.definition_label(), "backend-developer");
    let claude_code_row = &model.agents[1];
    assert_eq!(claude_code_row.agent_definition_name, None);
    assert_eq!(claude_code_row.task_title, None);
    assert_eq!(
        claude_code_row.definition_label(),
        "Claude Code",
        "a session with no run falls back to its provider label"
    );
    assert_eq!(model.agents[2].definition_label(), "Claude");
}

/// 3.1.6: the model slug is the display name, else the raw model, lowercased
/// with each whitespace run joined by one `-` and the effort appended.
#[test]
fn model_slug_lowercases_hyphenates_and_appends_effort() {
    let agent = |display: Option<&str>, model: Option<&str>, effort: Option<&str>| AgentEntry {
        model_display_name: display.map(str::to_string),
        model: model.map(str::to_string),
        effort: effort.map(str::to_string),
        ..AgentEntry::default()
    };

    assert_eq!(
        agent(Some("Fable 5.1"), Some("claude-fable-5-1"), Some("xhigh")).model_slug(),
        "claude-fable-5.1-xhigh",
        "the raw id's family leads when the display name drops it"
    );
    assert_eq!(
        agent(Some("GPT-6 Sol"), Some("gpt-6-sol"), Some("xhigh")).model_slug(),
        "gpt-6-sol-xhigh",
        "a display name that keeps the family is not prefixed twice"
    );
    assert_eq!(
        agent(Some("Claude  Opus\t5.5"), None, None).model_slug(),
        "claude-opus-5.5",
        "a whitespace run becomes one hyphen"
    );
    let mut claude = agent(Some("Claude Opus 5.5"), None, Some("high"));
    claude.provider = "claude".to_string();
    assert_eq!(
        claude.model_slug(),
        "claude-opus-5.5-high",
        "the provider's own name stays in the slug"
    );
    assert_eq!(
        agent(None, Some("GPT-5-Codex"), Some("high")).model_slug(),
        "gpt-5-codex-high",
        "the raw model stands in for a missing display name"
    );
    assert_eq!(agent(None, None, None).model_slug(), "");
}

#[test]
fn agy_sessions_are_labelled_antigravity() {
    assert_eq!(provider_label("agy"), "Antigravity");
    assert_eq!(provider_label(" AGY "), "Antigravity");
    assert_eq!(provider_label("codex"), "Codex");
}
