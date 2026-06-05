use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use gobby_core::config::AiRouting;
use serde::Serialize;

use crate::WikiError;
use crate::compile::index_lock_timeout;
use crate::events::{EventMonitor, SessionEvent};
use crate::research_loop::{
    ResearchLoop, ResearchLoopConfig, ResearchLoopDeps, ResearchLoopEvent, ResearchLoopInput,
    ResearchLoopResult,
};
use crate::session::{ResearchScope, ResearchSession};

const MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS: usize = 1000;
/// Enforced research-loop budgets, echoed in JSON output so callers see every
/// limit that can stop a run.
const RESEARCH_MAX_WALL_TIME_SECONDS: u64 = 900;
const RESEARCH_MAX_NOTE_BYTES: usize = 24_000;
const RESEARCH_NOTE_MARKER_STALE_AFTER: Duration = Duration::from_secs(15 * 60);
const RESEARCH_NOTE_MATERIALIZE_TIMEOUT: Duration = Duration::from_secs(10);
const RESEARCH_NOTE_MATERIALIZE_INITIAL_DELAY: Duration = Duration::from_millis(25);
const RESEARCH_NOTE_MATERIALIZE_MAX_DELAY: Duration = Duration::from_millis(250);
// Stable v5 namespace for accepted research-note identities. It is deliberately
// separate from source and audit UUIDs so title/body/source collisions cannot
// alias another research artifact family.
const RESEARCH_NOTE_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
    0x9c, 0x51, 0x3a, 0x5b, 0x35, 0x92, 0x4d, 0x6d, 0x8e, 0x5f, 0x42, 0x1f, 0x20, 0xb7, 0x8a, 0xe4,
]);

#[cfg(test)]
static MATERIALIZING_WAIT_SIGNAL: std::sync::OnceLock<
    std::sync::Mutex<Option<std::sync::mpsc::Sender<()>>>,
> = std::sync::OnceLock::new();

#[cfg(test)]
struct MaterializingWaitSignalGuard;

#[cfg(test)]
impl Drop for MaterializingWaitSignalGuard {
    fn drop(&mut self) {
        if let Some(signal) = MATERIALIZING_WAIT_SIGNAL.get() {
            *signal.lock().expect("materializing wait signal lock") = None;
        }
    }
}

#[cfg(test)]
fn set_materializing_wait_signal(
    sender: std::sync::mpsc::Sender<()>,
) -> MaterializingWaitSignalGuard {
    let signal = MATERIALIZING_WAIT_SIGNAL.get_or_init(|| std::sync::Mutex::new(None));
    *signal.lock().expect("materializing wait signal lock") = Some(sender);
    MaterializingWaitSignalGuard
}

#[cfg(test)]
fn notify_materializing_wait_observed() {
    if let Some(signal) = MATERIALIZING_WAIT_SIGNAL.get()
        && let Some(sender) = signal
            .lock()
            .expect("materializing wait signal lock")
            .take()
    {
        let _ = sender.send(());
    }
}

