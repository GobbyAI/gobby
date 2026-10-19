//! Loop-prompt escalation, as `gobby.agents.loop_tracker` (#24382, plan 2.13.5).
//! The dismissal count lives in `RunDetectState`, which the caller owns.

use super::RunDetectState;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoopTracker {
    threshold: u32,
}

impl LoopTracker {
    /// `None` for a threshold below one, where Python raises `ValueError`.
    pub fn new(threshold: u32) -> Option<Self> {
        (threshold >= 1).then_some(Self { threshold })
    }

    pub fn record_dismissal(&self, state: &mut RunDetectState) -> u32 {
        state.loop_dismissals = state.loop_dismissals.saturating_add(1);
        state.loop_dismissals
    }

    pub fn should_escalate(&self, state: &RunDetectState) -> bool {
        state.loop_dismissals >= self.threshold
    }
}
