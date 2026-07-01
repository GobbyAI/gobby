use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use super::helpers::{validate_chunk_paths, validate_link_paths};
use super::{
    StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion, WikiLink, WikiSource,
};

/// In-memory wiki index used by local shell commands and tests.
///
/// Large vaults can consume substantial memory because documents, chunks,
/// links, and source metadata are retained together. Set
/// `GWIKI_MAX_MEMORY_INDEX_BYTES` to cap the markdown bytes accepted by this
/// path before indexing.
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
        validate_chunk_paths(path, &chunks)?;
        self.chunk_replacements += 1;
        self.chunks.insert(path.to_path_buf(), chunks);
        Ok(())
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
        validate_link_paths(path, &links)?;
        self.link_replacements += 1;
        self.links.insert(path.to_path_buf(), links);
        Ok(())
    }

    fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
        self.source_upserts += 1;
        self.sources.insert(source.document_path.clone(), source);
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
