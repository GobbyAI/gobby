//! The five row states every level draws: agent, worktree, project and machine.
//! Each test names the behaviour it pins from the operator-approved spec of
//! task #25790.

use std::collections::BTreeMap;
use std::fmt::{Debug, Write as _};
use std::path::Path;
use std::sync::{Arc, Mutex};

use gobby_client::app::sidebar_model::{
    agent_state, build, log_needs_you_transitions, worktree_focus_target, SidebarInputs,
    SidebarModel,
};
use gobby_client::app::{Backend, Pane, PaneId, WorkspaceModel};
use gobby_client::daemon::{
    Attention, ProjectRow, RosterEntry, SessionRow, SidebarRows, TaskRef, TerminalRef, WorktreeRow,
};
use gobby_client::theme::{Theme, ThemeKind};
use gobby_client::ui::chrome::{Chrome, RowState, WorkspaceView};
use gobby_client::ui::sidebar::machine_rows;
use gobby_client::ui::status::{state_dot, state_label};
use gobby_client::ui::status_segments::agent_counts;
use tokio::time::Instant;
use tracing::field::{Field, Visit};
use tracing::{Event, Subscriber};
use tracing_subscriber::layer::{Context as LayerContext, SubscriberExt};
use tracing_subscriber::Layer;

const LOCAL_MACHINE: &str = "m-local";
const PROJECT: &str = "p1";

fn palette() -> gobby_client::theme::Palette {
    Theme::new(ThemeKind::Dark).palette()
}

/// A roster entry that is alive and running, on its own terminal.
fn running(entry_id: &str, session_id: &str, terminal_id: &str) -> RosterEntry {
    RosterEntry {
        entry_id: entry_id.to_string(),
        session_id: Some(session_id.to_string()),
        project_id: Some(PROJECT.to_string()),
        lifecycle_status: Some("running".to_string()),
        terminal: Some(TerminalRef {
            terminal_id: terminal_id.to_string(),
            backend: Backend::Native,
            state: None,
        }),
        ..Default::default()
    }
}

fn with_lifecycle(mut entry: RosterEntry, status: &str) -> RosterEntry {
    entry.lifecycle_status = Some(status.to_string());
    entry
}

fn dead(mut entry: RosterEntry) -> RosterEntry {
    if let Some(terminal) = entry.terminal.as_mut() {
        terminal.state = Some("orphaned".to_string());
    }
    entry
}

fn pane(index: u32, terminal_id: &str, new_output: bool, live: bool) -> Pane {
    let mut pane = Pane::new(PaneId(index), terminal_id, Backend::Native, "epoch");
    pane.new_output = new_output;
    pane.live = live;
    pane
}

fn project_row() -> ProjectRow {
    ProjectRow {
        id: PROJECT.to_string(),
        name: "gobby".to_string(),
        ..Default::default()
    }
}

fn build_model(rows: &SidebarRows, roster: &[RosterEntry], panes: &[Pane]) -> SidebarModel {
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

fn rows_with(worktrees: Vec<WorktreeRow>, sessions: Vec<SessionRow>) -> SidebarRows {
    SidebarRows {
        projects: vec![project_row()],
        worktrees,
        sessions: BTreeMap::from([(PROJECT.to_string(), sessions)]),
        ..Default::default()
    }
}

fn agent_entry<'a>(
    model: &'a SidebarModel,
    entry_id: &str,
) -> &'a gobby_client::app::sidebar_model::AgentEntry {
    model
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .expect("agent row exists")
}

/// A sidebar view whose daemon is reachable or not. Machine rows read only the
/// sidebar model and the daemon's reachability, so the rest answers nothing.
struct DaemonView<'a> {
    model: &'a SidebarModel,
    reachable: bool,
}

