use super::doc_paths::{
    collect_generated_doc_pages, prune_empty_doc_dirs, refresh_doc_if_needed,
    reject_symlinked_doc_path, safe_doc_path, scoped_file_doc, scoped_module_doc, write_doc,
};
use super::frontmatter::{
    apply_ai_outcome_to_markdown, lane_observability_from_content, page_frontmatter_blocks_reuse,
    source_files_from_frontmatter,
};
use super::*;

const CONTENT_SENSITIVE_INVALIDATION_PREFIX: &str = "content-sensitive:";

pub(crate) fn content_sensitive_invalidation_key(content: &str) -> String {
    format!(
        "{CONTENT_SENSITIVE_INVALIDATION_PREFIX}{}",
        hasher::content_hash(content.as_bytes())
    )
}

fn content_sensitive_target_matches(
    invalidation_key: Option<&str>,
    existing: Option<&str>,
    generated: &str,
) -> bool {
    !invalidation_key.is_some_and(|key| key.starts_with(CONTENT_SENSITIVE_INVALIDATION_PREFIX))
        || existing == Some(generated)
}

pub fn write_doc_set(out_dir: &Path, docs: &[(String, String)]) -> anyhow::Result<()> {
    std::fs::create_dir_all(out_dir)?;
    for (relative_path, content) in docs {
        write_doc(out_dir, relative_path, content)?;
    }
    Ok(())
}

pub fn write_incremental_doc_set(
    project_root: &Path,
    out_dir: &Path,
    docs: &[(String, String)],
) -> anyhow::Result<Vec<String>> {
    let docs = docs
        .iter()
        .map(|(path, content)| BuiltDoc::healthy(path.clone(), content.clone()))
        .collect::<Vec<_>>();
    write_incremental_doc_set_with_snapshot(
        project_root,
        out_dir,
        &docs,
        None,
        "off",
        DocPruneScope::unscoped(),
    )
}

pub(crate) fn write_incremental_doc_set_with_snapshot(
    project_root: &Path,
    out_dir: &Path,
    docs: &[BuiltDoc],
    index_snapshot: Option<CodewikiIndexSnapshot>,
    ai_mode: &str,
    prune_scope: DocPruneScope,
) -> anyhow::Result<Vec<String>> {
    let mut sink = DocSink::open_with_prune_scope(project_root, out_dir, ai_mode, prune_scope)?;
    for doc in docs {
        sink.persist(doc)?;
    }
    sink.finish(index_snapshot)
}

#[derive(Clone, Debug, Default)]
pub(crate) struct DocPruneScope {
    scopes: Vec<String>,
}

impl DocPruneScope {
    pub(crate) fn unscoped() -> Self {
        Self { scopes: Vec::new() }
    }

    pub(crate) fn from_scopes(scopes: &[String]) -> Self {
        if scopes.is_empty() || scopes.iter().any(|scope| scope.is_empty()) {
            Self::unscoped()
        } else {
            Self {
                scopes: scopes.to_vec(),
            }
        }
    }

    pub(crate) fn is_unscoped(&self) -> bool {
        self.scopes.is_empty()
    }

    pub(crate) fn includes_file(&self, file: &str) -> bool {
        self.is_unscoped() || in_scope(file, &self.scopes)
    }

    pub(crate) fn includes_module(&self, module: &str) -> bool {
        self.is_unscoped() || in_scope(module, &self.scopes)
    }

    pub(crate) fn includes_doc(&self, doc_path: &str) -> bool {
        if self.is_unscoped() {
            return true;
        }
        if let Some(file) = scoped_file_doc(doc_path) {
            return self.includes_file(file);
        }
        if let Some(module) = scoped_module_doc(doc_path) {
            return self.includes_module(module);
        }
        false
    }

    fn should_prune(&self, doc_path: &str) -> bool {
        self.includes_doc(doc_path)
    }
}

