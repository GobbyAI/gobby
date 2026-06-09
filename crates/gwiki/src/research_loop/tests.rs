use super::*;
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::WikiError;
use crate::research::{AcceptedNoteDraft, ResearchStopReason};
use crate::research_loop::helpers::validate_source_reference;
use crate::session::{AcceptedResearchNote, ResearchCodeCitation};

#[derive(Default)]
struct FakeModel {
    decisions: VecDeque<ModelDecision>,
}

impl FakeModel {
    fn new(decisions: Vec<ModelDecision>) -> Self {
        Self {
            decisions: decisions.into(),
        }
    }
}

impl ResearchModel for FakeModel {
    fn next_action(
        &mut self,
        _request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        self.decisions
            .pop_front()
            .ok_or_else(|| ResearchModelError::InvalidResponse("no decision".to_string()))
    }
}

struct BudgetModel;

impl ResearchModel for BudgetModel {
    fn next_action(
        &mut self,
        _request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        Err(ResearchModelError::BudgetExceeded)
    }
}

struct AiUnavailableModel;

impl ResearchModel for AiUnavailableModel {
    fn next_action(
        &mut self,
        _request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        Err(ResearchModelError::AiUnavailable(
            "text generation route is off".to_string(),
        ))
    }
}

struct FakeSearch;

impl WikiSearch for FakeSearch {
    fn search(&mut self, query: &str, _limit: usize) -> Result<ResearchObservation, WikiError> {
        Ok(
            ResearchObservation::new("search", format!("searched {query}"))
                .with_sources(vec!["raw/source.md".to_string()]),
        )
    }
}

struct FakeAsk;

impl WikiAsk for FakeAsk {
    fn ask(&mut self, query: &str) -> Result<ResearchObservation, WikiError> {
        Ok(ResearchObservation::new("ask", format!("asked {query}")))
    }
}

struct FakeRead;

impl WikiRead for FakeRead {
    fn read(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        Ok(ResearchObservation::new("read", path.display().to_string())
            .with_sources(vec![path.display().to_string()]))
    }
}

struct FakeIngest;

impl SourceIngestor for FakeIngest {
    fn ingest_url(&mut self, url: &str) -> Result<ResearchObservation, WikiError> {
        Ok(ResearchObservation::new("ingest_url", url).with_sources(vec![url.to_string()]))
    }

    fn ingest_file(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        Ok(
            ResearchObservation::new("ingest_file", path.display().to_string())
                .with_sources(vec![path.display().to_string()]),
        )
    }
}

#[derive(Default)]
struct FakeWriter {
    notes: Vec<AcceptedNoteDraft>,
    conflict: bool,
}

impl ResearchNoteWriter for FakeWriter {
    fn write_note(&mut self, note: &AcceptedNoteDraft) -> Result<NoteWriteOutcome, WikiError> {
        self.notes.push(note.clone());
        Ok(NoteWriteOutcome {
            note: AcceptedResearchNote {
                title: note.title.clone(),
                path: PathBuf::from(format!("raw/research/{}.md", note.title)),
                code_citations: note.code_citations.clone(),
                degradation: note.degradation.clone(),
            },
            created: !self.conflict,
            write_conflict: self.conflict,
        })
    }
}

fn test_deps<'a>(
    model: &'a mut dyn ResearchModel,
    ask: &'a mut dyn WikiAsk,
    search: &'a mut dyn WikiSearch,
    read: &'a mut dyn WikiRead,
    ingest: &'a mut dyn SourceIngestor,
    note_writer: &'a mut dyn ResearchNoteWriter,
) -> ResearchLoopDeps<'a> {
    ResearchLoopDepsBuilder::default()
        .model(model)
        .ask(ask)
        .search(search)
        .read(read)
        .ingest(ingest)
        .note_writer(note_writer)
        .build()
        .expect("all research loop dependencies are set")
}

