use std::fmt;
use std::path::PathBuf;

pub mod credibility;
pub mod daemon;
pub mod events;
pub mod provenance;
pub mod research;
pub mod session;
pub mod sources;

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
    Research(research::ResearchOptions),
    Status,
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScopeSelection {
    pub project: bool,
    pub topic: Option<String>,
}

impl ScopeSelection {
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
    Io {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Json {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Daemon {
        endpoint: &'static str,
        message: String,
    },
    InvalidInput {
        field: &'static str,
        message: String,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
            Self::Io { .. } => "io_error",
            Self::Json { .. } => "json_error",
            Self::Daemon { .. } => "daemon_error",
            Self::InvalidInput { .. } => "invalid_input",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
            Self::Io {
                action,
                path,
                source,
            } => match path {
                Some(path) => write!(
                    f,
                    "{action} failed for {}: {source} ({})",
                    path.display(),
                    self.code()
                ),
                None => write!(f, "{action} failed: {source} ({})", self.code()),
            },
            Self::Json {
                action,
                path,
                source,
            } => match path {
                Some(path) => write!(
                    f,
                    "{action} failed for {}: {source} ({})",
                    path.display(),
                    self.code()
                ),
                None => write!(f, "{action} failed: {source} ({})", self.code()),
            },
            Self::Daemon { endpoint, message } => {
                write!(f, "{endpoint}: {message} ({})", self.code())
            }
            Self::InvalidInput { field, message } => {
                write!(f, "{field}: {message} ({})", self.code())
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
        Command::Research(options) => research::run(options).map(|outcome| CommandOutput {
            kind: OutputKind::Status,
            message: outcome.message,
        }),
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
