use std::collections::HashSet;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use gobby_core::ai::effective_route;
use gobby_core::ai::generation::{
    DirectGenerationTarget, GenerationTier, generate_one_shot, profile_for_tier,
    resolve_direct_generation_target,
};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use crate::explainer::{ExplainerGenerator, ExplainerPrompt, ExplainerReport, ExplainerResponse};
use crate::sources::{SourceManifest, SourceRecord};
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    CommandOutcome, ScopeSelection, WikiError, compile as wiki_compile, daemon, paths, session,
    synthesis,
};

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
    session.accepted_notes = resolve_source_notes(session.scope.root(), &manifest, selectors)?;
    session.save_checkpoint()
}

fn resolve_source_notes(
    vault_root: &Path,
    manifest: &SourceManifest,
    selectors: &[String],
) -> Result<Vec<session::AcceptedResearchNote>, WikiError> {
    let mut selected = Vec::new();
    let mut seen = HashSet::new();
    for selector in selectors {
        let record = resolve_source_selector(manifest, selector)?;
        if seen.insert(record.id.clone()) {
            selected.push(accepted_note_from_source(vault_root, record)?);
        }
    }
    Ok(selected)
}

fn resolve_source_selector<'a>(
    manifest: &'a SourceManifest,
    selector: &str,
) -> Result<&'a SourceRecord, WikiError> {
    let selector = selector.trim();
    if let Some(record) = manifest.entries.iter().find(|entry| entry.id == selector) {
        return Ok(record);
    }

    let selector_path = Path::new(selector);
    for record in &manifest.entries {
        if paths::raw_source_path(&record.id)? == selector_path {
            return Ok(record);
        }
    }

    let matches = manifest
        .entries
        .iter()
        .filter(|entry| entry.location == selector || entry.canonical_location == selector)
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [record] => Ok(record),
        [] => Err(WikiError::NotFound {
            resource: "source",
            id: selector.to_string(),
        }),
        _ => Err(WikiError::InvalidInput {
            field: "source",
            message: format!(
                "source selector `{selector}` matched multiple records; pass a source id"
            ),
        }),
    }
}

fn accepted_note_from_source(
    vault_root: &Path,
    record: &SourceRecord,
) -> Result<session::AcceptedResearchNote, WikiError> {
    let raw_path = paths::raw_source_path(&record.id)?;
    let absolute_path = vault_root.join(&raw_path);
    match absolute_path.try_exists() {
        Ok(true) => {}
        Ok(false) => {
            return Err(WikiError::NotFound {
                resource: "raw_source",
                id: raw_path.display().to_string(),
            });
        }
        Err(error) => {
            return Err(WikiError::Io {
                action: "check raw source",
                path: Some(absolute_path),
                source: error,
            });
        }
    }

    Ok(session::AcceptedResearchNote {
        title: record
            .title
            .clone()
            .unwrap_or_else(|| record.location.clone()),
        path: raw_path,
        code_citations: Vec::new(),
        degradation: None,
    })
}

