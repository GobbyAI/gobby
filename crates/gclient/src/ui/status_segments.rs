// upstream: none (Gobby configurable status-row values)
//! Configurable status-row values. Health and attention stay in the fixed slot.

use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::pane_chrome::pane_footer;
use crate::ui::status::focused_overflow;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusSegment {
    Focus,
    Model,
    Context,
    Tokens,
}

impl StatusSegment {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "focus" => Some(Self::Focus),
            "model" => Some(Self::Model),
            "context" => Some(Self::Context),
            "tokens" => Some(Self::Tokens),
            _ => None,
        }
    }
}

pub fn segment_text<W: WorkspaceView>(
    segment: StatusSegment,
    ws: &W,
    chrome: &Chrome,
) -> Option<String> {
    if segment == StatusSegment::Focus {
        return focused_overflow(ws, chrome).map(|pane| pane_footer(ws, pane, true).left);
    }
    let pane = ws.pane(chrome.focused_pane()?);
    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    Some(match segment {
        StatusSegment::Model => agent
            .filter(|agent| agent.model_display_name.is_some() || agent.model.is_some())
            .map(|agent| agent.model_slug())
            .filter(|model| !model.is_empty())
            .unwrap_or_else(|| "—".to_string()),
        StatusSegment::Context => agent
            .and_then(|agent| agent.context_percent)
            .map_or_else(|| "—".to_string(), |percent| format!("{percent}%")),
        StatusSegment::Tokens => agent
            .and_then(|agent| agent.tokens_used)
            .map_or_else(|| "—".to_string(), grouped_tokens),
        StatusSegment::Focus => unreachable!("handled above"),
    })
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
        assert_eq!(StatusSegment::parse("model"), Some(StatusSegment::Model));
        assert_eq!(
            StatusSegment::parse("context"),
            Some(StatusSegment::Context)
        );
        assert_eq!(StatusSegment::parse("tokens"), Some(StatusSegment::Tokens));
        assert_eq!(StatusSegment::parse("cost"), None);
        assert_eq!(StatusSegment::parse("MODEL"), None);
        assert_eq!(grouped_tokens(0), "0");
        assert_eq!(grouped_tokens(12_345), "12,345");
        assert_eq!(grouped_tokens(1_234_567_890), "1,234,567,890");
    }
}
