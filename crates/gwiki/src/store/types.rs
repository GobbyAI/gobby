use std::collections::BTreeMap;
use std::fmt;
use std::path::{Path, PathBuf};

use crate::models::WikiScope;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WikiDocumentKind {
    SourceCatalog,
    SourceNote,
    Concept,
    Topic,
    CodeDoc,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiDocument {
    pub path: PathBuf,
    pub kind: WikiDocumentKind,
    pub title: Option<String>,
    pub content_hash: String,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiChunk {
    pub path: PathBuf,
    pub chunk_index: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    pub heading: Option<String>,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiLink {
    pub path: PathBuf,
    pub target: String,
    pub alias: Option<String>,
    pub byte_start: usize,
    pub byte_end: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiSource {
    pub path: PathBuf,
    pub document_path: PathBuf,
    pub kind: WikiDocumentKind,
    pub content_hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WikiIngestionEvent {
    Added,
    Changed,
    Deleted,
    Unchanged,
    Skipped,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiIngestion {
    pub path: PathBuf,
    pub event: WikiIngestionEvent,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiStoreScope {
    scope: WikiScope,
}

impl WikiStoreScope {
    pub fn project(project_id: impl Into<String>) -> Self {
        Self {
            scope: WikiScope::Project {
                project_id: project_id.into(),
            },
        }
    }

    pub fn topic(topic_name: impl Into<String>) -> Self {
        Self {
            scope: WikiScope::Topic {
                name: topic_name.into(),
            },
        }
    }

    pub fn scope_kind(&self) -> &str {
        self.scope.kind()
    }

    pub fn scope_id(&self) -> &str {
        self.scope.identity()
    }

    pub(super) fn project_id(&self) -> Option<String> {
        self.scope.project_id().map(ToOwned::to_owned)
    }

    pub(super) fn topic_name(&self) -> Option<String> {
        self.scope.topic_name().map(ToOwned::to_owned)
    }
}

#[derive(Debug)]
pub enum StoreError {
    InvalidData {
        field: &'static str,
        message: String,
    },
    Postgres(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::InvalidData { field, message } => {
                write!(f, "invalid {field}: {message}")
            }
            StoreError::Postgres(message) => {
                write!(f, "PostgreSQL wiki index write failed: {message}")
            }
        }
    }
}

impl std::error::Error for StoreError {}

impl From<postgres::Error> for StoreError {
    fn from(error: postgres::Error) -> Self {
        // `postgres::Error::to_string()` is just "db error"; the server's
        // message/detail lives on the DbError in the source chain.
        let message = match error.as_db_error() {
            Some(db_error) => db_error.to_string(),
            None => error.to_string(),
        };
        StoreError::Postgres(message)
    }
}

pub trait WikiIndexStore {
    fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError>;
    fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError>;
    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) -> Result<(), StoreError>;
    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError>;
    fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError>;
    fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError>;
    fn record_file_hash(&mut self, path: PathBuf, content_hash: String) -> Result<(), StoreError>;
    fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError>;
}
