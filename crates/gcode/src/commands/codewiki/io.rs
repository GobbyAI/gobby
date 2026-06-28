use super::*;
use gobby_core::codewiki_contract::{AI_FALLBACK_KEY, AI_GENERATION_STATUS_KEY, AI_ROUTE_KEY};
use gobby_core::config::AiRouting;

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
pub(crate) struct DocSink<'a> {
    project_root: &'a Path,
    out_dir: &'a Path,
    ai_mode: String,
    ai_outcome: CodewikiAiOutcome,
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
        let previous = read_codewiki_meta(out_dir)?;
        Ok(Self {
            project_root,
            out_dir,
            ai_mode: ai_mode.to_string(),
            ai_outcome: CodewikiAiOutcome::default(),
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
            since: None,
        })
    }

    pub(crate) fn with_ai_outcome(mut self, ai_outcome: CodewikiAiOutcome) -> Self {
        self.ai_outcome = ai_outcome;
        self
    }

    /// Scopes the sink's rewrite decisions to a `--since` change set: a
    /// source-provenance page whose sources and neighbors are all outside the
    /// set is left untouched (Leaf H, #893). `None` keeps the full-scan default.
    pub(crate) fn with_since(mut self, since: Option<BTreeSet<String>>) -> Self {
        self.since = since;
        self
    }

    /// Write one doc unless it is provably unchanged, then flush the meta log
    /// so what is on disk always matches what the meta records.
    pub(crate) fn persist(&mut self, doc: &BuiltDoc) -> anyhow::Result<bool> {
        let target = safe_doc_path(self.out_dir, &doc.path)?;
        let write_outcome = self.ai_outcome.for_doc(doc.degraded);
        let content = apply_ai_outcome_to_markdown(&doc.content, write_outcome);
        let previous_meta = self.previous_docs.get(&doc.path);
        if let (Some(since), Some(meta)) = (self.since.as_ref(), previous_meta)
            && doc.invalidation_key.is_none()
            && target.exists()
            && !meta.degraded
            && meta.ai_mode == self.ai_mode
            && meta.ai_route == self.ai_outcome.route_label()
            && meta.ai_fallback == self.ai_outcome.fallback
            && meta.ai_generation_status == self.ai_outcome.status.as_str()
            && meta.render_version == CODEWIKI_RENDER_VERSION
            && !meta.source_hashes.is_empty()
            && (doc.summary.is_none() || meta.summary.is_some())
            && meta
                .source_hashes
                .keys()
                .chain(meta.neighbor_hashes.keys())
                .all(|file| !since.contains(file))
        {
            self.next_docs.insert(doc.path.clone(), meta.clone());
            self.seen.insert(doc.path.clone());
            self.flush()?;
            return Ok(false);
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
        //   an AI-mode or render-version change invalidates content hashes
        //   cannot see.
        let unchanged = target.exists()
            && previous_meta.is_some_and(|meta| {
                !meta.degraded
                    && meta.ai_mode == self.ai_mode
                    && meta.ai_route == self.ai_outcome.route_label()
                    && meta.ai_fallback == self.ai_outcome.fallback
                    && meta.ai_generation_status == self.ai_outcome.status.as_str()
                    && meta.render_version == CODEWIKI_RENDER_VERSION
                    && match &doc.invalidation_key {
                        Some(key) => {
                            meta.invalidation_key.as_deref() == Some(key.as_str())
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
        let since_unchanged = doc.invalidation_key.is_none()
            && !source_hashes.is_empty()
            && target.exists()
            && previous_meta.is_some_and(|meta| {
                !meta.degraded
                    && meta.ai_mode == self.ai_mode
                    && meta.ai_route == self.ai_outcome.route_label()
                    && meta.ai_fallback == self.ai_outcome.fallback
                    && meta.ai_generation_status == self.ai_outcome.status.as_str()
                    && meta.render_version == CODEWIKI_RENDER_VERSION
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

        let entry = if unchanged {
            // A skip keeps the previous healthy content on disk, so the meta
            // entry keeps the previous summary and stays healthy even when
            // this run's generation failed — degraded fallback never displaces
            // healthy prose for unchanged sources.
            previous_meta.cloned().unwrap_or_default()
        } else {
            write_doc(self.out_dir, &doc.path, &content)?;
            self.generated_docs.push(doc.path.clone());
            if doc.degraded {
                self.degraded_docs.push(doc.path.clone());
            }
            CodewikiDocMeta {
                source_hashes,
                degraded: doc.degraded,
                // Degraded fallbacks are never reused, so their summaries are
                // never recorded.
                summary: if doc.degraded {
                    None
                } else {
                    doc.summary.clone()
                },
                ai_mode: self.ai_mode.clone(),
                ai_route: write_outcome.route_label().to_string(),
                ai_fallback: write_outcome.fallback,
                ai_generation_status: write_outcome.status.as_str().to_string(),
                render_version: CODEWIKI_RENDER_VERSION,
                neighbor_hashes,
                invalidation_key: doc.invalidation_key.clone(),
            }
        };
        self.next_docs.insert(doc.path.clone(), entry);
        self.seen.insert(doc.path.clone());
        self.flush()?;
        Ok(!unchanged)
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
        };
        write_codewiki_meta(self.out_dir, &meta)
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
        let mut stale = self
            .next_docs
            .keys()
            .filter(|key| !self.seen.contains(*key) && self.prune_scope.should_prune(key))
            .cloned()
            .collect::<BTreeSet<_>>();
        for doc_path in collect_generated_doc_pages(self.out_dir)? {
            if !self.seen.contains(&doc_path) && self.prune_scope.should_prune(&doc_path) {
                stale.insert(doc_path);
            }
        }
        for stale_path in stale {
            let target = safe_doc_path(self.out_dir, &stale_path)?;
            reject_symlinked_doc_path(self.out_dir, &target)?;
            match std::fs::remove_file(&target) {
                Ok(()) => prune_empty_doc_dirs(self.out_dir, &target)?,
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => return Err(err.into()),
            }
            self.next_docs.remove(&stale_path);
        }
        let meta = CodewikiMeta {
            docs: self.next_docs,
            generated_docs: self.generated_docs.clone(),
            index_snapshot: index_snapshot.or(self.previous_snapshot),
            ai_mode: self.ai_mode,
        };
        write_codewiki_meta(self.out_dir, &meta)?;
        Ok(self.generated_docs)
    }
}

const AI_NOTICE_START: &str = "<!-- codewiki-ai-notice:start -->\n";
const AI_NOTICE_END: &str = "<!-- codewiki-ai-notice:end -->\n";

fn apply_ai_outcome_to_markdown(content: &str, outcome: CodewikiAiOutcome) -> String {
    let Some((frontmatter_body, rest)) = split_frontmatter(content) else {
        return content.to_string();
    };

    let mut out = String::from("---\n");
    for line in frontmatter_body.lines() {
        if !is_ai_frontmatter_line(line) {
            out.push_str(line);
            out.push('\n');
        }
    }
    out.push_str(AI_ROUTE_KEY);
    out.push_str(": ");
    out.push_str(outcome.route_label());
    out.push('\n');
    out.push_str(AI_FALLBACK_KEY);
    out.push_str(": ");
    out.push_str(if outcome.fallback { "true" } else { "false" });
    out.push('\n');
    out.push_str(AI_GENERATION_STATUS_KEY);
    out.push_str(": ");
    out.push_str(outcome.status.as_str());
    out.push('\n');
    out.push_str("---\n");

    let rest = strip_existing_ai_notice(rest);
    if let Some(note) = ai_body_note(outcome) {
        out.push('\n');
        out.push_str(AI_NOTICE_START);
        out.push_str("> **AI notice:** ");
        out.push_str(note);
        out.push_str("\n\n");
        out.push_str(AI_NOTICE_END);
    }
    out.push_str(rest);
    out
}

fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    let first_line_end = content.find('\n')? + 1;
    if content[..first_line_end].trim_end_matches(['\r', '\n']) != "---" {
        return None;
    }

    let mut cursor = first_line_end;
    for line in content[first_line_end..].split_inclusive('\n') {
        let line_end = cursor + line.len();
        if line.trim_end_matches(['\r', '\n']) == "---" {
            return Some((&content[first_line_end..cursor], &content[line_end..]));
        }
        cursor = line_end;
    }
    None
}

fn is_ai_frontmatter_line(line: &str) -> bool {
    let key = line.trim_start();
    frontmatter_line_has_key(key, AI_ROUTE_KEY)
        || frontmatter_line_has_key(key, AI_FALLBACK_KEY)
        || frontmatter_line_has_key(key, AI_GENERATION_STATUS_KEY)
}

fn frontmatter_line_has_key(line: &str, key: &str) -> bool {
    line.strip_prefix(key)
        .is_some_and(|suffix| suffix.starts_with(':'))
}

fn strip_existing_ai_notice(rest: &str) -> &str {
    let Some(start) = rest.find(AI_NOTICE_START) else {
        return rest;
    };
    if rest[..start].trim().is_empty()
        && let Some(end) = rest[start + AI_NOTICE_START.len()..].find(AI_NOTICE_END)
    {
        let after = start + AI_NOTICE_START.len() + end + AI_NOTICE_END.len();
        return &rest[after..];
    }
    rest
}

fn ai_body_note(outcome: CodewikiAiOutcome) -> Option<&'static str> {
    match outcome.status {
        AiGenerationStatus::Degraded => {
            Some("AI generation failed for this page; structural fallback content is shown.")
        }
        AiGenerationStatus::Skipped if outcome.fallback || outcome.route != AiRouting::Off => {
            Some("AI generation did not run; this page contains structural documentation only.")
        }
        AiGenerationStatus::Generated if outcome.fallback && outcome.route == AiRouting::Direct => {
            Some(
                "Auto routing could not use the daemon, so this page was generated through the Direct route.",
            )
        }
        _ => None,
    }
}

/// On-disk `.md` pages under the codewiki-owned `code/` tree, as out-dir-relative
/// slash paths (e.g. `code/narrative/01-introduction.md`). Drives `finish`'s
/// cache-independent orphan GC (#900): a page on disk but absent from this run's
/// `seen` set is reclaimed even when the meta log never listed it. Scoped to
/// `code/` so the rest of the vault — the gwiki research notes, `.obsidian/`,
/// `_meta/` — is never walked. Symlinks are not followed and never returned,
/// matching `reject_symlinked_doc_path`.
fn collect_generated_doc_pages(out_dir: &Path) -> anyhow::Result<Vec<String>> {
    let code_root = out_dir.join("code");
    if !code_root.is_dir() {
        return Ok(Vec::new());
    }
    let mut pages = Vec::new();
    let mut stack = vec![code_root];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                stack.push(path);
            } else if file_type.is_file()
                && path.extension().is_some_and(|ext| ext == "md")
                && let Ok(rel) = path.strip_prefix(out_dir)
            {
                pages.push(
                    rel.to_string_lossy()
                        .replace(std::path::MAIN_SEPARATOR, "/"),
                );
            }
        }
    }
    Ok(pages)
}