impl WorkspaceView for DaemonView<'_> {
    fn project_id(&self) -> Option<&str> {
        None
    }
    fn focused_project(&self) -> Option<&str> {
        None
    }
    fn sidebar(&self) -> &SidebarModel {
        self.model
    }
    fn roster_terminal_ids(&self) -> Vec<String> {
        Vec::new()
    }
    fn attention_entry_ids(&self) -> Vec<String> {
        Vec::new()
    }
    fn pane_for_terminal(&self, _terminal_id: &str) -> Option<PaneId> {
        None
    }
    fn pane(&self, _id: PaneId) -> &Pane {
        panic!("machine rows never read a pane")
    }
    fn daemon_ready(&self) -> bool {
        self.reachable
    }
    fn gobby_home(&self) -> Option<&Path> {
        None
    }
    fn workspace_model(&self) -> Option<&WorkspaceModel> {
        None
    }
}

fn local_machine_state(model: &SidebarModel, reachable: bool) -> RowState {
    let view = DaemonView { model, reachable };
    machine_rows(&view, &Chrome::dark())
        .into_iter()
        .find(|row| row.id == LOCAL_MACHINE)
        .expect("the local machine row")
        .state
}

/// Each of the five states has one glyph and one label, and nothing else.
#[test]
fn five_row_states_each_have_one_glyph_and_label() {
    let p = palette();
    let expected = [
        (RowState::Attention, "⍾", "needs you"),
        (RowState::Working, "▶", "active"),
        (RowState::Paused, "‖", "paused"),
        (RowState::NoLive, "✕", "no live agents"),
        (RowState::Blank, " ", "blank"),
    ];
    assert_eq!(RowState::ALL.len(), expected.len());
    for (state, glyph, label) in expected {
        assert!(RowState::ALL.contains(&state), "{state:?} is listed in ALL");
        assert_eq!(state_dot(state, &p).0, glyph, "{state:?} glyph");
        assert_eq!(state_label(state), label, "{state:?} label");
    }
}

/// Needs you covers questions, plan approvals and permission prompts; an
/// operator pause or interrupt is held; a dead host is no live agent.
#[test]
fn agent_state_maps_each_roster_signal_to_one_of_the_five_states() {
    let base = running("e", "s", "t");
    let attention = RosterEntry {
        attention: Some(Attention {
            attention_id: Some("att".to_string()),
            kind: Some("actionable".to_string()),
            ..Default::default()
        }),
        ..base.clone()
    };
    assert_eq!(agent_state(&attention), RowState::Attention);
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "awaiting_input")),
        RowState::Attention
    );
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "awaiting_approval")),
        RowState::Attention
    );
    assert_eq!(agent_state(&dead(base.clone())), RowState::NoLive);
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "paused")),
        RowState::Paused
    );
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "interrupted")),
        RowState::Paused
    );
    assert_eq!(agent_state(&base), RowState::Working);
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "active")),
        RowState::Working
    );
    // Between turns with nothing running is not being worked: blank.
    assert_eq!(
        agent_state(&with_lifecycle(base.clone(), "completed")),
        RowState::Blank
    );
    assert_eq!(
        agent_state(&with_lifecycle(base, "waiting")),
        RowState::Blank
    );
}

/// Output on a pane is not a state: an idle agent with a streaming pane is blank.
#[test]
fn pane_output_does_not_make_an_agent_active() {
    let entry = with_lifecycle(running("e", "s", "t"), "completed");
    let rows = rows_with(vec![], vec![]);
    let model = build_model(&rows, &[entry], &[pane(1, "t", true, true)]);
    assert_eq!(agent_entry(&model, "e").state, RowState::Blank);
}

/// An agent waiting on a child run it started stays active while that run works.
#[test]
fn agent_waiting_on_a_running_child_run_is_active() {
    let parent = with_lifecycle(running("parent", "s-parent", "t-parent"), "completed");
    let child = running("run:child", "s-child", "t-child");
    let sessions = vec![SessionRow {
        id: "s-child".to_string(),
        parent_session_id: Some("s-parent".to_string()),
        ..Default::default()
    }];
    let rows = rows_with(vec![], sessions);
    let model = build_model(&rows, &[parent, child], &[]);
    assert_eq!(agent_entry(&model, "parent").state, RowState::Working);
    assert_eq!(agent_entry(&model, "run:child").state, RowState::Working);
}