/// Incremental doc writer that persists each doc and its meta entry the
/// moment the doc is built (#681). A killed run keeps every page written so
/// far plus a meta log that matches them, so the next run resumes from disk
/// instead of regenerating everything.
#[derive(Debug)]
pub(crate) struct DocSink<'a> {
    /// Exclusive per-out_dir writer lock (#17732), acquired at open and held
    /// through `finish` (or the drop of a failed run) so a second concurrent
    /// codewiki run cannot interleave page and meta writes with this one.
    _writer_lock: lock::CodewikiWriterLock,
    project_root: &'a Path,
    out_dir: &'a Path,
    ai_mode: String,
    ai_outcome: CodewikiAiOutcome,
    /// Requested AI generation settings of the current run, recorded into each
    /// written doc's meta and compared against the previous entry so a
    /// settings change is never mistaken for "unchanged" (#17530).
    ai_settings: AiGenerationSettings,
    previous_docs: BTreeMap<String, CodewikiDocMeta>,
    next_docs: BTreeMap<String, CodewikiDocMeta>,
    seen: BTreeSet<String>,
    generated_docs: Vec<String>,
    previous_snapshot: Option<CodewikiIndexSnapshot>,
    prune_scope: DocPruneScope,
    /// Pages actually written with `degraded = true` this run (a failed AI pass
    /// fell back to the structural body, #900). Excludes unchanged skips, which
    /// keep their previous healthy meta. Surfaced via `degraded_docs()` so the
    /// run reports degradation instead of silently caching it.
    degraded_docs: Vec<String>,
    diagram_stats: Option<DiagramStats>,
    /// Files git reported as possibly-changed since the `--since` ref (Leaf H,
    /// #893). When `Some`, a source-provenance page whose own sources and
    /// neighbors are all outside the diff is left exactly as it is on disk —
    /// not rewritten — so the rewrite set stays scoped to the change set plus
    /// dependents. `None` is the full-scan default.
    since: Option<BTreeSet<String>>,
}

impl<'a> DocSink<'a> {
    #[cfg(test)]
    pub(crate) fn open(
        project_root: &'a Path,
        out_dir: &'a Path,
        ai_mode: &str,
    ) -> anyhow::Result<Self> {
        Self::open_with_prune_scope(project_root, out_dir, ai_mode, DocPruneScope::unscoped())
    }

    pub(crate) fn open_with_prune_scope(
        project_root: &'a Path,
        out_dir: &'a Path,
        ai_mode: &str,
        prune_scope: DocPruneScope,
    ) -> anyhow::Result<Self> {
        std::fs::create_dir_all(out_dir)?;
        // Lock before reading the previous meta, so the snapshot this run
        // resumes from can never be a concurrent writer's half-flushed state.
        let writer_lock = lock::CodewikiWriterLock::acquire(out_dir)?;
        let previous = read_codewiki_meta(out_dir)?;
        Ok(Self {
            _writer_lock: writer_lock,
            project_root,
            out_dir,
            ai_mode: ai_mode.to_string(),
            ai_outcome: CodewikiAiOutcome::default(),
            ai_settings: AiGenerationSettings::default(),
            previous_docs: previous.docs.clone(),
            // An interrupted run must not lose entries for docs it never
            // reached, so the next meta starts from the previous entries and
            // is pruned only by a completed run (`finish`).
            next_docs: previous.docs,
            seen: BTreeSet::new(),
            generated_docs: Vec::new(),
            previous_snapshot: previous.index_snapshot,
            prune_scope,
            degraded_docs: Vec::new(),
            diagram_stats: None,
            since: None,
        })
    }

    pub(crate) fn with_ai_outcome(mut self, ai_outcome: CodewikiAiOutcome) -> Self {
        self.ai_outcome = ai_outcome;
        self
    }

    /// Sets the current run's requested AI generation settings, recorded into
    /// each written doc's meta and part of the unchanged comparison (#17530).
    pub(crate) fn with_ai_settings(mut self, ai_settings: AiGenerationSettings) -> Self {
        self.ai_settings = ai_settings;
        self
    }

    /// Scopes the sink's rewrite decisions to a `--since` change set: a
    /// source-provenance page whose sources and neighbors are all outside the
    /// set is left untouched (Leaf H, #893). `None` keeps the full-scan default.
    pub(crate) fn with_since(mut self, since: Option<BTreeSet<String>>) -> Self {
        self.since = since;
        self
    }

