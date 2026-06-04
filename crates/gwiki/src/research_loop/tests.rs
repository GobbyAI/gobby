use super::*;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::WikiError;
use crate::research::{AcceptedNoteDraft, ResearchStopReason};
use crate::session::AcceptedResearchNote;

#[derive(Default)]
struct FakeModel {
    decisions: Vec<ModelDecision>,
}

impl FakeModel {
    fn new(decisions: Vec<ModelDecision>) -> Self {
        Self {
            decisions: decisions.into_iter().rev().collect(),
        }
    }
}

impl ResearchModel for FakeModel {
    fn next_action(
        &mut self,
        _request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        self.decisions
            .pop()
            .ok_or_else(|| ResearchModelError::InvalidResponse("no decision".to_string()))
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
            },
            created: !self.conflict,
            write_conflict: self.conflict,
        })
    }
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
fn model_planned_note_is_written_after_source_is_observed() {
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
        Path::new("/tmp/wiki"),
        config(),
        ResearchLoopDeps {
            model: &mut model,
            ask: &mut ask,
            search: &mut search,
            read: &mut read,
            ingest: &mut ingest,
            note_writer: &mut writer,
        },
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
        Path::new("/tmp/wiki"),
        config(),
        ResearchLoopDeps {
            model: &mut model,
            ask: &mut ask,
            search: &mut search,
            read: &mut read,
            ingest: &mut ingest,
            note_writer: &mut writer,
        },
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
        ResearchLoopDeps {
            model: &mut model,
            ask: &mut ask,
            search: &mut search,
            read: &mut read,
            ingest: &mut ingest,
            note_writer: &mut writer,
        },
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
