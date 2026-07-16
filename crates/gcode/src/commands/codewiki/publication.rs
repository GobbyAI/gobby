//! Failure-safe staging and publication for generated codewiki pages.
//!
//! Generation writes a complete working vault below `_meta/`. The live vault
//! changes only after generation finishes and every generated `code/` link
//! resolves in the staged page set. Publication installs placeholders before
//! replacing pages, then prunes stale pages and commits live metadata last.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::lock::CodewikiWriterLock;
use super::truth_digest::TRUTH_DIGEST_META_PATH;
use super::{
    AiGenerationSettings, CODEWIKI_META_PATH, CodewikiAiOutcome, CodewikiIndexSnapshot,
    CodewikiMeta, OWNERSHIP_META_PATH, hash_snapshot_file,
};
use crate::index::hasher;

const STAGE_VERSION: u32 = 2;
const STAGE_DIR: &str = "_meta/codewiki-stage";
const STAGE_MANIFEST: &str = "manifest.json";
const STAGE_VAULT: &str = "vault";
const PUBLICATION_JOURNAL: &str = "_meta/codewiki-publication.json";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct PublicationFingerprint {
    version: u32,
    project_root: String,
    ai_mode: String,
    leaf_ai_outcome: String,
    aggregate_ai_outcome: String,
    ai_prose_depth: String,
    ai_register: String,
    ai_aggregate_profile: String,
    ai_aggregate_candidates: Vec<String>,
    scopes: Vec<String>,
    since_changed: Vec<String>,
    source_hashes: BTreeMap<String, String>,
    index_snapshot_hash: String,
}

#[derive(Debug, Deserialize, Serialize)]
struct StageManifest {
    fingerprint: PublicationFingerprint,
    force_full_hash_scan: bool,
}

impl PublicationFingerprint {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn from_run(
        project_root: &Path,
        source_files: &[String],
        ai_mode: &str,
        ai_settings: &AiGenerationSettings,
        leaf_ai_outcome: CodewikiAiOutcome,
        aggregate_ai_outcome: CodewikiAiOutcome,
        scopes: &[String],
        since_changed: Option<&BTreeSet<String>>,
        index_snapshot: &CodewikiIndexSnapshot,
    ) -> anyhow::Result<Self> {
        let mut source_hashes = BTreeMap::new();
        for source in source_files {
            // Indexed files can vanish from disk before the run starts (the
            // index lags external commits that delete sources); skip them like
            // the snapshot builder does instead of aborting the run (#18248).
            let Some(hash) = hash_snapshot_file(project_root, source)? else {
                eprintln!("warning: skipping codewiki source file missing from disk: {source}");
                continue;
            };
            source_hashes.insert(source.clone(), hash);
        }
        let mut normalized_scopes = scopes.to_vec();
        normalized_scopes.sort();
        normalized_scopes.dedup();
        let snapshot = serde_json::to_vec(index_snapshot)?;
        Ok(Self {
            version: STAGE_VERSION,
            project_root: project_root
                .canonicalize()
                .unwrap_or_else(|_| project_root.to_path_buf())
                .to_string_lossy()
                .into_owned(),
            ai_mode: ai_mode.to_string(),
            leaf_ai_outcome: ai_outcome_key(leaf_ai_outcome),
            aggregate_ai_outcome: ai_outcome_key(aggregate_ai_outcome),
            ai_prose_depth: ai_settings.prose_depth.clone(),
            ai_register: ai_settings.register.clone(),
            ai_aggregate_profile: ai_settings.aggregate_profile.clone(),
            ai_aggregate_candidates: ai_settings.aggregate_candidates.clone(),
            scopes: normalized_scopes,
            since_changed: since_changed.into_iter().flatten().cloned().collect(),
            source_hashes,
            index_snapshot_hash: hasher::content_hash(&snapshot),
        })
    }
}

fn ai_outcome_key(outcome: CodewikiAiOutcome) -> String {
    format!(
        "{}:{}:{}",
        outcome.route_label(),
        outcome.fallback,
        outcome.status.as_str()
    )
}

