// upstream: none (Gobby configurable status-row values)
//! Status-row values: the configurable segments, and this machine's agents
//! counted by legend class. Health and the needs-you count stay in the fixed
//! slot.

use crate::app::sidebar_model::state_class;
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::pane_chrome::pane_corners;
use crate::ui::sidebar::agents::agent_state;
use crate::ui::status::focused_overflow;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusSegment {
    Focus,
    Context,
    Tokens,
    /// The focused pane's sandbox mark, in words.
    Sandbox,
}

impl StatusSegment {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "focus" => Some(Self::Focus),
            "context" => Some(Self::Context),
            "tokens" => Some(Self::Tokens),
            "sandbox" => Some(Self::Sandbox),
            _ => None,
        }
    }
}

/// Every agent on this machine by its legend class, whatever the sidebar
/// filter shows. The four counts sum to that agent total.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AgentCounts {
    pub need_you: usize,
    pub idle: usize,
    pub active: usize,
    pub gone: usize,
}

pub fn agent_counts<W: WorkspaceView>(ws: &W) -> AgentCounts {
    let model = ws.sidebar();
    let mut counts = AgentCounts::default();
    for agent in model
        .agents
        .iter()
        .filter(|agent| model.local_machine.is_empty() || agent.machine_id == model.local_machine)
    {
        match state_class(agent_state(ws, agent)) {
            RowState::Attention => counts.need_you += 1,
            RowState::Orphaned => counts.gone += 1,
            RowState::Working => counts.active += 1,
            _ => counts.idle += 1,
        }
    }
    counts
}

/// A segment's text, or `None` when the focused pane has no value for it:
/// an absent segment draws nothing, never a placeholder.
pub fn segment_text<W: WorkspaceView>(
    segment: StatusSegment,
    ws: &W,
    chrome: &Chrome,
) -> Option<String> {
    if segment == StatusSegment::Focus {
        return focused_overflow(ws, chrome).map(|pane| pane_corners(ws, chrome, pane, true).title);
    }
    let pane = ws.pane(chrome.focused_pane()?);
    if segment == StatusSegment::Sandbox {
        let corners = pane_corners(ws, chrome, pane, true);
        return Some(corners.sandbox.label().to_owned());
    }
    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    match segment {
        StatusSegment::Context => agent
            .and_then(|agent| agent.context_percent)
            .map(|percent| format!("{percent}%")),
        StatusSegment::Tokens => agent
            .and_then(|agent| agent.tokens_used)
            .map(grouped_tokens),
        StatusSegment::Focus | StatusSegment::Sandbox => unreachable!("handled above"),
    }
}

fn grouped_tokens(tokens: u64) -> String {
    let mut digits = tokens.to_string();
    let mut position = digits.len();
    while position > 3 {
        position -= 3;
        digits.insert(position, ',');
    }
    digits
}

#[cfg(test)]
mod tests {
    use super::{grouped_tokens, StatusSegment};

    #[test]
    fn segment_names_and_token_groups() {
        assert_eq!(StatusSegment::parse("focus"), Some(StatusSegment::Focus));
        // The provider and model live on the Agents row, not the status line.
        assert_eq!(StatusSegment::parse("model"), None);
        assert_eq!(
            StatusSegment::parse("context"),
            Some(StatusSegment::Context)
        );
        assert_eq!(StatusSegment::parse("tokens"), Some(StatusSegment::Tokens));
        assert_eq!(
            StatusSegment::parse("sandbox"),
            Some(StatusSegment::Sandbox)
        );
        assert_eq!(StatusSegment::parse("cost"), None);
        assert_eq!(StatusSegment::parse("MODEL"), None);
        assert_eq!(grouped_tokens(0), "0");
        assert_eq!(grouped_tokens(12_345), "12,345");
        assert_eq!(grouped_tokens(1_234_567_890), "1,234,567,890");
    }
}
