use std::collections::BTreeMap;
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

pub trait WikiIndexStore {
    fn indexed_hashes(&self) -> BTreeMap<PathBuf, String>;
    fn upsert_document(&mut self, document: WikiDocument);
    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>);
    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>);
    fn upsert_source(&mut self, source: WikiSource);
    fn record_ingestion(&mut self, ingestion: WikiIngestion);
    fn record_file_hash(&mut self, path: PathBuf, content_hash: String);
    fn delete_derived_rows(&mut self, path: &Path);
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiIndexScope {
    pub scope_kind: String,
    pub scope_id: String,
    pub project_id: Option<String>,
    pub topic_name: Option<String>,
}

impl WikiIndexScope {
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
    fn indexed_hashes(&self) -> BTreeMap<PathBuf, String> {
        self.file_hashes.clone()
    }

    fn upsert_document(&mut self, document: WikiDocument) {
        self.document_upserts += 1;
        self.documents.insert(document.path.clone(), document);
    }

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) {
        self.chunk_replacements += 1;
        self.chunks.insert(path.to_path_buf(), chunks);
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) {
        self.link_replacements += 1;
        self.links.insert(path.to_path_buf(), links);
    }

    fn upsert_source(&mut self, source: WikiSource) {
        self.source_upserts += 1;
        self.sources.insert(source.path.clone(), source);
    }

    fn record_ingestion(&mut self, ingestion: WikiIngestion) {
        self.ingestions.push(ingestion);
    }

    fn record_file_hash(&mut self, path: PathBuf, content_hash: String) {
        self.file_hashes.insert(path, content_hash);
    }

    fn delete_derived_rows(&mut self, path: &Path) {
        let path = path.to_path_buf();
        self.documents.remove(&path);
        self.chunks.remove(&path);
        self.links.remove(&path);
        self.sources.remove(&path);
        self.file_hashes.remove(&path);
        self.deleted_paths.push(path);
    }
}

pub struct PostgresWikiStore {
    scope: WikiIndexScope,
    previous_hashes: BTreeMap<PathBuf, String>,
    staged: MemoryWikiStore,
}

impl PostgresWikiStore {
    pub fn load(
        conn: &mut postgres::Client,
        scope: WikiIndexScope,
    ) -> Result<Self, postgres::Error> {
        let previous_hashes = load_indexed_hashes(conn, &scope)?;
        Ok(Self {
            scope,
            previous_hashes,
            staged: MemoryWikiStore::default(),
        })
    }

    pub fn flush(self, conn: &mut postgres::Client) -> Result<MemoryWikiStore, postgres::Error> {
        let mut tx = conn.transaction()?;

        for path in &self.staged.deleted_paths {
            delete_derived_rows(&mut tx, &self.scope, path)?;
        }
        for document in self.staged.documents.values() {
            upsert_document(&mut tx, &self.scope, document)?;
        }
        for (path, chunks) in &self.staged.chunks {
            let content_hash = self
                .staged
                .documents
                .get(path)
                .map(|document| document.content_hash.as_str())
                .unwrap_or_default();
            replace_chunks(&mut tx, &self.scope, path, chunks, content_hash)?;
        }
        for (path, links) in &self.staged.links {
            replace_links(&mut tx, &self.scope, path, links)?;
        }
        for source in self.staged.sources.values() {
            upsert_source(&mut tx, &self.scope, source)?;
        }
        for (index, ingestion) in self.staged.ingestions.iter().enumerate() {
            record_ingestion(&mut tx, &self.scope, index, ingestion)?;
        }

        tx.commit()?;
        Ok(self.staged)
    }
}

impl WikiIndexStore for PostgresWikiStore {
    fn indexed_hashes(&self) -> BTreeMap<PathBuf, String> {
        self.previous_hashes.clone()
    }

    fn upsert_document(&mut self, document: WikiDocument) {
        self.staged.upsert_document(document);
    }

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) {
        self.staged.replace_chunks(path, chunks);
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) {
        self.staged.replace_links(path, links);
    }

    fn upsert_source(&mut self, source: WikiSource) {
        self.staged.upsert_source(source);
    }

    fn record_ingestion(&mut self, ingestion: WikiIngestion) {
        self.staged.record_ingestion(ingestion);
    }

    fn record_file_hash(&mut self, path: PathBuf, content_hash: String) {
        self.staged.record_file_hash(path, content_hash);
    }

    fn delete_derived_rows(&mut self, path: &Path) {
        self.staged.delete_derived_rows(path);
    }
}