fn scoped_file_doc(doc_path: &str) -> Option<&str> {
    doc_path
        .strip_prefix("code/files/")
        .and_then(|path| path.strip_suffix(".md"))
}

fn scoped_module_doc(doc_path: &str) -> Option<&str> {
    doc_path
        .strip_prefix("code/modules/")
        .and_then(|path| path.strip_suffix(".md"))
}

pub(crate) fn write_doc(out_dir: &Path, relative_path: &str, content: &str) -> anyhow::Result<()> {
    let target = safe_doc_path(out_dir, relative_path)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let content = if Path::new(relative_path)
        .extension()
        .and_then(|extension| extension.to_str())
        == Some("md")
    {
        gobby_core::markdown::normalize_markdown(content)
    } else {
        content.to_string()
    };
    std::fs::write(target, content)?;
    Ok(())
}

pub(crate) fn reject_symlinked_doc_path(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let relative = target.strip_prefix(out_dir)?;
    let mut current = out_dir.to_path_buf();
    for component in relative.components() {
        current.push(component);
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                anyhow::bail!(
                    "refusing to follow symlinked codewiki path: {}",
                    current.display()
                );
            }
            Ok(_) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

pub(crate) fn prune_empty_doc_dirs(out_dir: &Path, target: &Path) -> anyhow::Result<()> {
    let mut current = target.parent();
    while let Some(dir) = current {
        if dir == out_dir {
            break;
        }
        match std::fs::remove_dir(dir) {
            Ok(()) => current = dir.parent(),
            Err(err)
                if matches!(
                    err.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::DirectoryNotEmpty
                ) =>
            {
                break;
            }
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
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
        let canonical_source = source_path.canonicalize().map_err(|err| {
            anyhow::anyhow!("failed to resolve codewiki source file {file}: {err}")
        })?;
        if !canonical_source.starts_with(&canonical_root) {
            anyhow::bail!("codewiki source file {file} resolves outside project root");
        }
        let hash = hasher::file_content_hash(&canonical_source)
            .map_err(|err| anyhow::anyhow!("failed to hash codewiki source file {file}: {err}"))?;
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

pub(crate) fn source_files_from_frontmatter(content: &str) -> BTreeSet<String> {
    let mut files = BTreeSet::new();

    let mut lines = content.lines();
    if lines.next() != Some("---") {
        return files;
    }
    let frontmatter = lines
        .take_while(|line| *line != "---")
        .collect::<Vec<_>>()
        .join("\n");
    let Ok(serde_yaml::Value::Mapping(frontmatter)) =
        serde_yaml::from_str::<serde_yaml::Value>(&frontmatter)
    else {
        return files;
    };

    for key in [gobby_core::codewiki_contract::PROVENANCE_KEY] {
        let key = serde_yaml::Value::String(key.to_string());
        let Some(serde_yaml::Value::Sequence(sources)) = frontmatter.get(&key) else {
            continue;
        };
        for source in sources {
            let serde_yaml::Value::Mapping(source) = source else {
                continue;
            };
            let file_key = serde_yaml::Value::String(
                gobby_core::codewiki_contract::PROVENANCE_FILE_KEY.to_string(),
            );
            if let Some(serde_yaml::Value::String(file)) = source.get(&file_key) {
                files.insert(file.clone());
            }
        }
    }
    files
}

#[cfg(test)]
pub(crate) fn unquote_yaml_string(value: &str) -> Option<String> {
    let value = value.trim();
    let inner = value.strip_prefix('"')?.strip_suffix('"')?;
    let mut out = String::new();
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            out.push(match chars.next()? {
                '0' => '\0',
                'a' => '\u{0007}',
                'b' => '\u{0008}',
                't' => '\t',
                'n' => '\n',
                'v' => '\u{000b}',
                'f' => '\u{000c}',
                'r' => '\r',
                'e' => '\u{001b}',
                '"' => '"',
                '/' => '/',
                '\\' => '\\',
                'x' => decode_hex_escape(&mut chars, 2)?,
                'u' => decode_hex_escape(&mut chars, 4)?,
                'U' => decode_hex_escape(&mut chars, 8)?,
                _ => return None,
            });
        } else {
            out.push(ch);
        }
    }
    Some(out)
}

#[cfg(test)]
fn decode_hex_escape(chars: &mut std::str::Chars<'_>, digits: usize) -> Option<char> {
    let mut value = 0_u32;
    for _ in 0..digits {
        value = value.checked_mul(16)?;
        value = value.checked_add(chars.next()?.to_digit(16)?)?;
    }
    char::from_u32(value)
}

pub(crate) fn safe_doc_path(out_dir: &Path, relative_path: &str) -> anyhow::Result<PathBuf> {
    let path = Path::new(relative_path);
    if path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        anyhow::bail!("refusing to write unsafe codewiki path: {relative_path}");
    }
    Ok(out_dir.join(path))
}