    pub(crate) fn set_diagram_stats(&mut self, stats: DiagramStats) {
        debug_assert_eq!(stats.total(), stats.recorded_slots_len());
        self.diagram_stats = Some(stats);
    }

    /// Write one doc unless it is provably unchanged, then flush the meta log
    /// so what is on disk always matches what the meta records.
    pub(crate) fn persist(&mut self, doc: &BuiltDoc) -> anyhow::Result<bool> {
        self.persist_with_ai_outcome(doc, self.ai_outcome)
    }

    pub(crate) fn persist_with_ai_outcome(
        &mut self,
        doc: &BuiltDoc,
        ai_outcome: CodewikiAiOutcome,
    ) -> anyhow::Result<bool> {
        let target = safe_doc_path(self.out_dir, &doc.path)?;
        let write_outcome = ai_outcome.for_doc(doc.degraded);
        // A degraded write outcome degrades the page even when its builder
        // reported it healthy (e.g. `_ownership.md` emits `degraded: false`
        // but the run-level model probe failed): the recorded meta must say
        // so, or the #687 never-reuse-degraded rule silently skips the page
        // on the next run (#18291).
        let degraded = doc.degraded || write_outcome.status == AiGenerationStatus::Degraded;
        let content = apply_ai_outcome_to_markdown(&doc.content, write_outcome);
        let content = if doc.path.ends_with(".md") {
            strict_markdown::normalize_codewiki_markdown(&content)
        } else {
            content
        };
        let previous_meta = self.previous_docs.get(&doc.path);
        let doc_ai_settings = self.ai_settings.for_path(&doc.path);
        // The on-disk page can also be degraded while the manifest claims it
        // is healthy (manifest/page skew, #18291); such a page never
        // satisfies any reuse gate below.
        let existing_target = std::fs::read_to_string(&target).ok();
        let target_blocks_reuse = existing_target
            .as_deref()
            .is_some_and(page_frontmatter_blocks_reuse);
        // Deterministic docs can explicitly key their generated content. Their
        // metadata may still compare equal while a killed/resumed run leaves
        // an older body on disk, so require byte equality before reusing them.
        let content_sensitive_target_matches = content_sensitive_target_matches(
            doc.invalidation_key.as_deref(),
            existing_target.as_deref(),
            &content,
        );
        // Keyed docs take the fast path only while their key is unchanged: a
        // key move (aggregate digest, module link, child-link set) must fall
        // through to the full gate and rewrite even under `--since` (#17731).
        if let (Some(since), Some(meta)) = (self.since.as_ref(), previous_meta)
            && doc.invalidation_key == meta.invalidation_key
            && target.exists()
            && !target_blocks_reuse
            && content_sensitive_target_matches
            && !meta.degraded
            && meta.ai_mode == self.ai_mode
            && meta.ai_route == ai_outcome.route_label()
            && meta.ai_fallback == ai_outcome.fallback
            && meta.ai_generation_status == ai_outcome.status.as_str()
            && doc_ai_settings.matches_meta(meta)
            && meta.render_version == render_version_for_path(&doc.path)
            && !meta.source_hashes.is_empty()
            && (doc.summary.is_none() || meta.summary.is_some())
            && meta
                .source_hashes
                .keys()
                .chain(meta.neighbor_hashes.keys())
                .all(|file| !since.contains(file))
        {
            let refreshed = refresh_doc_if_needed(self.out_dir, &doc.path, &content)?;
            if refreshed {
                self.generated_docs.push(doc.path.clone());
            }
            self.next_docs.insert(doc.path.clone(), meta.clone());
            self.seen.insert(doc.path.clone());
            self.flush()?;
            return Ok(refreshed);
        }

        let source_hashes = source_hashes_for_doc(self.project_root, &content)?;
        let neighbor_hashes = neighbor_hashes_for_doc(self.project_root, &doc.neighbors)?;
        // Two invalidation models share this gate (Leaf H, #893):
        //
        // * A *derived aggregate page* (architecture/infrastructure/feature
        //   catalog/audit) carries an `invalidation_key` — a SystemModel /
        //   contract / deprecation digest. It is unchanged exactly when that
        //   digest still matches, so a model-irrelevant edit (a function body)
        //   leaves it alone while a manifest/contract change rebuilds it. The
        //   page usually has no provenance frontmatter, so the source-hash
        //   comparison would be vacuous and is skipped for it.
        // * A *source-file page* has no key. It is unchanged when its own
        //   sources AND its cross-file neighbors (#885) all still hash to the
        //   recorded values. Docs without provenance frontmatter have no source
        //   hashes to compare (e.g. code/_ownership.md), so they are always
        //   rewritten. A degraded doc is always rewritten (#687); a summary that
        //   should be recorded but is missing forces a one-time rewrite (#681);
        //   an AI-mode, render-version, or generation-settings (#17530) change
        //   invalidates content hashes cannot see.
        let unchanged = target.exists()
            && !target_blocks_reuse
            && content_sensitive_target_matches
            && previous_meta.is_some_and(|meta| {
                !meta.degraded
                    && meta.ai_mode == self.ai_mode
                    && meta.ai_route == ai_outcome.route_label()
                    && meta.ai_fallback == ai_outcome.fallback
                    && meta.ai_generation_status == ai_outcome.status.as_str()
                    && doc_ai_settings.matches_meta(meta)
                    && meta.render_version == render_version_for_path(&doc.path)
                    && match &doc.invalidation_key {
                        Some(key) => {
                            meta.invalidation_key.as_deref() == Some(key.as_str())
                                // Summary backfill (#681) applies to keyed
                                // source pages (file/module docs) exactly like
                                // unkeyed ones; aggregates carry no summary,
                                // so the condition is vacuous for them.
                                && (doc.summary.is_none() || meta.summary.is_some())
                                && (!doc.invalidation_key_requires_sources
                                    || (!source_hashes.is_empty()
                                        && meta.source_hashes == source_hashes
                                        && meta.neighbor_hashes == neighbor_hashes))
                        }
                        None => {
                            !source_hashes.is_empty()
                                && meta.source_hashes == source_hashes
                                && meta.neighbor_hashes == neighbor_hashes
                                && (doc.summary.is_none() || meta.summary.is_some())
                        }
                    }
            });
        // `--since` leaves a source-provenance page untouched when none of its
        // own sources or neighbors are in the diff — even if it would otherwise
        // re-hash differently — so a run is scoped to the change set plus
        // dependents. Keyed aggregates and provenance-less pages keep their
        // normal logic above, so a manifest/contract change still rebuilds them.
        let since_unchanged = !source_hashes.is_empty()
            && target.exists()
            && !target_blocks_reuse
            && content_sensitive_target_matches
            && previous_meta.is_some_and(|meta| {
                meta.invalidation_key == doc.invalidation_key
                    && !meta.degraded
                    && meta.ai_mode == self.ai_mode
                    && meta.ai_route == ai_outcome.route_label()
                    && meta.ai_fallback == ai_outcome.fallback
                    && meta.ai_generation_status == ai_outcome.status.as_str()
                    && doc_ai_settings.matches_meta(meta)
                    && meta.render_version == render_version_for_path(&doc.path)
                    && source_hash_key_sets_match(&meta.source_hashes, &source_hashes)
                    && source_hash_key_sets_match(&meta.neighbor_hashes, &neighbor_hashes)
                    && (doc.summary.is_none() || meta.summary.is_some())
            })
            && self.since.as_ref().is_some_and(|since| {
                source_hashes
                    .keys()
                    .chain(neighbor_hashes.keys())
                    .all(|file| !since.contains(file))
            });
        let unchanged = unchanged || since_unchanged;

        let refreshed = if unchanged {
            refresh_doc_if_needed(self.out_dir, &doc.path, &content)?
        } else {
            false
        };
        let entry = if unchanged {
            if refreshed {
                self.generated_docs.push(doc.path.clone());
            }
            // A skip keeps the previous healthy content on disk, so the meta
            // entry keeps the previous summary and stays healthy even when
            // this run's generation failed — degraded fallback never displaces
            // healthy prose for unchanged sources.
            previous_meta.cloned().unwrap_or_default()
        } else {
            write_doc(self.out_dir, &doc.path, &content)?;
            self.generated_docs.push(doc.path.clone());
            if degraded {
                self.degraded_docs.push(doc.path.clone());
            }
            // Mirror tool-loop observability (lane/tool-call/turn counts) from
            // the page frontmatter into `_meta/codewiki.json` for traceability;
            // absent for one-shot / leaf / deterministic pages (#978).
            let lane = lane_observability_from_content(&content);
            CodewikiDocMeta {
                source_hashes,
                degraded,
                // Degraded fallbacks are never reused, so their summaries are
                // never recorded.
                summary: if degraded { None } else { doc.summary.clone() },
                ai_mode: self.ai_mode.clone(),
                ai_route: write_outcome.route_label().to_string(),
                ai_fallback: write_outcome.fallback,
                ai_generation_status: write_outcome.status.as_str().to_string(),
                render_version: render_version_for_path(&doc.path),
                neighbor_hashes,
                invalidation_key: doc.invalidation_key.clone(),
                lane: lane.lane,
                tool_call_count: lane.tool_call_count,
                turns: lane.turns,
                ai_prose_depth: doc_ai_settings.prose_depth,
                ai_register: doc_ai_settings.register,
                ai_aggregate_profile: doc_ai_settings.aggregate_profile,
                ai_aggregate_candidates: doc_ai_settings.aggregate_candidates,
            }
        };
        self.next_docs.insert(doc.path.clone(), entry);
        self.seen.insert(doc.path.clone());
        self.flush()?;
        Ok(!unchanged || refreshed)
    }

