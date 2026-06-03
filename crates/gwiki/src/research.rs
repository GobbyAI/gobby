use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use gobby_core::ai::{daemon, effective_route, text};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};
use serde::Serialize;
use serde_json::Value;

use crate::commands::{ask, index, read, search};
use crate::compile::index_lock_timeout;
use crate::events::{EventMonitor, SessionEvent};
use crate::research_loop::{
    ModelDecision, ModelRequest, NoteWriteOutcome, ResearchLoop, ResearchLoopConfig,
    ResearchLoopDeps, ResearchLoopEvent, ResearchLoopInput, ResearchLoopResult, ResearchModel,
    ResearchModelError, ResearchNoteWriter, ResearchObservation, SourceIngestor, WikiAsk, WikiRead,
    WikiSearch, model_system_prompt, parse_model_action, render_model_prompt,
};
use crate::scope::{self, ScopeKind};
use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession, research_prompt};
use crate::{CommandOutcome, IngestFileOptions, ReadTarget, ScopeSelection, WikiError};

const MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS: usize = 1000;
const RESEARCH_NOTE_MARKER_STALE_AFTER: Duration = Duration::from_secs(15 * 60);
const RESEARCH_NOTE_MATERIALIZE_TIMEOUT: Duration = Duration::from_secs(10);
const RESEARCH_NOTE_MATERIALIZE_INITIAL_DELAY: Duration = Duration::from_millis(25);
const RESEARCH_NOTE_MATERIALIZE_MAX_DELAY: Duration = Duration::from_millis(250);
const RESEARCH_NOTE_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
    0x9c, 0x51, 0x3a, 0x5b, 0x35, 0x92, 0x4d, 0x6d, 0x8e, 0x5f, 0x42, 0x1f, 0x20, 0xb7, 0x8a, 0xe4,
]);

#[cfg(test)]
static MATERIALIZING_WAIT_SIGNAL: std::sync::OnceLock<
    std::sync::Mutex<Option<std::sync::mpsc::Sender<()>>>,
> = std::sync::OnceLock::new();

#[cfg(test)]
fn set_materializing_wait_signal(sender: std::sync::mpsc::Sender<()>) {
    let signal = MATERIALIZING_WAIT_SIGNAL.get_or_init(|| std::sync::Mutex::new(None));
    *signal.lock().expect("materializing wait signal lock") = Some(sender);
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
        steps_used: usize::from(options.audit),
        tokens_used: 0,
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
            max_wall_time: Duration::from_secs(900),
            max_note_bytes: 24_000,
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

struct GcoreResearchModel {
    requested_route: AiRouting,
    require_ai: bool,
}

impl GcoreResearchModel {
    fn ai_unavailable<T>(&self, message: String) -> Result<T, ResearchModelError> {
        if self.require_ai {
            return Err(WikiError::Config { detail: message }.into());
        }
        Err(ResearchModelError::AiUnavailable(message))
    }
}

impl ResearchModel for GcoreResearchModel {
    fn next_action(
        &mut self,
        request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        let mut source = research_ai_config_source()?;
        let context = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: false,
                forced_routing: Some(self.requested_route),
            },
        );
        let route = effective_route(&context, AiCapability::TextGenerate);
        let prompt = render_model_prompt(&request);
        let result = match route {
            AiRouting::Direct => {
                text::generate_text(&context, &prompt, Some(model_system_prompt()))
            }
            AiRouting::Daemon => {
                daemon::generate_via_daemon(&context, &prompt, Some(model_system_prompt()))
            }
            AiRouting::Auto | AiRouting::Off => {
                return self
                    .ai_unavailable(format!("text generation route '{route:?}' is unavailable"));
            }
        };

        let result = match result {
            Ok(result) => result,
            Err(error) => return self.ai_unavailable(error.to_string()),
        };
        let action =
            parse_model_action(&result.text).map_err(ResearchModelError::InvalidResponse)?;
        Ok(ModelDecision {
            action,
            tokens_used: estimate_tokens(&prompt).saturating_add(estimate_tokens(&result.text)),
        })
    }
}

fn research_ai_config_source() -> Result<AiConfigSource, WikiError> {
    let gobby_home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki research config: {error}"),
    })?;
    AiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for gwiki research: {error}"),
    })
}

