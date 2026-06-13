use std::path::PathBuf;

use gobby_core::ai::{daemon as core_ai_daemon, effective_route, text as core_ai_text};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use crate::explainer::{ExplainerGenerator, ExplainerPrompt, ExplainerReport, ExplainerResponse};
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    CommandOutcome, ScopeSelection, WikiError, compile as wiki_compile, daemon, session, synthesis,
};

#[allow(clippy::too_many_arguments)]
pub(crate) fn execute(
    topic: Option<String>,
    outline: Vec<String>,
    target_kind: synthesis::ArticleKind,
    target_page: Option<PathBuf>,
    write_intent: bool,
    ai: AiRouting,
    scope: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let resolved_scope = resolve_command_scope(&scope)?;
    let research_scope = session::ResearchScope::from(&resolved_scope);
    let mut session = session::ResearchSession::load_checkpoint(research_scope.root())?;
    // Article topic precedence: explicit positional, then the topic scope's
    // own name (a topic vault compiles its topic by default), then the
    // session's compile state or research question.
    let scope_topic = match &research_scope {
        session::ResearchScope::Topic { name, .. } => Some(name.clone()),
        _ => None,
    };
    let topic = topic.or(scope_topic).unwrap_or_else(|| {
        session
            .compile_state
            .as_ref()
            .map(|state| state.topic.clone())
            .unwrap_or_else(|| session.question.clone())
    });
    let daemon_report = daemon::probe_daemon_capabilities();
    let transport = resolve_explainer_transport(ai);
    let route_label = transport.route_label();
    let mut generate = |prompt: &ExplainerPrompt| transport.generate(prompt);
    let generator: Option<ExplainerGenerator<'_>> = if transport.is_active() {
        Some(&mut generate)
    } else {
        None
    };
    let outcome = wiki_compile::compile_to_wiki_with_options(
        &mut session,
        wiki_compile::CompileRequest {
            topic,
            outline: outline.clone(),
            target_page,
            write_intent,
        },
        wiki_compile::WikiCompileOptions {
            target_kind,
            daemon_synthesis_available: daemon_report.synthesis.available,
        },
        generator,
    )?;
    let explainer = outcome
        .explainer
        .clone()
        .unwrap_or_else(ExplainerReport::skipped);
    let output_scope = resolved_scope_identity(&resolved_scope);
    let payload = serde_json::json!({
        "command": "compile",
        "scope": output_scope,
        "status": "compiled",
        "target_kind": target_kind,
        "outline": outline,
        "daemon_synthesis_available": daemon_report.synthesis.available,
        "article_path": outcome.article_path,
        "source_paths": outcome.source_paths,
        "index_path": outcome.index_path,
        "handoff_id": outcome.handoff_id,
        "page_writes": outcome.page_writes,
        "prompt": outcome.prompt,
        "ai": {
            "requested_mode": routing_label(ai),
            "route": route_label,
            "status": explainer.status,
            "model": explainer.model,
            "error": explainer.error,
            "citations_kept": explainer.citations_kept,
            "citations_stripped": explainer.citations_stripped,
            "fallback_sections": explainer.fallback_sections,
        },
    });
    let text = format!(
        "Compiled wiki article
Scope: {output_scope}
Article: {}",
        outcome.article_path.display()
    );
    Ok(super::scoped_outcome(
        "compile",
        &output_scope,
        payload,
        text,
    ))
}

/// Resolved explainer transport, mirroring `gwiki ask` honesty semantics:
/// `Off` skips synthesis structurally; an unresolved explicit daemon/direct
/// request still runs an attempt so the failure is recorded as degradation.
enum ExplainerTransport {
    Off,
    Unresolved {
        route: AiRouting,
        error: String,
    },
    Resolved {
        route: AiRouting,
        context: Box<AiContext>,
    },
}

impl ExplainerTransport {
    fn is_active(&self) -> bool {
        !matches!(self, Self::Off)
    }

    fn route_label(&self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Unresolved { route, .. } | Self::Resolved { route, .. } => routing_label(*route),
        }
    }

    fn generate(&self, prompt: &ExplainerPrompt) -> Result<ExplainerResponse, String> {
        match self {
            Self::Off => Err("AI synthesis is off".to_string()),
            Self::Unresolved { error, .. } => Err(error.clone()),
            Self::Resolved { route, context } => {
                let result = match route {
                    AiRouting::Daemon => core_ai_daemon::generate_via_daemon(
                        context,
                        &prompt.user,
                        Some(prompt.system),
                    ),
                    _ => core_ai_text::generate_text(context, &prompt.user, Some(prompt.system)),
                }
                .map_err(|error| error.to_string())?;
                Ok(ExplainerResponse {
                    text: result.text,
                    model: result.model,
                    route: routing_label(*route),
                })
            }
        }
    }
}

/// Resolve the AI route for explainer synthesis through the same gcore
/// routing every other gwiki capability uses. `auto` that resolves to no
/// usable route degrades to a structural skip rather than a failure.
fn resolve_explainer_transport(requested: AiRouting) -> ExplainerTransport {
    if matches!(requested, AiRouting::Off) {
        return ExplainerTransport::Off;
    }
    match crate::support::config::hub_ai_config_source("gwiki compile") {
        Ok(mut source) => {
            let context = AiContext::resolve_with_options(
                None,
                &mut source,
                AiContextOptions {
                    no_ai: false,
                    forced_routing: Some(requested),
                },
            );
            match effective_route(&context, AiCapability::TextGenerate) {
                route @ (AiRouting::Daemon | AiRouting::Direct) => ExplainerTransport::Resolved {
                    route,
                    context: Box::new(context),
                },
                _ => ExplainerTransport::Off,
            }
        }
        Err(error) => match requested {
            AiRouting::Daemon | AiRouting::Direct => ExplainerTransport::Unresolved {
                route: requested,
                error: error.to_string(),
            },
            _ => ExplainerTransport::Off,
        },
    }
}

fn routing_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
}
