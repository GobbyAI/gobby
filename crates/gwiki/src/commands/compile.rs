use std::io::ErrorKind;
use std::path::PathBuf;

use gobby_core::ai::AiNoticeKind;
use gobby_core::config::AiRouting;

use crate::explainer::{ExplainerGenerator, ExplainerPrompt, ExplainerReport};
use crate::sources::SourceManifest;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, compile as wiki_compile, daemon,
    session, synthesis,
};

use super::lanes::{
    ai_notice_label, notice_for_explainer_status, resolve_explainer_transport,
    resolve_lane_b_generator, routing_label,
};

const COMMAND: &str = "gwiki compile";

#[allow(clippy::too_many_arguments)]
pub(crate) fn execute(
    topic: Option<String>,
    outline: Vec<String>,
    source: Vec<String>,
    target_kind: synthesis::ArticleKind,
    target_page: Option<PathBuf>,
    write_intent: bool,
    ai: AiRouting,
    scope: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let resolved_scope = resolve_command_scope(&scope)?;
    let research_scope = session::ResearchScope::from(&resolved_scope);
    let topic_seed = compile_topic_seed(topic.as_deref(), &research_scope);
    let mut session = load_compile_session(research_scope, topic_seed.as_deref())?;
    if !source.is_empty() {
        apply_source_selection(&mut session, &source)?;
    }
    let topic = resolve_compile_topic(topic_seed, &session);
    let daemon_report = daemon::probe_daemon_capabilities();
    let daemon_synthesis_available = daemon_report.synthesis.available;
    let output_scope = resolved_scope_identity(&resolved_scope);
    let vault_root = session.scope.root().to_path_buf();

    let request = wiki_compile::CompileRequest {
        topic,
        outline: outline.clone(),
        target_page,
        write_intent,
    };

    // Lane B (tool loop) is the primary compile narrative path: the model
    // investigates the indexed vault via tools to build its own grounding (#982,
    // matching codewiki #978). A resolved Lane B route hard-fails on generation
    // failure (no skeleton fallback); when no tool-chat route resolves, fall back
    // to the Lane A one-shot explainer.
    if let Some(mut lane_b) =
        resolve_lane_b_generator(ai, &scope, vault_root, output_scope.clone(), COMMAND)
    {
        let info = lane_b.info;
        let outcome = wiki_compile::compile_to_wiki_with_options(
            &mut session,
            request,
            wiki_compile::WikiCompileOptions {
                target_kind,
                daemon_synthesis_available,
                hard_fail_on_generation_failure: true,
                ..wiki_compile::WikiCompileOptions::default()
            },
            Some(lane_b.generator.as_mut()),
        )?;
        return Ok(compile_command_outcome(
            ai,
            "tool_loop",
            info.route_label,
            info.fallback,
            info.notice,
            &output_scope,
            target_kind,
            &outline,
            daemon_synthesis_available,
            outcome,
        ));
    }

    // Lane A one-shot explainer (no tool-chat route resolved).
    let transport = resolve_explainer_transport(ai, COMMAND);
    let route_label = transport.route_label();
    let notice = transport.notice_kind();
    let fallback = transport.fallback();
    let mut generate = |prompt: &ExplainerPrompt| transport.generate(prompt);
    let generator: Option<ExplainerGenerator<'_>> = if transport.is_active() {
        Some(&mut generate)
    } else {
        None
    };
    let outcome = wiki_compile::compile_to_wiki_with_options(
        &mut session,
        request,
        wiki_compile::WikiCompileOptions {
            target_kind,
            daemon_synthesis_available,
            hard_fail_on_generation_failure: false,
            ..wiki_compile::WikiCompileOptions::default()
        },
        generator,
    )?;
    Ok(compile_command_outcome(
        ai,
        "one_shot",
        route_label,
        fallback,
        notice,
        &output_scope,
        target_kind,
        &outline,
        daemon_synthesis_available,
        outcome,
    ))
}