struct CommandAsk {
    selection: ScopeSelection,
}

impl WikiAsk for CommandAsk {
    fn ask(&mut self, query: &str) -> Result<ResearchObservation, WikiError> {
        let outcome = ask::execute(
            query.to_string(),
            self.selection.clone(),
            false,
            AiRouting::Off,
            false,
        )?;
        Ok(observation_from_outcome("ask", &outcome))
    }
}

struct CommandSearch {
    selection: ScopeSelection,
}

impl WikiSearch for CommandSearch {
    fn search(&mut self, query: &str, limit: usize) -> Result<ResearchObservation, WikiError> {
        let output = search::retrieve(query.to_string(), self.selection.clone(), limit, true)?;
        let mut sources = Vec::new();
        for result in &output.results {
            sources.push(result.source_path.display().to_string());
            sources.extend(result.sources.iter().cloned());
        }
        Ok(ResearchObservation::new(
            "search",
            format!("{} search hit(s) for {query}", output.results.len()),
        )
        .with_sources(dedup_strings(sources)))
    }
}

struct CommandRead {
    selection: ScopeSelection,
}

impl WikiRead for CommandRead {
    fn read(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        let outcome = read::execute(ReadTarget::Path(path.to_path_buf()), self.selection.clone())?;
        let mut observation = observation_from_outcome("read", &outcome);
        observation.sources.push(path.display().to_string());
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }
}

struct CommandIngestor {
    selection: ScopeSelection,
}

impl SourceIngestor for CommandIngestor {
    fn ingest_url(&mut self, url: &str) -> Result<ResearchObservation, WikiError> {
        let outcome = index::execute_ingest_url(vec![url.to_string()], self.selection.clone())?;
        let mut observation = observation_from_outcome("ingest_url", &outcome);
        if outcome.exit_code == 0 && !observation.sources.iter().any(|source| source == url) {
            observation.sources.push(url.to_string());
        }
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }

    fn ingest_file(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        let outcome = index::execute_ingest_file(
            path.to_path_buf(),
            self.selection.clone(),
            IngestFileOptions::default(),
        )?;
        let mut observation = observation_from_outcome("ingest_file", &outcome);
        let path_string = path.display().to_string();
        if !observation
            .sources
            .iter()
            .any(|source| source == &path_string)
        {
            observation.sources.push(path_string);
        }
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }
}

struct AcceptedNoteWriter<'a> {
    root: &'a Path,
    session_id: &'a str,
}

impl ResearchNoteWriter for AcceptedNoteWriter<'_> {
    fn write_note(&mut self, note: &AcceptedNoteDraft) -> Result<NoteWriteOutcome, WikiError> {
        let accepted = write_accepted_note(self.root, self.session_id, note)?;
        Ok(NoteWriteOutcome {
            note: accepted.note,
            created: accepted.created,
        })
    }
}

fn observation_from_outcome(action: &str, outcome: &CommandOutcome) -> ResearchObservation {
    ResearchObservation::new(action, outcome.result.text.clone())
        .with_sources(outcome_sources(&outcome.result.payload))
        .with_changed_paths(outcome_changed_paths(&outcome.result.payload))
}

fn outcome_sources(payload: &Value) -> Vec<String> {
    const SOURCE_KEYS: &[&str] = &[
        "absolute_path",
        "asset_path",
        "final_url",
        "location",
        "raw_path",
        "requested_url",
        "source_path",
        "sources",
        "wiki_path",
    ];
    let mut sources = Vec::new();
    collect_keyed_strings(payload, SOURCE_KEYS, &mut sources);
    dedup_strings(sources)
}

fn outcome_changed_paths(payload: &Value) -> Vec<PathBuf> {
    const PATH_KEYS: &[&str] = &["absolute_path", "asset_path", "raw_path", "wiki_path"];
    let mut paths = Vec::new();
    collect_keyed_strings(payload, PATH_KEYS, &mut paths);
    dedup_strings(paths)
        .into_iter()
        .map(PathBuf::from)
        .collect()
}