    /// Pages written with a degraded structural fallback this run (#900), in
    /// build order. Read before `finish` consumes the sink.
    pub(crate) fn degraded_docs(&self) -> &[String] {
        &self.degraded_docs
    }

    fn flush(&self) -> anyhow::Result<()> {
        let meta = CodewikiMeta {
            docs: self.next_docs.clone(),
            generated_docs: self.generated_docs.clone(),
            // The previous snapshot is kept until the run completes so an
            // interrupted run still diffs changes against the last complete
            // one.
            index_snapshot: self.previous_snapshot.clone(),
            ai_mode: self.ai_mode.clone(),
            diagram_stats: self.diagram_stats.clone(),
        };
        write_codewiki_meta(self.out_dir, &meta)
    }

    /// Deletes one stale page from disk and drops its meta entry.
    fn remove_doc(&mut self, doc_path: &str) -> anyhow::Result<()> {
        let target = safe_doc_path(self.out_dir, doc_path)?;
        reject_symlinked_doc_path(self.out_dir, &target)?;
        match std::fs::remove_file(&target) {
            Ok(()) => prune_empty_doc_dirs(self.out_dir, &target)?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
        self.next_docs.remove(doc_path);
        Ok(())
    }

    /// Complete the run: delete docs the run no longer produced, then write
    /// the final meta log with the new index snapshot.
    pub(crate) fn finish(
        mut self,
        index_snapshot: Option<CodewikiIndexSnapshot>,
    ) -> anyhow::Result<Vec<String>> {
        // Reclaim every page the completed run did not (re)produce, unioning
        // two sources both gated by `prune_scope` (so a scoped run still only
        // touches in-scope pages):
        //   1. tracked meta entries carried over from the previous run that
        //      were not regenerated this run — slug churn, a deleted source.
        //   2. on-disk `code/**.md` pages absent from the meta entirely — a
        //      cleared `_meta/codewiki.json` (the "delete the cache to force a
        //      clean run" workflow) or a narrative chapter whose AI-derived slug
        //      changed before the deterministic-slug scheme landed. The cache-
        //      only prune (1) can never see these, so a churned page used to
        //      linger as a broken-link / degraded orphan (#900).
        // Synthesized breadcrumb stubs (#17639) are owned by this finish pass,
        // not by generation, so "not regenerated this run" never marks them
        // stale here; unrequired stubs are pruned after synthesis below.
        let mut stale = self
            .next_docs
            .iter()
            .filter(|(key, meta)| {
                !self.seen.contains(*key)
                    && self.prune_scope.should_prune(key)
                    && !super::stubs::is_stub_meta(Some(meta))
            })
            .map(|(key, _)| key.clone())
            .collect::<BTreeSet<_>>();
        for doc_path in collect_generated_doc_pages(self.out_dir)? {
            if !self.seen.contains(&doc_path)
                && self.prune_scope.should_prune(&doc_path)
                && !super::stubs::is_stub_meta(self.next_docs.get(&doc_path))
            {
                stale.insert(doc_path);
            }
        }
        for stale_path in stale {
            self.remove_doc(&stale_path)?;
        }
        // Breadcrumb closure (#17639): a scoped run emits module pages whose
        // Parent chain — and `code/repo.md` — can fall outside the scope
        // filter and never exist on disk, leaving dangling generated links.
        // Synthesize deterministic structural stubs for the missing ancestors
        // from the post-prune page set (pruning first, so a stub is never
        // parented on pages this run just reclaimed). Only missing pages and
        // pages recorded as stubs are written; real pages are never replaced.
        for doc in super::stubs::breadcrumb_stub_docs(self.out_dir, &self.next_docs)? {
            self.persist(&doc)?;
        }
        // A stub the closure no longer requires (its subtree was reclaimed or
        // replaced by real pages) was skipped by synthesis above; reclaim it
        // now under the same scope gate as the ordinary prune.
        let stale_stubs = self
            .next_docs
            .iter()
            .filter(|(key, meta)| {
                !self.seen.contains(*key)
                    && self.prune_scope.should_prune(key)
                    && super::stubs::is_stub_meta(Some(meta))
            })
            .map(|(key, _)| key.clone())
            .collect::<Vec<_>>();
        for stale_path in stale_stubs {
            self.remove_doc(&stale_path)?;
        }
        let meta = CodewikiMeta {
            docs: self.next_docs,
            generated_docs: self.generated_docs.clone(),
            index_snapshot: index_snapshot.or(self.previous_snapshot),
            ai_mode: self.ai_mode,
            diagram_stats: self.diagram_stats,
        };
        write_codewiki_meta(self.out_dir, &meta)?;
        Ok(self.generated_docs)
    }
}
pub(crate) fn read_codewiki_meta(out_dir: &Path) -> anyhow::Result<CodewikiMeta> {
    let path = safe_doc_path(out_dir, CODEWIKI_META_PATH)?;
    let mut meta: CodewikiMeta = match std::fs::read_to_string(&path) {
        Ok(raw) => serde_json::from_str(&raw)?,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Ok(CodewikiMeta::default());
        }
        Err(err) => return Err(err.into()),
    };
    // Entries written before per-doc AI modes existed inherit the run-level
    // mode they were generated under.
    let run_mode = meta.ai_mode.clone();
    for doc in meta.docs.values_mut() {
        if doc.ai_mode.is_empty() {
            doc.ai_mode = run_mode.clone();
        }
    }
    Ok(meta)
}

