//! Lane B: the provider-neutral, tool-enabled completion loop owned by gcore.
//!
//! gcore owns the loop mechanics — message bookkeeping, tool-call execution
//! limits, termination accounting, and observability — and defines the
//! read-only [`ToolExecutor`] trait. It does **not** depend on gcode/gwiki:
//! consumers implement [`ToolExecutor`] over their own indexed read primitives
//! (search, outline, symbol read, grep, bounded file read) and pass it in.
//!
//! The completion transport is abstracted behind [`ChatTransport`] so the same
//! loop drives the Direct OpenAI-compatible route (see
//! [`super::transport::DirectChatTransport`]) and, later, a daemon
//! tool-passthrough endpoint. Tool execution always stays local to the loop;
//! the transport only relays messages and returns the model's `tool_calls`.

use std::time::{Duration, Instant};

use serde_json::Value;

use crate::ai_types::{AiError, TokenUsage};

/// Role of a chat message in the Lane B transcript.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChatRole {
    System,
    User,
    Assistant,
    Tool,
}

impl ChatRole {
    /// OpenAI-compatible wire label for this role.
    pub fn as_str(self) -> &'static str {
        match self {
            ChatRole::System => "system",
            ChatRole::User => "user",
            ChatRole::Assistant => "assistant",
            ChatRole::Tool => "tool",
        }
    }
}

/// A single chat message in the loop transcript.
#[derive(Debug, Clone, PartialEq)]
pub struct ChatMessage {
    pub role: ChatRole,
    /// Text content. `None` on an assistant turn that only requested tools.
    pub content: Option<String>,
    /// Tool calls requested by the model (assistant turns only).
    pub tool_calls: Vec<ToolCall>,
    /// Links a `Tool` message back to the originating tool call.
    pub tool_call_id: Option<String>,
}

impl ChatMessage {
    /// A system-prompt message.
    pub fn system(content: impl Into<String>) -> Self {
        Self::text(ChatRole::System, content)
    }

    /// A user message.
    pub fn user(content: impl Into<String>) -> Self {
        Self::text(ChatRole::User, content)
    }

    fn text(role: ChatRole, content: impl Into<String>) -> Self {
        Self {
            role,
            content: Some(content.into()),
            tool_calls: Vec::new(),
            tool_call_id: None,
        }
    }

    fn assistant_tool_calls(content: Option<String>, tool_calls: Vec<ToolCall>) -> Self {
        Self {
            role: ChatRole::Assistant,
            content,
            tool_calls,
            tool_call_id: None,
        }
    }

    fn tool_result(tool_call_id: String, content: String) -> Self {
        Self {
            role: ChatRole::Tool,
            content: Some(content),
            tool_calls: Vec::new(),
            tool_call_id: Some(tool_call_id),
        }
    }
}

/// A tool call requested by the model.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    /// Provider-assigned call id, echoed back on the tool result message.
    pub id: String,
    /// Tool name (must match a [`ToolSchema::name`]).
    pub name: String,
    /// Parsed arguments object. Defaults to `Value::Null` when the provider
    /// sent no/invalid arguments.
    pub arguments: Value,
}

/// JSON-schema description of a tool the model may call.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    /// JSON Schema for the tool arguments (the `parameters` object).
    pub parameters: Value,
}

/// Error returned by a [`ToolExecutor`] when a tool call fails. The loop relays
/// the message back to the model as a tool result rather than aborting, so the
/// model can recover or choose a different tool.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolError {
    pub message: String,
}

impl ToolError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl std::fmt::Display for ToolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ToolError {}

/// Read-only repo tool executor. Implemented by consumers (gcode/gwiki) over
/// their indexed read primitives. Implementations must not mutate the repo or
/// any datastore; `&mut self` is allowed only so executors can hold connection
/// state (e.g. a database client).
pub trait ToolExecutor {
    /// The tool schemas advertised to the model for this run.
    fn schemas(&self) -> Vec<ToolSchema>;

    /// Execute one tool call and return its textual result. Returning `Err`
    /// surfaces the message to the model as a tool result; it does not abort the
    /// loop.
    fn execute(&mut self, call: &ToolCall) -> Result<String, ToolError>;
}

