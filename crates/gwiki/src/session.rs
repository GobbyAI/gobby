use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::{
    WikiError,
    scope::{ResolvedScope, ScopeKind},
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ResearchScope {
    Project { root: PathBuf },
    Topic { name: String, root: PathBuf },
}

impl ResearchScope {
    pub fn project(root: impl Into<PathBuf>) -> Self {
        Self::Project { root: root.into() }
    }

    pub fn topic(name: impl Into<String>, root: impl Into<PathBuf>) -> Self {
        Self::Topic {
            name: name.into(),
            root: root.into(),
        }
    }

    pub fn root(&self) -> &Path {
        match self {
            Self::Project { root } | Self::Topic { root, .. } => root,
        }
    }
}

impl From<&ResolvedScope> for ResearchScope {
    fn from(scope: &ResolvedScope) -> Self {
        match scope.kind() {
            ScopeKind::Topic { name } => Self::topic(name.clone(), scope.root().to_path_buf()),
            ScopeKind::Project { .. } => Self::project(scope.root().to_path_buf()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DaemonDispatch {
    pub dispatch_id: String,
    pub daemon_base_url: String,
    pub agent_run_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AcceptedResearchNote {
    pub title: String,
    pub path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompileState {
    pub handoff_id: String,
    pub topic: String,
    pub bundle_path: PathBuf,
    pub selected_note_paths: Vec<PathBuf>,
    pub selected_source_titles: Vec<String>,
    pub citations: Vec<String>,
    pub conflicting_claims: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub write_intent: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResearchSession {
    pub session_id: String,
    pub question: String,
    pub prompt: String,
    pub scope: ResearchScope,
    pub source_constraints: Vec<String>,
    pub agent_count: usize,
    pub dispatch_task_id: Option<String>,
    pub dispatch: Option<DaemonDispatch>,
    pub accepted_notes: Vec<AcceptedResearchNote>,
    #[serde(default)]
    pub compile_state: Option<CompileState>,
}

impl ResearchSession {
    pub fn new(
        question: impl Into<String>,
        scope: ResearchScope,
        source_constraints: Vec<String>,
        agent_count: usize,
        dispatch_task_id: Option<String>,
    ) -> Result<Self, WikiError> {
        if agent_count == 0 {
            return Err(WikiError::InvalidInput {
                field: "agent_count",
                message: "research requires at least one worker".to_string(),
            });
        }

        let question = question.into();
        Ok(Self {
            session_id: new_session_id()?,
            prompt: research_prompt(&question, &source_constraints, agent_count),
            question,
            scope,
            source_constraints,
            agent_count,
            dispatch_task_id,
            dispatch: None,
            accepted_notes: Vec::new(),
            compile_state: None,
        })
    }

    pub fn checkpoint_path(vault_root: &Path) -> PathBuf {
        vault_root.join(".gwiki").join("research-session.json")
    }

    pub fn save_checkpoint(&self) -> Result<(), WikiError> {
        let path = Self::checkpoint_path(self.scope.root());
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| WikiError::Io {
                action: "create research checkpoint directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })?;
        }

        let json = serde_json::to_string_pretty(self).map_err(|error| WikiError::Json {
            action: "serialize research checkpoint",
            path: Some(path.clone()),
            source: error,
        })?;
        let temp_path = path.with_file_name(format!(
            ".research-session.{}.tmp",
            uuid::Uuid::new_v4().simple()
        ));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)
            .map_err(|error| WikiError::Io {
                action: "create research checkpoint temp file",
                path: Some(temp_path.clone()),
                source: error,
            })?;
        if let Err(error) = file.write_all(json.as_bytes()) {
            let _ = fs::remove_file(&temp_path);
            return Err(WikiError::Io {
                action: "write research checkpoint temp file",
                path: Some(temp_path),
                source: error,
            });
        }
        if let Err(error) = file.sync_all() {
            let _ = fs::remove_file(&temp_path);
            return Err(WikiError::Io {
                action: "sync research checkpoint temp file",
                path: Some(temp_path),
                source: error,
            });
        }
        drop(file);
        if let Err(error) = fs::rename(&temp_path, &path) {
            let _ = fs::remove_file(&temp_path);
            return Err(WikiError::Io {
                action: "replace research checkpoint",
                path: Some(path),
                source: error,
            });
        }
        if let Some(parent) = path.parent()
            && let Ok(directory) = fs::File::open(parent)
        {
            let _ = directory.sync_all();
        }
        Ok(())
    }

    pub fn load_checkpoint(vault_root: &Path) -> Result<Self, WikiError> {
        let path = Self::checkpoint_path(vault_root);
        let json = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read research checkpoint",
            path: Some(path.clone()),
            source: error,
        })?;
        let session: Self = serde_json::from_str(&json).map_err(|error| WikiError::Json {
            action: "parse research checkpoint",
            path: Some(path.clone()),
            source: error,
        })?;
        validate_checkpoint_scope_root(vault_root, session.scope.root(), &path)?;
        Ok(session)
    }

    pub fn record_compile_state(&mut self, state: CompileState) -> Result<(), WikiError> {
        self.compile_state = Some(state);
        self.save_checkpoint()
    }
}

fn validate_checkpoint_scope_root(
    expected_root: &Path,
    loaded_root: &Path,
    checkpoint_path: &Path,
) -> Result<(), WikiError> {
    let expected = comparable_path(expected_root, None);
    let loaded_base = checkpoint_vault_root(checkpoint_path);
    let loaded = comparable_path(loaded_root, loaded_base.as_deref());
    if expected == loaded {
        return Ok(());
    }
    Err(WikiError::InvalidScope {
        detail: format!(
            "research checkpoint {} belongs to scope root {}, expected {}",
            checkpoint_path.display(),
            loaded_root.display(),
            expected_root.display()
        ),
    })
}

fn comparable_path(path: &Path, relative_base: Option<&Path>) -> PathBuf {
    let path = if path.is_relative() {
        relative_base
            .map(|base| base.join(path))
            .unwrap_or_else(|| path.to_path_buf())
    } else {
        path.to_path_buf()
    };
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

fn checkpoint_vault_root(checkpoint_path: &Path) -> Option<PathBuf> {
    checkpoint_path
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .map(Path::to_path_buf)
}

fn new_session_id() -> Result<String, WikiError> {
    let suffix = uuid::Uuid::new_v4().simple().to_string();
    Ok(format!(
        "research-{}-{}",
        unix_timestamp_ms()?,
        &suffix[..8]
    ))
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

fn research_prompt(question: &str, source_constraints: &[String], agent_count: usize) -> String {
    let mut prompt = format!(
        "Research question: {question}\nWorkers: {agent_count}\nReturn source-grounded notes suitable for raw/research/."
    );
    if !source_constraints.is_empty() {
        prompt.push_str("\nSource constraints:");
        for constraint in source_constraints {
            prompt.push_str("\n- ");
            prompt.push_str(constraint);
        }
    }
    prompt
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compile_state_is_resumable() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project(temp.path());
        let mut session = ResearchSession::new(
            "How should compile state resume?",
            scope.clone(),
            vec!["accepted notes".to_string()],
            1,
            Some("#302".to_string()),
        )
        .expect("session created");

        session
            .record_compile_state(CompileState {
                handoff_id: "compile-123".to_string(),
                topic: "Compile state".to_string(),
                bundle_path: scope.root().join(".gwiki/compile/compile-123.md"),
                selected_note_paths: vec![scope.root().join("raw/research/compile.md")],
                selected_source_titles: vec!["Compile behavior".to_string()],
                citations: vec!["Example Docs".to_string()],
                conflicting_claims: vec!["Conflicting claim".to_string()],
                missing_evidence: vec!["Missing evidence".to_string()],
                write_intent: false,
            })
            .expect("compile state recorded");

        let loaded = ResearchSession::load_checkpoint(scope.root()).expect("checkpoint loaded");
        let state = loaded.compile_state.expect("compile state persisted");
        assert_eq!(state.handoff_id, "compile-123");
        assert_eq!(state.topic, "Compile state");
        assert_eq!(state.selected_source_titles, vec!["Compile behavior"]);
        assert_eq!(state.citations, vec!["Example Docs"]);
        assert!(!state.write_intent);
    }

    #[test]
    fn load_checkpoint_rejects_mismatched_scope_root() {
        let temp = tempfile::tempdir().expect("tempdir");
        let expected = temp.path().join("expected");
        let other = temp.path().join("other");
        fs::create_dir_all(expected.join(".gwiki")).expect("create checkpoint dir");
        fs::create_dir_all(&other).expect("create other root");
        let session = ResearchSession::new(
            "Which root?",
            ResearchScope::project(&other),
            Vec::new(),
            1,
            None,
        )
        .expect("session created");
        let json = serde_json::to_string_pretty(&session).expect("serialize session");
        fs::write(ResearchSession::checkpoint_path(&expected), json).expect("write checkpoint");

        let error = ResearchSession::load_checkpoint(&expected)
            .expect_err("mismatched scope root is rejected");

        assert!(matches!(error, WikiError::InvalidScope { .. }));
    }

    #[test]
    fn load_checkpoint_resolves_relative_scope_root_against_checkpoint_vault() {
        let temp = tempfile::tempdir().expect("tempdir");
        let expected = temp.path().join("expected");
        fs::create_dir_all(expected.join(".gwiki/research")).expect("create checkpoint dir");
        let session = ResearchSession::new(
            "Which root?",
            ResearchScope::project("."),
            Vec::new(),
            1,
            None,
        )
        .expect("session created");
        let json = serde_json::to_string_pretty(&session).expect("serialize session");
        fs::write(ResearchSession::checkpoint_path(&expected), json).expect("write checkpoint");

        let loaded = ResearchSession::load_checkpoint(&expected).expect("checkpoint loaded");

        assert_eq!(loaded.scope.root(), Path::new("."));
    }
}
