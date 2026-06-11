use std::path::PathBuf;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    CommandOutcome, ScopeSelection, WikiError, compile as wiki_compile, daemon, session, synthesis,
};

pub(crate) fn execute(
    topic: Option<String>,
    outline: Vec<String>,
    target_kind: synthesis::ArticleKind,
    target_page: Option<PathBuf>,
    write_intent: bool,
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
    )?;
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
