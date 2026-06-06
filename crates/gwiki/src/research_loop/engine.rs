use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::WikiError;
use crate::research::{AcceptedNoteDraft, ResearchGap, ResearchStopReason};
use crate::session::AcceptedResearchNote;

use super::helpers::{
    action_fingerprint, default_stop_message, normalize_sources, sorted_sources, source_evidence,
    source_path_aliases, validate_source_reference,
};
use super::types::{
    ModelRequest, ResearchAction, ResearchLoopConfig, ResearchLoopDeps, ResearchLoopEvent,
    ResearchLoopInput, ResearchLoopResult, ResearchModel, ResearchModelError, ResearchNoteWriter,
    ResearchObservation, SourceIngestor, WikiAsk, WikiRead, WikiSearch,
};

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

        if let Some(reason) = self.write_initial_notes(&mut state, input.initial_notes)? {
            return Ok(state.finish(reason));
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
                Err(ResearchModelError::BudgetExceeded) => {
                    state
                        .warnings
                        .push("research_token_budget_exhausted".to_string());
                    state.events.push(ResearchLoopEvent {
                        kind: "research_budget_exhausted".to_string(),
                        message: "remaining token budget is zero".to_string(),
                    });
                    return Ok(state.finish(ResearchStopReason::BudgetExhausted));
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
                duplicate_count = 0;
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
    ) -> Result<Option<ResearchStopReason>, WikiError> {
        for note in notes {
            let validated = match self.validated_note(note.clone(), None) {
                Ok(validated) => validated,
                Err(gap) => {
                    record_validation_gap(state, gap);
                    return Ok(Some(ResearchStopReason::SourceBlocked));
                }
            };
            if matches!(
                self.write_validated_note(state, &validated)?,
                NoteWriteResult::Conflict
            ) {
                return Ok(Some(ResearchStopReason::WriteConflict));
            }
        }
        Ok(None)
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
                let validated = match self.validated_note(draft, Some(&state.known_sources)) {
                    Ok(validated) => validated,
                    Err(gap) => {
                        record_validation_gap(state, gap);
                        return Ok(StepOutcome::Stop(ResearchStopReason::SourceBlocked));
                    }
                };
                match self.write_validated_note(state, &validated)? {
                    NoteWriteResult::Conflict => {
                        Ok(StepOutcome::Stop(ResearchStopReason::WriteConflict))
                    }
                    NoteWriteResult::Written { progress } => Ok(StepOutcome::Continue { progress }),
                }
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
    ) -> Result<NoteWriteResult, WikiError> {
        let outcome = self.note_writer.write_note(note)?;
        if outcome.write_conflict {
            // The target note changed since draft validation. Per the research
            // contract, record the conflict and exit without overwriting or
            // recording a partial note body.
            state.write_conflict = true;
            state.events.push(ResearchLoopEvent {
                kind: "research_write_conflict".to_string(),
                message: format!(
                    "write conflict for research note {}; exiting without overwriting",
                    outcome.note.title
                ),
            });
            return Ok(NoteWriteResult::Conflict);
        }
        if outcome.created {
            state.changed_paths.push(outcome.note.path.clone());
        }
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
        Ok(NoteWriteResult::Written {
            progress: outcome.created,
        })
    }

    fn validated_note(
        &self,
        mut note: AcceptedNoteDraft,
        known_sources: Option<&HashSet<String>>,
    ) -> Result<AcceptedNoteDraft, ResearchGap> {
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
            return Err(gap);
        }

        Ok(note)
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

fn record_validation_gap(state: &mut LoopState, gap: ResearchGap) {
    state.events.push(ResearchLoopEvent {
        kind: "research_source_blocked".to_string(),
        message: gap.reason.clone(),
    });
    state.gaps.push(gap);
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

/// Result of attempting to write a validated note. A genuine write conflict
/// (the target note changed since draft validation) halts the run without
/// overwriting; a successful write reports whether it created a new note.
enum NoteWriteResult {
    Written { progress: bool },
    Conflict,
}