/// A dead child run does not keep its parent active.
#[test]
fn dead_child_run_does_not_keep_its_parent_active() {
    let parent = with_lifecycle(running("parent", "s-parent", "t-parent"), "completed");
    let child = dead(running("run:child", "s-child", "t-child"));
    let sessions = vec![SessionRow {
        id: "s-child".to_string(),
        parent_session_id: Some("s-parent".to_string()),
        ..Default::default()
    }];
    let rows = rows_with(vec![], sessions);
    let model = build_model(&rows, &[parent, child], &[]);
    assert_eq!(agent_entry(&model, "parent").state, RowState::Blank);
    assert_eq!(agent_entry(&model, "run:child").state, RowState::NoLive);
}

/// A project rolls up to its most urgent agent and counts the dead ones, so
/// one active agent beside two dead ones reads as active with two no-live.
#[test]
fn project_counts_its_no_live_agents_beside_the_rollup() {
    let working = running("a", "s-a", "t-a");
    let gone_one = dead(running("b", "s-b", "t-b"));
    let gone_two = dead(running("c", "s-c", "t-c"));
    let rows = rows_with(vec![], vec![]);
    let model = build_model(&rows, &[working, gone_one, gone_two], &[]);
    let project = &model.projects[0];
    assert_eq!(project.state, RowState::Working);
    assert_eq!(project.no_live, 2);
    assert_eq!(project.needs_you, 0);
}

/// A worktree rolls up from the agents bound to it; with none it is blank.
#[test]
fn worktree_rolls_up_its_bound_agents_and_is_blank_when_empty() {
    let worktrees = vec![
        WorktreeRow {
            id: "w-busy".to_string(),
            project_id: PROJECT.to_string(),
            worktree_path: "/wt/busy".to_string(),
            ..Default::default()
        },
        WorktreeRow {
            id: "w-empty".to_string(),
            project_id: PROJECT.to_string(),
            worktree_path: "/wt/empty".to_string(),
            ..Default::default()
        },
    ];
    let sessions = vec![SessionRow {
        id: "s-busy".to_string(),
        worktree_id: Some("w-busy".to_string()),
        ..Default::default()
    }];
    let rows = rows_with(worktrees, sessions);
    let busy = with_lifecycle(running("busy", "s-busy", "t-busy"), "awaiting_approval");
    let model = build_model(&rows, &[busy], &[]);
    let project = &model.projects[0];
    let state_of = |id: &str| {
        project
            .worktrees
            .iter()
            .find(|worktree| worktree.worktree_id == id)
            .map(|worktree| worktree.state)
            .expect("worktree row exists")
    };
    assert_eq!(state_of("w-busy"), RowState::Attention);
    assert_eq!(state_of("w-empty"), RowState::Blank);
}

/// An epic's integration tree counts its team's live lead as assigned, even
/// though the lead runs in the main checkout.
#[test]
fn epic_integration_tree_shows_its_live_lead() {
    let epic_id = "epic-task-id";
    let worktrees = vec![WorktreeRow {
        id: "w-epic".to_string(),
        project_id: PROJECT.to_string(),
        task_id: Some(epic_id.to_string()),
        branch_name: Some("epic/9/main".to_string()),
        worktree_path: "/wt/epic-9".to_string(),
        ..Default::default()
    }];
    let rows = rows_with(worktrees, vec![]);
    let lead = RosterEntry {
        task: Some(TaskRef {
            id: epic_id.to_string(),
            reference: Some("#9".to_string()),
            title: Some("Epic nine".to_string()),
            ..Default::default()
        }),
        ..running("lead", "s-lead", "t-lead")
    };
    let model = build_model(&rows, std::slice::from_ref(&lead), &[]);
    let tree = model.projects[0]
        .worktrees
        .iter()
        .find(|worktree| worktree.worktree_id == "w-epic")
        .expect("integration tree row exists");
    assert_eq!(tree.state, RowState::Working);

    let gone = build_model(&rows, &[dead(lead)], &[]);
    let tree = gone.projects[0]
        .worktrees
        .iter()
        .find(|worktree| worktree.worktree_id == "w-epic")
        .expect("integration tree row exists");
    assert_eq!(tree.state, RowState::NoLive);
}

