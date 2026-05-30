use std::fmt;
use std::path::PathBuf;

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init(ScopeSelection),
    Setup,
    Index(ScopeSelection),
    IngestFile {
        path: PathBuf,
    },
    Search {
        query: String,
        scope: ScopeSelection,
    },
    Backlinks {
        page: String,
        scope: ScopeSelection,
    },
    Status,
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeSelection {
    project: bool,
    topic: Option<String>,
}

impl ScopeSelection {
    pub fn global() -> Self {
        Self {
            project: false,
            topic: None,
        }
    }

    pub fn project() -> Self {
        Self {
            project: true,
            topic: None,
        }
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self {
            project: false,
            topic: Some(topic.into()),
        }
    }

    pub fn is_project(&self) -> bool {
        self.project
    }

    pub fn topic_name(&self) -> Option<&str> {
        self.topic.as_deref()
    }
}

impl Default for ScopeSelection {
    fn default() -> Self {
        Self::global()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub kind: OutputKind,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputKind {
    Status,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WikiError {
    NotImplemented {
        command: &'static str,
        detail: &'static str,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
        }
    }
}

impl std::error::Error for WikiError {}

pub fn run(command: Command) -> Result<CommandOutput, WikiError> {
    match command {
        Command::Status => Ok(status()),
        Command::Init(_) => {
            not_implemented("init", "vault initialization lands in the scope/vault task")
        }
        Command::Setup => not_implemented(
            "setup",
            "gwiki-owned schema setup lands in the datastore task",
        ),
        Command::Index(_) => {
            not_implemented("index", "wiki markdown indexing lands in the indexing task")
        }
        Command::IngestFile { .. } => not_implemented(
            "ingest-file",
            "source ingestion lands in the ingestion task",
        ),
        Command::Search { .. } => not_implemented("search", "wiki search lands in the search task"),
        Command::Backlinks { .. } => {
            not_implemented("backlinks", "wiki graph queries land in the graph task")
        }
    }
}

fn status() -> CommandOutput {
    CommandOutput {
        kind: OutputKind::Status,
        message: format!(
            "gwiki shell ready; daemon={}",
            gobby_core::daemon_url::daemon_url()
        ),
    }
}

fn not_implemented<T>(command: &'static str, detail: &'static str) -> Result<T, WikiError> {
    Err(WikiError::NotImplemented { command, detail })
}

#[cfg(test)]
mod lib {
    mod tests {
        use crate::ScopeSelection;

        #[test]
        fn scope_selection_constructors_express_allowed_states() {
            let global = ScopeSelection::global();
            assert!(!global.is_project());
            assert_eq!(global.topic_name(), None);
            assert_eq!(ScopeSelection::default(), global);

            let project = ScopeSelection::project();
            assert!(project.is_project());
            assert_eq!(project.topic_name(), None);

            let topic = ScopeSelection::topic("ops");
            assert!(!topic.is_project());
            assert_eq!(topic.topic_name(), Some("ops"));
        }

        #[test]
        fn crate_has_no_gcode_dependency() {
            let manifest =
                std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
                    .expect("manifest is readable");
            let manifest: toml::Value = toml::from_str(&manifest).expect("manifest is valid TOML");
            let dependencies = manifest
                .get("dependencies")
                .and_then(toml::Value::as_table)
                .expect("manifest has dependencies table");

            assert!(
                dependencies.contains_key("gobby-core"),
                "gobby-wiki must depend on gobby-core"
            );
            assert!(
                !dependencies.contains_key("gobby-code"),
                "gobby-wiki must not depend on gobby-code"
            );
        }
    }
}
