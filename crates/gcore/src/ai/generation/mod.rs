//! Shared, provider-neutral generation foundation for CodeWiki/gwiki narrative.
//!
//! Two lanes share one tier -> feature-profile resolver and the same routing:
//!
//! * **Lane A** ([`lane_a`]) — one-shot completion with tools suppressed.
//! * **Lane B** ([`tool_loop`]) — a gcore-owned, provider-neutral tool-calling
//!   loop driven by a consumer-supplied [`ToolExecutor`]. gcore owns the loop,
//!   limits, and observability and never depends on gcode/gwiki.
//!
//! Profiles route by [`GenerationTier`]: the Daemon route forwards the profile
//! name; the Direct route resolves it to a concrete [`DirectGenerationTarget`]
//! from `~/.gobby/gcore.yaml` (standalone, plaintext `api_key` allowed).

pub mod lane_a;
pub mod profile;
pub mod tier;
pub mod tool_loop;
pub mod transport;

pub use lane_a::{generate_one_shot, generate_text_with_target};
pub use profile::{DirectGenerationTarget, resolve_direct_generation_target};
pub use tier::{FEATURE_HIGH, FEATURE_LOW, FEATURE_MID, GenerationTier, profile_for_tier};
pub use tool_loop::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport, StopReason,
    ToolCall, ToolError, ToolExecutor, ToolLoopLimits, ToolLoopObservability, ToolLoopOutcome,
    ToolSchema, run_tool_loop,
};
pub use transport::DirectChatTransport;

#[cfg(test)]
mod tests;
