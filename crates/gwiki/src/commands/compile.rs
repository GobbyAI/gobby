use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use gobby_core::ai::AiNoticeKind;
use gobby_core::config::AiRouting;

use crate::explainer::{ExplainerGenerator, ExplainerPrompt, ExplainerReport};
use crate::sources::SourceManifest;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, compile as wiki_compile, session,
    synthesis, vault,
};

use super::generation_routes::{
    ai_notice_label, notice_for_explainer_status, resolve_ai_selection,
    resolve_explainer_transport, resolve_tool_loop_generator, routing_label,
};

const COMMAND: &str = "gwiki compile";
const DAEMON_AGENTIC_CALLER: &str = "gwiki.compile";

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
    // compile writes checkpoints (_gwiki/), raw/, outputs/, and pages under
    // the vault root, so claim the vault first — an unclaimed write poisons
    // the dir as a non-vault collision.
    vault::initialize(&resolved_scope)?;
    let research_scope = session::ResearchScope::from(&resolved_scope);
    validate_target_topic_identity(topic.as_deref(), &research_scope, target_page.as_deref())?;
    let topic_seed = compile_topic_seed(topic.as_deref(), &research_scope);
    let mut session = load_compile_session(research_scope, topic_seed.as_deref())?;
    validate_checkpoint_source_identity(topic_seed.as_deref(), &session, !source.is_empty())?;
    if !source.is_empty() {
        apply_source_selection(&mut session, &source)?;
    } else {
        reconcile_checkpoint_sources(&mut session)?;
    }
    let topic = resolve_compile_topic(topic_seed, &session);
    let ai_selection = resolve_ai_selection(ai);
    let daemon_synthesis_available = matches!(ai_selection.route, AiRouting::Daemon);
    let output_scope = resolved_scope_identity(&resolved_scope);
    let vault_root = session.scope.root().to_path_buf();

    let request = wiki_compile::CompileRequest {
        topic,
        outline: outline.clone(),
        target_page,
        write_intent,
    };

    // The tool loop is the primary compile narrative path: the model
    // investigates the indexed vault via tools to build its own grounding (#982,
    // matching codewiki #978). A resolved tool-loop route hard-fails on generation
    // failure (no skeleton fallback); when no tool-chat route resolves, fall back
    // to the one-shot explainer.
    let project_root = resolved_scope.project_root().map(|root| root.to_path_buf());
    if let Some(mut tool_loop) = resolve_tool_loop_generator(
        ai_selection.route,
        DAEMON_AGENTIC_CALLER,
        &scope,
        vault_root,
        project_root,
        output_scope.clone(),
        COMMAND,
    ) {
        let info = tool_loop.info;
        let outcome = wiki_compile::compile_to_wiki_with_options(
            &mut session,
            request,
            wiki_compile::WikiCompileOptions {
                target_kind,
                daemon_synthesis_available,
                hard_fail_on_generation_failure: true,
                ..wiki_compile::WikiCompileOptions::default()
            },
            Some(tool_loop.generator.as_mut()),
        )?;
        return Ok(compile_command_outcome(
            ai_selection.requested,
            "tool_loop",
            info.route_label,
            ai_selection.selection_reason,
            info.notice,
            &output_scope,
            target_kind,
            &outline,
            daemon_synthesis_available,
            outcome,
        ));
    }

    // One-shot explainer (no tool-chat route resolved).
    let transport = resolve_explainer_transport(ai_selection.route, COMMAND);
    let route_label = transport.route_label();
    let notice = transport.notice_kind();
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
        ai_selection.requested,
        "one_shot",
        route_label,
        ai_selection.selection_reason,
        notice,
        &output_scope,
        target_kind,
        &outline,
        daemon_synthesis_available,
        outcome,
    ))
}

