use super::*;

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
    previous_docs: BTreeMap<String, CodewikiDocMeta>,
    next_docs: BTreeMap<String, CodewikiDocMeta>,
    seen: BTreeSet<String>,
    generated_docs: Vec<String>,
    previous_snapshot: Option<CodewikiIndexSnapshot>,
    prune_scope: DocPruneScope,
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
            previous_docs: previous.docs.clone(),
            // An interrupted run must not lose entries for docs it never
            // reached, so the next meta starts from the previous entries and
            // is pruned only by a completed run (`finish`).
            next_docs: previous.docs,
            seen: BTreeSet::new(),
            generated_docs: Vec::new(),
            previous_snapshot: previous.index_snapshot,
            prune_scope,
        })
    }

    /// Write one doc unless it is provably unchanged, then flush the meta log
    /// so what is on disk always matches what the meta records.
    pub(crate) fn persist(&mut self, doc: &BuiltDoc) -> anyhow::Result<bool> {
        let source_hashes = source_hashes_for_doc(self.project_root, &doc.content)?;
        let target = safe_doc_path(self.out_dir, &doc.path)?;
        let previous_meta = self.previous_docs.get(&doc.path);
        // Docs without provenance frontmatter have no source hashes to compare,
        // so hash equality is vacuous; always rewrite them so generator changes
        // propagate (e.g. code/_ownership.md). A doc recorded degraded is also
        // always rewritten: hash equality cannot see generation failures, and
        // skipping would preserve the degraded page forever (#687). A doc that
        // should carry a reusable summary but has none recorded is rewritten
        // once to backfill it (metas written before #681). Doc content depends
        // on the AI generation mode, which source hashes cannot capture; a
        // mode change invalidates the doc.
        let unchanged = target.exists()
            && !source_hashes.is_empty()
            && previous_meta.is_some_and(|meta| {
                !meta.degraded
                    && meta.ai_mode == self.ai_mode
                    && meta.render_version == CODEWIKI_RENDER_VERSION
                    && meta.source_hashes == source_hashes
                    && (doc.summary.is_none() || meta.summary.is_some())
            });

        let entry = if unchanged {
            // A skip keeps the previous healthy content on disk, so the meta
            // entry keeps the previous summary and stays healthy even when
            // this run's generation failed — degraded fallback never displaces
            // healthy prose for unchanged sources.
            previous_meta.cloned().unwrap_or_default()
        } else {
            write_doc(self.out_dir, &doc.path, &doc.content)?;
            self.generated_docs.push(doc.path.clone());
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
                render_version: CODEWIKI_RENDER_VERSION,
            }
        };
        self.next_docs.insert(doc.path.clone(), entry);
        self.seen.insert(doc.path.clone());
        self.flush()?;
        Ok(!unchanged)
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
        let stale = self
            .next_docs
            .keys()
            .filter(|key| !self.seen.contains(*key) && self.prune_scope.should_prune(key))
            .cloned()
            .collect::<Vec<_>>();
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