#[test]
fn research_loop_deps_builder_reports_missing_required_fields() {
    let missing_model = match ResearchLoopDepsBuilder::default().build() {
        Err(field) => field,
        Ok(_) => panic!("builder should require model"),
    };
    assert_eq!(missing_model, ResearchLoopDepsBuildError::Model);

    let mut model = FakeModel::default();
    let mut ask = FakeAsk;
    let mut search = FakeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let missing_writer = match ResearchLoopDepsBuilder::default()
        .model(&mut model)
        .ask(&mut ask)
        .search(&mut search)
        .read(&mut read)
        .ingest(&mut ingest)
        .build()
    {
        Err(field) => field,
        Ok(_) => panic!("builder should require note_writer"),
    };
    assert_eq!(missing_writer, ResearchLoopDepsBuildError::NoteWriter);
}

#[test]
fn model_budget_error_stops_as_budget_exhausted() {
    let mut model = BudgetModel;
    let mut ask = FakeAsk;
    let mut search = FakeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        Path::new("/tmp/wiki"),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );

    let result = loop_
        .run(ResearchLoopInput {
            question: "What is exhausted?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");

    assert_eq!(result.stop_reason, ResearchStopReason::BudgetExhausted);
}

fn config() -> ResearchLoopConfig {
    ResearchLoopConfig {
        max_steps: 12,
        max_tokens: 24_000,
        max_sources: 8,
        max_wall_time: Duration::from_secs(900),
        max_note_bytes: 24_000,
    }
}

#[test]
fn research_code_citations_flow_into_accepted_notes() {
    struct CodeSearch;

    impl WikiSearch for CodeSearch {
        fn search(
            &mut self,
            _query: &str,
            _limit: usize,
        ) -> Result<ResearchObservation, WikiError> {
            Ok(ResearchObservation::new("search", "1 code hit")
                .with_sources(vec!["src/handler.rs".to_string()])
                .with_code_citations(vec![
                    ResearchCodeCitation::new(
                        "src/handler.rs",
                        Some(12),
                        Some("handle".to_string()),
                        vec!["search".to_string(), "graph_context".to_string()],
                    )
                    .expect("code citation"),
                ]))
        }
    }

    let root = tempfile::tempdir().expect("wiki root");
    let src_dir = root.path().join("src");
    std::fs::create_dir_all(&src_dir).expect("src dir");
    std::fs::write(src_dir.join("handler.rs"), "fn handle() {}").expect("source file");

    let expected = ResearchCodeCitation::new(
        "src/handler.rs",
        Some(12),
        Some("handle".to_string()),
        vec!["search".to_string(), "graph_context".to_string()],
    )
    .expect("expected citation");
    let mut model = FakeModel::new(vec![
        ModelDecision {
            action: ResearchAction::Search {
                query: "handler".to_string(),
            },
            tokens_used: 5,
        },
        ModelDecision {
            action: ResearchAction::AcceptNote {
                title: "Handler note".to_string(),
                body: "The handler is grounded in code.".to_string(),
                sources: vec!["src/handler.rs".to_string()],
            },
            tokens_used: 7,
        },
        ModelDecision {
            action: ResearchAction::Finish {
                reason: Some("done".to_string()),
            },
            tokens_used: 1,
        },
    ]);

    let mut ask = FakeAsk;
    let mut search = CodeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        root.path(),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "Where is handling wired?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");

    assert_eq!(result.stop_reason, ResearchStopReason::Finish);
    assert_eq!(writer.notes[0].code_citations, vec![expected.clone()]);
    assert_eq!(result.accepted_notes[0].code_citations, vec![expected]);
}

#[test]
fn research_code_model_off_returns_retrieval_only_scaffold() {
    struct ScaffoldSearch;

    impl WikiSearch for ScaffoldSearch {
        fn search(
            &mut self,
            _query: &str,
            _limit: usize,
        ) -> Result<ResearchObservation, WikiError> {
            Ok(ResearchObservation::new("search", "1 candidate source")
                .with_sources(vec!["code/files/src/handler.rs.md".to_string()])
                .with_code_citations(vec![
                    ResearchCodeCitation::new(
                        "src/handler.rs",
                        Some(3),
                        Some("handle".to_string()),
                        vec!["retrieval_scaffold".to_string()],
                    )
                    .expect("code citation"),
                ]))
        }
    }

    let mut model = AiUnavailableModel;
    let mut ask = FakeAsk;
    let mut search = ScaffoldSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        Path::new("/tmp/wiki"),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "Where is handling wired?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");

    assert_eq!(result.stop_reason, ResearchStopReason::AiUnavailable);
    assert!(writer.notes.is_empty());
    assert!(result.accepted_notes.is_empty());
    assert_eq!(
        result.candidate_sources,
        vec!["code/files/src/handler.rs.md".to_string()]
    );
    assert_eq!(result.code_citations.len(), 1);
    assert_eq!(
        result.degradation,
        Some("model_unavailable: text generation route is off".to_string())
    );
}