#[cfg(not(test))]
fn notify_materializing_wait_observed() {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedNoteDraft {
    pub title: String,
    pub body: String,
    pub sources: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResearchOptions {
    pub question: String,
    pub scope: ResearchScope,
    pub source_constraints: Vec<String>,
    pub audit: bool,
    pub max_steps: usize,
    pub max_tokens: usize,
    pub max_sources: usize,
    pub ai: AiRouting,
    pub require_ai: bool,
    pub accepted_notes: Vec<AcceptedNoteDraft>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResearchOutcome {
    pub session: ResearchSession,
    pub audit: bool,
    pub status: ResearchStatus,
    pub stop_reason: ResearchStopReason,
    pub steps_used: usize,
    pub tokens_used: usize,
    pub max_steps: usize,
    pub max_tokens: usize,
    pub max_sources: usize,
    pub max_wall_time_seconds: u64,
    pub max_note_bytes: usize,
    pub write_conflict: bool,
    pub sources_added: Vec<String>,
    pub findings: Vec<AuditFinding>,
    pub gaps: Vec<ResearchGap>,
    pub warnings: Vec<String>,
    pub changed_paths: Vec<PathBuf>,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResearchStatus {
    Ok,
    Partial,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ResearchStopReason {
    Finish,
    BudgetExhausted,
    NoProgress,
    DuplicateFrontier,
    SourceBlocked,
    WriteConflict,
    AiUnavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuditFinding {
    pub fingerprint: String,
    pub severity: AuditSeverity,
    pub kind: String,
    pub message: String,
    pub evidence: Vec<PathBuf>,
    pub validation_status: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AuditSeverity {
    Info,
    Warning,
    Error,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ResearchGap {
    pub reason: String,
    pub evidence: Vec<PathBuf>,
}

pub fn run(options: ResearchOptions) -> Result<ResearchOutcome, WikiError> {
    if !options.audit && options.ai == AiRouting::Off {
        return Err(WikiError::InvalidInput {
            field: "ai",
            message: "research enrichment requires AI; use --audit for deterministic checks"
                .to_string(),
        });
    }
    if options.audit && options.require_ai && matches!(options.ai, AiRouting::Auto | AiRouting::Off)
    {
        return Err(WikiError::InvalidInput {
            field: "require_ai",
            message: "model-assisted audit checks are unavailable for the selected AI route"
                .to_string(),
        });
    }

    let mut session = load_or_create_session(&options)?;
    let mut warnings = Vec::new();
    if options.audit && matches!(options.ai, AiRouting::Off | AiRouting::Auto) {
        warnings.push("ai_unavailable: model-assisted audit checks skipped".to_string());
    }

    let monitor = EventMonitor::for_vault(session.scope.root());
    monitor.append_event(&SessionEvent {
        session_id: session.session_id.clone(),
        dispatch_id: None,
        kind: if options.audit {
            "research_audit_started".to_string()
        } else {
            "research_started".to_string()
        },
        message: "research session active".to_string(),
        timestamp_ms: unix_timestamp_ms()?,
    })?;

    let mut changed_paths = Vec::new();
    let findings = if options.audit {
        deterministic_audit_findings(session.scope.root(), &session)?
    } else {
        let loop_result = run_enrichment_loop(&options, &session)?;
        append_loop_events(&monitor, &session.session_id, &loop_result.events)?;
        for note in &loop_result.accepted_notes {
            if !session
                .accepted_notes
                .iter()
                .any(|accepted| accepted.path == note.path)
            {
                session.accepted_notes.push(note.clone());
            }
        }
        changed_paths.extend(loop_result.changed_paths.clone());
        warnings.extend(loop_result.warnings.clone());

        session.save_checkpoint()?;
        changed_paths.push(ResearchSession::checkpoint_path(session.scope.root()));

        return Ok(ResearchOutcome {
            session,
            audit: false,
            status: enrichment_status(loop_result.stop_reason, loop_result.made_progress()),
            stop_reason: loop_result.stop_reason,
            steps_used: loop_result.steps_used,
            tokens_used: loop_result.tokens_used,
            max_steps: options.max_steps,
            max_tokens: options.max_tokens,
            max_sources: options.max_sources,
            max_wall_time_seconds: RESEARCH_MAX_WALL_TIME_SECONDS,
            max_note_bytes: RESEARCH_MAX_NOTE_BYTES,
            write_conflict: loop_result.write_conflict,
            sources_added: loop_result.sources_added,
            findings: Vec::new(),
            gaps: loop_result.gaps,
            warnings,
            changed_paths,
            message: loop_result.message,
        });
    };

    session.save_checkpoint()?;
    changed_paths.push(ResearchSession::checkpoint_path(session.scope.root()));

    let stop_reason = if !warnings.is_empty()
        && options.audit
        && matches!(options.ai, AiRouting::Off | AiRouting::Auto)
    {
        ResearchStopReason::AiUnavailable
    } else {
        ResearchStopReason::Finish
    };
    let status = if stop_reason == ResearchStopReason::AiUnavailable {
        ResearchStatus::Partial
    } else {
        ResearchStatus::Ok
    };

    Ok(ResearchOutcome {
        session,
        audit: options.audit,
        status,
        stop_reason,
        steps_used: 1,
        tokens_used: 0,
        max_steps: options.max_steps,
        max_tokens: options.max_tokens,
        max_sources: options.max_sources,
        max_wall_time_seconds: RESEARCH_MAX_WALL_TIME_SECONDS,
        max_note_bytes: RESEARCH_MAX_NOTE_BYTES,
        write_conflict: false,
        sources_added: Vec::new(),
        findings,
        gaps: Vec::new(),
        warnings,
        changed_paths,
        message: if options.audit {
            "research audit completed".to_string()
        } else {
            "research session checkpointed".to_string()
        },
    })
}

fn run_enrichment_loop(
    options: &ResearchOptions,
    session: &ResearchSession,
) -> Result<ResearchLoopResult, WikiError> {
    let selection = scope_selection_from_research_scope(&session.scope);
    let mut model = GcoreResearchModel {
        requested_route: options.ai,
        require_ai: options.require_ai,
    };
    let mut ask = CommandAsk {
        selection: selection.clone(),
    };
    let mut search = CommandSearch {
        selection: selection.clone(),
    };
    let mut read = CommandRead {
        selection: selection.clone(),
    };
    let mut ingest = CommandIngestor { selection };
    let mut note_writer = AcceptedNoteWriter {
        root: session.scope.root(),
        session_id: &session.session_id,
    };
    let mut loop_ = ResearchLoop::new(
        session.scope.root(),
        ResearchLoopConfig {
            max_steps: options.max_steps,
            max_tokens: options.max_tokens,
            max_sources: options.max_sources,
            max_wall_time: Duration::from_secs(RESEARCH_MAX_WALL_TIME_SECONDS),
            max_note_bytes: RESEARCH_MAX_NOTE_BYTES,
        },
        ResearchLoopDeps {
            model: &mut model,
            ask: &mut ask,
            search: &mut search,
            read: &mut read,
            ingest: &mut ingest,
            note_writer: &mut note_writer,
        },
    );

    loop_.run(ResearchLoopInput {
        question: &session.question,
        source_constraints: &session.source_constraints,
        initial_notes: &options.accepted_notes,
    })
}

fn append_loop_events(
    monitor: &EventMonitor,
    session_id: &str,
    events: &[ResearchLoopEvent],
) -> Result<(), WikiError> {
    for event in events {
        monitor.append_event(&SessionEvent {
            session_id: session_id.to_string(),
            dispatch_id: None,
            kind: event.kind.clone(),
            message: event.message.clone(),
            timestamp_ms: unix_timestamp_ms()?,
        })?;
    }
    Ok(())
}

fn enrichment_status(stop_reason: ResearchStopReason, made_progress: bool) -> ResearchStatus {
    match stop_reason {
        ResearchStopReason::Finish => ResearchStatus::Ok,
        ResearchStopReason::WriteConflict => ResearchStatus::Failed,
        ResearchStopReason::SourceBlocked if !made_progress => ResearchStatus::Failed,
        ResearchStopReason::AiUnavailable
        | ResearchStopReason::BudgetExhausted
        | ResearchStopReason::NoProgress
        | ResearchStopReason::DuplicateFrontier
        | ResearchStopReason::SourceBlocked => ResearchStatus::Partial,
    }
}

mod model;
mod notes;
mod outcome;
mod storage;

pub(crate) use model::*;
pub use outcome::*;
pub(crate) use storage::*;

#[cfg(test)]
mod tests;