/// An unreachable local daemon marks its machine ✕; a reachable idle one is blank.
#[test]
fn unreachable_local_daemon_shows_its_machine_as_no_live() {
    let model = build_model(&rows_with(vec![], vec![]), &[], &[]);
    assert_eq!(local_machine_state(&model, true), RowState::Blank);
    assert_eq!(local_machine_state(&model, false), RowState::NoLive);
}

#[test]
fn machine_row_counts_its_needs_you_and_no_live_agents() {
    let waiting = with_lifecycle(running("a", "s-a", "t-a"), "awaiting_input");
    let gone_one = dead(running("b", "s-b", "t-b"));
    let gone_two = dead(running("c", "s-c", "t-c"));
    let model = build_model(
        &rows_with(vec![], vec![]),
        &[waiting, gone_one, gone_two],
        &[],
    );
    let view = DaemonView {
        model: &model,
        reachable: true,
    };
    let machine = machine_rows(&view, &Chrome::dark())
        .into_iter()
        .find(|row| row.id == LOCAL_MACHINE)
        .expect("the local machine row");
    assert_eq!(machine.needs_you, 1);
    assert_eq!(machine.no_live, 2);
}

/// A worktree whose every bound agent is dead rolls up to ✕, not blank.
#[test]
fn worktree_whose_bound_agents_are_all_dead_is_no_live() {
    let worktrees = vec![WorktreeRow {
        id: "w-gone".to_string(),
        project_id: PROJECT.to_string(),
        worktree_path: "/wt/gone".to_string(),
        ..Default::default()
    }];
    let sessions = vec![SessionRow {
        id: "s-gone".to_string(),
        worktree_id: Some("w-gone".to_string()),
        ..Default::default()
    }];
    let rows = rows_with(worktrees, sessions);
    let gone = dead(running("gone", "s-gone", "t-gone"));
    let model = build_model(&rows, &[gone], &[]);
    let worktree = &model.projects[0].worktrees[0];
    assert_eq!(worktree.worktree_id, "w-gone");
    assert_eq!(worktree.state, RowState::NoLive);
}

/// The main checkout has no worktree row, so its coordinator seat rolls into
/// the project and leaves every worktree row blank.
#[test]
fn main_checkout_coordinator_seat_rolls_into_its_project_only() {
    let worktrees = vec![WorktreeRow {
        id: "w-feature".to_string(),
        project_id: PROJECT.to_string(),
        worktree_path: "/wt/feature".to_string(),
        ..Default::default()
    }];
    let rows = rows_with(worktrees, vec![]);
    let coordinator = with_lifecycle(running("coord", "s-coord", "t-coord"), "awaiting_approval");
    let model = build_model(&rows, &[coordinator], &[]);
    let project = &model.projects[0];
    assert_eq!(project.state, RowState::Attention);
    assert_eq!(project.needs_you, 1);
    assert_eq!(project.worktrees[0].state, RowState::Blank);
}