fn collect_keyed_strings(value: &Value, keys: &[&str], out: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if keys.iter().any(|candidate| candidate == key) {
                    collect_strings(value, out);
                }
                collect_keyed_strings(value, keys, out);
            }
        }
        Value::Array(values) => {
            for value in values {
                collect_keyed_strings(value, keys, out);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

fn collect_strings(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::String(value) if !value.trim().is_empty() => out.push(value.trim().to_string()),
        Value::Array(values) => {
            for value in values {
                collect_strings(value, out);
            }
        }
        Value::Object(map) => {
            for value in map.values() {
                collect_strings(value, out);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

fn dedup_strings(values: Vec<String>) -> Vec<String> {
    let mut deduped = Vec::new();
    for value in values {
        if !deduped.iter().any(|existing| existing == &value) {
            deduped.push(value);
        }
    }
    deduped
}

fn scope_selection_from_research_scope(scope: &ResearchScope) -> ScopeSelection {
    match scope {
        ResearchScope::Project { root } => ScopeSelection::project(root.clone()),
        ResearchScope::Topic { name, .. } => ScopeSelection::topic(name.clone()),
    }
}

fn estimate_tokens(text: &str) -> usize {
    text.split_whitespace().count()
}

fn load_or_create_session(options: &ResearchOptions) -> Result<ResearchSession, WikiError> {
    let checkpoint_path = ResearchSession::checkpoint_path(options.scope.root());
    let mut session = if checkpoint_path.exists() {
        ResearchSession::load_checkpoint(options.scope.root())?
    } else {
        ResearchSession::new(
            options.question.clone(),
            options.scope.clone(),
            options.source_constraints.clone(),
            1,
            None,
        )?
    };
    session.question = options.question.clone();
    session.source_constraints = options.source_constraints.clone();
    session.agent_count = 1;
    session.dispatch_task_id = None;
    session.dispatch = None;
    session.prompt = research_prompt(&session.question, &session.source_constraints, 1);
    Ok(session)
}

fn deterministic_audit_findings(
    vault_root: &Path,
    session: &ResearchSession,
) -> Result<Vec<AuditFinding>, WikiError> {
    let research_dir = vault_root.join("raw").join("research");
    let entries = match fs::read_dir(&research_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw research directory",
                path: Some(research_dir),
                source: error,
            });
        }
    };

    let mut note_paths = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw research directory entry",
            path: Some(research_dir.clone()),
            source: error,
        })?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("md") {
            note_paths.push(path);
        }
    }
    note_paths.sort();

    let accepted_paths = session
        .accepted_notes
        .iter()
        .map(|note| note.path.clone())
        .collect::<std::collections::BTreeSet<_>>();
    let mut findings = Vec::new();
    for path in note_paths {
        let contents = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read raw research note for audit",
            path: Some(path.clone()),
            source: error,
        })?;
        let Some(frontmatter) = frontmatter_block(&contents) else {
            continue;
        };
        if yaml_field_eq(frontmatter, "research_status", "materializing")
            && materializing_marker_is_stale(&path)
        {
            findings.push(AuditFinding {
                fingerprint: audit_fingerprint("stale-materializing", &path),
                severity: AuditSeverity::Warning,
                kind: "orphaned_raw_research_note".to_string(),
                message: "materializing research note marker is stale".to_string(),
                evidence: vec![path.clone()],
                validation_status: "deterministic".to_string(),
            });
            continue;
        }
        if yaml_field_eq(frontmatter, "research_status", "completed")
            && !accepted_paths.contains(&path)
        {
            findings.push(AuditFinding {
                fingerprint: audit_fingerprint("untracked-completed-note", &path),
                severity: AuditSeverity::Info,
                kind: "orphaned_raw_research_note".to_string(),
                message: "completed research note is not listed in the active checkpoint"
                    .to_string(),
                evidence: vec![path],
                validation_status: "deterministic".to_string(),
            });
        }
    }

    Ok(findings)
}

fn audit_fingerprint(kind: &str, path: &Path) -> String {
    let input = format!("{kind}:{}", path.display());
    uuid::Uuid::new_v5(&RESEARCH_NOTE_NAMESPACE, input.as_bytes()).to_string()
}

pub fn resolve_scope(selection: &ScopeSelection) -> Result<ResearchScope, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error,
    })?;
    let scope = scope::resolve(selection, &cwd)?;
    Ok(research_scope_from_resolved(&scope))
}