pub(crate) fn write_codewiki_meta(out_dir: &Path, meta: &CodewikiMeta) -> anyhow::Result<()> {
    let content = serde_json::to_string_pretty(meta)?;
    write_doc(out_dir, CODEWIKI_META_PATH, &(content + "\n"))
}

pub(crate) fn read_ownership_meta(out_dir: &Path) -> anyhow::Result<OwnershipMeta> {
    let path = safe_doc_path(out_dir, OWNERSHIP_META_PATH)?;
    match std::fs::read_to_string(&path) {
        Ok(raw) => Ok(serde_json::from_str::<OwnershipMeta>(&raw)?),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(OwnershipMeta::default()),
        Err(err) => Err(err.into()),
    }
}

pub(crate) fn write_ownership_meta(out_dir: &Path, meta: &OwnershipMeta) -> anyhow::Result<()> {
    let content = serde_json::to_string_pretty(meta)?;
    write_doc(out_dir, OWNERSHIP_META_PATH, &(content + "\n"))
}

pub(crate) fn source_hashes_for_doc(
    project_root: &Path,
    content: &str,
) -> anyhow::Result<BTreeMap<String, String>> {
    let mut hashes = BTreeMap::new();
    let canonical_root = project_root
        .canonicalize()
        .map_err(|err| anyhow::anyhow!("failed to resolve codewiki project root: {err}"))?;
    for file in source_files_from_frontmatter(content) {
        let source_path = project_root.join(&file);
        // Sources can be deleted by external commits between generation (which
        // reads the index) and this on-disk re-hash at persist; skip their
        // hashes instead of aborting the run (#18109). The doc then misses a
        // source hash and re-keys for regeneration on the next run.
        let canonical_source = match source_path.canonicalize() {
            Ok(path) => path,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                eprintln!("warning: skipping codewiki source hash for deleted file: {file}");
                continue;
            }
            Err(err) => {
                return Err(anyhow::anyhow!(
                    "failed to resolve codewiki source file {file}: {err}"
                ));
            }
        };
        if !canonical_source.starts_with(&canonical_root) {
            anyhow::bail!("codewiki source file {file} resolves outside project root");
        }
        let hash = match hasher::file_content_hash(&canonical_source) {
            Ok(hash) => hash,
            Err(err)
                if err
                    .downcast_ref::<std::io::Error>()
                    .is_some_and(|io_err| io_err.kind() == std::io::ErrorKind::NotFound) =>
            {
                eprintln!("warning: skipping codewiki source hash for deleted file: {file}");
                continue;
            }
            Err(err) => {
                return Err(anyhow::anyhow!(
                    "failed to hash codewiki source file {file}: {err}"
                ));
            }
        };
        hashes.insert(file, hash);
    }
    Ok(hashes)
}

