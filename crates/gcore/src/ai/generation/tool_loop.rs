//! Provider-neutral, tool-enabled completion loop owned by gcore.
//!
//! gcore owns the loop mechanics — message bookkeeping, tool-call execution
//! limits, termination accounting, and observability — and defines the
//! read-only [`ToolExecutor`] trait. It does **not** depend on gcode/gwiki:
//! consumers implement [`ToolExecutor`] over their own indexed read primitives
//! (search, outline, symbol read, grep, bounded file read) and pass it in.
//!
//! The completion transport is abstracted behind [`ChatTransport`] so the same
//! loop can drive a daemon tool-passthrough endpoint. Tool execution stays
//! local to the loop; the transport only relays messages and returns the
//! model's `tool_calls`.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock, mpsc};
use std::time::{Duration, Instant};

static SIDECAR_SEQ: AtomicU64 = AtomicU64::new(0);

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::ai_types::{AiError, TokenUsage};
use crate::config::{ConfigSource, resolve_ai_setting};

/// Role of a chat message in the tool-loop transcript.
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
/// any datastore. Each invocation may run on a detached worker after its caller
/// times out, so implementations must own thread-safe read-only state.
pub trait ToolExecutor: Send + Sync {
    /// The tool schemas advertised to the model for this run.
    fn schemas(&self) -> Vec<ToolSchema>;

    /// Execute one tool call and return its textual result. Returning `Err`
    /// surfaces the message to the model as a tool result; it does not abort the
    /// loop.
    fn execute(&self, call: &ToolCall) -> Result<String, ToolError>;
}

/// How the model is allowed to use the advertised tools on a given turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ToolChoice {
    /// The model decides whether to call a tool (OpenAI `"auto"`).
    #[default]
    Auto,
    /// The model must emit at least one tool call this turn (OpenAI
    /// `"required"`). The loop forces this until the first tool call lands so
    /// The tool loop always investigates before it is allowed to answer; a
    /// model whose runtime ignores `"required"` is re-prompted with a
    /// correction (see `FORCE_INVESTIGATION_CORRECTION`) rather than allowed to
    /// one-shot an ungrounded reply.
    Required,
}

impl ToolChoice {
    /// OpenAI `tool_choice` wire value.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Required => "required",
        }
    }
}

/// One completion request issued by the loop.
#[derive(Debug, Clone, Copy)]
pub struct ChatCompletionRequest<'a> {
    pub messages: &'a [ChatMessage],
    pub tools: &'a [ToolSchema],
    pub max_tokens: Option<usize>,
    /// Tool-use policy for this turn; only meaningful when `tools` is non-empty.
    pub tool_choice: ToolChoice,
    /// Remaining wall-clock budget for this model request, including retries.
    pub timeout: Duration,
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

/// Hard limits enforced on a tool-loop run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolLoopLimits {
    /// Maximum number of completion turns (model calls).
    pub max_turns: Option<usize>,
    /// Maximum number of tool calls executed across the whole run.
    pub max_tool_calls: usize,
    /// Inline budget for a single tool result fed back to the model.
    /// Larger results are offloaded (sidecar + pointer, or pointer/re-query)
    /// and are never prefix-sliced into the chat message.
    pub max_bytes_per_tool_result: usize,
    /// Wall-clock budget for each individual tool call.
    pub tool_timeout_seconds: u64,
    /// Wall-clock budget for the whole run.
    pub loop_timeout_seconds: u64,
}

impl Default for ToolLoopLimits {
    fn default() -> Self {
        let defaults = contract_defaults();
        Self {
            max_turns: defaults.max_turns,
            max_tool_calls: defaults.max_tool_calls,
            max_bytes_per_tool_result: defaults.max_bytes_per_tool_result,
            tool_timeout_seconds: defaults.tool_timeout_seconds,
            loop_timeout_seconds: defaults.loop_timeout_seconds,
        }
    }
}