#[derive(Debug, Deserialize, Serialize)]
struct PublicationJournal {
    version: u32,
    phase: PublicationPhase,
    #[serde(default)]
    changed_paths: Vec<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum PublicationPhase {
    Prepared,
    PlaceholdersInstalled,
    PagesReplaced,
    StalePruned,
    MetadataCommitted,
}

#[derive(Debug, Default)]
struct PublicationPlan {
    changed: Vec<String>,
    stale: Vec<String>,
    placeholders: Vec<String>,
}

pub(crate) struct CodewikiPublication {
    _live_lock: CodewikiWriterLock,
    live_out: PathBuf,
    stage_root: PathBuf,
    stage_out: PathBuf,
    recovered_changed: BTreeSet<String>,
    force_full_hash_scan: bool,
}

impl CodewikiPublication {
    pub(crate) fn prepare(
        live_out: &Path,
        fingerprint: &PublicationFingerprint,
    ) -> anyhow::Result<Self> {
        fs::create_dir_all(live_out)?;
        let live_lock = CodewikiWriterLock::acquire(live_out)?;
        let stage_root = live_out.join(STAGE_DIR);
        let stage_out = stage_root.join(STAGE_VAULT);
        let mut recovered_changed = BTreeSet::new();
        if live_out.join(PUBLICATION_JOURNAL).exists() {
            recovered_changed.extend(publish_staged(live_out, &stage_root, None)?);
            remove_stage(&stage_root)?;
        }
        let force_full_hash_scan = match read_stage_manifest(&stage_root) {
            Some(mut manifest)
                if manifest.fingerprint.version == fingerprint.version
                    && manifest.fingerprint.project_root == fingerprint.project_root
                    && manifest.fingerprint.scopes == fingerprint.scopes =>
            {
                if manifest.fingerprint != *fingerprint {
                    manifest.fingerprint = fingerprint.clone();
                    manifest.force_full_hash_scan = true;
                    atomic_write_json(&stage_root.join(STAGE_MANIFEST), &manifest)?;
                }
                manifest.force_full_hash_scan
            }
            _ => {
                seed_stage(live_out, &stage_root, fingerprint)?;
                false
            }
        };
        Ok(Self {
            _live_lock: live_lock,
            live_out: live_out.to_path_buf(),
            stage_root,
            stage_out,
            recovered_changed,
            force_full_hash_scan,
        })
    }

    pub(crate) fn stage_out(&self) -> &Path {
        &self.stage_out
    }

    pub(crate) fn requires_full_hash_scan(&self) -> bool {
        self.force_full_hash_scan
    }

    pub(crate) fn publish(mut self) -> anyhow::Result<Vec<String>> {
        self.recovered_changed
            .extend(publish_staged(&self.live_out, &self.stage_root, None)?);
        remove_stage(&self.stage_root)?;
        Ok(self.recovered_changed.into_iter().collect())
    }

    #[cfg(test)]
    pub(crate) fn publish_interrupt_after_replacements(
        self,
        replacements: usize,
    ) -> anyhow::Result<Vec<String>> {
        publish_staged(&self.live_out, &self.stage_root, Some(replacements))
    }
}

fn read_stage_manifest(stage_root: &Path) -> Option<StageManifest> {
    let manifest_path = stage_root.join(STAGE_MANIFEST);
    let stage_out = stage_root.join(STAGE_VAULT);
    if !stage_out.is_dir() || !manifest_path.is_file() {
        return None;
    }
    fs::read(&manifest_path)
        .ok()
        .and_then(|content| serde_json::from_slice(&content).ok())
}

fn seed_stage(
    live_out: &Path,
    stage_root: &Path,
    fingerprint: &PublicationFingerprint,
) -> anyhow::Result<()> {
    remove_stage(stage_root)?;
    let parent = stage_root
        .parent()
        .ok_or_else(|| anyhow::anyhow!("codewiki stage path has no parent"))?;
    fs::create_dir_all(parent)?;
    let seed_root = parent.join(format!("codewiki-stage.seed.{}", std::process::id()));
    remove_stage(&seed_root)?;
    let seed_out = seed_root.join(STAGE_VAULT);
    fs::create_dir_all(&seed_out)?;
    if live_out.join("code").is_dir() {
        copy_tree(&live_out.join("code"), &seed_out.join("code"))?;
    }
    for meta_path in [
        CODEWIKI_META_PATH,
        OWNERSHIP_META_PATH,
        TRUTH_DIGEST_META_PATH,
    ] {
        let source = live_out.join(meta_path);
        if source.is_file() {
            atomic_copy(&source, &seed_out.join(meta_path))?;
        }
    }
    atomic_write_json(
        &seed_root.join(STAGE_MANIFEST),
        &StageManifest {
            fingerprint: fingerprint.clone(),
            force_full_hash_scan: false,
        },
    )?;
    fs::rename(&seed_root, stage_root)?;
    Ok(())
}

fn copy_tree(source: &Path, target: &Path) -> anyhow::Result<()> {
    reject_symlink(source)?;
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            anyhow::bail!("refusing to stage symlink {}", entry.path().display());
        }
        let child_target = target.join(entry.file_name());
        if file_type.is_dir() {
            copy_tree(&entry.path(), &child_target)?;
        } else if file_type.is_file() {
            atomic_copy(&entry.path(), &child_target)?;
        }
    }
    Ok(())
}

