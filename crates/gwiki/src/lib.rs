use std::fmt;
use std::path::PathBuf;

pub mod frontmatter;
pub mod links;
pub mod markdown;
pub mod models;
pub mod registry;
pub mod schema;
pub mod scope;
pub mod setup;
pub mod vault;

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
    InvalidScope {
        detail: String,
    },
    Config {
        detail: String,
    },
    Io {
        detail: String,
    },
    Registry {
        detail: String,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
            Self::InvalidScope { .. } => "invalid_scope",
            Self::Config { .. } => "config_error",
            Self::Io { .. } => "io_error",
            Self::Registry { .. } => "registry_error",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
            Self::InvalidScope { detail }
            | Self::Config { detail }
            | Self::Io { detail }
            | Self::Registry { detail } => write!(f, "{detail} ({})", self.code()),
        }
    }
}

impl std::error::Error for WikiError {}

pub fn run(command: Command) -> Result<CommandOutput, WikiError> {
    match command {
        Command::Status => Ok(status()),
        Command::Init(scope) => init(scope),
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

fn init(selection: ScopeSelection) -> Result<CommandOutput, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        detail: format!("failed to read current directory: {error}"),
    })?;
    let scope = scope::resolve(&selection, &cwd)?;

    vault::initialize(&scope)?;
    registry::register_scope(scope.registry_path(), &scope)?;

    Ok(CommandOutput {
        kind: OutputKind::Status,
        message: format!(
            "initialized {} wiki at {}",
            scope.identity(),
            scope.root().display()
        ),
    })
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
