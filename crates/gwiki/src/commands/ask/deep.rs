use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path};

use gobby_core::ai::generation::{ChatMessage, ToolPolicy, daemon_agentic_chat, profile_for_tier};
use gobby_core::ai::{AiNoticeKind, resolve_route_observed};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::ai_types::TokenUsage;
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::ask::evidence::EvidencePlan;
use crate::commands::ask::narration::strip_leading_model_narration;
use crate::commands::ask::synthesis::{
    ASK_TIER, mark_ai_unavailable, push_ai_notice_warning, record_synthesis,
};
use crate::commands::generation_routes::routing_label;
use crate::output::{AskAiOutput, AskCitationCheckOutput, AskDeepOutput, AskOutput};
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{ScopeSelection, WikiError};

const DEEP_DAEMON_TOOLS: [&str; 4] = ["search", "read", "backlinks", "sources"];
const DAEMON_AGENTIC_CALLER: &str = "gwiki.ask.deep";

struct DeepGenerationResult {
    route: &'static str,
    model: Option<String>,
    content: Option<String>,
    turns: Option<usize>,
    max_turns: Option<usize>,
    tool_use_count: usize,
    usage: Option<TokenUsage>,
    stop_reason: Option<String>,
    completed: bool,
}

pub(super) fn synthesize(
    output: &mut AskOutput,
    plan: &EvidencePlan,
    selection: ScopeSelection,
    requested_mode: AiRouting,
    require_ai: bool,
) -> Result<(), WikiError> {
    let resolved_scope = resolve_command_scope(&selection)?;
    let vault_root = resolved_scope.root().to_path_buf();
    let project_root = resolved_scope.project_root().map(Path::to_path_buf);
    let _scope_identity = resolved_scope_identity(&resolved_scope);
    let mut source = crate::support::config::hub_ai_config_source("gwiki ask --deep")?;
    let context = AiContext::try_resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: Some(requested_mode),
        },
    )
    .map_err(|error| WikiError::Config {
        detail: error.to_string(),
    })?;
    let observed = resolve_route_observed(&context, AiCapability::ToolChat);
    let route = observed.route;
    if let Some(notice) = observed.reason.or_else(|| {
        (observed.fallback && route == AiRouting::Daemon)
            .then_some(AiNoticeKind::AutoFallbackToDirect)
    }) {
        push_ai_notice_warning(output, notice);
    }
    set_requested_ai(output, requested_mode, route);

    let profile = profile_for_tier(ASK_TIER, None);
    match route {
        AiRouting::Daemon => {
            let Some(project_root) = project_root else {
                return record_deep_unavailable(
                    output,
                    routing_label(route),
                    None,
                    Some("daemon deep ask requires a project scope root".to_string()),
                    require_ai,
                );
            };
            run_daemon(
                output,
                plan,
                &vault_root,
                &project_root,
                &context,
                &profile,
                require_ai,
            )
        }
        AiRouting::Off => {
            record_deep_unavailable(output, routing_label(route), None, None, require_ai)
        }
    }
}

fn run_daemon(
    output: &mut AskOutput,
    plan: &EvidencePlan,
    vault_root: &Path,
    project_root: &Path,
    context: &AiContext,
    profile: &str,
    require_ai: bool,
) -> Result<(), WikiError> {
    let messages = deep_messages(daemon_system(), plan);
    match daemon_agentic_chat(
        context,
        DAEMON_AGENTIC_CALLER,
        profile,
        None,
        &project_root.display().to_string(),
        &deep_daemon_tool_policy(),
        &messages,
        &context.tool_loop_limits,
        None,
    ) {
        Ok(result) => {
            let completed =
                daemon_result_completed(result.stop_reason.as_deref(), result.content.as_deref());
            record_deep_generation(
                output,
                vault_root,
                DeepGenerationResult {
                    route: "daemon",
                    model: result.model,
                    content: result.content,
                    turns: result.turns,
                    max_turns: context.tool_loop_limits.max_turns,
                    tool_use_count: result.tool_use_count,
                    usage: result.usage,
                    stop_reason: result.stop_reason,
                    completed,
                },
                require_ai,
            )
        }
        Err(error) => {
            record_deep_unavailable(output, "daemon", None, Some(error.to_string()), require_ai)
        }
    }
}

fn set_requested_ai(output: &mut AskOutput, requested_mode: AiRouting, route: AiRouting) {
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode: routing_label(requested_mode),
        route: routing_label(route),
        status: "unavailable",
        model: None,
        error: None,
    });
}