fn publish_staged(
    live_out: &Path,
    stage_root: &Path,
    interrupt_after_replacements: Option<usize>,
) -> anyhow::Result<Vec<String>> {
    let stage_out = stage_root.join(STAGE_VAULT);
    let plan = validate_and_plan(live_out, &stage_out)?;
    let mut reported_changed: BTreeSet<String> = read_journal(live_out)
        .map(|journal| journal.changed_paths.into_iter().collect())
        .unwrap_or_default();
    reported_changed.extend(plan.changed.iter().cloned());
    let reported_changed = reported_changed.into_iter().collect::<Vec<_>>();
    write_journal(live_out, PublicationPhase::Prepared, &reported_changed)?;

    for path in &plan.placeholders {
        let target = safe_join(live_out, path)?;
        if !target.exists() {
            atomic_write(&target, placeholder_page(path).as_bytes())?;
        }
    }
    write_journal(
        live_out,
        PublicationPhase::PlaceholdersInstalled,
        &reported_changed,
    )?;

    for (index, path) in plan.changed.iter().enumerate() {
        let source = safe_join(&stage_out, path)?;
        let target = safe_join(live_out, path)?;
        atomic_copy(&source, &target)?;
        if interrupt_after_replacements.is_some_and(|limit| index + 1 >= limit) {
            anyhow::bail!("injected codewiki publication interruption");
        }
    }
    write_journal(live_out, PublicationPhase::PagesReplaced, &reported_changed)?;

    for path in &plan.stale {
        let target = safe_join(live_out, path)?;
        match fs::remove_file(&target) {
            Ok(()) => prune_empty_dirs(live_out, target.parent()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    write_journal(live_out, PublicationPhase::StalePruned, &reported_changed)?;

    for path in [OWNERSHIP_META_PATH, TRUTH_DIGEST_META_PATH] {
        let source = safe_join(&stage_out, path)?;
        if source.is_file() {
            atomic_copy(&source, &safe_join(live_out, path)?)?;
        }
    }
    atomic_copy(
        &safe_join(&stage_out, CODEWIKI_META_PATH)?,
        &safe_join(live_out, CODEWIKI_META_PATH)?,
    )?;
    write_journal(
        live_out,
        PublicationPhase::MetadataCommitted,
        &reported_changed,
    )?;
    fs::remove_file(live_out.join(PUBLICATION_JOURNAL))?;
    Ok(reported_changed)
}

fn validate_and_plan(live_out: &Path, stage_out: &Path) -> anyhow::Result<PublicationPlan> {
    let meta_path = safe_join(stage_out, CODEWIKI_META_PATH)?;
    let meta: CodewikiMeta = serde_json::from_slice(&fs::read(&meta_path).map_err(|error| {
        anyhow::anyhow!(
            "completed codewiki stage is missing {}: {error}",
            meta_path.display()
        )
    })?)?;
    let mut stage_pages = collect_markdown_pages(stage_out)?;
    stage_pages.sort();
    let stage_set = stage_pages.iter().cloned().collect::<BTreeSet<_>>();
    for path in meta.docs.keys() {
        if path.starts_with("code/") && !stage_set.contains(path) {
            anyhow::bail!("completed codewiki stage is missing generated target {path}");
        }
    }

    let mut referenced = BTreeSet::new();
    for page in meta.docs.keys().filter(|path| path.starts_with("code/")) {
        let content = fs::read_to_string(safe_join(stage_out, page)?)?;
        for target in code_wikilinks(&content)? {
            if !stage_set.contains(&target) {
                anyhow::bail!("generated link in {page} has no staged target {target}");
            }
            referenced.insert(target);
        }
    }

    let live_pages = collect_markdown_pages(live_out)?;
    let live_set = live_pages.iter().cloned().collect::<BTreeSet<_>>();
    let mut changed = Vec::new();
    for path in &stage_pages {
        if files_differ(&safe_join(stage_out, path)?, &safe_join(live_out, path)?)? {
            changed.push(path.clone());
        }
    }
    let stale = live_set.difference(&stage_set).cloned().collect();
    let placeholders = referenced.difference(&live_set).cloned().collect();
    Ok(PublicationPlan {
        changed,
        stale,
        placeholders,
    })
}

pub(crate) fn code_wikilinks(content: &str) -> anyhow::Result<BTreeSet<String>> {
    let masked = mask_code_regions(content);
    let mut targets = BTreeSet::new();
    let mut rest = masked.as_str();
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find("]]") else {
            break;
        };
        let raw = &rest[..end];
        rest = &rest[end + 2..];
        let target = raw
            .split('|')
            .next()
            .unwrap_or(raw)
            .split('#')
            .next()
            .unwrap_or(raw)
            .trim();
        if !target.starts_with("code/") {
            continue;
        }
        let target = if target.ends_with(".md") {
            target.to_string()
        } else {
            format!("{target}.md")
        };
        validate_relative_path(&target)?;
        targets.insert(target);
    }
    Ok(targets)
}

/// Mask fenced code blocks and inline code spans with spaces so wikilink
/// extraction only sees prose. Generated pages legitimately quote wikilink
/// syntax inside code — e.g. the modules.rs doc page describing the
/// `Module: [[code/modules/<file.module>]]` line it renders — and a quoted
/// example is not a link, so it must not fail publish validation (#17823).
fn mask_code_regions(content: &str) -> String {
    let mut fenced_masked = String::with_capacity(content.len());
    let mut open_fence: Option<(char, usize)> = None;
    for line in content.split_inclusive('\n') {
        let fence = fence_marker(line);
        let inside = match (open_fence, fence) {
            (None, Some(opened)) => {
                open_fence = Some(opened);
                true
            }
            (Some((ch, len)), Some((close_ch, close_len)))
                if close_ch == ch && close_len >= len =>
            {
                open_fence = None;
                true
            }
            (Some(_), _) => true,
            (None, None) => false,
        };
        if inside {
            mask_into(&mut fenced_masked, line.chars());
        } else {
            fenced_masked.push_str(line);
        }
    }
    mask_inline_code(&fenced_masked)
}

/// A line opening or closing a fenced code block: at least three backticks or
/// tildes after optional indentation, per CommonMark.
fn fence_marker(line: &str) -> Option<(char, usize)> {
    let trimmed = line.trim_start_matches(' ');
    ['`', '~'].into_iter().find_map(|ch| {
        let count = trimmed.chars().take_while(|c| *c == ch).count();
        (count >= 3).then_some((ch, count))
    })
}

/// Mask inline code spans: a run of N backticks closes only on the next run of
/// exactly N backticks (CommonMark); an unmatched run stays literal text.
fn mask_inline_code(content: &str) -> String {
    let chars: Vec<char> = content.chars().collect();
    let mut masked = String::with_capacity(content.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] != '`' {
            masked.push(chars[i]);
            i += 1;
            continue;
        }
        let open = i;
        while i < chars.len() && chars[i] == '`' {
            i += 1;
        }
        match find_backtick_run(&chars, i, i - open) {
            Some(close) => {
                let span_end = close + (i - open);
                mask_into(&mut masked, chars[open..span_end].iter().copied());
                i = span_end;
            }
            None => masked.extend(&chars[open..i]),
        }
    }
    masked
}

/// Start of the next backtick run of exactly `len` characters at or after
/// `from`, if any.
fn find_backtick_run(chars: &[char], from: usize, len: usize) -> Option<usize> {
    let mut i = from;
    while i < chars.len() {
        if chars[i] != '`' {
            i += 1;
            continue;
        }
        let start = i;
        while i < chars.len() && chars[i] == '`' {
            i += 1;
        }
        if i - start == len {
            return Some(start);
        }
    }
    None
}

/// Append `chars` to `out` with every character except newlines replaced by a
/// space, preserving line structure for later passes.
fn mask_into(out: &mut String, chars: impl Iterator<Item = char>) {
    out.extend(chars.map(|c| if c == '\n' { '\n' } else { ' ' }));
}

fn collect_markdown_pages(root: &Path) -> anyhow::Result<Vec<String>> {
    let code_root = root.join("code");
    if !code_root.is_dir() {
        return Ok(Vec::new());
    }
    let mut pages = Vec::new();
    let mut stack = vec![code_root];
    while let Some(dir) = stack.pop() {
        reject_symlink(&dir)?;
        for entry in fs::read_dir(&dir)? {
            let entry = entry?;
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                anyhow::bail!("refusing codewiki symlink {}", entry.path().display());
            }
            if file_type.is_dir() {
                stack.push(entry.path());
            } else if file_type.is_file() && entry.path().extension().is_some_and(|ext| ext == "md")
            {
                pages.push(
                    entry
                        .path()
                        .strip_prefix(root)?
                        .to_string_lossy()
                        .replace(std::path::MAIN_SEPARATOR, "/"),
                );
            }
        }
    }
    Ok(pages)
}