/// One completion request issued by the loop.
#[derive(Debug, Clone, Copy)]
pub struct ChatCompletionRequest<'a> {
    pub messages: &'a [ChatMessage],
    pub tools: &'a [ToolSchema],
    pub max_tokens: Option<usize>,
}

/// One completion response returned by a [`ChatTransport`].
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ChatCompletion {
    /// Final assistant text, if the model produced any this turn.
    pub content: Option<String>,
    /// Tool calls the model wants executed before continuing.
    pub tool_calls: Vec<ToolCall>,
    /// Provider finish reason (`stop`, `tool_calls`, `length`, ...).
    pub finish_reason: Option<String>,
    /// Model that produced the completion.
    pub model: Option<String>,
    /// Token usage, when reported.
    pub usage: Option<TokenUsage>,
}

/// Provider-neutral chat-completion transport. The loop is blind to whether the
/// completion came from a direct OpenAI-compatible server or the daemon.
pub trait ChatTransport {
    /// Issue one completion turn.
    fn complete(&self, request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError>;

    /// Static route label for observability (`direct`, `daemon`, `stub`).
    fn route(&self) -> &'static str;

    /// Resolved feature profile name, if any.
    fn profile(&self) -> Option<&str> {
        None
    }

    /// Resolved provider label, if any.
    fn provider(&self) -> Option<&str> {
        None
    }

    /// Configured model, if any.
    fn model(&self) -> Option<&str> {
        None
    }
}

/// Hard limits enforced on a Lane B run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ToolLoopLimits {
    /// Maximum number of completion turns (model calls).
    pub max_turns: usize,
    /// Maximum number of tool calls executed across the whole run.
    pub max_tool_calls: usize,
    /// Maximum byte length of a single tool result fed back to the model.
    /// Larger results are truncated on a UTF-8 boundary.
    pub max_bytes_per_tool_result: usize,
    /// Wall-clock budget for the whole run.
    pub timeout: Duration,
}

impl Default for ToolLoopLimits {
    fn default() -> Self {
        Self {
            max_turns: 8,
            max_tool_calls: 24,
            max_bytes_per_tool_result: 16 * 1024,
            timeout: Duration::from_secs(180),
        }
    }
}

/// Why a Lane B run stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    /// Model returned a final answer with no further tool calls.
    Completed,
    /// `max_turns` reached before a final answer.
    MaxTurns,
    /// `max_tool_calls` reached before a final answer.
    MaxToolCalls,
    /// `timeout` elapsed before a final answer.
    Timeout,
}

impl StopReason {
    pub fn as_str(self) -> &'static str {
        match self {
            StopReason::Completed => "completed",
            StopReason::MaxTurns => "max_turns",
            StopReason::MaxToolCalls => "max_tool_calls",
            StopReason::Timeout => "timeout",
        }
    }

    /// Whether the run produced a final answer.
    pub fn is_completed(self) -> bool {
        matches!(self, StopReason::Completed)
    }
}

/// Structured observability for one Lane B run, suitable for a single log line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolLoopObservability {
    pub lane: &'static str,
    pub route: &'static str,
    pub profile: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    /// Distinct tool names invoked, in first-seen order.
    pub tool_names: Vec<String>,
    /// Total tool calls executed.
    pub tool_call_count: usize,
    /// Completion turns issued.
    pub turns: usize,
    /// Wall-clock elapsed in milliseconds.
    pub elapsed_ms: u64,
    /// Mirror of [`StopReason::as_str`] for logging.
    pub termination_reason: &'static str,
}

/// Outcome of a Lane B run.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolLoopOutcome {
    /// Final answer text. `Some` only when `stop_reason` is
    /// [`StopReason::Completed`].
    pub content: Option<String>,
    pub stop_reason: StopReason,
    pub observability: ToolLoopObservability,
    /// Token usage summed across every completion turn, for cost and
    /// context-window accounting. `None` when no turn reported usage.
    pub total_usage: Option<TokenUsage>,
}

/// Run the Lane B tool loop to completion or a limit.
///
/// `initial_messages` should carry the system/user prompt; tool schemas are
/// taken from the executor. Transport failures propagate as `Err`; reaching a
/// limit returns `Ok` with the corresponding [`StopReason`].
pub fn run_tool_loop(
    transport: &dyn ChatTransport,
    executor: &mut dyn ToolExecutor,
    initial_messages: Vec<ChatMessage>,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
) -> Result<ToolLoopOutcome, AiError> {
    let start = Instant::now();
    run_tool_loop_with_clock(
        transport,
        executor,
        initial_messages,
        limits,
        max_tokens,
        move || start.elapsed(),
    )
}