/// The main checkout is the project card: its branch is the checkout's, and
/// `project_rows` copies its state and counts. A coordinator seat that runs
/// there is unbound, so it counts on that row in each live state and in
/// no-live, and the feature worktree beside it stays blank.
#[test]
fn main_checkout_row_counts_its_coordinator_seats_in_every_state() {
    let worktrees = vec![WorktreeRow {
        id: "w-feature".to_string(),
        project_id: PROJECT.to_string(),
        worktree_path: "/wt/feature".to_string(),
        ..Default::default()
    }];
    let rows = rows_with(worktrees, vec![]);
    let working = running("coord-live", "s-live", "t-live");
    let ended = dead(running("coord-ended", "s-ended", "t-ended"));
    let waiting = with_lifecycle(running("coord-ask", "s-ask", "t-ask"), "awaiting_approval");
    let model = build_model(&rows, &[working.clone(), ended, waiting], &[]);
    let project = &model.projects[0];
    assert_eq!(project.state, RowState::Attention);
    assert_eq!(project.needs_you, 1);
    assert_eq!(project.no_live, 1);
    assert_eq!(project.worktrees[0].state, RowState::Blank);

    let model = build_model(&rows, &[working], &[]);
    let project = &model.projects[0];
    assert_eq!(project.state, RowState::Working);
    assert_eq!(project.needs_you, 0);
    assert_eq!(project.worktrees[0].state, RowState::Blank);
}

/// A tree bound by its task: each lead carries the epic's task, and no worktree.
fn task_lead(entry_id: &str) -> RosterEntry {
    RosterEntry {
        task: Some(TaskRef {
            id: "epic-task-id".to_string(),
            reference: Some("#9".to_string()),
            title: Some("Epic nine".to_string()),
            ..Default::default()
        }),
        ..running(entry_id, &format!("s-{entry_id}"), &format!("t-{entry_id}"))
    }
}

fn epic_tree_state(model: &SidebarModel) -> RowState {
    model.projects[0]
        .worktrees
        .iter()
        .find(|worktree| worktree.worktree_id == "w-epic")
        .expect("integration tree row exists")
        .state
}

fn epic_rows() -> SidebarRows {
    rows_with(
        vec![WorktreeRow {
            id: "w-epic".to_string(),
            project_id: PROJECT.to_string(),
            task_id: Some("epic-task-id".to_string()),
            branch_name: Some("epic/9/main".to_string()),
            worktree_path: "/wt/epic-9".to_string(),
            ..Default::default()
        }],
        vec![],
    )
}

/// A dead previous lead does not hold the tree at no live: the tree shows the
/// live lead's state, here blank.
#[test]
fn epic_tree_shows_its_live_lead_past_a_dead_previous_lead() {
    let previous = dead(task_lead("previous"));
    let current = with_lifecycle(task_lead("current"), "completed");
    let model = build_model(&epic_rows(), &[previous, current], &[]);
    assert_eq!(epic_tree_state(&model), RowState::Blank);
}

#[test]
fn counters_exclude_superseded_leads_but_keep_dead_worktree_assignments() {
    for lifecycle in ["running", "completed"] {
        let previous = dead(task_lead("previous"));
        let current = with_lifecycle(task_lead("current"), lifecycle);
        let assigned = dead(task_lead("assigned"));
        let mut rows = epic_rows();
        rows.sessions.get_mut(PROJECT).unwrap().push(SessionRow {
            id: "s-assigned".to_string(),
            worktree_id: Some("w-epic".to_string()),
            ..Default::default()
        });
        let model = build_model(&rows, &[previous, current, assigned], &[]);
        let view = DaemonView {
            model: &model,
            reachable: true,
        };
        let machine = machine_rows(&view, &Chrome::dark())
            .into_iter()
            .find(|row| row.id == LOCAL_MACHINE)
            .unwrap();
        assert_eq!(model.projects[0].no_live, 1, "{lifecycle}: project");
        assert_eq!(machine.no_live, 1, "{lifecycle}: machine");
        assert_eq!(agent_counts(&view).no_live, 1, "{lifecycle}: status");
    }
}

