use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use gobby_core::config::AiRouting;
use serde::Serialize;

use crate::compile::index_lock_timeout;
use crate::events::{EventMonitor, SessionEvent};
use crate::scope::{self, ScopeKind};
use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession, research_prompt};
use crate::{ScopeSelection, WikiError};

const MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS: usize = 1000;
const RESEARCH_NOTE_MARKER_STALE_AFTER: Duration = Duration::from_secs(15 * 60);
const RESEARCH_NOTE_MATERIALIZE_TIMEOUT: Duration = Duration::from_secs(10);
const RESEARCH_NOTE_MATERIALIZE_INITIAL_DELAY: Duration = Duration::from_millis(25);
const RESEARCH_NOTE_MATERIALIZE_MAX_DELAY: Duration = Duration::from_millis(250);
const RESEARCH_NOTE_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
    0x9c, 0x51, 0x3a, 0x5b, 0x35, 0x92, 0x4d, 0x6d, 0x8e, 0x5f, 0x42, 0x1f, 0x20, 0xb7, 0x8a, 0xe4,
]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedNoteDraft {
    pub title: String,
    pub body: String,
    pub source: Option<String>,
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

    let findings = if options.audit {
        deterministic_audit_findings(session.scope.root(), &session)?
    } else {
        Vec::new()
    };
    let mut changed_paths = Vec::new();

    for note in &options.accepted_notes {
        let accepted = write_accepted_note(session.scope.root(), &session.session_id, note)?;
        if accepted.created {
            changed_paths.push(accepted.note.path.clone());
            monitor.append_event(&SessionEvent {
                session_id: session.session_id.clone(),
                dispatch_id: None,
                kind: "note_accepted".to_string(),
                message: format!("accepted research note {}", accepted.note.title),
                timestamp_ms: unix_timestamp_ms()?,
            })?;
        }
        if !session
            .accepted_notes
            .iter()
            .any(|note| note.path == accepted.note.path)
        {
            session.accepted_notes.push(accepted.note);
        }
    }

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
    #[serde(skip_serializing_if = "Option::is_none")]
    source: Option<&'a str>,
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
        source: note.source.as_deref(),
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
    if let Some(source) = &note.source {
        key.push_str(source.trim());
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
        options.accepted_notes = vec![AcceptedNoteDraft {
            title: "Session event monitoring".to_string(),
            body: "Durable event logs are appended as JSONL.".to_string(),
            source: Some("daemon session stream".to_string()),
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
                source: None,
            },
        )
        .expect("first note");
        let second = write_accepted_note(
            root,
            "research-1",
            &AcceptedNoteDraft {
                title: "Same title".to_string(),
                body: "second".to_string(),
                source: None,
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
            source: Some("same source".to_string()),
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
            source: None,
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
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(50));
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

        assert_eq!(outcome.audit, true);
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
        options.accepted_notes = vec![AcceptedNoteDraft {
            title: "Tracked note".to_string(),
            body: "Tracked body.".to_string(),
            source: None,
        }];
        run(options).expect("accepted note written");

        let mut audit_options = default_options("Audit wiki scope", scope);
        audit_options.audit = true;
        audit_options.ai = AiRouting::Off;
        let outcome = run(audit_options).expect("audit ran");

        assert!(outcome.findings.is_empty());
    }
}