fn record_deep_unavailable(
    output: &mut AskOutput,
    route: &'static str,
    model: Option<String>,
    error: Option<String>,
    require_ai: bool,
) -> Result<(), WikiError> {
    output.deep = Some(AskDeepOutput {
        route,
        model: model.clone(),
        turns: None,
        tool_use_count: 0,
        max_turns: None,
        usage: None,
        stop_reason: None,
    });
    let requested_mode = output
        .ai
        .as_ref()
        .map(|ai| ai.requested_mode)
        .unwrap_or(route);
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode,
        route,
        status: "unavailable",
        model,
        error: None,
    });
    mark_ai_unavailable(output, require_ai, error, "deep_unavailable")
}

fn record_deep_generation(
    output: &mut AskOutput,
    vault_root: &Path,
    result: DeepGenerationResult,
    require_ai: bool,
) -> Result<(), WikiError> {
    let DeepGenerationResult {
        route,
        model,
        content,
        turns,
        max_turns,
        tool_use_count,
        usage,
        stop_reason,
        completed,
    } = result;
    output.deep = Some(AskDeepOutput {
        route,
        model: model.clone(),
        turns,
        tool_use_count,
        max_turns,
        usage,
        stop_reason: stop_reason.clone(),
    });
    let requested_mode = output
        .ai
        .as_ref()
        .map(|ai| ai.requested_mode)
        .unwrap_or(route);
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode,
        route,
        status: "unavailable",
        model: model.clone(),
        error: None,
    });

    if !completed {
        let error = stop_reason
            .as_deref()
            .map(|reason| format!("deep investigation did not complete ({reason})"))
            .or_else(|| Some("deep investigation did not complete".to_string()));
        return mark_ai_unavailable(output, require_ai, error, "deep_unavailable");
    }
    let Some(answer) = content.filter(|answer| !answer.trim().is_empty()) else {
        return mark_ai_unavailable(
            output,
            require_ai,
            Some("deep investigation returned no content".to_string()),
            "deep_unavailable",
        );
    };

    let answer = strip_leading_model_narration(&answer);
    let citation_check = deep_citation_check(&answer, vault_root);
    record_synthesis(output, route, answer, model, citation_check);
    Ok(())
}

fn deep_daemon_tool_policy() -> ToolPolicy {
    ToolPolicy {
        cli: "gwiki".to_string(),
        tools: DEEP_DAEMON_TOOLS
            .iter()
            .map(|tool| (*tool).to_string())
            .collect(),
        allow_mutation: false,
    }
}

fn daemon_system() -> &'static str {
    "Investigate the wiki with only the read-only gwiki tools search, read, backlinks, \
     and sources. Follow relevant links before answering. Cite every factual claim \
     with an existing [[wiki/page]] and explicitly state claims that cannot be \
     verified. Return only the final answer."
}

fn deep_messages(system: &'static str, plan: &EvidencePlan) -> Vec<ChatMessage> {
    vec![
        ChatMessage::system(system),
        ChatMessage::user(format!(
            "Use the seed retrieval below to begin a bounded investigation.\n\n{}",
            plan.prompt
        )),
    ]
}

fn daemon_result_completed(stop_reason: Option<&str>, content: Option<&str>) -> bool {
    match stop_reason {
        Some("completed") => true,
        Some("max_turns" | "max_tool_calls" | "timeout") => false,
        Some(_) | None => content.is_some_and(|value| !value.trim().is_empty()),
    }
}

fn deep_citation_check(answer: &str, vault_root: &Path) -> AskCitationCheckOutput {
    let links = wiki_links(answer);
    let page_stems = page_stem_index(vault_root);
    let unsupported_claims = links
        .iter()
        .filter(|link| !wiki_page_exists(vault_root, &link.target, &page_stems))
        .map(|link| link.rendered.clone())
        .collect::<Vec<_>>();
    let checked_claims = links.len();
    let status = if checked_claims == 0 {
        "no_citations"
    } else if unsupported_claims.is_empty() {
        "supported"
    } else {
        "unsupported_claims"
    };
    AskCitationCheckOutput {
        status,
        checked_claims,
        unsupported_claims,
    }
}

struct WikiLink {
    target: String,
    rendered: String,
}

fn wiki_links(answer: &str) -> Vec<WikiLink> {
    let mut links = Vec::new();
    let mut remaining = answer;
    while let Some(start) = remaining.find("[[") {
        let after_start = &remaining[start + 2..];
        let Some(end) = after_start.find("]]") else {
            break;
        };
        let inner = &after_start[..end];
        let target = inner
            .split('|')
            .next()
            .unwrap_or_default()
            .split('#')
            .next()
            .unwrap_or_default()
            .trim();
        links.push(WikiLink {
            target: target.to_string(),
            rendered: format!("[[{inner}]]"),
        });
        remaining = &after_start[end + 2..];
    }
    links
}