impl ToolLoopLimits {
    /// Resolve the canonical tool-loop settings from the config store.
    pub fn resolve(source: &mut impl ConfigSource) -> Result<Self, AiError> {
        let mut limits = Self::default();
        limits.max_turns = parse_nullable_positive(source, "max_turns", limits.max_turns)?;
        limits.max_tool_calls = parse_positive(source, "max_tool_calls", limits.max_tool_calls)?;
        limits.max_bytes_per_tool_result = parse_positive(
            source,
            "max_bytes_per_tool_result",
            limits.max_bytes_per_tool_result,
        )?;
        limits.tool_timeout_seconds =
            parse_positive(source, "tool_timeout_seconds", limits.tool_timeout_seconds)?;
        limits.loop_timeout_seconds =
            parse_positive(source, "loop_timeout_seconds", limits.loop_timeout_seconds)?;
        limits.validate()?;
        Ok(limits)
    }

    /// Validate a complete per-request override.
    pub fn validate(&self) -> Result<(), AiError> {
        if self.max_turns == Some(0) {
            return Err(invalid_limit("max_turns"));
        }
        if self.max_tool_calls == 0 {
            return Err(invalid_limit("max_tool_calls"));
        }
        if self.max_bytes_per_tool_result == 0 {
            return Err(invalid_limit("max_bytes_per_tool_result"));
        }
        if self.tool_timeout_seconds == 0 {
            return Err(invalid_limit("tool_timeout_seconds"));
        }
        if self.loop_timeout_seconds == 0 {
            return Err(invalid_limit("loop_timeout_seconds"));
        }
        Ok(())
    }

    pub fn tool_timeout(self) -> Duration {
        Duration::from_secs(self.tool_timeout_seconds)
    }

    pub fn loop_timeout(self) -> Duration {
        Duration::from_secs(self.loop_timeout_seconds)
    }
}

#[derive(Debug, Deserialize)]
struct ToolLoopContract {
    contract: String,
    version: u64,
    config_prefix: String,
    fields: ToolLoopContractFields,
}

#[derive(Debug, Deserialize)]
struct ToolLoopContractFields {
    max_turns: ContractField<Option<usize>>,
    max_tool_calls: ContractField<usize>,
    max_bytes_per_tool_result: ContractField<usize>,
    tool_timeout_seconds: ContractField<u64>,
    loop_timeout_seconds: ContractField<u64>,
}

#[derive(Debug, Deserialize)]
struct ContractField<T> {
    #[allow(dead_code)]
    r#type: String,
    default: T,
}

#[derive(Debug, Clone, Copy)]
struct ToolLoopContractDefaults {
    max_turns: Option<usize>,
    max_tool_calls: usize,
    max_bytes_per_tool_result: usize,
    tool_timeout_seconds: u64,
    loop_timeout_seconds: u64,
}

const TOOL_LOOP_CONTRACT_JSON: &str = include_str!("../../../contracts/tool_loop_limits.v1.json");
const TOOL_LOOP_CONTRACT_NAME: &str = "gobby.tool_loop_limits";
pub const TOOL_LOOP_CONTRACT_VERSION: u64 = 1;
pub const TOOL_LOOP_CONFIG_PREFIX: &str = "ai.generation.tool_loop";

fn contract_defaults() -> ToolLoopContractDefaults {
    static DEFAULTS: OnceLock<ToolLoopContractDefaults> = OnceLock::new();
    *DEFAULTS.get_or_init(|| {
        let contract: ToolLoopContract =
            serde_json::from_str(TOOL_LOOP_CONTRACT_JSON).expect("valid tool-loop contract");
        assert_eq!(contract.contract, TOOL_LOOP_CONTRACT_NAME);
        assert_eq!(contract.version, TOOL_LOOP_CONTRACT_VERSION);
        assert_eq!(contract.config_prefix, TOOL_LOOP_CONFIG_PREFIX);
        ToolLoopContractDefaults {
            max_turns: contract.fields.max_turns.default,
            max_tool_calls: contract.fields.max_tool_calls.default,
            max_bytes_per_tool_result: contract.fields.max_bytes_per_tool_result.default,
            tool_timeout_seconds: contract.fields.tool_timeout_seconds.default,
            loop_timeout_seconds: contract.fields.loop_timeout_seconds.default,
        }
    })
}

fn config_key(suffix: &str) -> String {
    format!("{TOOL_LOOP_CONFIG_PREFIX}.{suffix}")
}

