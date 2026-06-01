use std::fmt;
use std::path::PathBuf;

use gobby_core::setup::SetupError;

use crate::{indexer, search};

#[derive(Debug)]
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
        action: &'static str,
        path: Option<PathBuf>,
        source: std::io::Error,
    },
    Json {
        action: &'static str,
        path: Option<PathBuf>,
        source: serde_json::Error,
    },
    Yaml {
        action: &'static str,
        path: Option<PathBuf>,
        source: serde_yaml::Error,
    },
    Registry {
        detail: String,
    },
    Daemon {
        endpoint: &'static str,
        message: String,
    },
    InvalidInput {
        field: &'static str,
        message: String,
    },
    Index {
        source: indexer::IndexError,
    },
    Search {
        source: search::SearchError,
    },
    Setup {
        source: SetupError,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
            Self::InvalidScope { .. } => "invalid_scope",
            Self::Config { .. } => "config_error",
            Self::Io { .. } => "io_error",
            Self::Json { .. } => "json_error",
            Self::Yaml { .. } => "yaml_error",
            Self::Registry { .. } => "registry_error",
            Self::Daemon { .. } => "daemon_error",
            Self::InvalidInput { .. } => "invalid_input",
            Self::Index { .. } => "index_error",
            Self::Search { .. } => "search_error",
            Self::Setup { .. } => "setup_error",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
            Self::InvalidScope { detail } | Self::Config { detail } | Self::Registry { detail } => {
                write!(f, "{detail} ({})", self.code())
            }
            Self::Io {
                action,
                path,
                source,
            } => format_action_error(f, action, path.as_ref(), source, self.code()),
            Self::Json {
                action,
                path,
                source,
            } => format_action_error(f, action, path.as_ref(), source, self.code()),
            Self::Yaml {
                action,
                path,
                source,
            } => format_action_error(f, action, path.as_ref(), source, self.code()),
            Self::Daemon { endpoint, message } => {
                write!(f, "{endpoint}: {message} ({})", self.code())
            }
            Self::InvalidInput { field, message } => {
                write!(f, "{field}: {message} ({})", self.code())
            }
            Self::Index { source } => write!(f, "index: {source} ({})", self.code()),
            Self::Search { source } => write!(f, "query: {source} ({})", self.code()),
            Self::Setup { source } => write!(f, "gwiki setup failed: {source} ({})", self.code()),
        }
    }
}

fn format_action_error(
    f: &mut fmt::Formatter<'_>,
    action: &str,
    path: Option<&PathBuf>,
    source: &dyn std::error::Error,
    code: &str,
) -> fmt::Result {
    match path {
        Some(path) => write!(
            f,
            "{action} failed for {}: {source} ({code})",
            path.display()
        ),
        None => write!(f, "{action} failed: {source} ({code})"),
    }
}

impl std::error::Error for WikiError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Json { source, .. } => Some(source),
            Self::Yaml { source, .. } => Some(source),
            Self::Index { source } => Some(source),
            Self::Search { source } => Some(source),
            Self::Setup { source } => Some(source),
            _ => None,
        }
    }
}

pub(crate) fn setup_error_to_wiki_error(error: SetupError) -> WikiError {
    WikiError::Setup { source: error }
}

pub(crate) fn index_error_to_wiki_error(error: indexer::IndexError) -> WikiError {
    WikiError::Index { source: error }
}

pub(crate) fn search_error_to_wiki_error(error: search::SearchError) -> WikiError {
    WikiError::Search { source: error }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::error::Error;

    #[test]
    fn typed_error_sources_are_preserved() {
        let source =
            serde_json::from_str::<serde_json::Value>("{").expect_err("invalid JSON should fail");
        let error = WikiError::Json {
            action: "parse fixture",
            path: None,
            source,
        };

        assert!(error.source().is_some());
        assert!(error.to_string().contains("parse fixture failed"));
    }

    #[test]
    fn wrapped_error_codes_are_specific() {
        let index = WikiError::Index {
            source: indexer::IndexError::Walk("walk failed".to_string()),
        };
        let search = WikiError::Search {
            source: search::SearchError::Backend("backend failed".to_string()),
        };

        assert_eq!(index.code(), "index_error");
        assert_eq!(search.code(), "search_error");
    }

    #[test]
    fn setup_error_source_is_preserved() {
        let error = setup_error_to_wiki_error(SetupError::CreationFailed {
            object: "gwiki_documents".to_string(),
            message: "permission denied".to_string(),
        });

        assert_eq!(error.code(), "setup_error");
        assert!(error.source().is_some());
        assert!(error.to_string().contains("gwiki setup failed"));
    }
}