fn wiki_page_exists(vault_root: &Path, target: &str, page_stems: &HashSet<String>) -> bool {
    if target.is_empty() {
        return false;
    }
    let normalized = target.replace('\\', "/");
    let path = Path::new(&normalized);
    if path.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    }) {
        return false;
    }
    let candidate = if path.extension().is_some_and(|extension| extension == "md") {
        vault_root.join(path)
    } else {
        vault_root.join(path).with_extension("md")
    };
    if candidate.is_file() {
        return true;
    }
    if path.components().count() != 1 {
        return false;
    }
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    page_stems.contains(&stem.to_lowercase())
}

fn page_stem_index(directory: &Path) -> HashSet<String> {
    let mut stems = HashSet::new();
    let mut directories = vec![directory.to_path_buf()];
    while let Some(directory) = directories.pop() {
        let Ok(entries) = fs::read_dir(directory) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            let path = entry.path();
            if file_type.is_dir() {
                directories.push(path);
            } else if file_type.is_file()
                && path.extension().is_some_and(|extension| extension == "md")
                && let Some(stem) = path.file_stem().and_then(|stem| stem.to_str())
            {
                stems.insert(stem.to_lowercase());
            }
        }
    }
    stems
}

#[cfg(test)]
mod tests {
    use std::fs;

    use gobby_core::ai::generation::StopReason;

    use super::*;
    use crate::commands::ask::assembly::ask_output_from_retrieval;
    use crate::commands::ask::evidence::plan_evidence;
    use crate::commands::ask::test_support::retrieval_with_hooks_hit;

    #[test]
    fn daemon_agentic_caller_is_stable() {
        assert_eq!(DAEMON_AGENTIC_CALLER, "gwiki.ask.deep");
    }

    fn output() -> crate::output::AskOutput {
        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        ask_output_from_retrieval(retrieval.output, &plan)
    }

    #[test]
    fn daemon_policy_is_the_exact_readonly_deep_capability_set() {
        let policy = deep_daemon_tool_policy();
        assert_eq!(policy.cli, "gwiki");
        assert_eq!(policy.tools, ["search", "read", "backlinks", "sources"]);
        assert!(!policy.allow_mutation);
    }

    #[test]
    fn prompts_name_each_routes_exact_readonly_tools() {
        let daemon = daemon_system();
        for tool in ["search", "read", "backlinks", "sources"] {
            assert!(daemon.contains(tool), "daemon prompt omitted {tool}");
        }
    }

    #[test]
    fn deep_citations_validate_vault_pages_beyond_seed_evidence() {
        let temp = tempfile::tempdir().expect("tempdir");
        let existing = temp.path().join("knowledge/concepts/late-discovery.md");
        fs::create_dir_all(existing.parent().expect("parent")).expect("create vault dirs");
        fs::write(&existing, "# Late discovery\n").expect("write page");

        let check = deep_citation_check(
            "Verified [[knowledge/concepts/late-discovery]]; missing [[knowledge/concepts/ghost]].",
            temp.path(),
        );
        assert_eq!(check.checked_claims, 2);
        assert_eq!(check.status, "unsupported_claims");
        assert_eq!(
            check.unsupported_claims,
            vec!["[[knowledge/concepts/ghost]]".to_string()]
        );
    }

    #[test]
    fn direct_limit_exits_preserve_observability_and_degrade() {
        for reason in [
            StopReason::MaxTurns,
            StopReason::MaxToolCalls,
            StopReason::Timeout,
        ] {
            let mut output = output();
            record_deep_generation(
                &mut output,
                std::path::Path::new("."),
                DeepGenerationResult {
                    route: "direct",
                    model: Some("test-model".to_string()),
                    content: None,
                    turns: Some(8),
                    max_turns: None,
                    tool_use_count: 24,
                    usage: Some(TokenUsage {
                        input_tokens: Some(10),
                        output_tokens: Some(2),
                        total_tokens: Some(12),
                    }),
                    stop_reason: Some(reason.as_str().to_string()),
                    completed: false,
                },
                false,
            )
            .expect("limit exit degrades");

            let deep = output.deep.expect("deep observability");
            assert_eq!(deep.stop_reason.as_deref(), Some(reason.as_str()));
            assert_eq!(deep.turns, Some(8));
            assert_eq!(deep.tool_use_count, 24);
            assert_eq!(deep.usage.and_then(|usage| usage.total_tokens), Some(12));
            assert_eq!(output.status, "partial");
            assert_eq!(output.warnings, ["deep_unavailable"]);
            assert!(output.synthesis.is_none());
        }
    }

