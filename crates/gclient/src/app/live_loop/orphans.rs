//! Destroy orphaned terminals from the global menu or an agent row.
//!
//! An orphan is a row only the daemon can clean up: a native row in state
//! `orphaned` (its host epoch is gone) or an external tmux session with no
//! attached client (a Ghostty tab closed with `destroy-unattached off`).
//! Gobby-owned tmux rows are always detached and are never listed. The
//! candidates come from the WS `terminal_list` inventory on activation, so
//! the menu item needs no polling; the dialog pre-checks every row, and the
//! kills go through the same `terminal_kill` the close-terminal action uses.

use std::collections::HashSet;

use serde_json::Value;

use crate::daemon::{Daemon, DaemonError, KillOutcome, LiveDaemon, SidebarRows, TerminalRow};
use crate::frame_source::FrameError;
use crate::ui::dialogs::project::plural;
use crate::ui::dialogs::{Dialog, OrphanRow};
use crate::ui::{Chrome, Mode};

use super::super::{short_terminal_id, Workspace};

/// The inventory states an orphan candidate can sit in.
const CANDIDATE_STATES: [&str; 2] = ["live", "orphaned"];

/// The orphan rule over one inventory row.
pub fn is_orphan(row: &TerminalRow) -> bool {
    let field = |key: &str| row.fields.get(key).and_then(Value::as_str);
    if field("state") == Some("orphaned") {
        return true;
    }
    field("backend") == Some("tmux")
        && field("ownership") == Some("external")
        && row.fields.get("attached_clients").and_then(Value::as_u64) == Some(0)
}

/// The orphan candidates among `rows`, named for the dialog and sorted.
pub fn orphan_rows(rows: &[TerminalRow], sidebar: &SidebarRows) -> Vec<OrphanRow> {
    let mut orphans: Vec<OrphanRow> = rows
        .iter()
        .filter(|row| is_orphan(row))
        .map(|row| orphan_row(row, sidebar))
        .collect();
    orphans.sort_by(|a, b| {
        a.name
            .cmp(&b.name)
            .then_with(|| a.terminal_id.cmp(&b.terminal_id))
    });
    orphans
}

fn orphan_row(row: &TerminalRow, sidebar: &SidebarRows) -> OrphanRow {
    let field = |key: &str| {
        row.fields
            .get(key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
    };
    OrphanRow {
        terminal_id: row.id().to_string(),
        backend: field("backend").unwrap_or("native").to_string(),
        name: field("name")
            .or_else(|| field("title"))
            .map_or_else(|| short_terminal_id(row.id()).to_string(), str::to_string),
        owner: field("session_id").map(|id| session_label(id, sidebar)),
        last_seen: field("updated_at").and_then(clock_time),
    }
}

/// The session's `ref` (`#12217`), else its title, else its short id.
fn session_label(session_id: &str, sidebar: &SidebarRows) -> String {
    sidebar
        .sessions
        .values()
        .flatten()
        .find(|session| session.id == session_id)
        .and_then(|session| session.reference.clone().or_else(|| session.title.clone()))
        .unwrap_or_else(|| format!("session {}", short_terminal_id(session_id)))
}

/// `HH:MM` out of an ISO-8601 timestamp.
fn clock_time(stamp: &str) -> Option<String> {
    let time = stamp.get(11..16)?;
    (stamp.as_bytes().get(10) == Some(&b'T')).then(|| time.to_string())
}

/// Every orphan candidate the daemon knows, across all projects.
pub async fn fetch_orphans<D: Daemon>(
    workspace: &Workspace<D>,
) -> Result<Vec<OrphanRow>, DaemonError> {
    let mut cursor: Option<String> = None;
    let mut cursors = HashSet::new();
    let mut rows = Vec::new();
    loop {
        let page = workspace
            .daemon
            .inventory_page(&CANDIDATE_STATES, cursor.as_deref())
            .await?;
        rows.extend(page.items);
        match page.next_cursor.filter(|next| !next.is_empty()) {
            Some(next) if cursors.insert(next.clone()) => cursor = Some(next),
            Some(_) => {
                return Err(DaemonError::Protocol {
                    detail: "terminal cursor repeated".into(),
                });
            }
            None => break,
        }
    }
    Ok(orphan_rows(&rows, &workspace.sidebar_rows))
}

/// The agent row's terminal as a single dialog-less target.
pub fn agent_orphan<D: Daemon>(workspace: &Workspace<D>, terminal_id: &str) -> OrphanRow {
    let agent = workspace
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == terminal_id);
    OrphanRow {
        terminal_id: terminal_id.to_string(),
        backend: agent.map_or_else(|| "native".to_string(), |agent| agent.backend.clone()),
        name: agent.map_or_else(
            || short_terminal_id(terminal_id).to_string(),
            |agent| agent.name.clone(),
        ),
        owner: agent.and_then(|agent| agent.session_ref.clone()),
        last_seen: None,
    }
}

