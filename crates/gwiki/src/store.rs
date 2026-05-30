use std::collections::BTreeMap;
use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::json;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WikiDocumentKind {
    SourceCatalog,
    SourceNote,
    Concept,
    Topic,
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
    pub kind: WikiDocumentKind,
    pub content_hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WikiIngestionEvent {
    Added,
    Changed,
    Deleted,
    Unchanged,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiIngestion {
    pub path: PathBuf,
    pub event: WikiIngestionEvent,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiStoreScope {
    scope_kind: String,
    scope_id: String,
    project_id: Option<String>,
    topic_name: Option<String>,
}

impl WikiStoreScope {
    pub fn project(project_id: impl Into<String>) -> Self {
        let project_id = project_id.into();
        Self {
            scope_kind: "project".to_string(),
            scope_id: project_id.clone(),
            project_id: Some(project_id),
            topic_name: None,
        }
    }

    pub fn topic(topic_name: impl Into<String>) -> Self {
        let topic_name = topic_name.into();
        Self {
            scope_kind: "topic".to_string(),
            scope_id: topic_name.clone(),
            project_id: None,
            topic_name: Some(topic_name),
        }
    }

    pub fn scope_kind(&self) -> &str {
        &self.scope_kind
    }

    pub fn scope_id(&self) -> &str {
        &self.scope_id
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
        StoreError::Postgres(error.to_string())
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

#[derive(Debug, Default)]
pub struct MemoryWikiStore {
    pub documents: BTreeMap<PathBuf, WikiDocument>,
    pub chunks: BTreeMap<PathBuf, Vec<WikiChunk>>,
    pub links: BTreeMap<PathBuf, Vec<WikiLink>>,
    pub sources: BTreeMap<PathBuf, WikiSource>,
    pub file_hashes: BTreeMap<PathBuf, String>,
    pub ingestions: Vec<WikiIngestion>,
    pub deleted_paths: Vec<PathBuf>,
    pub document_upserts: usize,
    pub chunk_replacements: usize,
    pub link_replacements: usize,
    pub source_upserts: usize,
}

impl WikiIndexStore for MemoryWikiStore {
    fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError> {
        Ok(self.file_hashes.clone())
    }

    fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError> {
        self.document_upserts += 1;
        self.documents.insert(document.path.clone(), document);
        Ok(())
    }

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) -> Result<(), StoreError> {
        self.chunk_replacements += 1;
        self.chunks.insert(path.to_path_buf(), chunks);
        Ok(())
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
        self.link_replacements += 1;
        self.links.insert(path.to_path_buf(), links);
        Ok(())
    }

    fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
        self.source_upserts += 1;
        self.sources.insert(source.path.clone(), source);
        Ok(())
    }

    fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError> {
        self.ingestions.push(ingestion);
        Ok(())
    }

    fn record_file_hash(&mut self, path: PathBuf, content_hash: String) -> Result<(), StoreError> {
        self.file_hashes.insert(path, content_hash);
        Ok(())
    }

    fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
        let path = path.to_path_buf();
        self.documents.remove(&path);
        self.chunks.remove(&path);
        self.links.remove(&path);
        self.sources.remove(&path);
        self.file_hashes.remove(&path);
        self.deleted_paths.push(path);
        Ok(())
    }
}

#[derive(Debug, Clone)]
struct DocumentMeta {
    id: String,
    source_kind: String,
    content_hash: String,
}

pub struct PostgresWikiStore<'a> {
    conn: &'a mut postgres::Client,
    scope: WikiStoreScope,
    documents: BTreeMap<PathBuf, DocumentMeta>,
}

impl<'a> PostgresWikiStore<'a> {
    pub fn new(conn: &'a mut postgres::Client, scope: WikiStoreScope) -> Self {
        Self {
            conn,
            scope,
            documents: BTreeMap::new(),
        }
    }

    fn scope_params(&self) -> (String, String, Option<String>, Option<String>) {
        (
            self.scope.scope_kind.clone(),
            self.scope.scope_id.clone(),
            self.scope.project_id.clone(),
            self.scope.topic_name.clone(),
        )
    }

    fn document_meta(&mut self, path: &Path) -> Result<DocumentMeta, StoreError> {
        if let Some(meta) = self.documents.get(path) {
            return Ok(meta.clone());
        }

        let path_string = display_path(path);
        let row = self.conn.query_opt(
            "SELECT id, source_kind, content_hash
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[
                &self.scope.scope_kind.as_str(),
                &self.scope.scope_id.as_str(),
                &path_string,
            ],
        )?;
        let row = row.ok_or_else(|| StoreError::InvalidData {
            field: "document",
            message: format!("missing indexed document for {}", path.display()),
        })?;
        let meta = DocumentMeta {
            id: row.get("id"),
            source_kind: row.get("source_kind"),
            content_hash: row.get("content_hash"),
        };
        self.documents.insert(path.to_path_buf(), meta.clone());
        Ok(meta)
    }
}

impl WikiIndexStore for PostgresWikiStore<'_> {
    fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError> {
        let rows = self.conn.query(
            "SELECT path, content_hash
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2",
            &[
                &self.scope.scope_kind.as_str(),
                &self.scope.scope_id.as_str(),
            ],
        )?;
        Ok(rows
            .into_iter()
            .map(|row| {
                (
                    PathBuf::from(row.get::<_, String>("path")),
                    row.get("content_hash"),
                )
            })
            .collect())
    }

    fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError> {
        let id = scoped_id("document", &self.scope, &document.path, None);
        let path = display_path(&document.path);
        let source_kind = document_kind_name(document.kind);
        let provenance = json!({ "source_path": path }).to_string();
        let frontmatter = "{}";
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_documents (
                id, scope_kind, scope_id, project_id, topic_name, path, title, source_kind,
                content_hash, frontmatter, provenance, body, indexed_at, updated_at
             )
             VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10::jsonb, $11::jsonb, $12, NOW(), NOW()
             )
             ON CONFLICT (scope_kind, scope_id, path)
             DO UPDATE SET
                id = EXCLUDED.id,
                project_id = EXCLUDED.project_id,
                topic_name = EXCLUDED.topic_name,
                title = EXCLUDED.title,
                source_kind = EXCLUDED.source_kind,
                content_hash = EXCLUDED.content_hash,
                frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                body = EXCLUDED.body,
                indexed_at = NOW(),
                updated_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &document.title,
                &source_kind,
                &document.content_hash,
                &frontmatter,
                &provenance,
                &document.body,
            ],
        )?;
        self.documents.insert(
            document.path,
            DocumentMeta {
                id,
                source_kind: source_kind.to_string(),
                content_hash: document.content_hash,
            },
        );
        Ok(())
    }

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) -> Result<(), StoreError> {
        let document = self.document_meta(path)?;
        let path_string = display_path(path);
        self.conn.execute(
            "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[
                &self.scope.scope_kind.as_str(),
                &self.scope.scope_id.as_str(),
                &path_string,
            ],
        )?;

        for chunk in chunks {
            let chunk_index =
                i32::try_from(chunk.chunk_index).map_err(|_| StoreError::InvalidData {
                    field: "chunk_index",
                    message: format!("{} is too large for PostgreSQL INTEGER", chunk.chunk_index),
                })?;
            let chunk_path = display_path(&chunk.path);
            let id = scoped_id(
                "chunk",
                &self.scope,
                &chunk.path,
                Some(&chunk.chunk_index.to_string()),
            );
            let heading_path = chunk
                .heading
                .as_ref()
                .map(|heading| vec![heading.clone()])
                .unwrap_or_default();
            let provenance = json!({
                "source_path": chunk_path,
                "byte_start": chunk.byte_start,
                "byte_end": chunk.byte_end,
                "heading": chunk.heading,
            })
            .to_string();
            let frontmatter = "{}";
            let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

            self.conn.execute(
                "INSERT INTO gwiki_chunks (
                    id, document_id, scope_kind, scope_id, project_id, topic_name, path,
                    chunk_index, source_kind, content_hash, frontmatter, provenance,
                    heading_path, content, created_at
                 )
                 VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11::jsonb, $12::jsonb,
                    $13, $14, NOW()
                 )",
                &[
                    &id,
                    &document.id,
                    &scope_kind,
                    &scope_id,
                    &project_id,
                    &topic_name,
                    &chunk_path,
                    &chunk_index,
                    &document.source_kind,
                    &document.content_hash,
                    &frontmatter,
                    &provenance,
                    &heading_path,
                    &chunk.content,
                ],
            )?;
        }

        Ok(())
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
        let path_string = display_path(path);
        self.conn.execute(
            "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[
                &self.scope.scope_kind.as_str(),
                &self.scope.scope_id.as_str(),
                &path_string,
            ],
        )?;

        for link in links {
            let target_path = link.target.clone();
            let link_text = link.alias.clone().unwrap_or_else(|| link.target.clone());
            let link_kind = link_kind(&link.target);
            let id = scoped_text_id(
                "link",
                &self.scope,
                &link.path,
                &[&target_path, &link_text, link_kind],
            );
            let path = display_path(&link.path);
            let provenance = json!({
                "byte_start": link.byte_start,
                "byte_end": link.byte_end,
                "alias": link.alias,
            })
            .to_string();
            let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

            self.conn.execute(
                "INSERT INTO gwiki_links (
                    id, scope_kind, scope_id, project_id, topic_name, path,
                    target_path, link_text, link_kind, provenance, created_at
                 )
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
             ON CONFLICT (scope_kind, scope_id, path, target_path, link_text, link_kind)
             DO UPDATE SET
                    id = EXCLUDED.id,
                    project_id = EXCLUDED.project_id,
                    topic_name = EXCLUDED.topic_name,
                    provenance = EXCLUDED.provenance",
                &[
                    &id,
                    &scope_kind,
                    &scope_id,
                    &project_id,
                    &topic_name,
                    &path,
                    &target_path,
                    &link_text,
                    &link_kind,
                    &provenance,
                ],
            )?;
        }

        Ok(())
    }

    fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
        let id = scoped_id("source", &self.scope, &source.path, None);
        let path = display_path(&source.path);
        let source_kind = document_kind_name(source.kind);
        let provenance = json!({ "source_path": path }).to_string();
        let frontmatter = "{}";
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_sources (
                id, scope_kind, scope_id, project_id, topic_name, path, source_kind,
                content_hash, frontmatter, provenance, captured_at
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, NOW())
             ON CONFLICT (scope_kind, scope_id, path)
             DO UPDATE SET
                id = EXCLUDED.id,
                project_id = EXCLUDED.project_id,
                topic_name = EXCLUDED.topic_name,
                source_kind = EXCLUDED.source_kind,
                content_hash = EXCLUDED.content_hash,
                frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                captured_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &source_kind,
                &source.content_hash,
                &frontmatter,
                &provenance,
            ],
        )?;
        Ok(())
    }

    fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError> {
        let content_hash = ingestion
            .content_hash
            .clone()
            .unwrap_or_else(|| "none".to_string());
        let status = ingestion_status(ingestion.event);
        let id = scoped_text_id(
            "ingestion",
            &self.scope,
            &ingestion.path,
            &[status, &content_hash],
        );
        let path = display_path(&ingestion.path);
        let source_kind = self
            .documents
            .get(&ingestion.path)
            .map(|document| document.source_kind.as_str())
            .unwrap_or("unknown");
        let provenance = json!({ "event": status }).to_string();
        let frontmatter = "{}";
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_ingestions (
                id, scope_kind, scope_id, project_id, topic_name, path, source_kind,
                content_hash, frontmatter, provenance, status, ingested_at
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, NOW())
             ON CONFLICT (id)
             DO UPDATE SET
                project_id = EXCLUDED.project_id,
                topic_name = EXCLUDED.topic_name,
                source_kind = EXCLUDED.source_kind,
                content_hash = EXCLUDED.content_hash,
                frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                status = EXCLUDED.status,
                ingested_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &source_kind,
                &content_hash,
                &frontmatter,
                &provenance,
                &status,
            ],
        )?;
        Ok(())
    }

    fn record_file_hash(
        &mut self,
        _path: PathBuf,
        _content_hash: String,
    ) -> Result<(), StoreError> {
        Ok(())
    }

    fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
        let path = display_path(path);
        let params: [&(dyn postgres::types::ToSql + Sync); 3] = [
            &self.scope.scope_kind.as_str(),
            &self.scope.scope_id.as_str(),
            &path,
        ];
        self.conn.execute(
            "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        self.conn.execute(
            "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        self.conn.execute(
            "DELETE FROM gwiki_sources WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        self.conn.execute(
            "DELETE FROM gwiki_documents WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        self.documents.remove(Path::new(&path));
        Ok(())
    }
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn scoped_id(prefix: &str, scope: &WikiStoreScope, path: &Path, suffix: Option<&str>) -> String {
    match suffix {
        Some(value) => scoped_text_id(prefix, scope, path, &[value]),
        None => scoped_text_id(prefix, scope, path, &[]),
    }
}

fn scoped_text_id(prefix: &str, scope: &WikiStoreScope, path: &Path, suffixes: &[&str]) -> String {
    let mut id = format!(
        "{prefix}:{}:{}:{}",
        scope.scope_kind,
        scope.scope_id,
        display_path(path)
    );
    for suffix in suffixes {
        id.push(':');
        id.push_str(suffix);
    }
    id
}

fn document_kind_name(kind: WikiDocumentKind) -> &'static str {
    match kind {
        WikiDocumentKind::SourceCatalog => "source_catalog",
        WikiDocumentKind::SourceNote => "source_note",
        WikiDocumentKind::Concept => "concept",
        WikiDocumentKind::Topic => "topic",
    }
}

fn ingestion_status(event: WikiIngestionEvent) -> &'static str {
    match event {
        WikiIngestionEvent::Added => "added",
        WikiIngestionEvent::Changed => "changed",
        WikiIngestionEvent::Deleted => "deleted",
        WikiIngestionEvent::Unchanged => "unchanged",
    }
}

fn link_kind(target: &str) -> &'static str {
    if target.contains("://") || target.starts_with('#') {
        "markdown"
    } else {
        "wiki"
    }
}
