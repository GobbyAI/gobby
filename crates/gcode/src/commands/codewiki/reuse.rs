use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use super::io::{read_codewiki_meta, safe_doc_path};
use super::{BuiltDoc, CODEWIKI_RENDER_VERSION, CodewikiAiOutcome, CodewikiDocMeta, SourceSpan};
use crate::index::hasher;

/// Decides whether a doc's previous content can be reused without any LLM
/// call: same AI mode, healthy, sources unchanged, and still on disk (#681).
/// Degraded docs are never reusable (#687).
pub(crate) struct ReusePlan {
    project_root: PathBuf,
    out_dir: PathBuf,
    ai_mode: String,
    ai_outcome: CodewikiAiOutcome,
    docs: BTreeMap<String, CodewikiDocMeta>,
    /// Lazy current-content hashes; `None` records an unhashable file so a
    /// missing source is probed once and never reused.
    current_hashes: BTreeMap<String, Option<String>>,
    /// Files git reported as possibly-changed since the `--since` ref (Leaf H,
    /// #893). When `Some`, a file outside this set keeps its recorded hash
    /// without a disk read, so an unchanged page reuses without a re-hash;
    /// dependents fall out naturally because a page is reused only when every
    /// one of its own sources and neighbors hashes as recorded. `None` runs a
    /// full content-hash scan (the from-scratch default).
    since: Option<BTreeSet<String>>,
    /// File → recorded content hash from the previous run's metadata, used to
    /// answer `current_hash` for `--since`-unchanged files without disk I/O.
    recorded_hashes: BTreeMap<String, String>,
}

impl ReusePlan {
    #[cfg(test)]
    pub(crate) fn load(project_root: &Path, out_dir: &Path, ai_mode: &str) -> anyhow::Result<Self> {
        Self::load_with_ai_outcome(project_root, out_dir, ai_mode, CodewikiAiOutcome::default())
    }

    #[cfg(test)]
    pub(crate) fn load_with_ai_outcome(
        project_root: &Path,
        out_dir: &Path,
        ai_mode: &str,
        ai_outcome: CodewikiAiOutcome,
    ) -> anyhow::Result<Self> {
        Self::load_with_since_and_ai_outcome(project_root, out_dir, ai_mode, None, ai_outcome)
    }

    /// Like [`ReusePlan::load`] but scoping the change set to the files git
    /// reports changed since a ref. `None` is the full-scan default (Leaf H).
    #[cfg(test)]
    pub(crate) fn load_with_since(
        project_root: &Path,
        out_dir: &Path,
        ai_mode: &str,
        since: Option<BTreeSet<String>>,
    ) -> anyhow::Result<Self> {
        Self::load_with_since_and_ai_outcome(
            project_root,
            out_dir,
            ai_mode,
            since,
            CodewikiAiOutcome::default(),
        )
    }