#[test]
fn research_code_graph_off_keeps_docs_only_degradation_on_notes() {
    struct DocsOnlySearch;

    impl WikiSearch for DocsOnlySearch {
        fn search(
            &mut self,
            _query: &str,
            _limit: usize,
        ) -> Result<ResearchObservation, WikiError> {
            Ok(ResearchObservation::new("search", "1 docs hit")
                .with_sources(vec!["raw/source.md".to_string()])
                .with_degradations(vec!["shared_code_graph_unavailable".to_string()]))
        }
    }

    let root = tempfile::tempdir().expect("wiki root");
    let raw_dir = root.path().join("raw");
    std::fs::create_dir_all(&raw_dir).expect("raw dir");
    std::fs::write(raw_dir.join("source.md"), "source").expect("source file");

    let mut model = FakeModel::new(vec![
        ModelDecision {
            action: ResearchAction::Search {
                query: "events".to_string(),
            },
            tokens_used: 5,
        },
        ModelDecision {
            action: ResearchAction::AcceptNote {
                title: "Docs note".to_string(),
                body: "The answer is grounded in docs only.".to_string(),
                sources: vec!["raw/source.md".to_string()],
            },
            tokens_used: 7,
        },
        ModelDecision {
            action: ResearchAction::Finish {
                reason: Some("done".to_string()),
            },
            tokens_used: 1,
        },
    ]);

    let mut ask = FakeAsk;
    let mut search = DocsOnlySearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        root.path(),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "How are events persisted?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");

    assert_eq!(result.stop_reason, ResearchStopReason::Finish);
    assert!(writer.notes[0].code_citations.is_empty());
    assert_eq!(
        writer.notes[0].degradation,
        Some("shared_code_graph_unavailable".to_string())
    );
    assert_eq!(
        result.degradation,
        Some("shared_code_graph_unavailable".to_string())
    );
}

#[test]
fn model_planned_note_is_written_after_source_is_observed() {
    let root = tempfile::tempdir().expect("wiki root");
    let raw_dir = root.path().join("raw");
    std::fs::create_dir_all(&raw_dir).expect("raw dir");
    std::fs::write(raw_dir.join("source.md"), "source").expect("source file");

    let mut model = FakeModel::new(vec![
        ModelDecision {
            action: ResearchAction::Search {
                query: "events".to_string(),
            },
            tokens_used: 10,
        },
        ModelDecision {
            action: ResearchAction::AcceptNote {
                title: "Event notes".to_string(),
                body: "Events are persisted.".to_string(),
                sources: vec!["raw/source.md".to_string()],
            },
            tokens_used: 12,
        },
        ModelDecision {
            action: ResearchAction::Finish {
                reason: Some("done".to_string()),
            },
            tokens_used: 2,
        },
    ]);

    let mut ask = FakeAsk;
    let mut search = FakeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        root.path(),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "How are events persisted?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");
    assert_eq!(result.stop_reason, ResearchStopReason::Finish);
    assert_eq!(writer.notes.len(), 1);
    assert_eq!(writer.notes[0].sources, vec!["raw/source.md".to_string()]);
}