/// Compiled wiki articles are gwiki's curated narrative surface, so they
/// generate on the aggregate tier. Tier -> feature profile is owned by gcore's
/// `profile_for_tier` (Aggregate -> feature_high); provider/model resolution
/// stays in config and is never pinned here. The Daemon route forwards the
/// resolved profile name; the Direct route resolves it to a concrete target so
/// a standalone gcore.yaml routes compile to its own provider/model/api_key.
const COMPILE_TIER: GenerationTier = GenerationTier::Aggregate;

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
        /// Per-tier direct-generation target resolved for `COMPILE_TIER`,
        /// present only on the Direct route; the Daemon route forwards the
        /// profile name and leaves this `None`.
        target: Option<DirectGenerationTarget>,
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
            Self::Resolved {
                route,
                context,
                target,
            } => {
                let result = generate_one_shot(
                    context,
                    *route,
                    COMPILE_TIER,
                    None,
                    target.as_ref(),
                    &prompt.user,
                    Some(prompt.system),
                    None,
                )
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
                route @ (AiRouting::Daemon | AiRouting::Direct) => {
                    // The Direct route needs a concrete per-tier target resolved
                    // from the same config source (hub config_store plus any
                    // standalone gcore.yaml). The Daemon route forwards the
                    // profile name and ignores the target.
                    let target = matches!(route, AiRouting::Direct).then(|| {
                        resolve_direct_generation_target(
                            &mut source,
                            &profile_for_tier(COMPILE_TIER, None),
                        )
                    });
                    ExplainerTransport::Resolved {
                        route,
                        context: Box::new(context),
                        target,
                    }
                }
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind};
    use std::fs;

    #[test]
    fn compile_generates_on_the_aggregate_feature_profile() {
        use gobby_core::ai::generation::FEATURE_HIGH;
        assert_eq!(COMPILE_TIER, GenerationTier::Aggregate);
        assert_eq!(profile_for_tier(COMPILE_TIER, None), FEATURE_HIGH);
    }

    fn source_record(
        id: &str,
        location: &str,
        canonical_location: &str,
        title: Option<&str>,
    ) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: location.to_string(),
            canonical_location: canonical_location.to_string(),
            kind: SourceKind::Markdown,
            fetched_at: "2026-06-14T00:00:00Z".to_string(),
            content_hash: format!("{id}-hash"),
            title: title.map(str::to_string),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }

    fn write_raw_source(root: &Path, record: &SourceRecord) {
        let path = root.join(paths::raw_source_path(&record.id).expect("raw path"));
        fs::create_dir_all(path.parent().expect("raw parent")).expect("create raw parent");
        fs::write(&path, format!("# {}\n", record.id)).expect("write raw source");
    }

    #[test]
    fn source_selectors_resolve_id_raw_path_location_and_canonical_location() {
        let temp = tempfile::tempdir().expect("tempdir");
        let records = vec![
            source_record(
                "src-alpha",
                "alpha.md",
                "file:///vault/alpha.md",
                Some("Alpha"),
            ),
            source_record("src-beta", "beta.md", "file:///vault/beta.md", Some("Beta")),
            source_record(
                "src-gamma",
                "gamma.md",
                "file:///vault/gamma.md",
                Some("Gamma"),
            ),
            source_record("src-delta", "delta.md", "canonical:delta", None),
        ];
        for record in &records {
            write_raw_source(temp.path(), record);
        }
        let manifest = SourceManifest { entries: records };

        let notes = resolve_source_notes(
            temp.path(),
            &manifest,
            &[
                "src-alpha".to_string(),
                "raw/src-beta.md".to_string(),
                "gamma.md".to_string(),
                "canonical:delta".to_string(),
            ],
        )
        .expect("source notes");

        assert_eq!(
            notes
                .iter()
                .map(|note| note.path.clone())
                .collect::<Vec<_>>(),
            vec![
                PathBuf::from("raw/src-alpha.md"),
                PathBuf::from("raw/src-beta.md"),
                PathBuf::from("raw/src-gamma.md"),
                PathBuf::from("raw/src-delta.md"),
            ]
        );
        assert_eq!(
            notes
                .iter()
                .map(|note| note.title.as_str())
                .collect::<Vec<_>>(),
            vec!["Alpha", "Beta", "Gamma", "delta.md"]
        );
    }

    #[test]
    fn source_selection_dedupes_by_source_id_in_selector_order() {
        let temp = tempfile::tempdir().expect("tempdir");
        let alpha = source_record("src-alpha", "alpha.md", "canonical:alpha", Some("Alpha"));
        let beta = source_record("src-beta", "beta.md", "canonical:beta", Some("Beta"));
        write_raw_source(temp.path(), &alpha);
        write_raw_source(temp.path(), &beta);
        let manifest = SourceManifest {
            entries: vec![alpha, beta],
        };

        let notes = resolve_source_notes(
            temp.path(),
            &manifest,
            &[
                "src-beta".to_string(),
                "src-alpha".to_string(),
                "raw/src-beta.md".to_string(),
                "alpha.md".to_string(),
            ],
        )
        .expect("source notes");

        assert_eq!(
            notes
                .iter()
                .map(|note| note.path.clone())
                .collect::<Vec<_>>(),
            vec![
                PathBuf::from("raw/src-beta.md"),
                PathBuf::from("raw/src-alpha.md"),
            ]
        );
    }

    #[test]
    fn missing_source_selector_reports_source_not_found() {
        let manifest = SourceManifest {
            entries: vec![source_record(
                "src-alpha",
                "alpha.md",
                "canonical:alpha",
                Some("Alpha"),
            )],
        };
        let error = resolve_source_selector(&manifest, "missing").expect_err("missing source");

        match error {
            WikiError::NotFound { resource, id } => {
                assert_eq!(resource, "source");
                assert_eq!(id, "missing");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn ambiguous_non_id_selector_reports_invalid_input() {
        let manifest = SourceManifest {
            entries: vec![
                source_record("src-alpha", "shared.md", "canonical:alpha", Some("Alpha")),
                source_record("src-beta", "shared.md", "canonical:beta", Some("Beta")),
            ],
        };
        let error = resolve_source_selector(&manifest, "shared.md").expect_err("ambiguous source");

        match error {
            WikiError::InvalidInput { field, message } => {
                assert_eq!(field, "source");
                assert_eq!(
                    message,
                    "source selector `shared.md` matched multiple records; pass a source id"
                );
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn missing_raw_file_for_selected_source_reports_raw_source_not_found() {
        let temp = tempfile::tempdir().expect("tempdir");
        let manifest = SourceManifest {
            entries: vec![source_record(
                "src-alpha",
                "alpha.md",
                "canonical:alpha",
                Some("Alpha"),
            )],
        };

        let error = resolve_source_notes(temp.path(), &manifest, &["src-alpha".to_string()])
            .expect_err("missing raw source");

        match error {
            WikiError::NotFound { resource, id } => {
                assert_eq!(resource, "raw_source");
                assert_eq!(id, "raw/src-alpha.md");
            }
            other => panic!("unexpected error: {other:?}"),
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
}