fn placeholder_page(path: &str) -> String {
    let title = path
        .trim_end_matches(".md")
        .rsplit('/')
        .next()
        .unwrap_or("Codewiki page")
        .replace(['-', '_'], " ");
    format!(
        "---\ntitle: \"{}\"\ngenerated_by: gcode-codewiki\ntrust: structural\n\
         freshness: publishing\nstructural_placeholder: true\n---\n\n# {}\n\nPublication in progress.\n",
        yaml_escape(&title),
        title
    )
}

fn yaml_escape(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn files_differ(left: &Path, right: &Path) -> anyhow::Result<bool> {
    if !right.is_file() {
        return Ok(true);
    }
    Ok(fs::read(left)? != fs::read(right)?)
}

fn read_journal(live_out: &Path) -> Option<PublicationJournal> {
    fs::read(live_out.join(PUBLICATION_JOURNAL))
        .ok()
        .and_then(|content| serde_json::from_slice(&content).ok())
}

fn write_journal(
    live_out: &Path,
    phase: PublicationPhase,
    changed_paths: &[String],
) -> anyhow::Result<()> {
    atomic_write_json(
        &safe_join(live_out, PUBLICATION_JOURNAL)?,
        &PublicationJournal {
            version: STAGE_VERSION,
            phase,
            changed_paths: changed_paths.to_vec(),
        },
    )
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> anyhow::Result<()> {
    let mut content = serde_json::to_vec_pretty(value)?;
    content.push(b'\n');
    atomic_write(path, &content)
}

fn atomic_copy(source: &Path, target: &Path) -> anyhow::Result<()> {
    reject_symlink(source)?;
    atomic_write(target, &fs::read(source)?)
}

fn atomic_write(target: &Path, content: &[u8]) -> anyhow::Result<()> {
    let parent = target
        .parent()
        .ok_or_else(|| anyhow::anyhow!("codewiki target has no parent: {}", target.display()))?;
    fs::create_dir_all(parent)?;
    reject_symlink(parent)?;
    if target.exists() {
        reject_symlink(target)?;
    }
    let name = target
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| anyhow::anyhow!("invalid codewiki target: {}", target.display()))?;
    let temporary = parent.join(format!(".{name}.codewiki-tmp-{}", std::process::id()));
    match fs::remove_file(&temporary) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)?;
    file.write_all(content)?;
    file.sync_all()?;
    drop(file);
    fs::rename(&temporary, target)?;
    sync_dir(parent);
    Ok(())
}

fn sync_dir(path: &Path) {
    if let Ok(dir) = File::open(path) {
        let _ = dir.sync_all();
    }
}

fn safe_join(root: &Path, relative: &str) -> anyhow::Result<PathBuf> {
    validate_relative_path(relative)?;
    Ok(root.join(relative))
}

fn validate_relative_path(relative: &str) -> anyhow::Result<()> {
    let safe = !relative.is_empty()
        && Path::new(relative)
            .components()
            .all(|component| matches!(component, Component::Normal(_)));
    if !safe {
        anyhow::bail!("unsafe codewiki publication path: {relative}");
    }
    Ok(())
}

fn reject_symlink(path: &Path) -> anyhow::Result<()> {
    if fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        anyhow::bail!("refusing codewiki symlink {}", path.display());
    }
    Ok(())
}

fn prune_empty_dirs(root: &Path, mut current: Option<&Path>) {
    while let Some(dir) = current {
        if dir == root || !dir.starts_with(root) {
            break;
        }
        match fs::remove_dir(dir) {
            Ok(()) => current = dir.parent(),
            Err(_) => break,
        }
    }
}

fn remove_stage(stage_root: &Path) -> anyhow::Result<()> {
    match fs::remove_dir_all(stage_root) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}
