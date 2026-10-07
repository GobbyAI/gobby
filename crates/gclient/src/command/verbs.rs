use super::refs::{self, Rows, Target};
use super::{CommandEnv, CommandError, Parsed};
use crate::daemon::{project_rows, Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use serde_json::Value;
use tokio::time::{Duration, Instant};

#[derive(Debug)]
pub(super) struct CommandOutput {
    pub result: Value,
    pub plain: String,
    pub status: i32,
}

#[derive(Debug)]
pub(super) enum Action {
    List {
        workspace: String,
    },
    NewTab {
        workspace: String,
        project: String,
        title: Option<String>,
        prefix: Option<String>,
    },
    Split {
        pane: String,
        axis: LayoutAxis,
        cmd: Option<String>,
        target: Option<Target>,
    },
    Op {
        op: WorkspaceOp,
        kind: OutputKind,
        target: Option<Target>,
    },
}

#[derive(Debug)]
pub(super) enum OutputKind {
    Json,
    Capture,
    Wait,
    CreatedTab(Option<String>),
    CreatedPane(Option<String>),
}

fn required(value: Option<String>, name: &str) -> Result<String, CommandError> {
    value.ok_or_else(|| CommandError::usage(format!("missing {name}")))
}

fn pane_ref(parsed: &mut Parsed, env: &CommandEnv) -> Result<String, CommandError> {
    required(
        parsed.position().or_else(|| env.pane_ref.clone()),
        "pane REF",
    )
}

fn pane_ref_before_value(parsed: &mut Parsed, env: &CommandEnv) -> Result<String, CommandError> {
    if parsed.positions.len() == 1 {
        return required(env.pane_ref.clone(), "pane REF");
    }
    pane_ref(parsed, env)
}

/// The workspace check a pane or tab REF needs under `--workspace`.
fn ref_target(
    reference: &str,
    parsed: &mut Parsed,
    env: &CommandEnv,
    rows: Rows,
) -> Result<Option<Target>, CommandError> {
    Target::new(
        reference,
        parsed.take("--workspace"),
        env.workspace_id.as_ref(),
        rows,
    )
}

/// Which rows a title or kill REF may name under its `--kind`.
fn kind_rows(kind: Option<&str>) -> Rows {
    match kind {
        Some("tab") => Rows::Tabs,
        Some("pane") => Rows::Panes,
        _ => Rows::Either,
    }
}

fn workspace(parsed: &mut Parsed, env: &CommandEnv) -> Result<String, CommandError> {
    required(
        parsed
            .take("--workspace")
            .or_else(|| env.workspace_id.clone()),
        "--workspace",
    )
}

fn axis(parsed: &mut Parsed) -> Result<LayoutAxis, CommandError> {
    match (parsed.switch("--right"), parsed.switch("--down")) {
        (true, false) => Ok(LayoutAxis::Horizontal),
        (false, true) => Ok(LayoutAxis::Vertical),
        _ => Err(CommandError::usage(
            "choose exactly one of --right and --down",
        )),
    }
}

fn positive_f64(value: &str, name: &str) -> Result<f64, CommandError> {
    let number: f64 = value
        .parse()
        .map_err(|_| CommandError::usage(format!("invalid {name}")))?;
    if !number.is_finite() || number <= 0.0 {
        return Err(CommandError::usage(format!("invalid {name}")));
    }
    Ok(number)
}

fn ref_depth(reference: &str) -> usize {
    if uuid::Uuid::parse_str(reference).is_ok() {
        4
    } else {
        reference.split(':').count()
    }
}

fn numeric_prefix(reference: &str, count: usize) -> Option<String> {
    let parts: Vec<_> = reference.split(':').collect();
    (parts.len() >= count
        && parts[..count]
            .iter()
            .all(|part| part.parse::<u64>().is_ok()))
    .then(|| parts[..count].join(":"))
}

impl Action {
    pub(super) fn parse(mut args: Parsed, env: &CommandEnv) -> Result<Self, CommandError> {
        let verb = args.verb.clone();
        let action = match verb.as_str() {
            "list" => Self::List {
                workspace: workspace(&mut args, env)?,
            },
            "new-tab" => {
                let workspace = workspace(&mut args, env)?;
                let prefix = numeric_prefix(&workspace, 2).or_else(|| {
                    (env.workspace_id.as_deref() == Some(workspace.as_str()))
                        .then(|| env.pane_ref.as_deref().and_then(|s| numeric_prefix(s, 2)))
                        .flatten()
                });
                Self::NewTab {
                    workspace,
                    prefix,
                    project: required(args.take("--project"), "--project")?,
                    title: args.take("--name"),
                }
            }
            "split" => {
                let pane = pane_ref(&mut args, env)?;
                Self::Split {
                    target: ref_target(&pane, &mut args, env, Rows::Panes)?,
                    pane,
                    axis: axis(&mut args)?,
                    cmd: args.take("--cmd"),
                }
            }
            "resize" => {
                let pane = pane_ref_before_value(&mut args, env)?;
                let ratio = positive_f64(&required(args.position(), "RATIO")?, "RATIO")?;
                if ratio >= 1.0 {
                    return Err(CommandError::usage("RATIO must be less than 1"));
                }
                Self::Op {
                    target: ref_target(&pane, &mut args, env, Rows::Panes)?,
                    op: WorkspaceOp::PaneResize {
                        pane,
                        ratio,
                        node: None,
                    },
                    kind: OutputKind::Json,
                }
            }
            "title" => {
                let reference = pane_ref_before_value(&mut args, env)?;
                let text = required(args.position(), "TEXT")?;
                let kind = args.take("--kind");
                if kind.as_deref().is_some_and(|s| s != "tab" && s != "pane") {
                    return Err(CommandError::usage("--kind requires tab or pane"));
                }
                let target = ref_target(&reference, &mut args, env, kind_rows(kind.as_deref()))?;
                let tab = kind.as_deref() == Some("tab")
                    || (kind.is_none() && ref_depth(&reference) == 3);
                let op = if tab {
                    WorkspaceOp::TabRename {
                        tab: reference,
                        title: Some(text),
                        node: None,
                    }
                } else {
                    WorkspaceOp::PaneRename {
                        pane: reference,
                        label: Some(text),
                        node: None,
                    }
                };
                Self::Op {
                    op,
                    kind: OutputKind::Json,
                    target,
                }
            }
            "select" => {
                let reference = pane_ref(&mut args, env)?;
                let workspace_override = args.take("--workspace");
                let tab_hint = args.take("--tab-ref").or_else(|| env.tab_id.clone());
                let target = Target::new(
                    &reference,
                    workspace_override.clone(),
                    env.workspace_id.as_ref(),
                    Rows::Either,
                )?;
                let parts: Vec<_> = reference.split(':').collect();
                // A short ID's tab and pane come from its resolved row.
                let (workspace, tab, pane) = if let Some(short) =
                    target.as_ref().filter(|_| refs::is_short_id(&reference))
                {
                    (short.workspace.clone(), reference.clone(), None)
                } else if parts.len() == 3 || parts.len() == 4 {
                    let workspace = parts[..2].join(":");
                    let tab = parts[..3].join(":");
                    let pane = (parts.len() == 4).then_some(reference);
                    (workspace, tab, pane)
                } else {
                    let workspace = required(
                        workspace_override.or_else(|| env.workspace_id.clone()),
                        "--workspace",
                    )?;
                    if let Some(tab) = tab_hint {
                        (workspace, tab, Some(reference))
                    } else {
                        (workspace, reference, None)
                    }
                };
                Self::Op {
                    op: WorkspaceOp::WorkspaceSelect {
                        workspace,
                        tab,
                        pane,
                        node: None,
                    },
                    kind: OutputKind::Json,
                    target,
                }
            }
            "send-keys" => {
                let pane = pane_ref_before_value(&mut args, env)?;
                let text = required(args.position(), "TEXT")?;
                let submit = args.switch("--enter");
                Self::Op {
                    target: ref_target(&pane, &mut args, env, Rows::Panes)?,
                    op: WorkspaceOp::PaneSendText {
                        pane,
                        text,
                        submit: Some(submit),
                        idempotency_key: None,
                        node: None,
                    },
                    kind: OutputKind::Json,
                }
            }
            "capture-pane" => {
                let pane = pane_ref(&mut args, env)?;
                let lines = args
                    .take("--lines")
                    .map(|s| {
                        s.parse::<u32>()
                            .map_err(|_| CommandError::usage("invalid --lines"))
                    })
                    .transpose()?;
                if lines == Some(0) {
                    return Err(CommandError::usage("--lines must be positive"));
                }
                Self::Op {
                    target: ref_target(&pane, &mut args, env, Rows::Panes)?,
                    op: WorkspaceOp::PaneRead {
                        pane,
                        lines,
                        node: None,
                    },
                    kind: OutputKind::Capture,
                }
            }
            "wait-for-output" => {
                let pane = pane_ref(&mut args, env)?;
                let pattern = required(args.take("--pattern"), "--pattern")?;
                let timeout_seconds = positive_f64(
                    &args.take("--timeout").unwrap_or_else(|| "30".into()),
                    "--timeout",
                )?;
                let poll_interval_seconds = args
                    .take("--interval")
                    .map(|s| positive_f64(&s, "--interval"))
                    .transpose()?;
                Self::Op {
                    target: ref_target(&pane, &mut args, env, Rows::Panes)?,
                    op: WorkspaceOp::PaneWaitForOutput {
                        pane,
                        pattern,
                        timeout_seconds,
                        poll_interval_seconds,
                        node: None,
                    },
                    kind: OutputKind::Wait,
                }
            }
            "kill" => {
                let reference = pane_ref(&mut args, env)?;
                let kind = args.take("--kind");
                if kind.as_deref().is_some_and(|s| s != "tab" && s != "pane") {
                    return Err(CommandError::usage("--kind requires tab or pane"));
                }
                let target = ref_target(&reference, &mut args, env, kind_rows(kind.as_deref()))?;
                let tab = kind.as_deref() == Some("tab")
                    || (kind.is_none() && ref_depth(&reference) == 3);
                let op = if tab {
                    WorkspaceOp::TabClose {
                        tab: reference,
                        node: None,
                    }
                } else {
                    WorkspaceOp::PaneClose {
                        pane: reference,
                        node: None,
                    }
                };
                Self::Op {
                    op,
                    kind: OutputKind::Json,
                    target,
                }
            }
            _ => return Err(CommandError::usage("unknown verb")),
        };
        args.finish()?;
        Ok(action)
    }

    pub(super) async fn run(self, url: &str, token: &str) -> Result<CommandOutput, CommandError> {
        let daemon = LiveDaemon::connect(url, token.to_owned())
            .await
            .map_err(daemon_error)?;
        let output = match self.resolved(&daemon).await? {
            Self::List { workspace } => {
                let snapshot = daemon
                    .attach_workspace(None, Some(&workspace), None)
                    .await
                    .map_err(daemon_error)?;
                let plain = snapshot
                    .tabs
                    .iter()
                    .map(|tab| {
                        let panes = snapshot
                            .panes
                            .iter()
                            .filter(|pane| pane.tab_id == tab.id)
                            .map(|pane| {
                                format!(
                                    "{}  {}  {}",
                                    pane.id,
                                    pane.label.as_deref().unwrap_or(""),
                                    pane.terminal_id.as_deref().unwrap_or("")
                                )
                            })
                            .collect::<Vec<_>>()
                            .join("\n");
                        format!(
                            "{}  {}\n{}",
                            tab.id,
                            tab.title.as_deref().unwrap_or(""),
                            panes
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                Ok(CommandOutput {
                    result: serde_json::to_value(snapshot)
                        .map_err(|e| CommandError::connection(e.to_string()))?,
                    plain,
                    status: 0,
                })
            }
            Self::NewTab {
                workspace,
                project,
                title,
                prefix,
            } => {
                let project_id = resolve_project(url, token, &project).await?;
                let reply = daemon
                    .workspace_op(WorkspaceOp::TabCreate {
                        workspace,
                        project_id,
                        worktree_id: None,
                        title,
                        terminal_id: None,
                        cwd: None,
                        node: None,
                    })
                    .await
                    .map_err(daemon_error)?;
                output(reply.result, OutputKind::CreatedTab(prefix))
            }
            Self::Split {
                pane, axis, cmd, ..
            } => {
                let prefix = numeric_prefix(&pane, 3);
                let reply = daemon
                    .workspace_op(WorkspaceOp::PaneSplit {
                        pane,
                        axis,
                        terminal_id: None,
                        cwd: None,
                        node: None,
                    })
                    .await
                    .map_err(daemon_error)?;
                let result = reply.result;
                if let Some(cmd) = cmd {
                    let new_pane = created_ref(&result, "panes")?;
                    run_op(
                        &daemon,
                        WorkspaceOp::PaneSendText {
                            pane: new_pane,
                            text: cmd,
                            submit: Some(true),
                            idempotency_key: None,
                            node: None,
                        },
                    )
                    .await?;
                }
                output(result, OutputKind::CreatedPane(prefix))
            }
            Self::Op { op, kind, .. } => output(run_op(&daemon, op).await?, kind),
        };
        let _ = daemon.close(Instant::now() + Duration::from_secs(2)).await;
        output
    }

    /// The action with its REF checked against the selected workspace and
    /// a short ID swapped for the row it names.
    async fn resolved(self, daemon: &LiveDaemon) -> Result<Self, CommandError> {
        let (Self::Split {
            target: Some(target),
            ..
        }
        | Self::Op {
            target: Some(target),
            ..
        }) = &self
        else {
            return Ok(self);
        };
        let snapshot = daemon
            .attach_workspace(None, Some(&target.workspace), None)
            .await
            .map_err(daemon_error)?;
        let Some(row) = target.resolve(&snapshot)? else {
            return Ok(self);
        };
        Ok(match self {
            Self::Split { axis, cmd, .. } => {
                let refs::Row::Pane { id, .. } = row else {
                    unreachable!("a split resolves panes only");
                };
                Self::Split {
                    pane: id,
                    axis,
                    cmd,
                    target: None,
                }
            }
            Self::Op { op, kind, .. } => Self::Op {
                op: refs::retarget(op, row),
                kind,
                target: None,
            },
            other => other,
        })
    }
}

/// How long a submitted pane text waits for its verdict. The daemon answers only
/// after it verified the Enter: the write, a 1.5s gap, then Enters re-sent for up
/// to 30s while the CLI keeps the text in its composer (`SUBMIT_HELD_RETRY_SECONDS`
/// in src/gobby/terminals/pane_io.py). The normal request deadline gave up
/// mid-verification and lost the verdict (#23730).
const VERIFIED_SUBMIT_DEADLINE: Duration = Duration::from_secs(60);

/// The daemon's `TEXT_NOT_SUBMITTED_ERROR_CODE`: the CLI kept the text in its
/// composer through every Enter.
const TEXT_NOT_SUBMITTED: &str = "command_not_submitted";

/// Run one workspace op under its deadline and return the reply's result. A
/// submitted pane text the daemon proved was not submitted is an error, not a
/// result; one it could not verify is returned for the caller to read.
async fn run_op(daemon: &LiveDaemon, op: WorkspaceOp) -> Result<Value, CommandError> {
    let submit = matches!(
        op,
        WorkspaceOp::PaneSendText {
            submit: Some(true),
            ..
        }
    );
    let deadline = match &op {
        WorkspaceOp::PaneWaitForOutput {
            timeout_seconds, ..
        } => Some(Duration::from_secs_f64(timeout_seconds.min(300.0) + 5.0)),
        _ if submit => Some(VERIFIED_SUBMIT_DEADLINE),
        _ => None,
    };
    let result = match deadline {
        Some(deadline) => daemon.workspace_op_with_deadline(op, deadline).await,
        None => daemon.workspace_op(op).await,
    }
    .map_err(daemon_error)?
    .result;
    if submit && result.get("error_code").and_then(Value::as_str) == Some(TEXT_NOT_SUBMITTED) {
        let detail = result
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or("the CLI never submitted the text");
        return Err(CommandError {
            code: 1,
            message: format!("{TEXT_NOT_SUBMITTED}: {detail}"),
        });
    }
    Ok(result)
}

fn created_ref(result: &Value, key: &str) -> Result<String, CommandError> {
    result
        .get(key)
        .and_then(Value::as_array)
        .and_then(|rows| rows.first())
        .and_then(|row| row.get("id"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| CommandError::connection(format!("missing {key} in reply")))
}

fn output(result: Value, kind: OutputKind) -> Result<CommandOutput, CommandError> {
    let status = if matches!(kind, OutputKind::Wait) {
        match result.get("reason").and_then(Value::as_str) {
            Some("matched") => 0,
            Some("timeout") => 1,
            Some("pane_lost") => 2,
            _ => return Err(CommandError::connection("invalid wait reply")),
        }
    } else {
        0
    };
    let plain = match kind {
        OutputKind::CreatedTab(prefix) => {
            let pane = created_ref(&result, "panes")?;
            let tab = created_ref(&result, "tabs")?;
            if let (Some(prefix), Some(tab_no), Some(pane_no)) = (
                prefix,
                result["tabs"][0]["ref"].as_u64(),
                result["panes"][0]["ref"].as_u64(),
            ) {
                format!("{prefix}:{tab_no} {prefix}:{tab_no}:{pane_no}")
            } else {
                format!("{tab} {pane}")
            }
        }
        OutputKind::CreatedPane(prefix) => {
            let pane = created_ref(&result, "panes")?;
            if let (Some(prefix), Some(pane_no)) = (prefix, result["panes"][0]["ref"].as_u64()) {
                format!("{prefix}:{pane_no}")
            } else {
                pane
            }
        }
        OutputKind::Capture => result
            .get("text")
            .and_then(Value::as_str)
            .ok_or_else(|| CommandError::connection("invalid capture reply"))?
            .to_owned(),
        _ => serde_json::to_string(&result).map_err(|e| CommandError::connection(e.to_string()))?,
    };
    Ok(CommandOutput {
        result,
        plain,
        status,
    })
}

async fn resolve_project(url: &str, token: &str, project: &str) -> Result<String, CommandError> {
    if uuid::Uuid::parse_str(project).is_ok() {
        return Ok(project.to_owned());
    }
    let projects = project_rows(url, token).await.map_err(daemon_error)?;
    projects
        .into_iter()
        .find(|row| row.name == project || row.display_name == project)
        .map(|row| row.id)
        .ok_or_else(|| CommandError::usage(format!("unknown project: {project}")))
}

fn daemon_error(error: DaemonError) -> CommandError {
    match error {
        DaemonError::Workspace(refused) => {
            let code = serde_json::to_value(refused.code)
                .ok()
                .and_then(|value| value.as_str().map(str::to_owned))
                .unwrap_or_else(|| "workspace_error".into());
            CommandError {
                code: 1,
                message: format!("{code}: {}", refused.reason),
            }
        }
        DaemonError::Timeout { .. } => CommandError::connection(format!(
            "{error} It may still take effect, and text sent to a pane may already be there: \
             inspect the pane with `gclient capture-pane REF` before retrying. \
             `gobby status` shows whether the daemon is healthy."
        )),
        other => CommandError::connection(other.to_string()),
    }
}
