use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

use crate::WikiError;
use crate::research::{AcceptedNoteDraft, ResearchGap, ResearchStopReason};
use crate::session::AcceptedResearchNote;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ResearchLoopConfig {
    pub max_steps: usize,
    pub max_tokens: usize,
    pub max_sources: usize,
    pub max_wall_time: Duration,
    pub max_note_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ResearchLoopInput<'a> {
    pub question: &'a str,
    pub source_constraints: &'a [String],
    pub initial_notes: &'a [AcceptedNoteDraft],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ModelRequest<'a> {
    pub question: &'a str,
    pub source_constraints: &'a [String],
    pub step_index: usize,
    pub max_steps: usize,
    pub tokens_used: usize,
    pub max_tokens: usize,
    pub sources_added: &'a [String],
    pub max_sources: usize,
    pub known_sources: &'a [String],
    pub observations: &'a [ResearchObservation],
    pub gaps: &'a [ResearchGap],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ModelDecision {
    pub action: ResearchAction,
    pub tokens_used: usize,
}

#[derive(Debug)]
pub(crate) enum ResearchModelError {
    AiUnavailable(String),
    BudgetExceeded,
    InvalidResponse(String),
    Wiki(WikiError),
}

impl From<WikiError> for ResearchModelError {
    fn from(error: WikiError) -> Self {
        Self::Wiki(error)
    }
}

pub(crate) trait ResearchModel {
    fn next_action(
        &mut self,
        request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError>;
}

pub(crate) trait WikiAsk {
    fn ask(&mut self, query: &str) -> Result<ResearchObservation, WikiError>;
}

pub(crate) trait WikiSearch {
    fn search(&mut self, query: &str, limit: usize) -> Result<ResearchObservation, WikiError>;
}

pub(crate) trait WikiRead {
    fn read(&mut self, path: &Path) -> Result<ResearchObservation, WikiError>;
}

pub(crate) trait SourceIngestor {
    fn ingest_url(&mut self, url: &str) -> Result<ResearchObservation, WikiError>;

    fn ingest_file(&mut self, path: &Path) -> Result<ResearchObservation, WikiError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct NoteWriteOutcome {
    pub note: AcceptedResearchNote,
    pub created: bool,
    pub write_conflict: bool,
}

pub(crate) trait ResearchNoteWriter {
    fn write_note(&mut self, note: &AcceptedNoteDraft) -> Result<NoteWriteOutcome, WikiError>;
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub(crate) enum ResearchAction {
    Ask {
        query: String,
    },
    Search {
        query: String,
    },
    Read {
        path: PathBuf,
    },
    IngestUrl {
        url: String,
    },
    IngestFile {
        path: PathBuf,
    },
    AcceptNote {
        title: String,
        body: String,
        #[serde(default)]
        sources: Vec<String>,
    },
    RecordGap {
        reason: String,
        #[serde(default)]
        evidence: Vec<PathBuf>,
    },
    Finish {
        #[serde(default)]
        reason: Option<String>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ResearchObservation {
    pub action: String,
    pub summary: String,
    pub sources: Vec<String>,
    pub changed_paths: Vec<PathBuf>,
}

impl ResearchObservation {
    pub(crate) fn new(action: impl Into<String>, summary: impl Into<String>) -> Self {
        Self {
            action: action.into(),
            summary: summary.into(),
            sources: Vec::new(),
            changed_paths: Vec::new(),
        }
    }

    pub(crate) fn with_sources(mut self, sources: Vec<String>) -> Self {
        self.sources = sources;
        self
    }

    pub(crate) fn with_changed_paths(mut self, changed_paths: Vec<PathBuf>) -> Self {
        self.changed_paths = changed_paths;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ResearchLoopEvent {
    pub kind: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ResearchLoopResult {
    pub stop_reason: ResearchStopReason,
    pub steps_used: usize,
    pub tokens_used: usize,
    pub write_conflict: bool,
    pub sources_added: Vec<String>,
    pub gaps: Vec<ResearchGap>,
    pub warnings: Vec<String>,
    pub changed_paths: Vec<PathBuf>,
    pub accepted_notes: Vec<AcceptedResearchNote>,
    pub events: Vec<ResearchLoopEvent>,
    pub message: String,
}

impl ResearchLoopResult {
    pub(crate) fn made_progress(&self) -> bool {
        !self.sources_added.is_empty()
            || !self.changed_paths.is_empty()
            || !self.accepted_notes.is_empty()
            || !self.gaps.is_empty()
    }
}

pub(crate) struct ResearchLoopDeps<'a> {
    pub model: &'a mut dyn ResearchModel,
    pub ask: &'a mut dyn WikiAsk,
    pub search: &'a mut dyn WikiSearch,
    pub read: &'a mut dyn WikiRead,
    pub ingest: &'a mut dyn SourceIngestor,
    pub note_writer: &'a mut dyn ResearchNoteWriter,
}

#[cfg(test)]
#[derive(Default)]
pub(crate) struct ResearchLoopDepsBuilder<'a> {
    model: Option<&'a mut dyn ResearchModel>,
    ask: Option<&'a mut dyn WikiAsk>,
    search: Option<&'a mut dyn WikiSearch>,
    read: Option<&'a mut dyn WikiRead>,
    ingest: Option<&'a mut dyn SourceIngestor>,
    note_writer: Option<&'a mut dyn ResearchNoteWriter>,
}

#[cfg(test)]
impl<'a> ResearchLoopDepsBuilder<'a> {
    pub(crate) fn model(mut self, model: &'a mut dyn ResearchModel) -> Self {
        self.model = Some(model);
        self
    }

    pub(crate) fn ask(mut self, ask: &'a mut dyn WikiAsk) -> Self {
        self.ask = Some(ask);
        self
    }

    pub(crate) fn search(mut self, search: &'a mut dyn WikiSearch) -> Self {
        self.search = Some(search);
        self
    }

    pub(crate) fn read(mut self, read: &'a mut dyn WikiRead) -> Self {
        self.read = Some(read);
        self
    }

    pub(crate) fn ingest(mut self, ingest: &'a mut dyn SourceIngestor) -> Self {
        self.ingest = Some(ingest);
        self
    }

    pub(crate) fn note_writer(mut self, note_writer: &'a mut dyn ResearchNoteWriter) -> Self {
        self.note_writer = Some(note_writer);
        self
    }

    pub(crate) fn build(self) -> Result<ResearchLoopDeps<'a>, &'static str> {
        Ok(ResearchLoopDeps {
            model: self.model.ok_or("model")?,
            ask: self.ask.ok_or("ask")?,
            search: self.search.ok_or("search")?,
            read: self.read.ok_or("read")?,
            ingest: self.ingest.ok_or("ingest")?,
            note_writer: self.note_writer.ok_or("note_writer")?,
        })
    }
}