    pub(crate) fn load_with_since_and_ai_outcome(
        project_root: &Path,
        out_dir: &Path,
        ai_mode: &str,
        since: Option<BTreeSet<String>>,
        ai_outcome: CodewikiAiOutcome,
    ) -> anyhow::Result<Self> {
        let previous = read_codewiki_meta(out_dir)?;
        let mut recorded_hashes = BTreeMap::new();
        for meta in previous.docs.values() {
            for (file, hash) in meta.source_hashes.iter().chain(meta.neighbor_hashes.iter()) {
                recorded_hashes.insert(file.clone(), hash.clone());
            }
        }
        Ok(Self {
            project_root: project_root.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            ai_mode: ai_mode.to_string(),
            ai_outcome,
            docs: previous.docs,
            current_hashes: BTreeMap::new(),
            since,
            recorded_hashes,
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
        if !self.reusable(doc_path, sources, &BTreeSet::new()) {
            return None;
        }
        let target = safe_doc_path(&self.out_dir, doc_path).ok()?;
        std::fs::read_to_string(target).ok()
    }

    /// The on-disk page of a derived aggregate page (architecture,
    /// infrastructure, …) keyed on a page-type invalidation digest rather than
    /// a source-file set (Leaf H, #893). Reused only when the recorded digest
    /// matches `invalidation_key` — so a model-irrelevant edit (a function body)
    /// keeps the page even though source files changed.
    pub(crate) fn reusable_page_keyed(
        &mut self,
        doc_path: &str,
        invalidation_key: &str,
    ) -> Option<String> {
        let entry = self.docs.get(doc_path)?;
        if entry.degraded
            || entry.ai_mode != self.ai_mode
            || !entry_matches_ai_outcome(entry, self.ai_outcome)
            || entry.render_version != CODEWIKI_RENDER_VERSION
            || entry.invalidation_key.as_deref() != Some(invalidation_key)
        {
            return None;
        }
        let target = safe_doc_path(&self.out_dir, doc_path).ok()?;
        if !target.exists() {
            return None;
        }
        std::fs::read_to_string(target).ok()
    }

    pub(crate) fn reusable_page_keyed_with_sources(
        &mut self,
        doc_path: &str,
        invalidation_key: &str,
        sources: &BTreeSet<String>,
    ) -> Option<String> {
        let entry = self.docs.get(doc_path)?;
        if entry.invalidation_key.as_deref() != Some(invalidation_key) {
            return None;
        }
        if !self.reusable(doc_path, sources, &BTreeSet::new()) {
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
        self.reusable_page_with_summary_and_neighbors(doc_path, sources, &BTreeSet::new())
    }

    /// Like [`ReusePlan::reusable_page_with_summary`] but also invalidating the
    /// page when a cross-file neighbor's content changed even though the page's
    /// own sources did not (#885, Leaf H) — so a caller edit refreshes the
    /// callee's relationship narrative.
    pub(crate) fn reusable_page_with_summary_and_neighbors(
        &mut self,
        doc_path: &str,
        sources: &BTreeSet<String>,
        neighbors: &BTreeSet<String>,
    ) -> Option<(String, String)> {
        let summary = self.docs.get(doc_path)?.summary.clone()?;
        if !self.reusable(doc_path, sources, neighbors) {
            return None;
        }
        let target = safe_doc_path(&self.out_dir, doc_path).ok()?;
        let page = std::fs::read_to_string(target).ok()?;
        Some((page, summary))
    }

    pub(crate) fn reusable_pages_with_prefixes(
        &mut self,
        prefixes: &[&str],
    ) -> Option<Vec<BuiltDoc>> {
        let paths = self
            .docs
            .keys()
            .filter(|path| prefixes.iter().any(|prefix| path.starts_with(prefix)))
            .cloned()
            .collect::<Vec<_>>();
        if paths.is_empty() {
            return None;
        }

        let mut docs = Vec::with_capacity(paths.len());
        for path in paths {
            let entry = self.docs.get(&path)?;
            let sources = entry.source_hashes.keys().cloned().collect::<BTreeSet<_>>();
            let summary = entry.summary.clone();
            let content = self.reusable_page(&path, &sources)?;
            docs.push(BuiltDoc {
                path,
                content,
                degraded: false,
                summary,
                neighbors: BTreeSet::new(),
                invalidation_key: None,
                invalidation_key_requires_sources: false,
            });
        }
        docs.sort_by(|left, right| left.path.cmp(&right.path));
        Some(docs)
    }

    fn reusable(
        &mut self,
        doc_path: &str,
        sources: &BTreeSet<String>,
        neighbors: &BTreeSet<String>,
    ) -> bool {
        let Some(entry) = self.docs.get(doc_path) else {
            return false;
        };
        // A degraded doc is never "unchanged" — re-runs must repair it even
        // when its sources match (#687). An empty hash set cannot prove the
        // doc unchanged (#672), and a mode change invalidates content that
        // hashes cannot see (#677).
        if entry.degraded
            || entry.ai_mode != self.ai_mode
            || !entry_matches_ai_outcome(entry, self.ai_outcome)
            || entry.render_version != CODEWIKI_RENDER_VERSION
            || entry.source_hashes.is_empty()
        {
            return false;
        }
        // The recorded source set and the recorded cross-file neighbor set must
        // both still match exactly — a new or dropped source/neighbor (e.g. an
        // added caller, #885) is itself a change even before any hash differs.
        if !set_matches(&entry.source_hashes, sources)
            || !set_matches(&entry.neighbor_hashes, neighbors)
        {
            return false;
        }
        let expected = entry
            .source_hashes
            .clone()
            .into_iter()
            .chain(entry.neighbor_hashes.clone())
            .collect::<BTreeMap<_, _>>();
        for (file, expected_hash) in &expected {
            if self.current_hash(file).as_deref() != Some(expected_hash.as_str()) {
                return false;
            }
        }
        // Meta alone is not proof the page exists: deleting a page from disk
        // must force regeneration, which is also the supported manual way to
        // invalidate a single doc (#681).
        let Ok(target) = safe_doc_path(&self.out_dir, doc_path) else {
            return false;
        };
        target.exists()
    }

    fn current_hash(&mut self, file: &str) -> Option<String> {
        if let Some(hash) = self.current_hashes.get(file) {
            return hash.clone();
        }
        // Under `--since`, a file git did not report changed keeps its recorded
        // hash without a disk read, so an unchanged page reuses without a
        // re-hash and the rewrite set stays scoped to the diff + dependents.
        let hash = if self
            .since
            .as_ref()
            .is_some_and(|since| !since.contains(file))
        {
            self.recorded_hashes.get(file).cloned()
        } else {
            hasher::file_content_hash(&self.project_root.join(file)).ok()
        };
        self.current_hashes.insert(file.to_string(), hash.clone());
        hash
    }
}

/// Distinct source files cited by a doc's spans — the provenance set whose
/// hashes decide reuse.
pub(crate) fn span_files(spans: &[SourceSpan]) -> BTreeSet<String> {
    spans.iter().map(|span| span.file.clone()).collect()
}

/// True when the recorded hash map covers exactly the current file set — same
/// size and same keys — so an added or dropped file fails the match.
fn set_matches(recorded: &BTreeMap<String, String>, current: &BTreeSet<String>) -> bool {
    recorded.len() == current.len() && recorded.keys().all(|file| current.contains(file))
}

fn entry_matches_ai_outcome(entry: &CodewikiDocMeta, ai_outcome: CodewikiAiOutcome) -> bool {
    entry.ai_route == ai_outcome.route_label()
        && entry.ai_fallback == ai_outcome.fallback
        && entry.ai_generation_status == ai_outcome.status.as_str()
}
