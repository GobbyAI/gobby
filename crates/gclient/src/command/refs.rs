//! Pane and tab REFs checked against the selected workspace before an op
//! goes out. A short UUID prefix resolves to the one row it names there, and
//! an explicit `--workspace` refuses a REF outside it. A full REF with no
//! explicit workspace goes to the daemon untouched.

use super::CommandError;
use crate::daemon::{WorkspaceOp, WorkspaceSnapshot};

/// Which rows a verb's REF may name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Rows {
    Panes,
    Tabs,
    Either,
}

/// A REF to check against a workspace snapshot.
#[derive(Debug)]
pub(super) struct Target {
    pub reference: String,
    pub workspace: String,
    rows: Rows,
}

/// The row a short ID resolved to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Row {
    Pane { id: String, tab_id: String },
    Tab { id: String },
}

/// A short ID: a UUID prefix. Digits-and-colons text is a daemon REF and a
/// whole UUID names its row directly, so neither counts.
pub(super) fn is_short_id(reference: &str) -> bool {
    !reference.is_empty()
        && !reference.chars().all(|c| c.is_ascii_digit() || c == ':')
        && uuid::Uuid::parse_str(reference).is_err()
        && reference.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

impl Target {
    /// The check a REF needs, if any. A short ID resolves in the explicit
    /// workspace, else the pane's own (`GOBBY_WORKSPACE_ID`); an explicit
    /// workspace also bounds a full REF.
    pub fn new(
        reference: &str,
        explicit: Option<String>,
        default: Option<&String>,
        rows: Rows,
    ) -> Result<Option<Self>, CommandError> {
        let short = is_short_id(reference);
        if !short && explicit.is_none() {
            return Ok(None);
        }
        let workspace = explicit.or_else(|| default.cloned()).ok_or_else(|| {
            CommandError::usage(format!(
                "short ID {reference} needs --workspace REF or GOBBY_WORKSPACE_ID"
            ))
        })?;
        Ok(Some(Self {
            reference: reference.to_owned(),
            workspace,
            rows,
        }))
    }

    /// The row a short ID names, or `None` for a full REF inside the
    /// workspace. Refuses no match, an ambiguous prefix, and a full REF
    /// outside the workspace.
    pub fn resolve(&self, snapshot: &WorkspaceSnapshot) -> Result<Option<Row>, CommandError> {
        if is_short_id(&self.reference) {
            return self.match_prefix(snapshot).map(Some);
        }
        if self.contains(snapshot) {
            Ok(None)
        } else {
            Err(CommandError::usage(format!(
                "{} is not in workspace {}",
                self.reference, self.workspace
            )))
        }
    }

    fn match_prefix(&self, snapshot: &WorkspaceSnapshot) -> Result<Row, CommandError> {
        let prefix = self.reference.to_ascii_lowercase();
        let mut rows = Vec::new();
        if self.rows != Rows::Tabs {
            rows.extend(
                snapshot
                    .panes
                    .iter()
                    .filter(|pane| pane.id.to_ascii_lowercase().starts_with(&prefix))
                    .map(|pane| Row::Pane {
                        id: pane.id.clone(),
                        tab_id: pane.tab_id.clone(),
                    }),
            );
        }
        if self.rows != Rows::Panes {
            rows.extend(
                snapshot
                    .tabs
                    .iter()
                    .filter(|tab| tab.id.to_ascii_lowercase().starts_with(&prefix))
                    .map(|tab| Row::Tab { id: tab.id.clone() }),
            );
        }
        match rows.len() {
            1 => Ok(rows.remove(0)),
            0 => Err(CommandError {
                code: 1,
                message: format!(
                    "no {} in workspace {} starts with {}",
                    self.noun(),
                    self.workspace,
                    self.reference
                ),
            }),
            _ => Err(CommandError::usage(format!(
                "short ID {} is ambiguous in workspace {}: {}",
                self.reference,
                self.workspace,
                rows.iter()
                    .map(|row| match row {
                        Row::Pane { id, .. } => format!("pane {id}"),
                        Row::Tab { id } => format!("tab {id}"),
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            ))),
        }
    }

    fn noun(&self) -> &'static str {
        match self.rows {
            Rows::Panes => "pane",
            Rows::Tabs => "tab",
            Rows::Either => "tab or pane",
        }
    }

    /// Whether a full REF (a row id or `n:w:t[:p]`) lies in the workspace.
    fn contains(&self, snapshot: &WorkspaceSnapshot) -> bool {
        let reference = self.reference.as_str();
        if snapshot.panes.iter().any(|pane| pane.id == reference)
            || snapshot.tabs.iter().any(|tab| tab.id == reference)
        {
            return true;
        }
        let parts: Vec<_> = reference.split(':').collect();
        let [node, workspace, ..] = parts.as_slice() else {
            return false;
        };
        workspace.parse::<u64>().ok() == Some(snapshot.workspace.reference)
            && snapshot
                .workspace
                .node_ref
                .is_none_or(|node_ref| node.parse::<u64>().ok() == Some(node_ref))
    }
}

/// `op` aimed at the resolved row. A title or kill whose short ID named a tab
/// becomes the tab op; focus hints take the pane's tab too.
pub(super) fn retarget(op: WorkspaceOp, row: Row) -> WorkspaceOp {
    let (id, tab_id) = match &row {
        Row::Pane { id, tab_id } => (id.clone(), Some(tab_id.clone())),
        Row::Tab { id } => (id.clone(), None),
    };
    match op {
        WorkspaceOp::PaneRename { label, node, .. } if tab_id.is_none() => WorkspaceOp::TabRename {
            tab: id,
            title: label,
            node,
        },
        WorkspaceOp::PaneClose { node, .. } if tab_id.is_none() => {
            WorkspaceOp::TabClose { tab: id, node }
        }
        WorkspaceOp::TabRename { title, node, .. } if tab_id.is_some() => WorkspaceOp::PaneRename {
            pane: id,
            label: title,
            node,
        },
        WorkspaceOp::TabClose { node, .. } if tab_id.is_some() => {
            WorkspaceOp::PaneClose { pane: id, node }
        }
        WorkspaceOp::WorkspaceSetFocusHints {
            workspace,
            project_id,
            node,
            ..
        } => match tab_id {
            Some(tab) => WorkspaceOp::WorkspaceSetFocusHints {
                workspace,
                project_id,
                tab: Some(tab),
                pane: Some(id),
                node,
            },
            None => WorkspaceOp::WorkspaceSetFocusHints {
                workspace,
                project_id,
                tab: Some(id),
                pane: None,
                node,
            },
        },
        mut op => {
            match &mut op {
                WorkspaceOp::TabRename { tab, .. } | WorkspaceOp::TabClose { tab, .. } => {
                    *tab = id;
                }
                WorkspaceOp::PaneSplit { pane, .. }
                | WorkspaceOp::PaneResize { pane, .. }
                | WorkspaceOp::PaneRename { pane, .. }
                | WorkspaceOp::PaneClose { pane, .. }
                | WorkspaceOp::PaneSendText { pane, .. }
                | WorkspaceOp::PaneRead { pane, .. }
                | WorkspaceOp::PaneWaitForOutput { pane, .. } => *pane = id,
                _ => {}
            }
            op
        }
    }
}

#[cfg(test)]
mod tests;
