use std::fmt;
use std::path::PathBuf;

use crate::{exports, research, synthesis};

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
    Collect {
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
    Research(research::ResearchOptions),
    Compile {
        topic: Option<String>,
        outline: Vec<String>,
        target_kind: synthesis::ArticleKind,
        target_page: Option<PathBuf>,
        write_intent: bool,
        scope: ScopeSelection,
    },
    Export {
        scope: ScopeSelection,
        command: exports::ExportCommand,
    },
    Audit {
        scope: ScopeSelection,
    },
    Lint {
        scope: ScopeSelection,
    },
    Health {
        scope: ScopeSelection,
    },
    Status {
        scope: ScopeSelection,
    },
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

    pub fn identity(&self) -> ScopeIdentity {
        if let Some(topic) = &self.topic {
            return ScopeIdentity::topic(topic.clone());
        }

        ScopeIdentity::project("current")
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

#[cfg(test)]
mod tests {
    use super::ScopeSelection;

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
        let manifest = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
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