pub fn research_scope_from_resolved(scope: &scope::ResolvedScope) -> ResearchScope {
    match scope.kind() {
        ScopeKind::Topic { name } => ResearchScope::topic(name.clone(), scope.root().to_path_buf()),
        ScopeKind::Project { .. } => ResearchScope::project(scope.root().to_path_buf()),
    }
}

#[derive(Serialize)]
struct AcceptedNoteFrontmatter<'a> {
    title: &'a str,
    research_session: &'a str,
    research_note_id: &'a str,
    research_status: &'a str,
    indexable: bool,
    sources: &'a [String],
}

struct AcceptedNoteWrite {
    note: AcceptedResearchNote,
    created: bool,
}

fn write_accepted_note(
    vault_root: &Path,
    session_id: &str,
    note: &AcceptedNoteDraft,
) -> Result<AcceptedNoteWrite, WikiError> {
    let research_dir = vault_root.join("raw").join("research");
    fs::create_dir_all(&research_dir).map_err(|error| WikiError::Io {
        action: "create raw research directory",
        path: Some(research_dir.clone()),
        source: error,
    })?;

    let draft_id = accepted_note_draft_id(note);
    if let Some(path) = find_completed_accepted_note(&research_dir, &draft_id)? {
        return Ok(AcceptedNoteWrite {
            note: AcceptedResearchNote {
                title: note.title.clone(),
                path,
            },
            created: false,
        });
    }

    let path = match create_materializing_research_note(&research_dir, session_id, note, &draft_id)?
    {
        AcceptedNoteSlot::Existing(path) => {
            return Ok(AcceptedNoteWrite {
                note: AcceptedResearchNote {
                    title: note.title.clone(),
                    path,
                },
                created: false,
            });
        }
        AcceptedNoteSlot::Materializing(path) => path,
    };

    let body = render_accepted_note_body(session_id, note, &draft_id, "completed", true)?;
    if let Err(error) = write_file_atomically(&path, body.as_bytes(), "accepted research note") {
        let _ = fs::remove_file(&path);
        return Err(error);
    }

    if let Err(error) = append_raw_index(vault_root, &note.title, &path) {
        let _ = fs::remove_file(&path);
        return Err(error);
    }

    Ok(AcceptedNoteWrite {
        note: AcceptedResearchNote {
            title: note.title.clone(),
            path,
        },
        created: true,
    })
}

enum AcceptedNoteSlot {
    Existing(PathBuf),
    Materializing(PathBuf),
}

enum ResearchNoteFileState {
    Missing,
    CompletedMatching,
    MaterializingMatching { stale: bool },
    Occupied,
}

fn render_accepted_note_body(
    session_id: &str,
    note: &AcceptedNoteDraft,
    draft_id: &str,
    status: &str,
    indexable: bool,
) -> Result<String, WikiError> {
    let frontmatter = serde_yaml::to_string(&AcceptedNoteFrontmatter {
        title: &note.title,
        research_session: session_id,
        research_note_id: draft_id,
        research_status: status,
        indexable,
        sources: &note.sources,
    })
    .map_err(|error| WikiError::Yaml {
        action: "serialize accepted research note frontmatter",
        path: None,
        source: error,
    })?;
    let mut body = String::new();
    body.push_str("---\n");
    body.push_str(&frontmatter);
    body.push_str("---\n\n");
    body.push_str(note.body.trim());
    body.push('\n');
    Ok(body)
}