#[test]
fn superseded_lead_does_not_leave_no_live_on_an_unassigned_rollup() {
    let previous = dead(task_lead("previous"));
    let current = with_lifecycle(task_lead("current"), "completed");
    let model = build_model(&epic_rows(), &[previous, current], &[]);
    let view = DaemonView {
        model: &model,
        reachable: true,
    };
    assert_eq!(epic_tree_state(&model), RowState::Blank);
    assert_eq!(model.projects[0].state, RowState::Blank);
    assert_eq!(model.projects[0].no_live, 0);
    assert_eq!(local_machine_state(&model, true), RowState::Blank);
    assert_eq!(agent_counts(&view).no_live, 0);
}

/// Every lead of the tree dead: the tree is no live.
#[test]
fn epic_tree_whose_leads_are_all_dead_is_no_live() {
    let previous = dead(task_lead("previous"));
    let current = dead(task_lead("current"));
    let model = build_model(&epic_rows(), &[previous, current], &[]);
    assert_eq!(epic_tree_state(&model), RowState::NoLive);
}

/// Rollup order at every level: needs you, active, paused, no live, blank.
#[test]
fn rollup_of_no_members_is_blank() {
    assert_eq!(
        gobby_client::app::sidebar_model::rollup(Vec::<RowState>::new()),
        RowState::Blank
    );
}

/// A roster entry with no terminal has nothing to draw, so it is no agent row.
#[test]
fn entry_without_a_terminal_is_not_an_agent_row() {
    let headless = RosterEntry {
        terminal: None,
        ..running("e", "s", "t")
    };
    let model = build_model(&rows_with(vec![], vec![]), &[headless], &[]);
    assert!(model.agents.is_empty());
}

/// A click on an integration tree's state dot reaches the lead it draws.
#[test]
fn integration_tree_focus_reaches_its_live_lead() {
    let epic_id = "epic-task-id";
    let worktrees = vec![WorktreeRow {
        id: "w-epic".to_string(),
        project_id: PROJECT.to_string(),
        task_id: Some(epic_id.to_string()),
        worktree_path: "/wt/epic-9".to_string(),
        ..Default::default()
    }];
    let lead = RosterEntry {
        task: Some(TaskRef {
            id: epic_id.to_string(),
            reference: Some("#9".to_string()),
            title: Some("Epic nine".to_string()),
            ..Default::default()
        }),
        ..running("lead", "s-lead", "t-lead")
    };
    let model = build_model(&rows_with(worktrees, vec![]), &[lead], &[]);
    let target = worktree_focus_target(&model, "w-epic").expect("the tree has a focus target");
    assert_eq!(target.entry_id, "lead");
}

/// Records each event's level and fields, so a test reads what was logged.
#[derive(Clone, Default)]
struct EventLog(Arc<Mutex<Vec<String>>>);

impl<S: Subscriber> Layer<S> for EventLog {
    fn on_event(&self, event: &Event<'_>, _context: LayerContext<'_, S>) {
        let mut line = event.metadata().level().to_string();
        event.record(&mut FieldText(&mut line));
        self.0.lock().expect("log lock").push(line);
    }
}

struct FieldText<'a>(&'a mut String);

impl Visit for FieldText<'_> {
    fn record_debug(&mut self, field: &Field, value: &dyn Debug) {
        let _ = write!(self.0, " {}={value:?}", field.name());
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        let _ = write!(self.0, " {}={value}", field.name());
    }
}

/// Runs the needs-you transition log over one roster change and returns its lines.
fn needs_you_warnings(
    rows: &SidebarRows,
    before: &[RosterEntry],
    after: &[RosterEntry],
) -> Vec<String> {
    let log = EventLog::default();
    let subscriber = tracing_subscriber::registry().with(log.clone());
    tracing::subscriber::with_default(subscriber, || {
        let before = build_model(rows, before, &[]);
        let after = build_model(rows, after, &[]);
        log_needs_you_transitions(&before, &after);
        log_needs_you_transitions(&after, &after);
    });
    let lines = log.0.lock().expect("log lock").clone();
    lines
}

