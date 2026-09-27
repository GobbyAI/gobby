use super::{CommandEnv, CommandError, Parsed};
use crate::daemon::{project_rows, Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use serde_json::Value;
use tokio::time::{Duration, Instant};

pub(super) const HELP: &str = "gclient <verb> [options]\n\
  list [--workspace REF]              List tabs and panes\n\
  new-tab --project NAME|ID [--workspace REF]  Create a tab\n\
  split [REF] --right|--down           Split a pane\n\
  resize [REF] RATIO                   Resize a split\n\
  title [REF] TEXT                     Rename a tab or pane\n\
  select [REF] [--workspace REF]       Set focus hints\n\
  send-keys [REF] TEXT [--enter]       Send pane text\n\
  capture-pane [REF] [--lines N]      Read pane text\n\
  wait-for-output [REF] --pattern REGEX [--timeout S] [--interval S]\n\
  kill [REF]                           Close a tab or pane\n\
  help                                 Show this table\n\
Action options: --json, --daemon-url URL, --token-file PATH.\n\
--workspace applies to list, new-tab, select; list/new-tab default to GOBBY_WORKSPACE_ID.\n\
select derives workspace from a full REF, otherwise from GOBBY_WORKSPACE_ID.\n\
Omitted pane REF uses GOBBY_PANE_REF; outside a pane, pass explicit values.\n\
UUID tab refs: use --kind tab with title/kill; UUID pane focus: use --tab-ref TAB.\n";

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
    },
    Op {
        op: WorkspaceOp,
        kind: OutputKind,
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
            "split" => Self::Split {
                pane: pane_ref(&mut args, env)?,
                axis: axis(&mut args)?,
                cmd: args.take("--cmd"),
            },
            "resize" => {
                let pane = pane_ref_before_value(&mut args, env)?;
                let ratio = positive_f64(&required(args.position(), "RATIO")?, "RATIO")?;
                if ratio >= 1.0 {
                    return Err(CommandError::usage("RATIO must be less than 1"));
                }
                Self::Op {
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
                }
            }
            "select" => {
                let reference = pane_ref(&mut args, env)?;
                let workspace_override = args.take("--workspace");
                let tab_hint = args.take("--tab-ref").or_else(|| env.tab_id.clone());
                let parts: Vec<_> = reference.split(':').collect();
                let (workspace, tab, pane) = if parts.len() == 3 || parts.len() == 4 {
                    let workspace = parts[..2].join(":");
                    let tab = Some(parts[..3].join(":"));
                    let pane = (parts.len() == 4).then_some(reference);
                    (workspace, tab, pane)
                } else {
                    let workspace = required(
                        workspace_override.or_else(|| env.workspace_id.clone()),
                        "--workspace",
                    )?;
                    if let Some(tab) = tab_hint {
                        (workspace, Some(tab), Some(reference))
                    } else {
                        (workspace, Some(reference), None)
                    }
                };
                Self::Op {
                    op: WorkspaceOp::WorkspaceSetFocusHints {
                        workspace,
                        project_id: None,
                        tab,
                        pane,
                        node: None,
                    },
                    kind: OutputKind::Json,
                }
            }
            "send-keys" => {
                let pane = pane_ref_before_value(&mut args, env)?;
                let text = required(args.position(), "TEXT")?;
                let submit = args.switch("--enter");
                Self::Op {
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
        let output = match self {
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
            Self::Split { pane, axis, cmd } => {
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
                    daemon
                        .workspace_op(WorkspaceOp::PaneSendText {
                            pane: new_pane,
                            text: cmd,
                            submit: Some(true),
                            idempotency_key: None,
                            node: None,
                        })
                        .await
                        .map_err(daemon_error)?;
                }
                output(result, OutputKind::CreatedPane(prefix))
            }
            Self::Op { op, kind } => {
                let reply = if let WorkspaceOp::PaneWaitForOutput {
                    timeout_seconds, ..
                } = &op
                {
                    daemon
                        .workspace_op_with_deadline(
                            op.clone(),
                            Duration::from_secs_f64(timeout_seconds.min(300.0) + 5.0),
                        )
                        .await
                        .map_err(daemon_error)?
                } else {
                    daemon.workspace_op(op).await.map_err(daemon_error)?
                };
                output(reply.result, kind)
            }
        };
        let _ = daemon.close(Instant::now() + Duration::from_secs(2)).await;
        output
    }
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
        other => CommandError::connection(other.to_string()),
    }
}