fn create_materializing_research_note(
    research_dir: &Path,
    session_id: &str,
    note: &AcceptedNoteDraft,
    draft_id: &str,
) -> Result<AcceptedNoteSlot, WikiError> {
    let title = &note.title;
    let slug = slugify(title);
    for attempt in 1..=MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS {
        let file_name = if attempt == 1 {
            format!("{slug}.md")
        } else {
            format!("{slug}-{attempt}.md")
        };
        let path = research_dir.join(file_name);
        match research_note_file_state(&path, draft_id)? {
            ResearchNoteFileState::CompletedMatching => {
                return Ok(AcceptedNoteSlot::Existing(path));
            }
            ResearchNoteFileState::MaterializingMatching { stale } if stale => {
                fs::remove_file(&path).map_err(|error| WikiError::Io {
                    action: "remove stale accepted research note marker",
                    path: Some(path.clone()),
                    source: error,
                })?;
            }
            ResearchNoteFileState::MaterializingMatching { .. } => {
                if let Some(path) = wait_for_materializing_research_note(&path, draft_id, title)? {
                    return Ok(AcceptedNoteSlot::Existing(path));
                }
                continue;
            }
            ResearchNoteFileState::Occupied => continue,
            ResearchNoteFileState::Missing => {}
        }

        let marker = render_accepted_note_body(session_id, note, draft_id, "materializing", false)?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                if let Err(error) = file.write_all(marker.as_bytes()) {
                    let _ = fs::remove_file(&path);
                    return Err(WikiError::Io {
                        action: "write accepted research note marker",
                        path: Some(path),
                        source: error,
                    });
                }
                if let Err(error) = file.sync_all() {
                    let _ = fs::remove_file(&path);
                    return Err(WikiError::Io {
                        action: "sync accepted research note marker",
                        path: Some(path),
                        source: error,
                    });
                }
                return Ok(AcceptedNoteSlot::Materializing(path));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(WikiError::Io {
                    action: "create accepted research note marker",
                    path: Some(path),
                    source: error,
                });
            }
        }
    }
    Err(WikiError::InvalidInput {
        field: "title",
        message: format!("could not allocate unique research note path for `{title}`"),
    })
}

fn wait_for_materializing_research_note(
    path: &Path,
    draft_id: &str,
    title: &str,
) -> Result<Option<PathBuf>, WikiError> {
    let started = Instant::now();
    let mut delay = RESEARCH_NOTE_MATERIALIZE_INITIAL_DELAY;
    loop {
        match research_note_file_state(path, draft_id)? {
            ResearchNoteFileState::CompletedMatching => return Ok(Some(path.to_path_buf())),
            ResearchNoteFileState::Missing | ResearchNoteFileState::Occupied => return Ok(None),
            ResearchNoteFileState::MaterializingMatching { stale } if stale => {
                fs::remove_file(path).map_err(|error| WikiError::Io {
                    action: "remove stale accepted research note marker",
                    path: Some(path.to_path_buf()),
                    source: error,
                })?;
                return Ok(None);
            }
            ResearchNoteFileState::MaterializingMatching { .. } => {
                if started.elapsed() >= RESEARCH_NOTE_MATERIALIZE_TIMEOUT {
                    return Err(WikiError::InvalidInput {
                        field: "accepted_note",
                        message: format!(
                            "accepted research note `{title}` is still materializing at {}",
                            path.display()
                        ),
                    });
                }
                notify_materializing_wait_observed();
                thread::sleep(delay);
                delay = delay
                    .saturating_mul(2)
                    .min(RESEARCH_NOTE_MATERIALIZE_MAX_DELAY);
            }
        }
    }
}

fn accepted_note_draft_id(note: &AcceptedNoteDraft) -> String {
    let mut key = String::new();
    key.push_str(note.title.trim());
    key.push('\0');
    key.push_str(note.body.trim());
    key.push('\0');
    for source in &note.sources {
        key.push_str(source.trim());
        key.push('\0');
    }
    uuid::Uuid::new_v5(&RESEARCH_NOTE_NAMESPACE, key.as_bytes()).to_string()
}

fn find_completed_accepted_note(
    research_dir: &Path,
    draft_id: &str,
) -> Result<Option<PathBuf>, WikiError> {
    let entries = match fs::read_dir(research_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw research directory",
                path: Some(research_dir.to_path_buf()),
                source: error,
            });
        }
    };

    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw research directory entry",
            path: Some(research_dir.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("md") {
            continue;
        }
        if matches!(
            research_note_file_state(&path, draft_id)?,
            ResearchNoteFileState::CompletedMatching
        ) {
            return Ok(Some(path));
        }
    }
    Ok(None)
}

