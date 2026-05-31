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
        source: serde_norway::Error,
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
            Self::Yaml {
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

impl std::error::Error for WikiError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Json { source, .. } => Some(source),
            Self::Yaml { source, .. } => Some(source),
            _ => None,
        }
    }
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
}

pub(crate) fn setup_error_to_wiki_error(error: SetupError) -> WikiError {
    WikiError::Config {
        detail: format!("gwiki setup failed: {error}"),
    }
}

pub(crate) fn index_error_to_wiki_error(error: indexer::IndexError) -> WikiError {
    WikiError::InvalidInput {
        field: "vault_root",
        message: error.to_string(),
    }
}

pub(crate) fn search_error_to_wiki_error(error: search::SearchError) -> WikiError {
    WikiError::InvalidInput {
        field: "query",
        message: error.to_string(),
    }
}