#[test]
fn write_conflict_stops_the_run_without_recording_the_note() {
    let root = tempfile::tempdir().expect("wiki root");
    let raw_dir = root.path().join("raw");
    std::fs::create_dir_all(&raw_dir).expect("raw dir");
    std::fs::write(raw_dir.join("source.md"), "source").expect("source file");

    let mut model = FakeModel::new(vec![
        ModelDecision {
            action: ResearchAction::Search {
                query: "events".to_string(),
            },
            tokens_used: 10,
        },
        ModelDecision {
            action: ResearchAction::AcceptNote {
                title: "Event notes".to_string(),
                body: "Events are persisted.".to_string(),
                sources: vec!["raw/source.md".to_string()],
            },
            tokens_used: 12,
        },
    ]);

    let mut ask = FakeAsk;
    let mut search = FakeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter {
        conflict: true,
        ..Default::default()
    };
    let mut loop_ = ResearchLoop::new(
        root.path(),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "How are events persisted?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");
    assert_eq!(result.stop_reason, ResearchStopReason::WriteConflict);
    assert!(result.write_conflict);
    // The run halts without recording or overwriting the note.
    assert!(result.accepted_notes.is_empty());
    assert!(result.changed_paths.is_empty());
}

#[test]
fn accepted_note_without_observed_source_is_blocked() {
    let mut model = FakeModel::new(vec![ModelDecision {
        action: ResearchAction::AcceptNote {
            title: "Unsupported".to_string(),
            body: "A claim with no source.".to_string(),
            sources: vec!["raw/missing.md".to_string()],
        },
        tokens_used: 7,
    }]);

    let mut ask = FakeAsk;
    let mut search = FakeSearch;
    let mut read = FakeRead;
    let mut ingest = FakeIngest;
    let mut writer = FakeWriter::default();
    let mut loop_ = ResearchLoop::new(
        Path::new("/tmp/wiki"),
        config(),
        test_deps(
            &mut model,
            &mut ask,
            &mut search,
            &mut read,
            &mut ingest,
            &mut writer,
        ),
    );
    let result = loop_
        .run(ResearchLoopInput {
            question: "What is unsupported?",
            source_constraints: &[],
            initial_notes: &[],
        })
        .expect("loop runs");
    assert_eq!(result.stop_reason, ResearchStopReason::SourceBlocked);
    assert!(writer.notes.is_empty());
    assert_eq!(result.gaps.len(), 1);
}

#[test]
fn parses_fenced_json_action() {
    let action =
        parse_model_action("```json\n{\"action\":\"finish\",\"reason\":\"enough evidence\"}\n```")
            .expect("action parsed");
    assert_eq!(
        action,
        ResearchAction::Finish {
            reason: Some("enough evidence".to_string())
        }
    );
}

#[test]
fn file_url_source_references_use_path_scope_validation() {
    let root = tempfile::tempdir().expect("wiki root");
    let inside = root.path().join("raw/source.md");
    std::fs::create_dir_all(inside.parent().expect("inside parent")).expect("inside raw dir");
    std::fs::write(&inside, "source").expect("inside source");
    let inside_url = url::Url::from_file_path(&inside).expect("inside file URL");
    assert!(validate_source_reference(root.path(), inside_url.as_str()).is_ok());

    let outside = tempfile::tempdir().expect("outside root");
    std::fs::create_dir_all(outside.path().join("raw")).expect("outside raw dir");
    std::fs::write(outside.path().join("raw/source.md"), "source").expect("outside source");
    let outside_url =
        url::Url::from_file_path(outside.path().join("raw/source.md")).expect("outside file URL");
    let error = validate_source_reference(root.path(), outside_url.as_str())
        .expect_err("outside file URL rejected");

    assert!(error.contains("outside the wiki scope"));
}

#[cfg(unix)]
#[test]
fn source_reference_rejects_relative_symlink_escape() {
    let root = tempfile::tempdir().expect("wiki root");
    let outside = tempfile::tempdir().expect("outside root");
    let outside_source = outside.path().join("source.md");
    std::fs::write(&outside_source, "source").expect("outside source");

    let raw_dir = root.path().join("raw");
    std::fs::create_dir_all(&raw_dir).expect("raw dir");
    std::os::unix::fs::symlink(&outside_source, raw_dir.join("source.md")).expect("source symlink");

    let error = validate_source_reference(root.path(), "raw/source.md")
        .expect_err("symlink escape rejected");

    assert!(error.contains("relative source path must stay inside the wiki scope"));
}