/// A non-default agent that comes to need you logs one warning naming its
/// session and definition. The default definition logs nothing, and an agent
/// that already needed you is not logged again.
#[test]
fn non_default_needs_you_logs_a_warning_on_transition_only() {
    let rows = rows_with(
        vec![],
        vec![
            SessionRow {
                id: "s-custom".to_string(),
                reference: Some("#42".to_string()),
                ..Default::default()
            },
            SessionRow {
                id: "s-plain".to_string(),
                reference: Some("#43".to_string()),
                ..Default::default()
            },
        ],
    );
    // Only the definition identity decides; the default definition is not logged.
    let custom = RosterEntry {
        definition: Some("custom-seat".to_string()),
        ..with_lifecycle(running("custom", "s-custom", "t-custom"), "awaiting_input")
    };
    let plain = RosterEntry {
        definition: Some("default".to_string()),
        ..with_lifecycle(running("plain", "s-plain", "t-plain"), "awaiting_input")
    };
    let lines = needs_you_warnings(&rows, &[], &[custom, plain]);
    assert_eq!(lines.len(), 1, "one transition, one line: {lines:?}");
    assert!(lines[0].starts_with("WARN"), "{}", lines[0]);
    assert!(lines[0].contains("custom-seat"), "{}", lines[0]);
    assert!(lines[0].contains("#42"), "{}", lines[0]);
}

#[test]
fn needs_you_log_names_the_session_id_when_the_session_is_not_fetched() {
    let entry = RosterEntry {
        definition: Some("custom-seat".to_string()),
        ..with_lifecycle(running("bare", "s-bare", "t-bare"), "awaiting_input")
    };
    let lines = needs_you_warnings(&rows_with(vec![], vec![]), &[], &[entry]);
    assert_eq!(lines.len(), 1, "one transition, one line: {lines:?}");
    assert!(lines[0].contains("s-bare"), "{}", lines[0]);
}

#[test]
fn ended_assignment_without_a_terminal_is_no_live_in_its_project() {
    let ended = RosterEntry {
        terminal: None,
        task: Some(TaskRef {
            id: "task-ended".to_string(),
            reference: Some("#7".to_string()),
            title: None,
            ..Default::default()
        }),
        ..with_lifecycle(running("ended", "s-ended", "t-unused"), "dead")
    };
    let model = build_model(&rows_with(vec![], vec![]), &[ended], &[]);
    assert_eq!(model.agents.len(), 1, "a dead assignment keeps its row");
    assert_eq!(model.agents[0].state, RowState::NoLive);
    let project = &model.projects[0];
    assert_eq!(project.state, RowState::NoLive);
    assert_eq!(project.no_live, 1);
}

#[test]
fn epic_tree_whose_terminal_less_leads_are_dead_is_no_live() {
    let previous = RosterEntry {
        terminal: None,
        ..with_lifecycle(task_lead("previous"), "dead")
    };
    let current = RosterEntry {
        terminal: None,
        ..with_lifecycle(task_lead("current"), "dead")
    };
    let model = build_model(&epic_rows(), &[previous, current], &[]);
    assert_eq!(epic_tree_state(&model), RowState::NoLive);
}

#[test]
fn worktree_focus_skips_an_ended_assignment_that_has_no_terminal() {
    let worktrees = vec![WorktreeRow {
        id: "w-gone".to_string(),
        project_id: PROJECT.to_string(),
        worktree_path: "/wt/gone".to_string(),
        ..Default::default()
    }];
    let sessions = vec![SessionRow {
        id: "s-gone".to_string(),
        worktree_id: Some("w-gone".to_string()),
        ..Default::default()
    }];
    let ended = RosterEntry {
        terminal: None,
        ..with_lifecycle(running("gone", "s-gone", "t-gone"), "dead")
    };
    let model = build_model(&rows_with(worktrees, sessions), &[ended], &[]);
    assert_eq!(model.agents.len(), 1, "a dead assignment keeps its row");
    // Focus would otherwise hand the row's empty terminal id to the daemon.
    assert!(worktree_focus_target(&model, "w-gone").is_none());
}
