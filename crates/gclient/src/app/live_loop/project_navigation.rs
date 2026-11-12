//! Latest-intent project attachment and its deferred UI action.

use tokio::sync::mpsc::UnboundedSender;

use crate::daemon::{Daemon, Generation, LiveDaemon, TerminalRow, WorkspaceSnapshot, WorktreeRow};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::{dialogs::Dialog, Chrome};

use super::super::Workspace;
use super::daemon_ops::op_tag;
use super::jobs::{spawn_job, JobOutcome, JobResult};
use super::menu::{ContextMenuKind, MenuAction};
use super::modal_input::close_modal;
use super::sync_live_chrome;

#[derive(Debug, Clone)]
pub enum ProjectAction {
    Focus,
    OpenWorktree(String),
    RemoveWorktree(String),
    CloseProject(String),
    FocusAgent(String),
    OpenAgent(String),
    Menu(ContextMenuKind, MenuAction),
    OpenProject { path: String, create: bool },
    FocusTerminal(String),
    CreateWorktree(WorktreeRequest),
    OpenCreatedWorktree(WorktreeRow),
}

#[derive(Debug, Clone, PartialEq)]
pub struct WorktreeRequest {
    project: String,
    branch: String,
    base: Option<String>,
}

#[derive(Debug)]
struct Request {
    id: u64,
    generation: Generation,
    target: Target,
    then: ProjectAction,
}

#[derive(Debug)]
enum Target {
    CreateWorktree(WorktreeRequest),
    Project {
        project: String,
        workspace: Option<String>,
    },
    Path {
        path: String,
        home: std::path::PathBuf,
        create: bool,
    },
    Terminal {
        id: String,
        known: bool,
        attention: bool,
        reuse: bool,
    },
}

#[derive(Debug, Default)]
pub(in crate::app) struct ProjectNavigation {
    serial: u64,
    prepared_terminal: Option<String>,
    active: Option<ActiveRequest>,
    pending: Option<Request>,
    ready: Option<(u64, Generation, ProjectAction)>,
}

#[derive(Debug)]
struct ActiveRequest {
    request: u64,
    generation: Generation,
    intent: u64,
    path: Option<(String, bool)>,
    worktree: Option<WorktreeRequest>,
}

impl ProjectNavigation {
    fn expire(&mut self, generation: Generation) {
        if self
            .active
            .as_ref()
            .is_some_and(|active| active.generation != generation)
        {
            self.active = None;
        }
        if self
            .pending
            .as_ref()
            .is_some_and(|request| request.generation != generation)
        {
            self.pending = None;
        }
    }
}

/// Return true when attachment must finish before the caller continues.
pub(super) fn defer_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project: &str,
    force_default: bool,
    then: ProjectAction,
) -> bool {
    let selected = workspace.project_id() == Some(project);
    let attached_default = workspace
        .workspace_model()
        .is_some_and(|model| model.workspace.default_project_id.as_deref() == Some(project));
    let generation = workspace.daemon().generation();
    workspace.project_navigation.expire(generation);
    let busy = workspace.project_navigation.active.is_some();
    workspace.project_navigation.serial += 1;
    workspace.project_navigation.pending = None;
    workspace.project_navigation.ready = None;
    if !busy && ((selected && !force_default) || attached_default) {
        select_project(workspace, chrome, project);
        return false;
    }
    let target = (selected && !force_default)
        .then(|| {
            workspace
                .workspace_model()
                .map(|model| model.workspace.id.clone())
        })
        .flatten();
    workspace.project_navigation.pending = Some(Request {
        id: workspace.project_navigation.serial,
        generation,
        target: Target::Project {
            project: project.to_owned(),
            workspace: target,
        },
        then,
    });
    true
}

pub(super) fn select_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project: &str,
) {
    if workspace.project_id() != Some(project) {
        if let Some(current) = workspace.project_id() {
            chrome.focus_project(current);
        }
        workspace.select_project(project);
        workspace.request_roster(project);
        workspace.request_focused_sessions();
    }
    chrome.focus_project(project);
    chrome.sidebar.expanded_project = Some(project.to_owned());
    sync_live_chrome(workspace, chrome);
}