/// Fetch the candidates and open the dialog with every row checked; with
/// none, say so in the status line instead.
pub async fn open_destroy_orphans_dialog(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return;
    }
    let rows = match fetch_orphans(workspace).await {
        Ok(rows) => rows,
        Err(error) => {
            chrome.status_message = Some(format!("Orphaned terminals: {error}"));
            return;
        }
    };
    if rows.is_empty() {
        chrome.status_message = Some("No orphaned terminals.".to_string());
        return;
    }
    chrome.dialog = Some(Dialog::DestroyOrphans {
        checked: vec![true; rows.len()],
        rows,
        selected: 0,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// The kill outcomes over one batch of targets.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct DestroySummary {
    pub killed: usize,
    pub total: usize,
    /// Names of the rows the daemon refused to kill.
    pub refused: Vec<String>,
}

impl DestroySummary {
    /// The status-line summary.
    pub fn message(&self) -> String {
        let mut message = format!(
            "destroyed {} of {}",
            self.killed,
            plural(self.total, "orphaned terminal")
        );
        if !self.refused.is_empty() {
            message.push_str("; refused: ");
            message.push_str(&self.refused.join(", "));
        }
        message
    }
}

/// Kill `targets` one by one; each kill waits for the daemon's answer.
pub async fn kill_orphans<D: Daemon>(
    daemon: &D,
    targets: &[OrphanRow],
) -> Result<DestroySummary, DaemonError> {
    let mut summary = DestroySummary {
        total: targets.len(),
        ..DestroySummary::default()
    };
    for target in targets {
        match daemon.terminate(&target.terminal_id).await? {
            KillOutcome::Killed { .. } => summary.killed += 1,
            KillOutcome::Refused { .. } => summary.refused.push(target.name.clone()),
        }
    }
    Ok(summary)
}

/// Destroy `targets`, report in the status line, and refresh the roster so
/// the sidebar drops the rows.
pub async fn destroy_orphans(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    targets: Vec<OrphanRow>,
) -> Result<(), FrameError> {
    if targets.is_empty() || workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let summary = kill_orphans(workspace.daemon(), &targets).await?;
    chrome.status_message = Some(summary.message());
    if summary.killed > 0 {
        workspace.fetch_roster().await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::{ScriptedDaemon, SessionRow};
    use serde_json::json;

    fn row(value: Value) -> TerminalRow {
        serde_json::from_value(value).expect("terminal row")
    }

    fn target(terminal_id: &str, name: &str) -> OrphanRow {
        OrphanRow {
            terminal_id: terminal_id.to_string(),
            backend: "tmux".to_string(),
            name: name.to_string(),
            owner: None,
            last_seen: None,
        }
    }

    #[test]
    fn orphan_rows_keeps_native_orphaned_and_detached_external_tmux_only() {
        let rows = [
            row(json!({
                "terminal_id": "native-orphan",
                "backend": "native",
                "ownership": "gobby",
                "state": "orphaned",
                "title": "shell",
            })),
            row(json!({
                "terminal_id": "tmux-detached",
                "backend": "tmux",
                "ownership": "external",
                "state": "live",
                "name": "scratch",
                "session_id": "sess-1",
                "attached_clients": 0,
                "updated_at": "2026-09-10T14:05:00+00:00",
            })),
            row(json!({
                "terminal_id": "tmux-attached",
                "backend": "tmux",
                "ownership": "external",
                "state": "live",
                "name": "editor",
                "attached_clients": 1,
            })),
            row(json!({
                "terminal_id": "tmux-agent",
                "backend": "tmux",
                "ownership": "gobby",
                "state": "live",
                "name": "gobby-1234",
                "attached_clients": 0,
            })),
            row(json!({
                "terminal_id": "native-live",
                "backend": "native",
                "ownership": "gobby",
                "state": "live",
            })),
        ];
        let sidebar = SidebarRows {
            sessions: [(
                "proj-1".to_string(),
                vec![SessionRow {
                    id: "sess-1".to_string(),
                    reference: Some("#12".to_string()),
                    ..SessionRow::default()
                }],
            )]
            .into_iter()
            .collect(),
            ..SidebarRows::default()
        };

        assert_eq!(
            orphan_rows(&rows, &sidebar),
            [
                OrphanRow {
                    terminal_id: "tmux-detached".to_string(),
                    backend: "tmux".to_string(),
                    name: "scratch".to_string(),
                    owner: Some("#12".to_string()),
                    last_seen: Some("14:05".to_string()),
                },
                OrphanRow {
                    terminal_id: "native-orphan".to_string(),
                    backend: "native".to_string(),
                    name: "shell".to_string(),
                    owner: None,
                    last_seen: None,
                },
            ]
        );
    }

    #[tokio::test]
    async fn fetch_orphans_pages_the_inventory() {
        let ws = Workspace::scripted();
        ws.daemon().set_inventory_pages(vec![
            json!({
                "items": [{"terminal_id": "a", "backend": "native", "state": "orphaned"}],
                "next_cursor": "c1",
            }),
            json!({
                "items": [{"terminal_id": "b", "backend": "native", "state": "orphaned"}],
                "next_cursor": null,
            }),
        ]);

        let names: Vec<String> = fetch_orphans(&ws)
            .await
            .expect("inventory")
            .into_iter()
            .map(|row| row.terminal_id)
            .collect();

        assert_eq!(names, ["a", "b"]);
        let requests: Vec<Option<String>> = ws
            .daemon()
            .ws_sent()
            .into_iter()
            .filter(|message| message["type"] == "terminal_list")
            .map(|message| {
                assert_eq!(message["states"], json!(["live", "orphaned"]));
                message["cursor"].as_str().map(str::to_string)
            })
            .collect();
        assert_eq!(requests, [None, Some("c1".to_string())]);
    }

    #[tokio::test]
    async fn destroy_orphans_reports_per_row_outcomes() {
        let daemon = ScriptedDaemon::new();
        daemon.set_kill_refusals(vec!["t-2".to_string()]);

        let summary = kill_orphans(&daemon, &[target("t-1", "alpha"), target("t-2", "beta")])
            .await
            .expect("kills");

        assert_eq!(
            summary,
            DestroySummary {
                killed: 1,
                total: 2,
                refused: vec!["beta".to_string()],
            }
        );
        assert_eq!(
            summary.message(),
            "destroyed 1 of 2 orphaned terminals; refused: beta"
        );
        assert_eq!(
            DestroySummary {
                killed: 1,
                total: 1,
                refused: Vec::new(),
            }
            .message(),
            "destroyed 1 of 1 orphaned terminal"
        );
        assert_eq!(daemon.ws_sent_types(), ["terminal_kill", "terminal_kill"]);
    }
}
