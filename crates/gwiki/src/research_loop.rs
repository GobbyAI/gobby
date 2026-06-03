use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

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

pub(crate) struct ResearchLoop<'a> {
    root: &'a Path,
    config: ResearchLoopConfig,
    model: &'a mut dyn ResearchModel,
    ask: &'a mut dyn WikiAsk,
    search: &'a mut dyn WikiSearch,
    read: &'a mut dyn WikiRead,
    ingest: &'a mut dyn SourceIngestor,
    note_writer: &'a mut dyn ResearchNoteWriter,
}

pub(crate) struct ResearchLoopDeps<'a> {
    pub model: &'a mut dyn ResearchModel,
    pub ask: &'a mut dyn WikiAsk,
    pub search: &'a mut dyn WikiSearch,
    pub read: &'a mut dyn WikiRead,
    pub ingest: &'a mut dyn SourceIngestor,
    pub note_writer: &'a mut dyn ResearchNoteWriter,
}

impl<'a> ResearchLoop<'a> {
    pub(crate) fn new(
        root: &'a Path,
        config: ResearchLoopConfig,
        deps: ResearchLoopDeps<'a>,
    ) -> Self {
        Self {
            root,
            config,
            model: deps.model,
            ask: deps.ask,
            search: deps.search,
            read: deps.read,
            ingest: deps.ingest,
            note_writer: deps.note_writer,
        }
    }

    pub(crate) fn run(
        &mut self,
        input: ResearchLoopInput<'_>,
    ) -> Result<ResearchLoopResult, WikiError> {
        let started = Instant::now();
        let mut state = LoopState::default();

        if self.write_initial_notes(&mut state, input.initial_notes)? {
            return Ok(state.finish(ResearchStopReason::SourceBlocked));
        }

        let mut last_fingerprint: Option<String> = None;
        let mut duplicate_count = 0usize;
        let mut no_progress_count = 0usize;

        loop {
            if state.steps_used >= self.config.max_steps {
                return Ok(state.finish(ResearchStopReason::BudgetExhausted));
            }
            if state.tokens_used >= self.config.max_tokens {
                return Ok(state.finish(ResearchStopReason::BudgetExhausted));
            }
            if started.elapsed() >= self.config.max_wall_time {
                state
                    .warnings
                    .push("research_wall_time_budget_exhausted".to_string());
                return Ok(state.finish(ResearchStopReason::BudgetExhausted));
            }

            let known_sources = sorted_sources(&state.known_sources);
            let decision = match self.model.next_action(ModelRequest {
                question: input.question,
                source_constraints: input.source_constraints,
                step_index: state.steps_used + 1,
                max_steps: self.config.max_steps,
                tokens_used: state.tokens_used,
                max_tokens: self.config.max_tokens,
                sources_added: &state.sources_added,
                max_sources: self.config.max_sources,
                known_sources: &known_sources,
                observations: &state.observations,
                gaps: &state.gaps,
            }) {
                Ok(decision) => decision,
                Err(ResearchModelError::AiUnavailable(message)) => {
                    state.warnings.push(format!("ai_unavailable: {message}"));
                    state.events.push(ResearchLoopEvent {
                        kind: "research_ai_unavailable".to_string(),
                        message,
                    });
                    return Ok(state.finish(ResearchStopReason::AiUnavailable));
                }
                Err(ResearchModelError::InvalidResponse(message)) => {
                    state
                        .warnings
                        .push(format!("model_response_invalid: {message}"));
                    state.events.push(ResearchLoopEvent {
                        kind: "research_model_response_invalid".to_string(),
                        message,
                    });
                    return Ok(state.finish(ResearchStopReason::NoProgress));
                }
                Err(ResearchModelError::Wiki(error)) => return Err(error),
            };

            state.steps_used += 1;
            state.tokens_used = state.tokens_used.saturating_add(decision.tokens_used);
            if state.tokens_used > self.config.max_tokens {
                return Ok(state.finish(ResearchStopReason::BudgetExhausted));
            }

            let fingerprint = action_fingerprint(&decision.action);
            if last_fingerprint.as_deref() == Some(fingerprint.as_str()) {
                duplicate_count += 1;
            } else {
                duplicate_count = 1;
                last_fingerprint = Some(fingerprint);
            }
            if duplicate_count >= 3 {
                return Ok(state.finish(ResearchStopReason::DuplicateFrontier));
            }

            match self.execute_action(&mut state, decision.action)? {
                StepOutcome::Continue { progress } => {
                    if progress {
                        no_progress_count = 0;
                    } else {
                        no_progress_count += 1;
                    }
                    if no_progress_count >= 3 {
                        return Ok(state.finish(ResearchStopReason::NoProgress));
                    }
                }
                StepOutcome::Stop(reason) => return Ok(state.finish(reason)),
            }
        }
    }