fn load_indexed_hashes(
    conn: &mut postgres::Client,
    scope: &WikiIndexScope,
) -> Result<BTreeMap<PathBuf, String>, postgres::Error> {
    let mut hashes = BTreeMap::new();
    for row in conn.query(
        "SELECT path, content_hash
         FROM gwiki_documents
         WHERE scope_kind = $1 AND scope_id = $2",
        &[&scope.scope_kind, &scope.scope_id],
    )? {
        let path: String = row.get("path");
        let content_hash: String = row.get("content_hash");
        hashes.insert(PathBuf::from(path), content_hash);
    }
    Ok(hashes)
}

fn delete_derived_rows(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    path: &Path,
) -> Result<(), postgres::Error> {
    let path = path_string(path);
    tx.execute(
        "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;
    tx.execute(
        "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;
    tx.execute(
        "DELETE FROM gwiki_sources WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;
    tx.execute(
        "DELETE FROM gwiki_documents WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;
    Ok(())
}

fn upsert_document(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    document: &WikiDocument,
) -> Result<(), postgres::Error> {
    let path = path_string(&document.path);
    let id = scoped_id(scope, "document", &[&path]);
    let source_kind = document_kind_name(document.kind);
    let provenance = json!({
        "source_path": path.as_str(),
        "content_hash": document.content_hash.as_str(),
    })
    .to_string();

    tx.execute(
        "INSERT INTO gwiki_documents (
             id, scope_kind, scope_id, project_id, topic_name, path, title, source_kind,
             content_hash, provenance, body, indexed_at, updated_at
         )
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, NOW(), NOW())
         ON CONFLICT (scope_kind, scope_id, path) DO UPDATE SET
             title = EXCLUDED.title,
             source_kind = EXCLUDED.source_kind,
             content_hash = EXCLUDED.content_hash,
             provenance = EXCLUDED.provenance,
             body = EXCLUDED.body,
             indexed_at = NOW(),
             updated_at = NOW()",
        &[
            &id,
            &scope.scope_kind,
            &scope.scope_id,
            &scope.project_id,
            &scope.topic_name,
            &path,
            &document.title,
            &source_kind,
            &document.content_hash,
            &provenance,
            &document.body,
        ],
    )?;
    Ok(())
}

fn replace_chunks(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    path: &Path,
    chunks: &[WikiChunk],
    content_hash: &str,
) -> Result<(), postgres::Error> {
    let path = path_string(path);
    let document_id = scoped_id(scope, "document", &[&path]);
    tx.execute(
        "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;

    for chunk in chunks {
        let chunk_index = chunk.chunk_index as i32;
        let id = scoped_id(scope, "chunk", &[&path, &chunk.chunk_index.to_string()]);
        let source_kind = document_kind_for_path(Path::new(&path))
            .map(document_kind_name)
            .unwrap_or("unknown");
        let heading_path = chunk.heading.clone().into_iter().collect::<Vec<_>>();
        let provenance = json!({
            "source_path": path.as_str(),
            "byte_start": chunk.byte_start,
            "byte_end": chunk.byte_end,
            "heading": chunk.heading.as_deref(),
        })
        .to_string();

        tx.execute(
            "INSERT INTO gwiki_chunks (
                 id, document_id, scope_kind, scope_id, project_id, topic_name, path,
                 chunk_index, source_kind, content_hash, provenance, heading_path, content
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)
             ON CONFLICT (scope_kind, scope_id, path, chunk_index) DO UPDATE SET
                 document_id = EXCLUDED.document_id,
                 source_kind = EXCLUDED.source_kind,
                 content_hash = EXCLUDED.content_hash,
                 provenance = EXCLUDED.provenance,
                 heading_path = EXCLUDED.heading_path,
                 content = EXCLUDED.content",
            &[
                &id,
                &document_id,
                &scope.scope_kind,
                &scope.scope_id,
                &scope.project_id,
                &scope.topic_name,
                &path,
                &chunk_index,
                &source_kind,
                &content_hash,
                &provenance,
                &heading_path,
                &chunk.content,
            ],
        )?;
    }
    Ok(())
}

fn replace_links(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    path: &Path,
    links: &[WikiLink],
) -> Result<(), postgres::Error> {
    let path = path_string(path);
    tx.execute(
        "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
        &[&scope.scope_kind, &scope.scope_id, &path],
    )?;

    for link in links {
        let link_text = link.alias.as_deref().unwrap_or(&link.target);
        let id = scoped_id(
            scope,
            "link",
            &[
                &path,
                &link.target,
                link_text,
                &link.byte_start.to_string(),
                &link.byte_end.to_string(),
            ],
        );
        let provenance = json!({
            "byte_start": link.byte_start,
            "byte_end": link.byte_end,
            "alias": link.alias.as_deref(),
        })
        .to_string();

        tx.execute(
            "INSERT INTO gwiki_links (
                 id, scope_kind, scope_id, project_id, topic_name, path, target_path,
                 link_text, link_kind, provenance
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'markdown', $9::jsonb)
             ON CONFLICT (scope_kind, scope_id, path, target_path, link_text, link_kind)
             DO UPDATE SET provenance = EXCLUDED.provenance",
            &[
                &id,
                &scope.scope_kind,
                &scope.scope_id,
                &scope.project_id,
                &scope.topic_name,
                &path,
                &link.target,
                &link_text,
                &provenance,
            ],
        )?;
    }
    Ok(())
}

fn upsert_source(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    source: &WikiSource,
) -> Result<(), postgres::Error> {
    let path = path_string(&source.path);
    let id = scoped_id(scope, "source", &[&path]);
    let source_kind = document_kind_name(source.kind);
    let provenance = json!({ "source_path": path.as_str() }).to_string();

    tx.execute(
        "INSERT INTO gwiki_sources (
             id, scope_kind, scope_id, project_id, topic_name, path, source_kind,
             content_hash, provenance, captured_at
         )
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
         ON CONFLICT (scope_kind, scope_id, path) DO UPDATE SET
             source_kind = EXCLUDED.source_kind,
             content_hash = EXCLUDED.content_hash,
             provenance = EXCLUDED.provenance,
             captured_at = NOW()",
        &[
            &id,
            &scope.scope_kind,
            &scope.scope_id,
            &scope.project_id,
            &scope.topic_name,
            &path,
            &source_kind,
            &source.content_hash,
            &provenance,
        ],
    )?;
    Ok(())
}

