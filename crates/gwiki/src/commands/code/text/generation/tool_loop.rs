use std::sync::Arc;

use gobby_core::ai::generation::{
    ChatMessage, ChatTransport, GenerationTier, ToolExecutor, ToolLoopLimits, ToolPolicy,
    daemon_agentic_chat, profile_for_tier, run_tool_loop,
};
use gobby_core::ai::resolve_route_observed;
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::code::CodeEngineRuntime;
use crate::commands::code::{
    CodewikiAiOptions, CodewikiAiOutcome, CodewikiGraphAvailability, CodewikiToolExecutor, prompts,
};

use super::outcome::GenerationOutcome;
use super::routing::resolve_ai_context;

const CODEWIKI_READONLY_GCODE_TOOLS: &[&str] = &[
    "search",
    "search-symbol",
    "search-text",
    "search-content",
    "grep",
    "outline",
    "symbol",
    "symbols",
    "symbol-at",
    "repo-outline",
    "tree",
    "kinds",
    "callers",
    "usages",
    "imports",
    "path",
    "blast-radius",
];
const DAEMON_AGENTIC_CALLER: &str = "gwiki.code";

fn log_tool_loop_error(ctx: &CodeEngineRuntime, stage: &str, error: &dyn std::fmt::Display) {
    if !ctx.quiet {
        eprintln!("CodeWiki tool loop {stage} failed: {error}");
    }
}

fn codewiki_readonly_tool_policy() -> ToolPolicy {
    ToolPolicy {
        cli: "gcode".to_string(),
        tools: CODEWIKI_READONLY_GCODE_TOOLS
            .iter()
            .map(|tool| (*tool).to_string())
            .collect(),
        allow_mutation: false,
    }
}

pub(crate) struct ToolLoopResult {
    pub(crate) outcome: GenerationOutcome,
    pub(crate) data_source_degraded: Vec<String>,
}

impl ToolLoopResult {
    fn unavailable() -> Self {
        Self {
            outcome: GenerationOutcome::unavailable(),
            data_source_degraded: Vec::new(),
        }
    }

    fn skipped() -> Self {
        Self {
            outcome: GenerationOutcome::skipped(),
            data_source_degraded: Vec::new(),
        }
    }
}

pub(crate) type ToolLoopGenerator<'a> = dyn FnMut(&str, &str) -> ToolLoopResult + 'a;

pub(crate) struct ResolvedToolLoopGenerator {
    pub(crate) generator: Option<Box<ToolLoopGenerator<'static>>>,
    pub(crate) ai_outcome: CodewikiAiOutcome,
}

const TOOL_LOOP_SEED_MAX_BYTES: usize = 16 * 1024;

fn bound_seed_prompt(prompt: &str) -> String {
    if prompt.len() <= TOOL_LOOP_SEED_MAX_BYTES {
        return prompt.to_string();
    }
    let mut end = TOOL_LOOP_SEED_MAX_BYTES;
    while end > 0 && !prompt.is_char_boundary(end) {
        end -= 1;
    }
    format!(
        "{}\n\n[Seed truncated to fit context. Use the available tools to investigate \
         the codebase for details beyond this excerpt.]",
        &prompt[..end]
    )
}

const TOOL_LOOP_SYSTEM_DIRECTIVE: &str = "Investigation mode: you have tools \
(search_code, outline_file, read_symbol, read_file, grep_repo, find_callers, \
find_usages, imports). The user message is a bounded seed for orientation, not \
the full evidence. Before answering, call these tools to read the actual source \
so your output is grounded in the real code; treat tool results as authoritative \
repository source, cite the file:line anchors they reveal alongside any in the \
seed, and never invent files, symbols, or line numbers. If the task instructions \
require specific section headings, your final answer must include every one of \
them verbatim and in the given order; when a section's evidence is thin, state \
that briefly under its heading rather than dropping the heading. When you have \
gathered enough, write the complete response in a single final message and \
return only that answer in exactly the format the task instructions require — no \
tool-call narration, no preamble, and no extra commentary or code fences \
wrapping a required JSON object.";