fn research_note_file_state(
    path: &Path,
    draft_id: &str,
) -> Result<ResearchNoteFileState, WikiError> {
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Ok(ResearchNoteFileState::Missing);
        }
        Err(error) => {
            return Err(WikiError::Io {
                action: "read accepted research note",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    };
    let Some(frontmatter) = frontmatter_block(&contents) else {
        return Ok(ResearchNoteFileState::Occupied);
    };
    if !yaml_field_eq(frontmatter, "research_note_id", draft_id) {
        return Ok(ResearchNoteFileState::Occupied);
    }
    if yaml_field_eq(frontmatter, "research_status", "completed") {
        return Ok(ResearchNoteFileState::CompletedMatching);
    }
    if yaml_field_eq(frontmatter, "research_status", "materializing") {
        return Ok(ResearchNoteFileState::MaterializingMatching {
            stale: materializing_marker_is_stale(path),
        });
    }
    Ok(ResearchNoteFileState::Occupied)
}

fn frontmatter_block(markdown: &str) -> Option<&str> {
    let rest = markdown
        .strip_prefix("---\n")
        .or_else(|| markdown.strip_prefix("---\r\n"))?;
    let end = rest.find("\n---").or_else(|| rest.find("\r\n---"))?;
    Some(&rest[..end])
}

fn yaml_field_eq(frontmatter: &str, key: &str, value: &str) -> bool {
    let plain = format!("{key}: {value}");
    let quoted = format!("{key}: \"{value}\"");
    frontmatter
        .lines()
        .map(str::trim)
        .any(|line| line == plain || line == quoted)
}

fn materializing_marker_is_stale(path: &Path) -> bool {
    fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| SystemTime::now().duration_since(modified).ok())
        .is_some_and(|age| age >= RESEARCH_NOTE_MARKER_STALE_AFTER)
}

fn append_raw_index(vault_root: &Path, title: &str, note_path: &Path) -> Result<(), WikiError> {
    crate::sources::SourceManifest::with_lock(vault_root, || {
        append_raw_index_locked(vault_root, title, note_path)
    })
}

fn append_raw_index_locked(
    vault_root: &Path,
    title: &str,
    note_path: &Path,
) -> Result<(), WikiError> {
    let raw_dir = vault_root.join("raw");
    fs::create_dir_all(&raw_dir).map_err(|error| WikiError::Io {
        action: "create raw directory",
        path: Some(raw_dir.clone()),
        source: error,
    })?;
    let index_path = raw_dir.join("INDEX.md");
    let relative = note_path
        .strip_prefix(vault_root)
        .unwrap_or(note_path)
        .to_string_lossy();
    let lock_path = raw_index_lock_path(vault_root);
    let lock = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open raw index lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_raw_index(&lock, &lock_path)?;
    let mut contents = match fs::read_to_string(&index_path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => "# Raw Sources\n\n".to_string(),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read raw index",
                path: Some(index_path.clone()),
                source: error,
            });
        }
    };
    if contents.is_empty() {
        contents.push_str("# Raw Sources\n\n");
    }
    contents.push_str(&format!("- [{title}]({relative})\n"));
    // Keep the lock handle alive through the full read-modify-write sequence.
    write_file_atomically(&index_path, contents.as_bytes(), "raw index")
}