    #[test]
    fn direct_completion_preserves_observability_and_vault_citations() {
        let temp = tempfile::tempdir().expect("tempdir");
        let existing = temp.path().join("knowledge/concepts/verified.md");
        fs::create_dir_all(existing.parent().expect("parent")).expect("create vault dirs");
        fs::write(&existing, "# Verified\n").expect("write page");
        let mut output = output();

        record_deep_generation(
            &mut output,
            temp.path(),
            DeepGenerationResult {
                route: "direct",
                model: Some("test-model".to_string()),
                content: Some("Answer cites [[knowledge/concepts/verified]].".to_string()),
                turns: Some(3),
                max_turns: None,
                tool_use_count: 4,
                usage: Some(TokenUsage {
                    input_tokens: Some(20),
                    output_tokens: Some(5),
                    total_tokens: Some(25),
                }),
                stop_reason: Some(StopReason::Completed.as_str().to_string()),
                completed: true,
            },
            false,
        )
        .expect("completed direct answer records");

        let deep = output.deep.expect("deep observability");
        assert_eq!(deep.stop_reason.as_deref(), Some("completed"));
        assert_eq!(deep.turns, Some(3));
        assert_eq!(deep.tool_use_count, 4);
        assert_eq!(deep.usage.and_then(|usage| usage.total_tokens), Some(25));
        assert_eq!(output.status, "answered");
        assert_eq!(
            output.synthesis.expect("synthesis").citation_check.status,
            "supported"
        );
    }

    #[test]
    fn off_route_degrades_with_deep_warning_and_require_ai_errors() {
        let mut degraded = output();
        record_deep_unavailable(&mut degraded, "off", None, None, false)
            .expect("off route degrades");
        assert_eq!(degraded.status, "partial");
        assert_eq!(degraded.warnings, ["deep_unavailable"]);
        assert_eq!(degraded.deep.expect("deep metadata").route, "off");

        let mut required = output();
        let error = record_deep_unavailable(
            &mut required,
            "off",
            None,
            Some("no deep route".to_string()),
            true,
        )
        .expect_err("require-ai rejects unavailable deep route");
        assert!(error.to_string().contains("no deep route"));
    }

    #[test]
    fn completed_result_without_content_reports_missing_content() {
        let mut output = output();
        record_deep_generation(
            &mut output,
            Path::new("."),
            DeepGenerationResult {
                route: "daemon",
                model: None,
                content: None,
                turns: None,
                max_turns: None,
                tool_use_count: 0,
                usage: None,
                stop_reason: Some("completed".to_string()),
                completed: true,
            },
            false,
        )
        .expect("missing content degrades");

        assert_eq!(output.status, "partial");
        assert_eq!(
            output.ai.and_then(|ai| ai.error).as_deref(),
            Some("deep investigation returned no content")
        );
    }

    #[test]
    fn daemon_provenance_is_verbatim_and_optional() {
        let temp = tempfile::tempdir().expect("tempdir");
        let existing = temp.path().join("knowledge/concepts/verified.md");
        fs::create_dir_all(existing.parent().expect("parent")).expect("create vault dirs");
        fs::write(&existing, "# Verified\n").expect("write page");
        let mut output = output();

        record_deep_generation(
            &mut output,
            temp.path(),
            DeepGenerationResult {
                route: "daemon",
                model: None,
                content: Some("Answer cites [[knowledge/concepts/verified]].".to_string()),
                turns: None,
                max_turns: None,
                tool_use_count: 3,
                usage: None,
                stop_reason: Some("provider_specific".to_string()),
                completed: true,
            },
            false,
        )
        .expect("daemon answer records");

        let deep = output.deep.expect("deep observability");
        assert_eq!(deep.stop_reason.as_deref(), Some("provider_specific"));
        assert_eq!(deep.turns, None);
        assert_eq!(output.status, "answered");
        assert_eq!(
            output.synthesis.expect("synthesis").citation_check.status,
            "supported"
        );
    }

    #[test]
    fn daemon_completion_uses_reported_limits_without_inventing_provenance() {
        assert!(daemon_result_completed(Some("completed"), Some("answer")));
        for reason in ["max_turns", "max_tool_calls", "timeout"] {
            assert!(!daemon_result_completed(Some(reason), Some("partial")));
        }
        assert!(daemon_result_completed(None, Some("provider answer")));
        assert!(daemon_result_completed(
            Some("provider_specific"),
            Some("provider answer")
        ));
        assert!(!daemon_result_completed(None, None));
    }
}