fn tool_loop_system_prompt(page_system: &str) -> String {
    format!("{page_system}\n\n{TOOL_LOOP_SYSTEM_DIRECTIVE}")
}

const TOOL_LOOP_DAEMON_DIRECTIVE: &str = "Investigation mode: investigate the \
modules and files named in this task by reading the actual source in the \
repository before writing, so every claim is grounded in real code. Cite the \
file:line anchors you actually read; never invent files, symbols, or line \
numbers. If the task requires specific section headings, include every one \
verbatim and in order. When you have gathered enough, write the complete \
response as a single final message in exactly the format the task requires.";

fn tool_loop_daemon_system_prompt(page_system: &str) -> String {
    format!("{page_system}\n\n{TOOL_LOOP_DAEMON_DIRECTIVE}")
}

#[allow(dead_code)]
fn run_direct_tool_loop(
    transport: &dyn ChatTransport,
    ctx: &CodeEngineRuntime,
    graph_availability: CodewikiGraphAvailability,
    system: &str,
    prompt: &str,
    limits: &ToolLoopLimits,
    max_tokens: Option<usize>,
) -> ToolLoopResult {
    let executor = match CodewikiToolExecutor::new(ctx, graph_availability) {
        Ok(executor) => executor,
        Err(error) => {
            log_tool_loop_error(ctx, "executor initialization", &error);
            return ToolLoopResult::unavailable();
        }
    };
    let executor = Arc::new(executor);
    let prompt = bound_seed_prompt(prompt);
    let messages = vec![
        ChatMessage::system(tool_loop_system_prompt(system)),
        ChatMessage::user(prompt.clone()),
    ];
    let loop_executor: Arc<dyn ToolExecutor> = executor.clone();
    let outcome = match run_tool_loop(transport, loop_executor, messages, limits, max_tokens) {
        Ok(outcome) => GenerationOutcome::from_tool_loop(outcome, &prompt),
        Err(error) => {
            log_tool_loop_error(ctx, "execution", &error);
            GenerationOutcome::unavailable()
        }
    };
    ToolLoopResult {
        outcome,
        data_source_degraded: executor.data_source_degraded(),
    }
}

pub(crate) fn resolve_tool_loop_generator(
    ctx: &CodeEngineRuntime,
    ai: &CodewikiAiOptions,
    _graph_availability: CodewikiGraphAvailability,
) -> ResolvedToolLoopGenerator {
    let ai_context = resolve_ai_context(ctx, ai.routing);
    let observed = resolve_route_observed(&ai_context, AiCapability::ToolChat);
    let route = observed.route;
    if matches!(route, AiRouting::Off) {
        return ResolvedToolLoopGenerator {
            generator: None,
            ai_outcome: CodewikiAiOutcome::skipped(route, observed.fallback),
        };
    }
    let aggregate_profile = ai.aggregate_profile.clone();
    let profile = profile_for_tier(GenerationTier::Aggregate, aggregate_profile.as_deref());
    let aggregate_candidates = ai.aggregate_candidates.clone();
    let _max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let ctx_owned = ctx.clone();
    let project_path = ctx.project_root.display().to_string();
    let generator: Box<ToolLoopGenerator<'static>> = Box::new(move |prompt, system| {
        let system = prompts::with_register(system, register);
        match route {
            AiRouting::Daemon => {
                let bounded_prompt = bound_seed_prompt(prompt);
                let messages = vec![
                    ChatMessage::system(tool_loop_daemon_system_prompt(system.as_ref())),
                    ChatMessage::user(bounded_prompt.clone()),
                ];
                let binding = ai_context.binding(AiCapability::ToolChat);
                let tool_policy = codewiki_readonly_tool_policy();
                match daemon_agentic_chat(
                    &ai_context,
                    DAEMON_AGENTIC_CALLER,
                    &profile,
                    (!aggregate_candidates.is_empty()).then_some(aggregate_candidates.as_slice()),
                    &project_path,
                    &tool_policy,
                    &messages,
                    &ai_context.tool_loop_limits,
                    binding.reasoning_effort.as_deref(),
                ) {
                    Ok(result) => ToolLoopResult {
                        outcome: GenerationOutcome::from_daemon_agentic(result, &bounded_prompt),
                        data_source_degraded: Vec::new(),
                    },
                    Err(error) => {
                        log_tool_loop_error(&ctx_owned, "daemon generation", &error);
                        ToolLoopResult::unavailable()
                    }
                }
            }
            AiRouting::Off => ToolLoopResult::skipped(),
        }
    });
    ResolvedToolLoopGenerator {
        generator: Some(generator),
        ai_outcome: CodewikiAiOutcome::generated(route, observed.fallback),
    }
}