fn lock_raw_index(lock: &fs::File, lock_path: &Path) -> Result<(), WikiError> {
    let timeout = index_lock_timeout();
    let started = Instant::now();

    loop {
        match fs4::FileExt::try_lock(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    return Err(WikiError::Io {
                        action: "lock raw index",
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(Duration::from_millis(25).min(timeout - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock raw index",
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

fn raw_index_lock_path(vault_root: &Path) -> PathBuf {
    vault_root.join("raw").join("INDEX.md.lock")
}

fn write_file_atomically(
    path: &Path,
    contents: &[u8],
    label: &'static str,
) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: label,
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    Ok(())
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("INDEX.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

fn slugify(title: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;

    for ch in title.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }

    while slug.ends_with('-') {
        slug.pop();
    }

    if slug.is_empty() {
        "research-note".to_string()
    } else {
        slug
    }
}

fn unix_timestamp_ms() -> Result<u64, WikiError> {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| WikiError::Config {
            detail: format!("system clock is before Unix epoch: {error}"),
        })?;
    u64::try_from(duration.as_millis()).map_err(|_| WikiError::Config {
        detail: "system timestamp exceeds u64 milliseconds".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_options(question: &str, scope: ResearchScope) -> ResearchOptions {
        ResearchOptions {
            question: question.to_string(),
            scope,
            source_constraints: Vec::new(),
            audit: false,
            max_steps: 12,
            max_tokens: 24_000,
            max_sources: 8,
            ai: AiRouting::Direct,
            require_ai: false,
            accepted_notes: Vec::new(),
        }
    }

    #[test]
    fn frontmatter_block_accepts_crlf_delimiters() {
        let markdown = "---\r\nresearch_note_id: abc\r\nresearch_status: completed\r\n---\r\nBody";
        let frontmatter = frontmatter_block(markdown).expect("frontmatter");

        assert!(yaml_field_eq(frontmatter, "research_note_id", "abc"));
        assert!(yaml_field_eq(frontmatter, "research_status", "completed"));
    }

    #[test]
    fn research_reloads_checkpoint_without_daemon_dispatch() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let checkpoint = ResearchSession {
            session_id: "research-existing".to_string(),
            question: "What changed in the parser?".to_string(),
            prompt: "Research parser changes".to_string(),
            scope: scope.clone(),
            source_constraints: vec!["official docs only".to_string()],
            agent_count: 2,
            dispatch_task_id: Some("#300".to_string()),
            dispatch: Some(crate::session::DaemonDispatch {
                dispatch_id: "dispatch-existing".to_string(),
                daemon_base_url: "http://daemon.test".to_string(),
                agent_run_ids: vec!["run-a".to_string(), "run-b".to_string()],
            }),
            accepted_notes: Vec::new(),
            compile_state: None,
        };
        checkpoint.save_checkpoint().expect("checkpoint saved");

        let outcome = run(default_options("What should be refreshed?", scope))
            .expect("research checkpoint loaded");

        assert_eq!(outcome.session.session_id, "research-existing");
        assert_eq!(outcome.session.agent_count, 1);
        assert_eq!(outcome.session.dispatch_task_id, None);
        assert_eq!(outcome.session.dispatch, None);
        assert_eq!(outcome.session.question, "What should be refreshed?");
    }

    #[test]
    fn enrichment_rejects_ai_off() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut options = default_options(
            "What should be researched?",
            ResearchScope::project(temp.path()),
        );
        options.ai = AiRouting::Off;
        let error = run(options).expect_err("AI off should be rejected for enrichment");

        assert!(matches!(error, WikiError::InvalidInput { field: "ai", .. }));
    }

    #[test]
    fn accepted_notes_land_in_raw_research() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());

        let mut options = default_options("How should events be monitored?", scope.clone());
        options.source_constraints = vec!["source-linked notes".to_string()];
        options.max_steps = 0;
        options.accepted_notes = vec![AcceptedNoteDraft {
            title: "Session event monitoring".to_string(),
            body: "Durable event logs are appended as JSONL.".to_string(),
            sources: vec!["raw/source.md".to_string()],
        }];
        let outcome = run(options).expect("research ran");

        let note = outcome
            .session
            .accepted_notes
            .first()
            .expect("accepted note recorded");
        assert_eq!(outcome.session.dispatch_task_id, None);
        assert!(note.path.starts_with(scope.root().join("raw/research")));
        assert_eq!(
            note.path.file_name().and_then(|name| name.to_str()),
            Some("session-event-monitoring.md")
        );

        let note_text = std::fs::read_to_string(&note.path).expect("note written");
        assert!(note_text.contains("title: Session event monitoring"));
        assert!(note_text.contains("research_status: completed"));
        assert!(note_text.contains("research_note_id:"));
        assert!(note_text.contains("sources:"));
        assert!(note_text.contains("raw/source.md"));
        assert!(note_text.contains("Durable event logs are appended as JSONL."));

        let index = std::fs::read_to_string(scope.root().join("raw/INDEX.md"))
            .expect("raw index includes note");
        assert!(index.contains("raw/research/session-event-monitoring.md"));
    }

    #[test]
    fn accepted_note_collisions_use_numeric_suffixes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let first = write_accepted_note(
            root,
            "research-1",
            &AcceptedNoteDraft {
                title: "Same title".to_string(),
                body: "first".to_string(),
                sources: Vec::new(),
            },
        )
        .expect("first note");
        let second = write_accepted_note(
            root,
            "research-1",
            &AcceptedNoteDraft {
                title: "Same title".to_string(),
                body: "second".to_string(),
                sources: Vec::new(),
            },
        )
        .expect("second note");

        assert_eq!(
            first.note.path.file_name().and_then(|name| name.to_str()),
            Some("same-title.md")
        );
        assert_eq!(
            second.note.path.file_name().and_then(|name| name.to_str()),
            Some("same-title-2.md")
        );
    }

    #[test]
    fn accepted_notes_are_idempotent_by_draft_id() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let draft = AcceptedNoteDraft {
            title: "Same title".to_string(),
            body: "same body".to_string(),
            sources: vec!["same-source.md".to_string()],
        };

        let first = write_accepted_note(root, "research-1", &draft).expect("first note");
        let second = write_accepted_note(root, "research-1", &draft).expect("second note");

        assert!(first.created);
        assert!(!second.created);
        assert_eq!(first.note.path, second.note.path);
        let index = std::fs::read_to_string(root.join("raw/INDEX.md")).expect("raw index");
        assert_eq!(index.matches("raw/research/same-title.md").count(), 1);
        assert!(!root.join("raw/research/same-title-2.md").exists());
    }

    #[test]
    fn accepted_note_waits_for_materializing_marker_to_complete() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let research_dir = root.join("raw/research");
        std::fs::create_dir_all(&research_dir).expect("research dir");
        let draft = AcceptedNoteDraft {
            title: "Shared note".to_string(),
            body: "same body".to_string(),
            sources: Vec::new(),
        };
        let draft_id = accepted_note_draft_id(&draft);
        let path = research_dir.join("shared-note.md");
        let marker =
            render_accepted_note_body("research-1", &draft, &draft_id, "materializing", false)
                .expect("marker");
        std::fs::write(&path, marker).expect("write materializing marker");
        let completed_path = path.clone();
        let completed_draft = draft.clone();
        let completed_id = draft_id.clone();
        let (wait_observed_tx, wait_observed_rx) = std::sync::mpsc::channel();
        set_materializing_wait_signal(wait_observed_tx);
        let completer = std::thread::spawn(move || {
            wait_observed_rx.recv().expect("wait observed");
            let body = render_accepted_note_body(
                "research-1",
                &completed_draft,
                &completed_id,
                "completed",
                true,
            )
            .expect("completed body");
            std::fs::write(completed_path, body).expect("complete marker");
        });

        let accepted = write_accepted_note(root, "research-1", &draft).expect("accepted note");
        completer.join().expect("completed marker writer joined");

        assert!(!accepted.created);
        assert_eq!(accepted.note.path, path);
    }

    #[test]
    fn deterministic_audit_reports_untracked_completed_note() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let research_dir = root.join("raw/research");
        std::fs::create_dir_all(&research_dir).expect("research dir");
        let note_path = research_dir.join("orphan.md");
        std::fs::write(
            &note_path,
            "---\nresearch_note_id: orphan\nresearch_status: completed\n---\n\nBody\n",
        )
        .expect("orphan note");

        let mut options = default_options("Audit wiki scope", ResearchScope::project(root));
        options.audit = true;
        options.ai = AiRouting::Off;
        let outcome = run(options).expect("audit ran");

        assert!(outcome.audit);
        assert_eq!(outcome.status, ResearchStatus::Partial);
        assert_eq!(outcome.stop_reason, ResearchStopReason::AiUnavailable);
        assert_eq!(outcome.findings.len(), 1);
        assert_eq!(outcome.findings[0].evidence, vec![note_path]);
        assert_eq!(
            outcome.findings[0].kind,
            "orphaned_raw_research_note".to_string()
        );
        assert_eq!(
            outcome.warnings,
            vec!["ai_unavailable: model-assisted audit checks skipped".to_string()]
        );
    }

    #[test]
    fn deterministic_audit_uses_checkpoint_inventory() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let mut options = default_options("Record accepted note", scope.clone());
        options.max_steps = 0;
        options.accepted_notes = vec![AcceptedNoteDraft {
            title: "Tracked note".to_string(),
            body: "Tracked body.".to_string(),
            sources: vec!["raw/source.md".to_string()],
        }];
        run(options).expect("accepted note written");

        let mut audit_options = default_options("Audit wiki scope", scope);
        audit_options.audit = true;
        audit_options.ai = AiRouting::Off;
        let outcome = run(audit_options).expect("audit ran");

        assert!(outcome.findings.is_empty());
    }
}
