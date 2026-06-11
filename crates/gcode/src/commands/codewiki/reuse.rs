use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use super::io::{read_codewiki_meta, safe_doc_path};
use super::{CodewikiDocMeta, SourceSpan};
use crate::index::hasher;

/// Decides whether a doc's previous content can be reused without any LLM
/// call: same AI mode, healthy, sources unchanged, and still on disk (#681).
/// Degraded docs are never reusable (#687).
pub(crate) struct ReusePlan {
    project_root: PathBuf,
    out_dir: PathBuf,
    ai_mode: String,
    docs: BTreeMap<String, CodewikiDocMeta>,
    /// Lazy current-content hashes; `None` records an unhashable file so a
    /// missing source is probed once and never reused.
    current_hashes: BTreeMap<String, Option<String>>,
}

impl ReusePlan {
    pub(crate) fn load(project_root: &Path, out_dir: &Path, ai_mode: &str) -> anyhow::Result<Self> {
        let previous = read_codewiki_meta(out_dir)?;
        Ok(Self {
            project_root: project_root.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            ai_mode: ai_mode.to_string(),
            docs: previous.docs,
            current_hashes: BTreeMap::new(),
        })
    }

    /// The on-disk page of a reusable doc, or `None` when the doc must be
    /// regenerated. Emitting disk content verbatim keeps a forced rewrite
    /// lossless.
    pub(crate) fn reusable_page(
        &mut self,
        doc_path: &str,
        sources: &BTreeSet<String>,
    ) -> Option<String> {
        if !self.reusable(doc_path, sources) {
            return None;
        }
        let target = safe_doc_path(&self.out_dir, doc_path).ok()?;
        std::fs::read_to_string(target).ok()
    }

    /// Both the on-disk page and the recorded summary of a reusable doc.
    pub(crate) fn reusable_page_with_summary(
        &mut self,
        doc_path: &str,
        sources: &BTreeSet<String>,
    ) -> Option<(String, String)> {
        let summary = self.docs.get(doc_path)?.summary.clone()?;
        let page = self.reusable_page(doc_path, sources)?;
        Some((page, summary))
    }

    fn reusable(&mut self, doc_path: &str, sources: &BTreeSet<String>) -> bool {
        let Some(entry) = self.docs.get(doc_path) else {
            return false;
        };
        // An empty hash set cannot prove the doc unchanged (#672), and a mode
        // change invalidates content that hashes cannot see (#677).
        if entry.degraded || entry.ai_mode != self.ai_mode || entry.source_hashes.is_empty() {
            return false;
        }
        if entry.source_hashes.len() != sources.len()
            || !entry
                .source_hashes
                .keys()
                .all(|file| sources.contains(file))
        {
            return false;
        }
        let expected = entry.source_hashes.clone();
        for (file, expected_hash) in &expected {
            if self.current_hash(file).as_deref() != Some(expected_hash.as_str()) {
                return false;
            }
        }
        let Ok(target) = safe_doc_path(&self.out_dir, doc_path) else {
            return false;
        };
        target.exists()
    }

    fn current_hash(&mut self, file: &str) -> Option<String> {
        if let Some(hash) = self.current_hashes.get(file) {
            return hash.clone();
        }
        let hash = hasher::file_content_hash(&self.project_root.join(file)).ok();
        self.current_hashes.insert(file.to_string(), hash.clone());
        hash
    }
}

/// Distinct source files cited by a doc's spans — the provenance set whose
/// hashes decide reuse.
pub(crate) fn span_files(spans: &[SourceSpan]) -> BTreeSet<String> {
    spans.iter().map(|span| span.file.clone()).collect()
}
