use crate::runtime::TerminalId;

/// Viewport state for a pane.
///
/// Terminal identity, cwd, and labels live on the durable terminal resource.
pub struct PaneState {
    pub attached_terminal_id: TerminalId,
    /// Whether the user has seen this pane since its last state change to Idle.
    /// False = "Done" (agent finished while user was in another workspace).
    pub seen: bool,
}

impl PaneState {
    pub fn new(attached_terminal_id: TerminalId) -> Self {
        Self {
            attached_terminal_id,
            seen: true,
        }
    }
}