fn parse_positive<T>(source: &mut impl ConfigSource, suffix: &str, default: T) -> Result<T, AiError>
where
    T: std::str::FromStr + PartialEq + From<u8>,
{
    let Some(raw) = resolve_ai_setting(source, &config_key(suffix)) else {
        return Ok(default);
    };
    let value = raw.trim().parse::<T>().map_err(|_| invalid_limit(suffix))?;
    if value == T::from(0) {
        return Err(invalid_limit(suffix));
    }
    Ok(value)
}

fn parse_nullable_positive(
    source: &mut impl ConfigSource,
    suffix: &str,
    default: Option<usize>,
) -> Result<Option<usize>, AiError> {
    let Some(raw) = resolve_ai_setting(source, &config_key(suffix)) else {
        return Ok(default);
    };
    let raw = raw.trim();
    if raw.eq_ignore_ascii_case("null") || raw.is_empty() {
        return Ok(None);
    }
    let value = raw.parse::<usize>().map_err(|_| invalid_limit(suffix))?;
    if value == 0 {
        return Err(invalid_limit(suffix));
    }
    Ok(Some(value))
}

fn invalid_limit(suffix: &str) -> AiError {
    AiError::parse_failure(format!(
        "{TOOL_LOOP_CONFIG_PREFIX}.{suffix} must be a positive integer or the documented nullable value"
    ))
}

/// Why a tool-loop run stopped.
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

/// Structured observability for one tool-loop run, suitable for a single log line.
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

/// Outcome of a tool-loop run.
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

/// How many times the loop re-prompts a model that ignored
/// `tool_choice = "required"` on a forcing turn before giving up. Re-issuing the
/// identical request is futile (the model repeats the same one-shot reply), so
/// each retry appends an escalating correction from
/// [`FORCE_INVESTIGATION_CORRECTIONS`]; empirically a tool-capable-but-reluctant
/// model (e.g. a local reasoning model whose runtime treats `required` as
/// best-effort) starts investigating within one or two corrections, and a few
/// extra escalations cover the rare model that stays stubborn on a given page.
pub(super) const MAX_FORCED_INVESTIGATION_RETRIES: usize = 6;

/// Escalating corrections appended (after the model's rejected draft) when it
/// answers a forcing turn without calling any tool. The wording intensifies so a
/// model that ignores the first nudge is pushed harder — a varied, increasingly
/// explicit instruction breaks it out of repeating the same one-shot reply,
/// where an identical re-issue would not. The final entry repeats for any
/// retries past its length.
const FORCE_INVESTIGATION_CORRECTIONS: [&str; 3] = [
    "You answered without calling any tool. The tool loop requires you to investigate the code with the \
     provided tools before writing. Discard that draft. Call at least one tool now — for example \
     search_code or outline_file for a symbol or file named in the seed — and read its result \
     before you write anything.",
    "You still have not called a tool. Do NOT write the page yet. Your next message must be a tool \
     call (search_code, outline_file, read_symbol, grep_repo, or read_file) and nothing else. \
     Investigate first, then answer.",
    "Stop. Respond with ONLY a single tool call this turn — no prose at all. Pick any tool (for \
     example search_code with a term from the seed) and call it now. You may write the page only \
     after you have read at least one tool result.",
];

/// Per-run context for [`run_tool_loop_with_context`].
#[derive(Debug, Clone, Default)]
pub struct ToolLoopRunContext {
    /// Directory for oversized tool-result sidecars. When absent, over-cap
    /// results become pointer/re-query text only.
    pub artifact_dir: Option<PathBuf>,
}

/// Run the tool loop to completion or a limit.
///
/// `initial_messages` should carry the system/user prompt; tool schemas are
/// taken from the executor. Transport failures propagate as `Err`; reaching a
/// limit returns `Ok` with the corresponding [`StopReason`]. Oversized tool
/// results are not inlined; pass [`ToolLoopRunContext::artifact_dir`] via
/// [`run_tool_loop_with_context`] to persist them as sidecars.
pub fn run_tool_loop(
    transport: &dyn ChatTransport,
    executor: Arc<dyn ToolExecutor>,
    initial_messages: Vec<ChatMessage>,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
) -> Result<ToolLoopOutcome, AiError> {
    run_tool_loop_with_context(
        transport,
        executor,
        initial_messages,
        limits,
        max_tokens,
        &ToolLoopRunContext::default(),
    )
}

