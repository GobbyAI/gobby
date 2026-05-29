use std::fmt;
use std::path::PathBuf;

pub mod commands;
pub mod graph;
pub mod indexer;
pub mod output;
pub mod search;
pub mod store;

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init {
        scope: ScopeSelection,
    },
    Setup {
        scope: ScopeSelection,
    },
    Index {
        scope: ScopeSelection,
    },
    IngestFile {
        path: PathBuf,
        scope: ScopeSelection,
    },
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
    },
    Backlinks {
        page: String,
        scope: ScopeSelection,
    },
    LinkSuggest {
        scope: ScopeSelection,
        limit: usize,
    },
    Status {
        scope: ScopeSelection,
    },
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

    pub fn identity(&self) -> ScopeIdentity {
        if let Some(topic) = &self.topic {
            return ScopeIdentity::topic(topic.clone());
        }

        ScopeIdentity::project("current")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ScopeIdentity {
    pub kind: String,
    pub id: String,
}

impl ScopeIdentity {
    pub fn project(id: impl Into<String>) -> Self {
        Self {
            kind: "project".to_string(),
            id: id.into(),
        }
    }

    pub fn topic(id: impl Into<String>) -> Self {
        Self {
            kind: "topic".to_string(),
            id: id.into(),
        }
    }
}

impl fmt::Display for ScopeIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.kind, self.id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandOutcome {
    pub status_messages: Vec<String>,
    pub result: CommandResult,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandResult {
    pub payload: serde_json::Value,
    pub text: String,
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

pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => Ok(commands::init::run(scope.identity())),
        Command::Setup { scope } => Ok(commands::setup::run(scope.identity())),
        Command::Index { scope } => Ok(commands::index::run(scope.identity())),
        Command::IngestFile { path, scope } => {
            Ok(commands::index::ingest_file(path, scope.identity()))
        }
        Command::Search {
            query,
            scope,
            limit,
        } => Ok(commands::search::run(query, scope.identity(), limit)),
        Command::Backlinks { page, scope } => Ok(commands::backlinks::run(page, scope.identity())),
        Command::LinkSuggest { scope, limit } => {
            Ok(commands::backlinks::link_suggest(scope.identity(), limit))
        }
        Command::Status { scope } => Ok(commands::status::run(scope.identity())),
    }
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