fn record_ingestion(
    tx: &mut postgres::Transaction<'_>,
    scope: &WikiIndexScope,
    index: usize,
    ingestion: &WikiIngestion,
) -> Result<(), postgres::Error> {
    let path = path_string(&ingestion.path);
    let status = ingestion_event_name(ingestion.event);
    let content_hash = ingestion.content_hash.clone().unwrap_or_default();
    let source_kind = document_kind_for_path(&ingestion.path)
        .map(document_kind_name)
        .unwrap_or("unknown");
    let id = scoped_id(
        scope,
        "ingestion",
        &[&path, status, &content_hash, &index.to_string()],
    );
    let provenance = json!({ "event": status }).to_string();

    tx.execute(
        "INSERT INTO gwiki_ingestions (
             id, scope_kind, scope_id, project_id, topic_name, path, source_kind,
             content_hash, provenance, status, ingested_at
         )
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, NOW())
         ON CONFLICT (id) DO NOTHING",
        &[
            &id,
            &scope.scope_kind,
            &scope.scope_id,
            &scope.project_id,
            &scope.topic_name,
            &path,
            &source_kind,
            &content_hash,
            &provenance,
            &status,
        ],
    )?;
    Ok(())
}

fn scoped_id(scope: &WikiIndexScope, row_kind: &str, parts: &[&str]) -> String {
    let mut id = format!("{}:{}:{row_kind}", scope.scope_kind, scope.scope_id);
    for part in parts {
        id.push(':');
        let normalized = part
            .chars()
            .map(|ch| if matches!(ch, '\\' | ':') { '/' } else { ch })
            .collect::<String>();
        id.push_str(&normalized);
    }
    id
}

fn path_string(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn document_kind_name(kind: WikiDocumentKind) -> &'static str {
    match kind {
        WikiDocumentKind::SourceCatalog => "source_catalog",
        WikiDocumentKind::SourceNote => "source_note",
        WikiDocumentKind::Concept => "concept",
        WikiDocumentKind::Topic => "topic",
    }
}

fn document_kind_for_path(path: &Path) -> Option<WikiDocumentKind> {
    let normalized = path_string(path);
    if normalized == "raw/INDEX.md" {
        return Some(WikiDocumentKind::SourceCatalog);
    }
    if normalized.starts_with("wiki/sources/") {
        return Some(WikiDocumentKind::SourceNote);
    }
    if normalized.starts_with("wiki/concepts/") {
        return Some(WikiDocumentKind::Concept);
    }
    if normalized.starts_with("wiki/topics/") {
        return Some(WikiDocumentKind::Topic);
    }
    None
}

fn ingestion_event_name(event: WikiIngestionEvent) -> &'static str {
    match event {
        WikiIngestionEvent::Added => "added",
        WikiIngestionEvent::Changed => "changed",
        WikiIngestionEvent::Deleted => "deleted",
        WikiIngestionEvent::Unchanged => "unchanged",
    }
}