/// Run the tool loop with an explicit [`ToolLoopRunContext`].
pub fn run_tool_loop_with_context(
    transport: &dyn ChatTransport,
    executor: Arc<dyn ToolExecutor>,
    initial_messages: Vec<ChatMessage>,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
    context: &ToolLoopRunContext,
) -> Result<ToolLoopOutcome, AiError> {
    let start = Instant::now();
    run_tool_loop_with_clock(
        transport,
        executor,
        initial_messages,
        limits,
        max_tokens,
        context.artifact_dir.as_deref(),
        move || start.elapsed(),
    )
}

pub(super) fn run_tool_loop_with_clock<C: FnMut() -> Duration>(
    transport: &dyn ChatTransport,
    executor: Arc<dyn ToolExecutor>,
    initial_messages: Vec<ChatMessage>,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
    artifact_dir: Option<&Path>,
    mut elapsed: C,
) -> Result<ToolLoopOutcome, AiError> {
    limits.validate()?;
    let tools = executor.schemas();
    let mut messages = initial_messages;
    let mut tool_names: Vec<String> = Vec::new();
    let mut tool_call_count = 0usize;
    let mut turns = 0usize;
    let mut forced_retries = 0usize;
    let mut model = transport.model().map(str::to_string);
    let mut usage = UsageAccumulator::default();

    let (content, stop_reason) = loop {
        let loop_timeout = limits.loop_timeout();
        let elapsed_before_turn = elapsed();
        if elapsed_before_turn >= loop_timeout {
            break (None, StopReason::Timeout);
        }
        if limits.max_turns.is_some_and(|max_turns| turns >= max_turns) {
            break (None, StopReason::MaxTurns);
        }

        // Force tool use until the model has actually investigated at least
        // once, so the tool loop never answers from the seed alone. `tool_choice =
        // "required"` is only best-effort on some runtimes (a local reasoning
        // model routinely ignores it and one-shots), so the forcing window
        // stays open across turns until the first tool call lands; afterward
        // the model finalizes freely (`auto`).
        let forcing = tool_call_count == 0 && !tools.is_empty();
        let tool_choice = if forcing {
            ToolChoice::Required
        } else {
            ToolChoice::Auto
        };
        let request = ChatCompletionRequest {
            messages: &messages,
            tools: &tools,
            max_tokens,
            tool_choice,
            timeout: loop_timeout.saturating_sub(elapsed_before_turn),
        };
        let completion = transport.complete(request)?;
        turns += 1;
        usage.add(completion.usage.as_ref());
        if let Some(completion_model) = completion.model.clone() {
            model = Some(completion_model);
        }

        if elapsed() >= loop_timeout {
            break (None, StopReason::Timeout);
        }

        if completion.tool_calls.is_empty() {
            // No tool call this turn. During the forcing window the model was
            // told it must investigate first; if it ignored that, don't accept
            // the uninvestigated answer — surface its rejected draft (so the
            // correction reads as a natural follow-up) and append an escalating
            // correction, then retry. A bare re-issue, or an identical repeated
            // nudge, just repeats the same reply.
            if forcing {
                if forced_retries < MAX_FORCED_INVESTIGATION_RETRIES {
                    if completion
                        .content
                        .as_deref()
                        .is_some_and(|text| !text.trim().is_empty())
                    {
                        messages.push(ChatMessage::assistant_tool_calls(
                            completion.content.clone(),
                            Vec::new(),
                        ));
                    }
                    let correction = FORCE_INVESTIGATION_CORRECTIONS
                        [forced_retries.min(FORCE_INVESTIGATION_CORRECTIONS.len() - 1)];
                    messages.push(ChatMessage::user(correction));
                    forced_retries += 1;
                    continue;
                }
                // The model refused to investigate even after repeated
                // escalating corrections. The tool-loop contract is
                // investigate-then-answer, so fail (callers hard-fail the page,
                // no skeleton) rather than ship a silent uninvestigated one-shot.
                break (None, StopReason::MaxTurns);
            }
            break (completion.content, StopReason::Completed);
        }

        // The last finite turn may produce a natural final answer, but tool
        // calls from that turn cannot be executed because no model turn remains
        // to consume their results.
        if limits.max_turns.is_some_and(|max_turns| turns >= max_turns) {
            break (None, StopReason::MaxTurns);
        }

        messages.push(ChatMessage::assistant_tool_calls(
            completion.content.clone(),
            completion.tool_calls.clone(),
        ));

        let mut hit_call_limit = false;
        let mut hit_timeout = false;
        for call in &completion.tool_calls {
            if tool_call_count >= limits.max_tool_calls {
                hit_call_limit = true;
                break;
            }
            if !tool_names.iter().any(|name| name == &call.name) {
                tool_names.push(call.name.clone());
            }
            let remaining = loop_timeout.saturating_sub(elapsed());
            if remaining.is_zero() {
                hit_timeout = true;
                break;
            }
            let tool_timeout = limits.tool_timeout();
            let overall_deadline_first = remaining <= tool_timeout;
            let wait_for = remaining.min(tool_timeout);
            let (sender, receiver) = mpsc::sync_channel(1);
            let worker_executor = Arc::clone(&executor);
            let worker_call = call.clone();
            std::thread::spawn(move || {
                let _ = sender.send(worker_executor.execute(&worker_call));
            });
            let result = match receiver.recv_timeout(wait_for) {
                Ok(Ok(result)) => result,
                Ok(Err(error)) => format!("tool error: {}", error.message),
                Err(mpsc::RecvTimeoutError::Timeout) if overall_deadline_first => {
                    hit_timeout = true;
                    break;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    format!(
                        "tool error: timed out after {} seconds",
                        limits.tool_timeout_seconds
                    )
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    "tool error: execution worker disconnected".to_string()
                }
            };
            let result = ingest_tool_result(
                result,
                limits.max_bytes_per_tool_result,
                artifact_dir,
                &call.id,
            );
            messages.push(ChatMessage::tool_result(call.id.clone(), result));
            tool_call_count += 1;
            if elapsed() >= loop_timeout {
                hit_timeout = true;
                break;
            }
        }

        if hit_timeout {
            break (None, StopReason::Timeout);
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

/// Sums per-turn [`TokenUsage`] across a tool-loop run. Each component is summed
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
/// Char-boundary primitive. Tool-result ingest no longer calls this.
#[allow(dead_code)]
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

fn ingest_tool_result(
    result: String,
    max_bytes: usize,
    artifact_dir: Option<&Path>,
    tool_call_id: &str,
) -> String {
    if result.len() <= max_bytes {
        return result;
    }
    let byte_count = result.len();
    if let Some(dir) = artifact_dir
        && let Ok(path) = write_tool_result_sidecar(dir, tool_call_id, result.as_bytes())
    {
        return format!(
            "Tool result omitted ({byte_count} bytes). Full result written to {}. \
             Read the sidecar or re-query the tool.",
            path.display()
        );
    }
    format!(
        "Tool result omitted ({byte_count} bytes). Result exceeds the inline \
         budget; re-query the tool. The body is not inlined."
    )
}

fn write_tool_result_sidecar(
    dir: &Path,
    tool_call_id: &str,
    bytes: &[u8],
) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    let encoded = encode_tool_call_id(tool_call_id);
    loop {
        let suffix = SIDECAR_SEQ.fetch_add(1, Ordering::Relaxed);
        let path = dir.join(format!("tool-result-{encoded}-{suffix}.txt"));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(bytes)?;
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
}

fn encode_tool_call_id(id: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let bytes = id.as_bytes();
    let mut out = String::with_capacity(bytes.len().saturating_mul(2).max(2));
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    if out.is_empty() {
        out.push_str("00");
    }
    out
}

fn duration_to_ms(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod sidecar_tests {
    use super::{encode_tool_call_id, write_tool_result_sidecar};

    #[test]
    fn sidecar_paths_keep_distinct_ids_that_sanitize_to_the_same_lossy_name() {
        let dir = tempfile::tempdir().expect("tempdir");
        let first = write_tool_result_sidecar(dir.path(), "a/b", b"slash").expect("write slash");
        let second = write_tool_result_sidecar(dir.path(), "a?b", b"query").expect("write query");
        assert_ne!(first, second);
        assert_eq!(std::fs::read(&first).expect("read slash"), b"slash");
        assert_eq!(std::fs::read(&second).expect("read query"), b"query");
        assert_ne!(encode_tool_call_id("a/b"), encode_tool_call_id("a?b"));
    }
}