/// Serialize remote attaches: a superseded remote reply cannot leave the daemon
/// subscribed to a different workspace than the latest request installs locally.
pub fn dispatch(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
) -> bool {
    let generation = workspace.daemon().generation();
    workspace.project_navigation.expire(generation);
    if workspace.project_navigation.active.is_some() {
        return false;
    }
    let Some(request) = workspace.project_navigation.pending.take() else {
        return false;
    };
    if request.generation != generation {
        return false;
    }
    workspace.project_navigation.active = Some(ActiveRequest {
        request: request.id,
        generation,
        intent: request.id,
        path: match &request.target {
            Target::Path { path, create, .. } => Some((path.clone(), *create)),
            _ => None,
        },
        worktree: match &request.target {
            Target::CreateWorktree(request) => Some(request.clone()),
            _ => None,
        },
    });
    let daemon = workspace.daemon().clone();
    let node = workspace.attach_target().node.clone();
    let roster =
        workspace.issue_roster_fetch(workspace.project_id().unwrap_or_default().to_owned());
    let label = match &request.target {
        Target::CreateWorktree(request) => {
            format!("worktree {} in project {}", request.branch, request.project)
        }
        Target::Project { project, .. } => format!("project {project}"),
        Target::Path { path, .. } => format!("project at {path}"),
        Target::Terminal { id, .. } => format!("terminal {id}"),
    };
    spawn_job(outcomes, op_tag(&daemon), async move {
        let result = match request.target {
            Target::CreateWorktree(request) => daemon
                .create_worktree(&request.project, &request.branch, request.base.as_deref())
                .await
                .map(|row| Prepared {
                    created: Some(row),
                    ..Prepared::default()
                })
                .map_err(|error| super::project_operations::daemon_reason(&error)),
            Target::Project { project, workspace } => daemon
                .attach_workspace(
                    node.as_deref(),
                    workspace.as_deref(),
                    workspace.is_none().then_some(project.as_str()),
                )
                .await
                .map(|snapshot| Prepared {
                    snapshot: Some(snapshot),
                    project: Some(project),
                    ..Prepared::default()
                })
                .map_err(|error| error.to_string()),
            Target::Path { path, home, create } => {
                async {
                    let project =
                        super::project_paths::resolve_project(&daemon, &path, &home, create)
                            .await?;
                    let snapshot = daemon
                        .attach_workspace(node.as_deref(), None, Some(&project.id))
                        .await
                        .map_err(|error| error.to_string())?;
                    Ok(Prepared {
                        snapshot: Some(snapshot),
                        project: Some(project.id),
                        ..Prepared::default()
                    })
                }
                .await
            }
            Target::Terminal {
                id,
                known,
                attention,
                reuse,
            } => {
                super::terminal_location::prepare(
                    &daemon,
                    node.as_deref(),
                    &id,
                    known,
                    attention,
                    reuse,
                    roster,
                )
                .await
            }
        };
        JobResult::ProjectAttached(ProjectAttachment {
            request: request.id,
            generation,
            label,
            then: request.then,
            result: result.map(Box::new),
        })
    });
    true
}

#[derive(Debug)]
pub struct ProjectAttachment {
    request: u64,
    generation: Generation,
    label: String,
    then: ProjectAction,
    result: Result<Box<Prepared>, String>,
}

#[derive(Debug, Default)]
pub(super) struct Prepared {
    pub created: Option<WorktreeRow>,
    pub snapshot: Option<WorkspaceSnapshot>,
    pub project: Option<String>,
    pub terminal: Option<TerminalRow>,
    pub terminal_id: Option<String>,
    pub roster: Option<super::super::live::relist::Relist>,
    pub missing: bool,
}

pub(super) fn create_worktree(
    workspace: &mut Workspace<LiveDaemon>,
    project: &str,
    branch: &str,
    base: Option<&str>,
) {
    let generation = workspace.daemon().generation();
    let navigation = &mut workspace.project_navigation;
    navigation.expire(generation);
    let request = WorktreeRequest {
        project: project.to_owned(),
        branch: branch.trim().to_owned(),
        base: base.map(str::to_owned),
    };
    if let Some(active) = navigation
        .active
        .as_mut()
        .filter(|active| active.worktree.as_ref() == Some(&request))
    {
        if active.intent != navigation.serial {
            navigation.serial += 1;
            active.intent = navigation.serial;
            navigation.pending = None;
            navigation.ready = None;
        }
        return;
    }
    if navigation.pending.as_ref().is_some_and(
        |pending| matches!(&pending.target, Target::CreateWorktree(value) if value == &request),
    ) {
        return;
    }
    queue(
        workspace,
        Target::CreateWorktree(request.clone()),
        ProjectAction::CreateWorktree(request),
    );
}