fn source_hash_key_sets_match(
    recorded: &BTreeMap<String, String>,
    current: &BTreeMap<String, String>,
) -> bool {
    recorded.len() == current.len() && current.keys().all(|file| recorded.contains_key(file))
}

/// Content hashes of a page's cross-file neighbor files (#885, Leaf H). Unlike
/// [`source_hashes_for_doc`], a neighbor that no longer resolves inside the
/// project is dropped rather than erroring: a vanished neighbor is itself a
/// change, surfaced when the recorded set no longer matches on the next compare.
pub(crate) fn neighbor_hashes_for_doc(
    project_root: &Path,
    neighbors: &BTreeSet<String>,
) -> anyhow::Result<BTreeMap<String, String>> {
    if neighbors.is_empty() {
        return Ok(BTreeMap::new());
    }
    let canonical_root = project_root
        .canonicalize()
        .map_err(|err| anyhow::anyhow!("failed to resolve codewiki project root: {err}"))?;
    let mut hashes = BTreeMap::new();
    for file in neighbors {
        let Ok(canonical_source) = project_root.join(file).canonicalize() else {
            continue;
        };
        if !canonical_source.starts_with(&canonical_root) {
            continue;
        }
        if let Ok(hash) = hasher::file_content_hash(&canonical_source) {
            hashes.insert(file.clone(), hash);
        }
    }
    Ok(hashes)
}

#[cfg(test)]
mod diagram_stats_tests {
    use super::*;

    #[test]
    fn diagram_outcomes_are_persisted_in_codewiki_metadata() {
        let project = tempfile::tempdir().expect("project tempdir");
        let out = tempfile::tempdir().expect("output tempdir");
        let mut progress = CodewikiProgress::capture();
        let mut stats = DiagramStats::default();
        stats.record(
            "code/_architecture.md",
            DiagramKind::CuratedFlow,
            &DiagramOutcome::Rejected,
            &mut progress,
        );

        let mut sink = DocSink::open(project.path(), out.path(), "off").expect("sink opens");
        sink.set_diagram_stats(stats);
        sink.finish(None).expect("sink finishes");

        let meta = read_codewiki_meta(out.path()).expect("metadata reads");
        let stats = meta.diagram_stats.expect("diagram stats persisted");
        assert_eq!(stats.emitted, 0);
        assert_eq!(stats.sparse_evidence, 0);
        assert_eq!(stats.no_generator, 0);
        assert_eq!(stats.rejected, 1);
        assert_eq!(stats.total(), 1);
    }
}
