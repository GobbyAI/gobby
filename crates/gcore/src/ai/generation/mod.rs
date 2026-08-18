//! Shared, provider-neutral generation foundation for CodeWiki/gwiki narrative.
//!
//! Two lanes share one tier -> feature-profile resolver and the same routing:
//!
//! * **One-shot** ([`one_shot`]) — single completion with tools suppressed.
//! * **Tool loop** ([`tool_loop`]) — a gcore-owned, provider-neutral tool-calling
//!   loop driven by a consumer-supplied [`ToolExecutor`]. gcore owns the loop,
//!   limits, and observability and never depends on gcode/gwiki.
//!
//! Profiles route by [`GenerationTier`]: the Daemon route forwards the profile
//! name to the daemon, which owns provider and model selection.

pub mod one_shot;
pub mod profile;
pub mod tier;
pub mod tool_loop;
pub mod transport;

pub use one_shot::{generate_one_shot, generate_one_shot_pinned, generate_text_with_target};
pub use profile::{DirectGenerationTarget, resolve_direct_generation_target};
pub use tier::{FEATURE_HIGH, FEATURE_LOW, FEATURE_MID, GenerationTier, profile_for_tier};
pub use tool_loop::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport, StopReason,
    ToolCall, ToolChoice, ToolError, ToolExecutor, ToolLoopLimits, ToolLoopObservability,
    ToolLoopOutcome, ToolLoopRunContext, ToolSchema, run_tool_loop, run_tool_loop_with_context,
};
pub use transport::{DaemonAgenticResult, ToolPolicy, daemon_agentic_chat};

#[cfg(test)]
mod tests;