pub(super) fn submit_path(
    workspace: &mut Workspace<LiveDaemon>,
    path: &str,
    home: std::path::PathBuf,
    create: bool,
) {
    let generation = workspace.daemon().generation();
    let navigation = &mut workspace.project_navigation;
    navigation.expire(generation);
    if let Some(active) = navigation.active.as_mut().filter(|active| {
        active
            .path
            .as_ref()
            .is_some_and(|(current, mode)| current == path && *mode == create)
    }) {
        if active.intent != navigation.serial {
            navigation.serial += 1;
            active.intent = navigation.serial;
            navigation.pending = None;
            navigation.ready = None;
        }
        return;
    }
    if navigation.pending.as_ref().is_some_and(|request| matches!(&request.target, Target::Path { path: pending, create: mode, .. } if pending == path && *mode == create)) {
        return;
    }
    queue(
        workspace,
        Target::Path {
            path: path.to_owned(),
            home,
            create,
        },
        ProjectAction::OpenProject {
            path: path.to_owned(),
            create,
        },
    );
}

fn queue(workspace: &mut Workspace<LiveDaemon>, target: Target, then: ProjectAction) {
    let generation = workspace.daemon().generation();
    let navigation = &mut workspace.project_navigation;
    navigation.expire(generation);
    navigation.serial += 1;
    navigation.ready = None;
    navigation.prepared_terminal = None;
    navigation.pending = Some(Request {
        id: navigation.serial,
        generation,
        target,
        then,
    });
}

pub(super) fn cancel_path(workspace: &mut Workspace<LiveDaemon>) {
    let navigation = &mut workspace.project_navigation;
    if navigation
        .active
        .as_ref()
        .is_some_and(|active| active.path.is_some() || active.worktree.is_some())
        || navigation.pending.as_ref().is_some_and(|request| {
            matches!(
                request.target,
                Target::Path { .. } | Target::CreateWorktree(_)
            )
        })
    {
        navigation.serial += 1;
        navigation.pending = None;
        navigation.ready = None;
        if let Some(active) = &mut navigation.active {
            active.path = None;
            active.worktree = None;
        }
    }
}

pub(super) fn terminal_prepared(workspace: &Workspace<LiveDaemon>, terminal: &str) -> bool {
    workspace.project_navigation.prepared_terminal.as_deref() == Some(terminal)
}

pub(super) fn defer_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    terminal: &str,
    reuse: bool,
    then: ProjectAction,
) -> bool {
    workspace
        .project_navigation
        .expire(workspace.daemon().generation());
    if workspace.project_navigation.prepared_terminal.as_deref() == Some(terminal) {
        workspace.project_navigation.prepared_terminal = None;
        return false;
    }
    let project = workspace.workspace_model().and_then(|model| {
        let pane = model
            .panes()
            .find(|pane| pane.terminal_id.as_deref() == Some(terminal))?;
        model.tab(&pane.tab_id).map(|tab| tab.project_id.clone())
    });
    if workspace.project_navigation.active.is_none()
        && workspace.pane_for_terminal(terminal).is_some()
        && project.is_some()
    {
        workspace.project_navigation.serial += 1;
        workspace.project_navigation.pending = None;
        workspace.project_navigation.ready = None;
        if let Some(project) = project {
            select_project(workspace, chrome, &project);
        }
        return false;
    }
    let known = workspace.pane_for_terminal(terminal).is_some();
    let attention = workspace
        .sidebar()
        .agents
        .iter()
        .any(|agent| agent.terminal_id == terminal);
    queue(
        workspace,
        Target::Terminal {
            id: terminal.to_owned(),
            known,
            attention,
            reuse,
        },
        then,
    );
    true
}

