use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use serde::Serialize;

use crate::compile::index_lock_timeout;
use crate::events::{EventMonitor, SessionEvent};
use crate::scope::{self, ScopeKind};
use crate::session::{AcceptedResearchNote, DaemonDispatch, ResearchScope, ResearchSession};
use crate::{ScopeSelection, WikiError};

const MAX_RESEARCH_NOTE_SUFFIX_ATTEMPTS: usize = 1000;
const RESEARCH_NOTE_MARKER_STALE_AFTER: Duration = Duration::from_secs(15 * 60);
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
    pub agent_count: usize,
    pub dispatch_task_id: Option<String>,
    pub resume: bool,
    pub accepted_notes: Vec<AcceptedNoteDraft>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResearchOutcome {
    pub session: ResearchSession,
    pub message: String,
}

pub trait ResearchDispatcher {
    fn dispatch(&self, session: &ResearchSession) -> Result<DaemonDispatch, WikiError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GobbyDaemonResearchDispatcher {
    pub base_url: String,
}

pub fn run(options: ResearchOptions) -> Result<ResearchOutcome, WikiError> {
    run_with_dispatcher(options, &GobbyDaemonResearchDispatcher::default())
}

pub fn run_with_dispatcher(
    options: ResearchOptions,
    dispatcher: &impl ResearchDispatcher,
) -> Result<ResearchOutcome, WikiError> {
    let mut session = if options.resume {
        ResearchSession::load_checkpoint(options.scope.root())?
    } else {
        let mut session = ResearchSession::new(
            options.question.clone(),
            options.scope.clone(),
            options.source_constraints.clone(),
            options.agent_count,
            options.dispatch_task_id.clone(),
        )?;
        session.dispatch = Some(dispatcher.dispatch(&session)?);
        session
    };

    let monitor = EventMonitor::for_vault(session.scope.root());
    monitor.append_event(&SessionEvent {
        session_id: session.session_id.clone(),
        dispatch_id: session
            .dispatch
            .as_ref()
            .map(|dispatch| dispatch.dispatch_id.clone()),
        kind: if options.resume {
            "research_resumed".to_string()
        } else {
            "research_dispatched".to_string()
        },
        message: "research session active".to_string(),
        timestamp_ms: unix_timestamp_ms()?,
    })?;

    for note in &options.accepted_notes {
        let accepted = write_accepted_note(session.scope.root(), &session.session_id, note)?;
        if accepted.created {
            monitor.append_event(&SessionEvent {
                session_id: session.session_id.clone(),
                dispatch_id: session
                    .dispatch
                    .as_ref()
                    .map(|dispatch| dispatch.dispatch_id.clone()),
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

    Ok(ResearchOutcome {
        session,
        message: "research session checkpointed".to_string(),
    })
}

impl Default for GobbyDaemonResearchDispatcher {
    fn default() -> Self {
        Self {
            base_url: gobby_core::daemon_url::daemon_url(),
        }
    }
}

impl ResearchDispatcher for GobbyDaemonResearchDispatcher {
    fn dispatch(&self, session: &ResearchSession) -> Result<DaemonDispatch, WikiError> {
        let endpoint = "/api/agents/spawn";
        let url = format!("{}{}", self.base_url.trim_end_matches('/'), endpoint);
        let task_id =
            session
                .dispatch_task_id
                .as_deref()
                .ok_or_else(|| WikiError::InvalidInput {
                    field: "task_id",
                    message: "daemon research dispatch requires a Gobby task id".to_string(),
                })?;
        let mut agent_run_ids = Vec::with_capacity(session.agent_count);
        let mut dispatch_id = None;

        for worker_number in 1..=session.agent_count {
            let payload = serde_json::json!({
                "task_id": task_id,
                "agent_name": "researcher",
                "prompt": worker_prompt(session, worker_number),
                "workflow": "gwiki_research",
                "isolation": "none",
            });
            let response = ureq::post(&url)
                .timeout(Duration::from_secs(30))
                .set("Content-Type", "application/json")
                .send_string(&payload.to_string())
                .map_err(|error| daemon_error(endpoint, error));
            let response = match response {
                Ok(response) => response,
                Err(error) => {
                    stop_spawned_agents(&self.base_url, &agent_run_ids);
                    return Err(error);
                }
            };
            let response_body = match response.into_string() {
                Ok(body) => body,
                Err(error) => {
                    stop_spawned_agents(&self.base_url, &agent_run_ids);
                    return Err(WikiError::Io {
                        action: "read daemon agent dispatch response",
                        path: None,
                        source: error,
                    });
                }
            };
            let response: AgentSpawnResponse = match serde_json::from_str(&response_body) {
                Ok(response) => response,
                Err(error) => {
                    stop_spawned_agents(&self.base_url, &agent_run_ids);
                    return Err(WikiError::Json {
                        action: "parse daemon agent dispatch response",
                        path: None,
                        source: error,
                    });
                }
            };

            if response.success == Some(false) {
                stop_spawned_agents(&self.base_url, &agent_run_ids);
                return Err(WikiError::Daemon {
                    endpoint,
                    message: response
                        .error
                        .unwrap_or_else(|| "daemon rejected research dispatch".to_string()),
                });
            }

            let run_id = match response
                .run_id
                .clone()
                .or_else(|| response.child_session_id.clone())
                .or_else(|| response.conversation_id.clone())
            {
                Some(run_id) => run_id,
                None => {
                    stop_spawned_agents(&self.base_url, &agent_run_ids);
                    return Err(WikiError::Daemon {
                        endpoint,
                        message: "daemon dispatch response did not include a run id".to_string(),
                    });
                }
            };

            dispatch_id.get_or_insert_with(|| {
                response
                    .dispatch_id
                    .clone()
                    .unwrap_or_else(|| run_id.clone())
            });
            agent_run_ids.push(run_id);
        }

        Ok(DaemonDispatch {
            dispatch_id: dispatch_id.unwrap_or_else(|| session.session_id.clone()),
            daemon_base_url: self.base_url.clone(),
            agent_run_ids,
        })
    }
}

fn stop_spawned_agents(base_url: &str, run_ids: &[String]) {
    let endpoint = "/api/mcp/gobby-agents/tools/stop_agent";
    let url = format!("{}{}", base_url.trim_end_matches('/'), endpoint);
    for run_id in run_ids {
        let payload = serde_json::json!({ "run_id": run_id });
        let _ = ureq::post(&url)
            .timeout(Duration::from_secs(10))
            .set("Content-Type", "application/json")
            .send_string(&payload.to_string());
    }
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

#[derive(Debug, serde::Deserialize)]
struct AgentSpawnResponse {
    success: Option<bool>,
    dispatch_id: Option<String>,
    run_id: Option<String>,
    child_session_id: Option<String>,
    conversation_id: Option<String>,
    error: Option<String>,
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

fn worker_prompt(session: &ResearchSession, worker_number: usize) -> String {
    format!(
        "{}\n\nWorker {worker_number} of {}. Record findings as concise source notes.",
        session.prompt, session.agent_count
    )
}

fn daemon_error(endpoint: &'static str, error: ureq::Error) -> WikiError {
    match error {
        ureq::Error::Status(status, response) => {
            let body = response.into_string().unwrap_or_default();
            WikiError::Daemon {
                endpoint,
                message: if body.is_empty() {
                    format!("daemon returned HTTP {status}")
                } else {
                    format!("daemon returned HTTP {status}: {body}")
                },
            }
        }
        ureq::Error::Transport(error) => WikiError::Daemon {
            endpoint,
            message: error.to_string(),
        },
    }
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
                return Err(WikiError::InvalidInput {
                    field: "accepted_note",
                    message: format!(
                        "accepted research note `{title}` is already materializing at {}",
                        path.display()
                    ),
                });
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
    let rest = markdown.strip_prefix("---\n")?;
    let end = rest.find("\n---")?;
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

    struct FakeDispatcher;

    impl ResearchDispatcher for FakeDispatcher {
        fn dispatch(&self, _session: &ResearchSession) -> Result<DaemonDispatch, WikiError> {
            Ok(DaemonDispatch {
                dispatch_id: "dispatch-123".to_string(),
                daemon_base_url: "http://daemon.test".to_string(),
                agent_run_ids: vec!["run-1".to_string(), "run-2".to_string()],
            })
        }
    }

    #[test]
    fn resume_reloads_checkpoint() {
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
            dispatch: Some(DaemonDispatch {
                dispatch_id: "dispatch-existing".to_string(),
                daemon_base_url: "http://daemon.test".to_string(),
                agent_run_ids: vec!["run-a".to_string(), "run-b".to_string()],
            }),
            accepted_notes: Vec::new(),
            compile_state: None,
        };
        checkpoint.save_checkpoint().expect("checkpoint saved");

        let outcome = run_with_dispatcher(
            ResearchOptions {
                question: "ignored on resume".to_string(),
                scope,
                source_constraints: Vec::new(),
                agent_count: 99,
                dispatch_task_id: None,
                resume: true,
                accepted_notes: Vec::new(),
            },
            &FakeDispatcher,
        )
        .expect("research resumed");

        assert_eq!(outcome.session.session_id, "research-existing");
        assert_eq!(outcome.session.agent_count, 2);
        assert_eq!(outcome.session.dispatch_task_id.as_deref(), Some("#300"));
        assert_eq!(
            outcome
                .session
                .dispatch
                .as_ref()
                .map(|dispatch| dispatch.dispatch_id.as_str()),
            Some("dispatch-existing")
        );
        assert_eq!(
            outcome.session.source_constraints,
            vec!["official docs only".to_string()]
        );
    }

    #[test]
    fn accepted_notes_land_in_raw_research() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());

        let outcome = run_with_dispatcher(
            ResearchOptions {
                question: "How should events be monitored?".to_string(),
                scope: scope.clone(),
                source_constraints: vec!["source-linked notes".to_string()],
                agent_count: 2,
                dispatch_task_id: Some("#300".to_string()),
                resume: false,
                accepted_notes: vec![AcceptedNoteDraft {
                    title: "Session event monitoring".to_string(),
                    body: "Durable event logs are appended as JSONL.".to_string(),
                    source: Some("daemon session stream".to_string()),
                }],
            },
            &FakeDispatcher,
        )
        .expect("research ran");

        let note = outcome
            .session
            .accepted_notes
            .first()
            .expect("accepted note recorded");
        assert_eq!(outcome.session.dispatch_task_id.as_deref(), Some("#300"));
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
    fn daemon_dispatch_requires_task_id() {
        let temp = tempfile::tempdir().expect("tempdir");
        let session = ResearchSession::new(
            "What should workers read?",
            ResearchScope::project(temp.path()),
            Vec::new(),
            1,
            None,
        )
        .expect("session");

        let error = GobbyDaemonResearchDispatcher {
            base_url: "http://127.0.0.1:1".to_string(),
        }
        .dispatch(&session)
        .expect_err("missing task id is rejected before network dispatch");

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "task_id",
                ..
            }
        ));
    }
}