/// Build the `compile` command outcome (JSON payload + human text) shared by the
/// Tool-loop and one-shot paths. `lane` is `tool_loop` or `one_shot`.
#[allow(clippy::too_many_arguments)]
fn compile_command_outcome(
    ai: AiRouting,
    lane: &'static str,
    route_label: &'static str,
    selection_reason: &'static str,
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
            "selection_reason": selection_reason,
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

fn validate_target_topic_identity(
    topic: Option<&str>,
    scope: &session::ResearchScope,
    target_page: Option<&Path>,
) -> Result<(), WikiError> {
    if target_page.is_some()
        && matches!(scope, session::ResearchScope::Project { .. })
        && topic.is_none()
    {
        return Err(WikiError::InvalidInput {
            field: "topic",
            message: "project-scoped compile with --target requires an explicit TOPIC".to_string(),
        });
    }
    Ok(())
}

fn validate_checkpoint_source_identity(
    requested_topic: Option<&str>,
    session: &session::ResearchSession,
    has_explicit_sources: bool,
) -> Result<(), WikiError> {
    let Some(requested_topic) = requested_topic else {
        return Ok(());
    };
    if has_explicit_sources {
        return Ok(());
    }
    let checkpoint_topic = session.compile_state.as_ref().map_or_else(
        || match &session.scope {
            session::ResearchScope::Topic { name, .. } => name.as_str(),
            session::ResearchScope::Project { .. } => session.question.as_str(),
        },
        |state| state.topic.as_str(),
    );
    if requested_topic.trim() != checkpoint_topic.trim() {
        return Err(WikiError::InvalidInput {
            field: "source",
            message: format!(
                "requested topic {requested_topic:?} does not match checkpoint topic \
                 {checkpoint_topic:?}; pass explicit --source selectors"
            ),
        });
    }
    Ok(())
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

/// Re-point a reused research checkpoint's accepted notes at the current source
/// manifest so a `gwiki refresh` that re-hashed an unrelated source cannot
/// hard-fail this compile with `accepted_note ... was not found` (#17702).
fn reconcile_checkpoint_sources(session: &mut session::ResearchSession) -> Result<(), WikiError> {
    let manifest = SourceManifest::read(session.scope.root())?;
    let vault_root = session.scope.root().to_path_buf();
    if wiki_compile::select::reconcile_accepted_notes_with_manifest(
        &vault_root,
        &mut session.accepted_notes,
        &manifest,
    ) {
        session.save_checkpoint()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::paths;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind, SourceRecord};

    #[test]
    fn daemon_agentic_caller_is_stable() {
        assert_eq!(DAEMON_AGENTIC_CALLER, "gwiki.compile");
    }

    fn manifest_source(id: &str, title: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: format!("{id}.md"),
            canonical_location: format!("file:///vault/{id}.md"),
            kind: SourceKind::Markdown,
            fetched_at: "2026-07-05T00:00:00Z".to_string(),
            last_verified_at: "2026-07-05T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: format!("{id}-hash"),
            title: Some(title.to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }

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

    #[test]
    fn targeted_project_compile_requires_explicit_topic() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = session::ResearchScope::project_for_id("project-1", temp.path());

        let error = validate_target_topic_identity(None, &scope, Some(&PathBuf::from("page.md")))
            .expect_err("project target without an explicit topic must fail");

        match error {
            WikiError::InvalidInput { field, message } => {
                assert_eq!(field, "topic");
                assert!(message.contains("--target"), "{message}");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn checkpoint_topic_mismatch_requires_explicit_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = session::ResearchScope::project_for_id("project-1", temp.path());
        let checkpoint =
            session::ResearchSession::new("Checkpoint Topic", scope, Vec::new(), 1, None)
                .expect("checkpoint session");

        let error =
            validate_checkpoint_source_identity(Some("Different Topic"), &checkpoint, false)
                .expect_err("cross-topic checkpoint reuse must fail");

        match error {
            WikiError::InvalidInput { field, message } => {
                assert_eq!(field, "source");
                assert!(message.contains("--source"), "{message}");
            }
            other => panic!("unexpected error: {other:?}"),
        }

        validate_checkpoint_source_identity(Some("Checkpoint Topic"), &checkpoint, false)
            .expect("matching checkpoint topic may reuse sources");
        validate_checkpoint_source_identity(Some("Different Topic"), &checkpoint, true)
            .expect("explicit sources permit a new topic");

        let topic_scope = session::ResearchScope::topic("Scoped Topic", temp.path());
        let scoped_checkpoint =
            session::ResearchSession::new("A research question", topic_scope, Vec::new(), 1, None)
                .expect("topic checkpoint");
        validate_checkpoint_source_identity(Some("Scoped Topic"), &scoped_checkpoint, false)
            .expect("topic scope name is the checkpoint topic identity");
    }

    #[test]
    fn explicit_topic_and_sources_compile_cited_page_without_checkpoint() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = session::ResearchScope::project_for_id("project-1", temp.path());
        let checkpoint = session::ResearchSession::checkpoint_path(temp.path());
        assert!(
            !checkpoint.exists(),
            "precondition: no research checkpoint at {}",
            checkpoint.display()
        );

        let records = vec![
            manifest_source("src-alpha", "Alpha Guide"),
            manifest_source("src-beta", "Beta Notes"),
            manifest_source("src-gamma", "Gamma Extra"),
        ];
        for record in &records {
            let raw = temp
                .path()
                .join(paths::raw_source_path(&record.id).expect("raw path"));
            std::fs::create_dir_all(raw.parent().expect("raw parent")).expect("raw dir");
            std::fs::write(
                &raw,
                format!(
                    "Citation: {} Reference\nEvidence chunk from {}.\n",
                    record.title.as_deref().expect("title"),
                    record.id
                ),
            )
            .expect("raw source written");
        }
        SourceManifest { entries: records }
            .write(temp.path())
            .expect("manifest written");

        let mut session =
            load_compile_session(scope, Some("Explicit Topic")).expect("fresh compile session");
        apply_source_selection(
            &mut session,
            &["src-alpha".to_string(), "src-beta".to_string()],
        )
        .expect("source selection applied");
        let topic = resolve_compile_topic(Some("Explicit Topic".to_string()), &session);

        let outcome = wiki_compile::compile_to_wiki_with_options(
            &mut session,
            wiki_compile::CompileRequest {
                topic,
                outline: Vec::new(),
                target_page: None,
                write_intent: false,
            },
            wiki_compile::WikiCompileOptions::default(),
            None,
        )
        .expect("compile succeeds without a pre-existing checkpoint");

        let article = std::fs::read_to_string(&outcome.article_path).expect("article written");
        assert!(
            article.contains("Alpha Guide") && article.contains("Beta Notes"),
            "article must cite both selected sources: {article}"
        );
        assert!(
            !article.contains("Gamma"),
            "unselected manifest source must not be cited: {article}"
        );
        assert_eq!(
            outcome.source_paths.len(),
            2,
            "one digest page per selected source"
        );
    }
}