pub fn apply(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    current_generation: bool,
    attachment: ProjectAttachment,
) {
    let navigation = &mut workspace.project_navigation;
    if !navigation.active.as_ref().is_some_and(|active| {
        active.request == attachment.request && active.generation == attachment.generation
    }) {
        return;
    }
    let active = navigation.active.take().expect("matched active request");
    if matches!(attachment.then, ProjectAction::CreateWorktree(_)) {
        workspace.pending_sidebar.projects = true;
    }
    let navigation = &mut workspace.project_navigation;
    if !current_generation || navigation.serial != active.intent {
        return;
    }
    if let ProjectAction::OpenProject { path, create } = &attachment.then {
        let matches = match &chrome.dialog {
            Some(Dialog::NewProject { path: current, .. }) => *create && current == path,
            Some(Dialog::OpenProject { path: current, .. }) => !*create && current == path,
            _ => false,
        };
        if !matches {
            return;
        }
    }
    if let ProjectAction::CreateWorktree(request) = &attachment.then {
        if !matches!(&chrome.dialog, Some(Dialog::NewWorktree { project_id, branch, base, .. })
            if project_id == &request.project && branch.trim() == request.branch
                && base.as_str() == request.base.as_deref().unwrap_or_default())
            || workspace.project_id() != Some(request.project.as_str())
        {
            if let Err(error) = attachment.result {
                chrome.notify(Toast::warning(format!(
                    "Cannot create {}: {error}",
                    attachment.label
                )));
            }
            return;
        }
    }
    match attachment.result {
        Ok(prepared) => {
            let prepared = *prepared;
            workspace.pending_sidebar.projects |= prepared.snapshot.is_some()
                || prepared.project.is_some()
                || prepared.created.is_some();
            let then = if let Some(row) = prepared.created {
                close_modal(chrome);
                ProjectAction::OpenCreatedWorktree(row)
            } else {
                attachment.then
            };
            if let Some(snapshot) = prepared.snapshot {
                let id = snapshot.workspace.id.clone();
                workspace.apply_workspace_snapshot(snapshot);
                let mut target = workspace.attach_target().clone();
                target.workspace = Some(id);
                target.project_id = None;
                workspace.set_attach_target(target);
            }
            if let Some(project) = prepared.project {
                select_project(workspace, chrome, &project);
            }
            if let Some(roster) = prepared.roster {
                workspace.roster_landed |= workspace.apply_relist(roster);
            }
            if let Some(row) = prepared.terminal {
                workspace.install_unresolved_rows(vec![row]);
                workspace.roster_landed = true;
            }
            workspace.project_navigation.prepared_terminal = prepared.terminal_id;
            sync_live_chrome(workspace, chrome);
            if prepared.missing {
                chrome.notify(Toast::warning("Terminal is no longer available"));
                return;
            }
            workspace.project_navigation.ready = Some((active.intent, attachment.generation, then));
        }
        Err(error) => {
            if matches!(
                attachment.then,
                ProjectAction::OpenProject { .. } | ProjectAction::CreateWorktree(_)
            ) {
                super::project_operations::set_dialog_error(chrome, error);
            } else {
                chrome.notify(Toast::warning(format!(
                    "Cannot open {}: {error}",
                    attachment.label
                )));
            }
        }
    }
}

pub async fn resume(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
) -> Result<(), FrameError> {
    let Some((request, generation, action)) = workspace.project_navigation.ready.take() else {
        return Ok(());
    };
    if workspace.project_navigation.serial != request
        || workspace.daemon().generation() != generation
    {
        return Ok(());
    }
    match action {
        ProjectAction::CreateWorktree(_) => {}
        ProjectAction::OpenCreatedWorktree(row) => {
            super::lifecycle::spawn_live_shell(
                workspace,
                chrome,
                outcomes,
                super::super::Placement::Tab,
                Some(row.worktree_path),
                Some(row.id),
            )
            .await?;
        }
        ProjectAction::Focus => {}
        ProjectAction::FocusTerminal(id) => {
            super::projects::focus_terminal(workspace, chrome, outcomes, &id).await?
        }
        ProjectAction::OpenProject { .. } => {
            close_modal(chrome);
        }
        ProjectAction::OpenWorktree(id) => {
            super::projects::open_worktree(workspace, chrome, outcomes, &id).await?
        }
        ProjectAction::RemoveWorktree(id) => {
            super::project_operations::remove_worktree(workspace, chrome, outcomes, &id).await?
        }
        ProjectAction::CloseProject(id) => {
            super::project_operations::close_project_confirmed(workspace, chrome, outcomes, &id)
                .await?
        }
        ProjectAction::FocusAgent(id) => {
            super::projects::focus_agent(workspace, chrome, outcomes, &id).await?
        }
        ProjectAction::OpenAgent(id) => {
            super::projects::open_agent_in_new_tab(workspace, chrome, outcomes, &id).await?
        }
        ProjectAction::Menu(kind, action) => {
            super::menu_dispatch::apply_live_menu_action(workspace, chrome, outcomes, kind, action)
                .await?;
        }
    }
    Ok(())
}