pub(super) fn run_tool_loop_with_clock<C: FnMut() -> Duration>(
    transport: &dyn ChatTransport,
    executor: &mut dyn ToolExecutor,
    initial_messages: Vec<ChatMessage>,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
    mut elapsed: C,
) -> Result<ToolLoopOutcome, AiError> {
    let tools = executor.schemas();
    let mut messages = initial_messages;
    let mut tool_names: Vec<String> = Vec::new();
    let mut tool_call_count = 0usize;
    let mut turns = 0usize;
    let mut model = transport.model().map(str::to_string);
    let mut usage = UsageAccumulator::default();

    let (content, stop_reason) = loop {
        if elapsed() >= limits.timeout {
            break (None, StopReason::Timeout);
        }
        if turns >= limits.max_turns {
            break (None, StopReason::MaxTurns);
        }

        let request = ChatCompletionRequest {
            messages: &messages,
            tools: &tools,
            max_tokens,
        };
        let completion = transport.complete(request)?;
        turns += 1;
        usage.add(completion.usage.as_ref());
        if let Some(completion_model) = completion.model.clone() {
            model = Some(completion_model);
        }

        if completion.tool_calls.is_empty() {
            break (completion.content, StopReason::Completed);
        }

        messages.push(ChatMessage::assistant_tool_calls(
            completion.content.clone(),
            completion.tool_calls.clone(),
        ));

        let mut hit_call_limit = false;
        for call in &completion.tool_calls {
            if tool_call_count >= limits.max_tool_calls {
                hit_call_limit = true;
                break;
            }
            if !tool_names.iter().any(|name| name == &call.name) {
                tool_names.push(call.name.clone());
            }
            let result = match executor.execute(call) {
                Ok(result) => result,
                Err(error) => format!("tool error: {}", error.message),
            };
            let result = truncate_utf8(result, limits.max_bytes_per_tool_result);
            messages.push(ChatMessage::tool_result(call.id.clone(), result));
            tool_call_count += 1;
        }

        if hit_call_limit {
            break (None, StopReason::MaxToolCalls);
        }
    };

    let observability = ToolLoopObservability {
        lane: "tool_loop",
        route: transport.route(),
        profile: transport.profile().map(str::to_string),
        provider: transport.provider().map(str::to_string),
        model,
        tool_names,
        tool_call_count,
        turns,
        elapsed_ms: duration_to_ms(elapsed()),
        termination_reason: stop_reason.as_str(),
    };

    Ok(ToolLoopOutcome {
        content,
        stop_reason,
        observability,
        total_usage: usage.into_usage(),
    })
}

/// Sums per-turn [`TokenUsage`] across a Lane B run. Each component is summed
/// independently; a component is reported only when at least one turn supplied
/// it. `total_tokens` accumulates provider-reported totals, so
/// [`TokenUsage::token_count`] still prefers the provider total over the
/// input+output sum for callers.
#[derive(Default)]
struct UsageAccumulator {
    input: Option<usize>,
    output: Option<usize>,
    total: Option<usize>,
    seen: bool,
}

impl UsageAccumulator {
    fn add(&mut self, usage: Option<&TokenUsage>) {
        let Some(usage) = usage else {
            return;
        };
        self.seen = true;
        add_component(&mut self.input, usage.input_tokens);
        add_component(&mut self.output, usage.output_tokens);
        add_component(&mut self.total, usage.total_tokens);
    }

    fn into_usage(self) -> Option<TokenUsage> {
        if !self.seen {
            return None;
        }
        Some(TokenUsage {
            input_tokens: self.input,
            output_tokens: self.output,
            total_tokens: self.total,
        })
    }
}

fn add_component(accumulator: &mut Option<usize>, value: Option<usize>) {
    if let Some(value) = value {
        *accumulator = Some(accumulator.unwrap_or(0).saturating_add(value));
    }
}

/// Truncate `value` to at most `max_bytes`, never splitting a UTF-8 character.
fn truncate_utf8(mut value: String, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value;
    }
    let mut end = max_bytes;
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    value.truncate(end);
    value
}

fn duration_to_ms(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}
