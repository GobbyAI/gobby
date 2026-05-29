use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

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