    fn write_initial_notes(
        &mut self,
        state: &mut LoopState,
        notes: &[AcceptedNoteDraft],
    ) -> Result<bool, WikiError> {
        for note in notes {
            let Some(validated) = self.validated_note(note.clone(), None, state) else {
                return Ok(true);
            };
            self.write_validated_note(state, &validated)?;
        }
        Ok(false)
    }

    fn execute_action(
        &mut self,
        state: &mut LoopState,
        action: ResearchAction,
    ) -> Result<StepOutcome, WikiError> {
        match action {
            ResearchAction::Ask { query } => {
                let observation = self.ask.ask(&query)?;
                let progress =
                    state.record_observation(observation, false, &self.config, self.root);
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::Search { query } => {
                let observation = self.search.search(&query, self.config.max_sources)?;
                let progress =
                    state.record_observation(observation, false, &self.config, self.root);
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::Read { path } => {
                let observation = self.read.read(&path)?;
                let progress =
                    state.record_observation(observation, false, &self.config, self.root);
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::IngestUrl { url } => {
                if state.sources_added.len() >= self.config.max_sources {
                    return Ok(StepOutcome::Stop(ResearchStopReason::BudgetExhausted));
                }
                let observation = self.ingest.ingest_url(&url)?;
                let progress = state.record_observation(observation, true, &self.config, self.root);
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::IngestFile { path } => {
                if state.sources_added.len() >= self.config.max_sources {
                    return Ok(StepOutcome::Stop(ResearchStopReason::BudgetExhausted));
                }
                let observation = self.ingest.ingest_file(&path)?;
                let progress = state.record_observation(observation, true, &self.config, self.root);
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::AcceptNote {
                title,
                body,
                sources,
            } => {
                let draft = AcceptedNoteDraft {
                    title,
                    body,
                    sources,
                };
                let known_sources = state.known_sources.clone();
                let Some(validated) = self.validated_note(draft, Some(&known_sources), state)
                else {
                    return Ok(StepOutcome::Stop(ResearchStopReason::SourceBlocked));
                };
                let progress = self.write_validated_note(state, &validated)?;
                Ok(StepOutcome::Continue { progress })
            }
            ResearchAction::RecordGap { reason, evidence } => {
                let reason = reason.trim();
                if reason.is_empty() {
                    return Ok(StepOutcome::Continue { progress: false });
                }
                state.gaps.push(ResearchGap {
                    reason: reason.to_string(),
                    evidence,
                });
                state.events.push(ResearchLoopEvent {
                    kind: "research_gap_recorded".to_string(),
                    message: reason.to_string(),
                });
                Ok(StepOutcome::Continue { progress: true })
            }
            ResearchAction::Finish { reason } => {
                if let Some(reason) = reason.filter(|reason| !reason.trim().is_empty()) {
                    state.message = Some(reason.clone());
                    state.events.push(ResearchLoopEvent {
                        kind: "research_finished".to_string(),
                        message: reason,
                    });
                }
                Ok(StepOutcome::Stop(ResearchStopReason::Finish))
            }
        }
    }

    fn write_validated_note(
        &mut self,
        state: &mut LoopState,
        note: &AcceptedNoteDraft,
    ) -> Result<bool, WikiError> {
        let outcome = self.note_writer.write_note(note)?;
        if outcome.created {
            state.changed_paths.push(outcome.note.path.clone());
        }
        state.write_conflict |= outcome.write_conflict;
        if !state
            .accepted_notes
            .iter()
            .any(|accepted| accepted.path == outcome.note.path)
        {
            state.accepted_notes.push(outcome.note.clone());
        }
        state.events.push(ResearchLoopEvent {
            kind: "note_accepted".to_string(),
            message: format!("accepted research note {}", outcome.note.title),
        });
        Ok(outcome.created)
    }

    fn validated_note(
        &self,
        mut note: AcceptedNoteDraft,
        known_sources: Option<&HashSet<String>>,
        state: &mut LoopState,
    ) -> Option<AcceptedNoteDraft> {
        note.title = note.title.trim().to_string();
        note.body = note.body.trim().to_string();
        note.sources = normalize_sources(&note.sources);

        let gap = if note.title.is_empty() {
            Some(ResearchGap {
                reason: "accepted note title is empty".to_string(),
                evidence: Vec::new(),
            })
        } else if note.body.is_empty() {
            Some(ResearchGap {
                reason: format!("accepted note '{}' body is empty", note.title),
                evidence: Vec::new(),
            })
        } else if note.body.len() > self.config.max_note_bytes {
            Some(ResearchGap {
                reason: format!(
                    "accepted note '{}' exceeds {} byte budget",
                    note.title, self.config.max_note_bytes
                ),
                evidence: Vec::new(),
            })
        } else if note.sources.is_empty() {
            Some(ResearchGap {
                reason: format!("accepted note '{}' has no sources", note.title),
                evidence: Vec::new(),
            })
        } else {
            self.unsupported_source_gap(&note, known_sources)
        };

        if let Some(gap) = gap {
            state.events.push(ResearchLoopEvent {
                kind: "research_source_blocked".to_string(),
                message: gap.reason.clone(),
            });
            state.gaps.push(gap);
            return None;
        }

        Some(note)
    }

    fn unsupported_source_gap(
        &self,
        note: &AcceptedNoteDraft,
        known_sources: Option<&HashSet<String>>,
    ) -> Option<ResearchGap> {
        for source in &note.sources {
            if let Err(reason) = validate_source_reference(self.root, source) {
                return Some(ResearchGap {
                    reason: format!(
                        "accepted note '{}' cites unsupported source '{}': {reason}",
                        note.title, source
                    ),
                    evidence: source_evidence(self.root, source),
                });
            }
            if let Some(known_sources) = known_sources
                && !known_sources.contains(source)
                && !source_path_aliases(self.root, source)
                    .iter()
                    .any(|alias| known_sources.contains(alias))
            {
                return Some(ResearchGap {
                    reason: format!(
                        "accepted note '{}' cites source '{}' before it is observed",
                        note.title, source
                    ),
                    evidence: source_evidence(self.root, source),
                });
            }
        }
        None
    }
}

#[derive(Debug, Default)]
struct LoopState {
    steps_used: usize,
    tokens_used: usize,
    write_conflict: bool,
    sources_added: Vec<String>,
    known_sources: HashSet<String>,
    observations: Vec<ResearchObservation>,
    gaps: Vec<ResearchGap>,
    warnings: Vec<String>,
    changed_paths: Vec<PathBuf>,
    accepted_notes: Vec<AcceptedResearchNote>,
    events: Vec<ResearchLoopEvent>,
    message: Option<String>,
}

impl LoopState {
    fn record_observation(
        &mut self,
        observation: ResearchObservation,
        adds_sources: bool,
        config: &ResearchLoopConfig,
        root: &Path,
    ) -> bool {
        let mut progress = !observation.summary.trim().is_empty();
        for source in &observation.sources {
            self.known_sources.insert(source.clone());
            for alias in source_path_aliases(root, source) {
                self.known_sources.insert(alias);
            }
            if adds_sources
                && self.sources_added.len() < config.max_sources
                && !self.sources_added.iter().any(|added| added == source)
            {
                self.sources_added.push(source.clone());
                progress = true;
            }
        }
        if !observation.changed_paths.is_empty() {
            self.changed_paths.extend(observation.changed_paths.clone());
            progress = true;
        }
        self.events.push(ResearchLoopEvent {
            kind: format!("research_{}", observation.action),
            message: observation.summary.clone(),
        });
        self.observations.push(observation);
        progress
    }

    fn finish(self, stop_reason: ResearchStopReason) -> ResearchLoopResult {
        ResearchLoopResult {
            stop_reason,
            steps_used: self.steps_used,
            tokens_used: self.tokens_used,
            write_conflict: self.write_conflict,
            sources_added: self.sources_added,
            gaps: self.gaps,
            warnings: self.warnings,
            changed_paths: self.changed_paths,
            accepted_notes: self.accepted_notes,
            events: self.events,
            message: self
                .message
                .unwrap_or_else(|| default_stop_message(stop_reason).to_string()),
        }
    }
}

enum StepOutcome {
    Continue { progress: bool },
    Stop(ResearchStopReason),
}

pub(crate) fn parse_model_action(response: &str) -> Result<ResearchAction, String> {
    let json = extract_json_object(response)?;
    serde_json::from_str(json).map_err(|error| format!("failed to parse action JSON: {error}"))
}

pub(crate) fn render_model_prompt(request: &ModelRequest<'_>) -> String {
    let mut prompt = format!(
        "Research question: {}\nStep: {}/{}\nTokens used: {}/{}\nSources added: {}/{}\n",
        request.question,
        request.step_index,
        request.max_steps,
        request.tokens_used,
        request.max_tokens,
        request.sources_added.len(),
        request.max_sources
    );
    if !request.source_constraints.is_empty() {
        prompt.push_str("Source constraints:\n");
        for constraint in request.source_constraints {
            prompt.push_str("- ");
            prompt.push_str(constraint);
            prompt.push('\n');
        }
    }
    if !request.known_sources.is_empty() {
        prompt.push_str("Observed sources:\n");
        for source in request.known_sources.iter().take(20) {
            prompt.push_str("- ");
            prompt.push_str(source);
            prompt.push('\n');
        }
    }
    if !request.observations.is_empty() {
        prompt.push_str("Recent observations:\n");
        let start = request.observations.len().saturating_sub(8);
        for observation in &request.observations[start..] {
            prompt.push_str("- ");
            prompt.push_str(&observation.action);
            prompt.push_str(": ");
            prompt.push_str(observation.summary.trim());
            prompt.push('\n');
        }
    }
    if !request.gaps.is_empty() {
        prompt.push_str("Recorded gaps:\n");
        for gap in request.gaps.iter().rev().take(8) {
            prompt.push_str("- ");
            prompt.push_str(&gap.reason);
            prompt.push('\n');
        }
    }
    prompt.push_str(
        "Return one JSON object only. Supported actions are: \
         {\"action\":\"ask\",\"query\":\"...\"}, \
         {\"action\":\"search\",\"query\":\"...\"}, \
         {\"action\":\"read\",\"path\":\"...\"}, \
         {\"action\":\"ingest_url\",\"url\":\"https://...\"}, \
         {\"action\":\"ingest_file\",\"path\":\"...\"}, \
         {\"action\":\"accept_note\",\"title\":\"...\",\"body\":\"...\",\"sources\":[\"...\"]}, \
         {\"action\":\"record_gap\",\"reason\":\"...\",\"evidence\":[\"...\"]}, \
         {\"action\":\"finish\",\"reason\":\"...\"}. \
         Accepted notes must cite only observed sources.",
    );
    prompt
}

pub(crate) fn model_system_prompt() -> &'static str {
    "You are gwiki research planning. Choose exactly one source-grounded action as JSON. \
     Do not write accepted notes until cited sources have been observed."
}

fn extract_json_object(response: &str) -> Result<&str, String> {
    let trimmed = response.trim();
    let trimmed = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .unwrap_or(trimmed)
        .trim();
    let trimmed = trimmed.strip_suffix("```").unwrap_or(trimmed).trim();
    if trimmed.starts_with('{') && trimmed.ends_with('}') {
        return Ok(trimmed);
    }
    let start = trimmed
        .find('{')
        .ok_or_else(|| "model response did not include a JSON object".to_string())?;
    let end = trimmed
        .rfind('}')
        .ok_or_else(|| "model response did not include a complete JSON object".to_string())?;
    if start >= end {
        return Err("model response JSON object is empty".to_string());
    }
    Ok(&trimmed[start..=end])
}

fn action_fingerprint(action: &ResearchAction) -> String {
    format!("{action:?}")
}

fn normalize_sources(sources: &[String]) -> Vec<String> {
    let mut normalized = Vec::new();
    for source in sources {
        let source = source.trim();
        if source.is_empty() || normalized.iter().any(|existing| existing == source) {
            continue;
        }
        normalized.push(source.to_string());
    }
    normalized
}

fn validate_source_reference(root: &Path, source: &str) -> Result<(), String> {
    if let Ok(url) = url::Url::parse(source) {
        return match url.scheme() {
            "http" | "https" | "file" => Ok(()),
            scheme => Err(format!("unsupported URL scheme '{scheme}'")),
        };
    }

    let path = Path::new(source);
    if path.as_os_str().is_empty() {
        return Err("source path is empty".to_string());
    }
    if path
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err("source path cannot contain parent traversal".to_string());
    }
    if path.is_absolute() {
        if path.starts_with(root) {
            return Ok(());
        }
        return Err("absolute source path is outside the wiki scope".to_string());
    }
    if path
        .components()
        .any(|component| matches!(component, Component::Prefix(_) | Component::RootDir))
    {
        return Err("relative source path must stay inside the wiki scope".to_string());
    }
    Ok(())
}

fn source_evidence(root: &Path, source: &str) -> Vec<PathBuf> {
    let path = Path::new(source);
    if path.as_os_str().is_empty() || url::Url::parse(source).is_ok() {
        return Vec::new();
    }
    if path.is_absolute() {
        vec![path.to_path_buf()]
    } else {
        vec![root.join(path)]
    }
}

fn source_path_aliases(root: &Path, source: &str) -> Vec<String> {
    if url::Url::parse(source).is_ok() {
        return Vec::new();
    }
    let path = Path::new(source);
    let mut aliases = Vec::new();
    if path.is_absolute() {
        if let Ok(relative) = path.strip_prefix(root) {
            aliases.push(relative.display().to_string());
        }
    } else {
        aliases.push(root.join(path).display().to_string());
    }
    aliases
}

fn sorted_sources(sources: &HashSet<String>) -> Vec<String> {
    let mut sources = sources.iter().cloned().collect::<Vec<_>>();
    sources.sort();
    sources
}

fn default_stop_message(stop_reason: ResearchStopReason) -> &'static str {
    match stop_reason {
        ResearchStopReason::Finish => "research session completed",
        ResearchStopReason::BudgetExhausted => "research budget exhausted",
        ResearchStopReason::NoProgress => "research stopped after no progress",
        ResearchStopReason::DuplicateFrontier => "research stopped on duplicate frontier",
        ResearchStopReason::SourceBlocked => "research stopped on unsupported source",
        ResearchStopReason::WriteConflict => "research stopped on write conflict",
        ResearchStopReason::AiUnavailable => "research stopped because AI is unavailable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    }

    impl ResearchNoteWriter for FakeWriter {
        fn write_note(&mut self, note: &AcceptedNoteDraft) -> Result<NoteWriteOutcome, WikiError> {
            self.notes.push(note.clone());
            Ok(NoteWriteOutcome {
                note: AcceptedResearchNote {
                    title: note.title.clone(),
                    path: PathBuf::from(format!("raw/research/{}.md", note.title)),
                },
                created: true,
                write_conflict: false,
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
        let action = parse_model_action(
            "```json\n{\"action\":\"finish\",\"reason\":\"enough evidence\"}\n```",
        )
        .expect("action parsed");
        assert_eq!(
            action,
            ResearchAction::Finish {
                reason: Some("enough evidence".to_string())
            }
        );
    }
}