/// Build the `compile` command outcome (JSON payload + human text) shared by the
/// Lane B and Lane A paths. `lane` is `tool_loop` or `one_shot`.
#[allow(clippy::too_many_arguments)]
fn compile_command_outcome(
    ai: AiRouting,
    lane: &'static str,
    route_label: &'static str,
    fallback: bool,
    notice: Option<AiNoticeKind>,
    output_scope: &ScopeIdentity,
    target_kind: synthesis::ArticleKind,
    outline: &[String],
    daemon_synthesis_available: bool,
    outcome: wiki_compile::WikiCompileOutcome,
) -> CommandOutcome {
    let explainer = outcome
        .explainer
        .clone()
        .unwrap_or_else(ExplainerReport::skipped);
    let notice = notice_for_explainer_status(explainer.status, notice);
    let payload = serde_json::json!({
        "command": "compile",
        "scope": output_scope,
        "status": "compiled",
        "target_kind": target_kind,
        "outline": outline,
        "daemon_synthesis_available": daemon_synthesis_available,
        "article_path": outcome.article_path,
        "source_paths": outcome.source_paths,
        "index_path": outcome.index_path,
        "handoff_id": outcome.handoff_id,
        "page_writes": outcome.page_writes,
        "prompt": outcome.prompt,
        "ai": {
            "requested_mode": routing_label(ai),
            "lane": lane,
            "route": route_label,
            "fallback": fallback,
            "notice": notice.map(ai_notice_label),
            "status": explainer.status,
            "model": explainer.model,
            "error": explainer.error,
            "citations_kept": explainer.citations_kept,
            "citations_stripped": explainer.citations_stripped,
            "fallback_sections": explainer.fallback_sections,
        },
    });
    let notice_text = notice
        .map(|notice| format!("\nAI notice: {}", ai_notice_label(notice)))
        .unwrap_or_default();
    let text = format!(
        "Compiled wiki article
Scope: {output_scope}
Article: {}{}",
        outcome.article_path.display(),
        notice_text
    );
    super::scoped_outcome("compile", output_scope, payload, text)
}

fn compile_topic_seed(
    topic: Option<&str>,
    research_scope: &session::ResearchScope,
) -> Option<String> {
    topic.map(str::to_owned).or_else(|| match research_scope {
        session::ResearchScope::Topic { name, .. } => Some(name.clone()),
        _ => None,
    })
}

fn load_compile_session(
    research_scope: session::ResearchScope,
    topic_seed: Option<&str>,
) -> Result<session::ResearchSession, WikiError> {
    match session::ResearchSession::load_checkpoint(research_scope.root()) {
        Ok(session) => Ok(session),
        Err(WikiError::Io { action, source, .. })
            if action == "read research checkpoint" && source.kind() == ErrorKind::NotFound =>
        {
            let Some(topic) = topic_seed else {
                return Err(WikiError::InvalidInput {
                    field: "topic",
                    message: "compile requires TOPIC or --topic when no research checkpoint exists"
                        .to_string(),
                });
            };
            session::ResearchSession::new(topic.to_string(), research_scope, Vec::new(), 1, None)
        }
        Err(error) => Err(error),
    }
}

fn resolve_compile_topic(topic_seed: Option<String>, session: &session::ResearchSession) -> String {
    topic_seed.unwrap_or_else(|| {
        session
            .compile_state
            .as_ref()
            .map(|state| state.topic.clone())
            .unwrap_or_else(|| session.question.clone())
    })
}

fn apply_source_selection(
    session: &mut session::ResearchSession,
    selectors: &[String],
) -> Result<(), WikiError> {
    let manifest = SourceManifest::read(session.scope.root())?;
    session.accepted_notes =
        wiki_compile::select::resolve_source_notes(session.scope.root(), &manifest, selectors)?;
    session.save_checkpoint()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_checkpoint_with_topic_seed_creates_fresh_compile_session() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = session::ResearchScope::project_for_id("project-1", temp.path());

        let session =
            load_compile_session(scope, Some("Fresh Topic")).expect("fresh compile session");

        assert_eq!(session.question, "Fresh Topic");
        assert!(session.accepted_notes.is_empty());
        assert_eq!(session.scope.root(), temp.path());
    }

    #[test]
    fn missing_checkpoint_without_topic_seed_requires_topic() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = session::ResearchScope::project_for_id("project-1", temp.path());

        let error = load_compile_session(scope, None).expect_err("missing topic");

        match error {
            WikiError::InvalidInput { field, message } => {
                assert_eq!(field, "topic");
                assert_eq!(
                    message,
                    "compile requires TOPIC or --topic when no research checkpoint exists"
                );
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }
}
