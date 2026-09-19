// upstream: herdr v0.8.0 src/ui.rs
//! What the chrome calls a roster row, and what state it draws it in.
//!
//! The D1 ladder lives on `Pane::display_name`, because most surfaces hold a
//! pane and nothing else. These are the surfaces that start from a terminal id
//! or an attention entry instead and have to find the pane first — and, when
//! there is no pane, still answer with a name rather than the id they were
//! handed.

use crate::app::sidebar_model::{agent_row_state, pane_state, AgentEntry};
use crate::app::{PaneId, UNNAMED_PANE};
use crate::ui::chrome::WorkspaceView;
use crate::ui::sidebar::agent_label;

/// herdr `AgentState`, mapped onto Gobby roster rows.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum RowState {
    /// An attention prompt is waiting on this terminal.
    Attention,
    /// The terminal's host is gone; the row can only be destroyed.
    Orphaned,
    /// Output is arriving on an attached pane.
    Working,
    /// New output landed since the pane was last focused.
    Unseen,
    /// Attached and quiet.
    #[default]
    Idle,
    /// No pane is attached to this roster row.
    Unknown,
}

impl RowState {
    pub const ALL: [RowState; 6] = [
        RowState::Attention,
        RowState::Orphaned,
        RowState::Working,
        RowState::Unseen,
        RowState::Idle,
        RowState::Unknown,
    ];
}

/// Attention entries are keyed `<kind>:<subject>`; this is the subject.
pub fn attention_subject(entry_id: &str) -> &str {
    entry_id.rsplit_once(':').map_or(entry_id, |(_, id)| id)
}

/// The sidebar's agent for an attention entry, when the roster joined one.
fn agent_entry<'a, W: WorkspaceView>(ws: &'a W, entry_id: &str) -> Option<&'a AgentEntry> {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
}

/// The sidebar's agent for a terminal, when the roster lists one.
fn agent_for_terminal<'a, W: WorkspaceView>(
    ws: &'a W,
    terminal_id: &str,
) -> Option<&'a AgentEntry> {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == terminal_id)
}

/// The roster row an attention entry points at.
///
/// The subject is a run id for a spawned agent and a session id for an
/// interactive session — the daemon keys every live entry `session:<uuid>` —
/// while the roster is keyed by terminal. Matching the two by string alone
/// therefore resolves nothing, which is why a blocked session never lit up its
/// row. The terminal that hosts the session is the answer in both cases.
pub fn attention_pane<W: WorkspaceView>(ws: &W, entry_id: &str) -> Option<PaneId> {
    if let Some(agent) = agent_entry(ws, entry_id) {
        if let Some(pane) = ws.pane_for_terminal(&agent.terminal_id) {
            return Some(pane);
        }
    }
    let subject = attention_subject(entry_id);
    if let Some(pane) = ws.pane_for_terminal(subject) {
        return Some(pane);
    }
    ws.roster_terminal_ids()
        .iter()
        .filter_map(|id| ws.pane_for_terminal(id))
        .find(|id| ws.pane(*id).session_id.as_deref() == Some(subject))
}

/// What the chrome calls the terminal behind an attention entry: its name,
/// then its address when it has one the name does not already show, so a
/// blocked session reads `15 %15` and never its session uuid.
pub fn attention_label<W: WorkspaceView>(ws: &W, entry_id: &str) -> String {
    if let Some(agent) = agent_entry(ws, entry_id) {
        return agent_label(ws, agent);
    }
    // No agent row and no pane leaves only the entry id, and that is a uuid the
    // ladder exists to keep out of the chrome (D1), so the row reads as the
    // unnamed terminal it is.
    let Some(pane) = attention_pane(ws, entry_id) else {
        return UNNAMED_PANE.to_string();
    };
    let pane = ws.pane(pane);
    let name = pane.display_name();
    match pane.address.as_deref().filter(|address| *address != name) {
        Some(address) => format!("{name} {address}"),
        None => name.to_string(),
    }
}

/// The one name every chrome surface shows for a terminal. Roster and attention
/// rows are keyed by terminal id, so a row can name a terminal no pane owns
/// yet; the sidebar's own agent name covers that, and the ladder's last rung
/// covers the rest.
pub fn terminal_label<W: WorkspaceView>(ws: &W, terminal_id: &str) -> String {
    if let Some(pane) = ws.pane_for_terminal(terminal_id) {
        return ws.pane(pane).display_name().to_string();
    }
    agent_for_terminal(ws, terminal_id)
        .map_or_else(|| UNNAMED_PANE.to_string(), |agent| agent.name.clone())
}

pub fn row_state<W: WorkspaceView>(ws: &W, terminal_id: &str) -> RowState {
    let Some(pane_id) = ws.pane_for_terminal(terminal_id) else {
        return RowState::Unknown;
    };
    let pane = ws.pane(pane_id);
    agent_for_terminal(ws, terminal_id)
        .map_or_else(|| pane_state(pane), |agent| agent_row_state(agent, pane))
}