pub(crate) fn maybe_generate_tool_loop(
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    prompt: &str,
    system: &str,
) -> ToolLoopResult {
    match tool_loop.as_deref_mut() {
        None => ToolLoopResult {
            outcome: GenerationOutcome::skipped(),
            data_source_degraded: Vec::new(),
        },
        Some(generate) => generate(prompt, system),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn daemon_agentic_caller_is_stable() {
        assert_eq!(DAEMON_AGENTIC_CALLER, "gwiki.code");
    }

    #[test]
    fn bound_seed_prompt_passes_small_prompts_through() {
        let prompt = "short seed";
        assert_eq!(bound_seed_prompt(prompt), prompt);
    }

    #[test]
    fn codewiki_tool_policy_is_read_only_gcode() {
        let policy = codewiki_readonly_tool_policy();
        assert_eq!(policy.cli, "gcode");
        assert!(!policy.allow_mutation);
        assert!(policy.tools.contains(&"search".to_string()));
        assert!(policy.tools.contains(&"outline".to_string()));
        assert!(policy.tools.contains(&"blast-radius".to_string()));
        for forbidden in ["index", "codewiki", "graph", "vector", "setup", "prune"] {
            assert!(
                !policy.tools.iter().any(|tool| tool == forbidden),
                "read-only policy must not expose the mutating `{forbidden}` subcommand",
            );
        }
    }

    #[test]
    fn tool_loop_system_prompt_appends_investigation_directive() {
        let composed = tool_loop_system_prompt(prompts::CONCEPT_PAGE_SYSTEM);
        assert!(composed.starts_with(prompts::CONCEPT_PAGE_SYSTEM));
        assert!(composed.contains("Investigation mode"));
        assert!(composed.contains("search_code"));
        assert!(composed.contains("authoritative repository source"));
        assert!(composed.contains("in exactly the format the task"));
        assert!(!composed.contains("write the page"));
        assert!(composed.contains("verbatim and in the given order"));
    }

    #[test]
    fn bound_seed_prompt_truncates_oversized_seed_on_a_char_boundary_with_a_tool_note() {
        let prompt = "é".repeat(TOOL_LOOP_SEED_MAX_BYTES);
        let bounded = bound_seed_prompt(&prompt);
        assert!(bounded.is_char_boundary(0) && std::str::from_utf8(bounded.as_bytes()).is_ok());
        assert!(bounded.contains("[Seed truncated to fit context."));
        assert!(bounded.contains("Use the available tools"));
        assert!(bounded.len() < prompt.len());
        assert!(bounded.len() <= TOOL_LOOP_SEED_MAX_BYTES + 512);
    }

    #[test]
    fn tool_loop_daemon_system_prompt_is_tool_agnostic() {
        let composed = tool_loop_daemon_system_prompt(prompts::CONCEPT_PAGE_SYSTEM);
        assert!(composed.starts_with(prompts::CONCEPT_PAGE_SYSTEM));
        assert!(composed.contains("Investigation mode"));
        assert!(!composed.contains("search_code"));
        assert!(!composed.contains("outline_file"));
        assert!(!composed.contains("read_symbol"));
        assert!(composed.contains("grounded in real code"));
        assert!(composed.contains("verbatim and in order"));
        assert!(composed.contains("in exactly the format the task"));
    }
}
