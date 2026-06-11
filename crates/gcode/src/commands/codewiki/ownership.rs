use std::collections::{BTreeMap, BTreeSet, HashMap, btree_map::Entry};
use std::fmt::Write;
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, Instant};

use gix::bstr::ByteSlice;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::{file_wikilink, module_wikilink};
use crate::index::hasher;

#[derive(Debug, Clone)]
pub(crate) struct OwnershipOptions {
    pub blame_file_cap: usize,
    pub blame_timeout: Duration,
}

impl Default for OwnershipOptions {
    fn default() -> Self {
        Self {
            blame_file_cap: 200,
            blame_timeout: Duration::from_secs(20),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct OwnershipMeta {
    #[serde(default)]
    pub files: BTreeMap<String, CachedBlameSummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct CachedBlameSummary {
    pub content_hash: String,
    pub contributors: Vec<OwnershipContributor>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct OwnershipContributor {
    pub contributor_id: String,
    pub name: String,
    #[serde(default, skip_serializing)]
    pub email: Option<String>,
    pub lines: usize,
}

#[derive(Debug)]
enum BlameContributorsOutcome {
    Success(Vec<OwnershipContributor>),
    Timeout,
    Error(anyhow::Error),
}

enum WorkerRecv<T> {
    Completed(T),
    TimedOut,
    Disconnected,
}

#[derive(Debug)]
struct Codeowners {
    entries: Vec<CodeownersEntry>,
}

#[derive(Debug)]
struct CodeownersEntry {
    pattern: String,
    owners: Vec<String>,
}

#[derive(Debug, Default)]
struct OwnershipStatus {
    codeowners_available: bool,
    blame_available: bool,
    blame_errors: bool,
    partial: bool,
}

#[derive(Debug, Default)]
struct FileOwnership {
    declared: Vec<String>,
    derived: Vec<OwnershipContributor>,
}

pub(crate) fn build_ownership_doc(
    project_root: &Path,
    files: &[String],
    file_modules: &HashMap<String, String>,
    meta: &mut OwnershipMeta,
    options: OwnershipOptions,
) -> anyhow::Result<String> {
    let codeowners = read_codeowners(project_root)?;
    let mut status = OwnershipStatus {
        codeowners_available: codeowners.is_some(),
        ..OwnershipStatus::default()
    };
    let declared = declared_owners_for_files(codeowners.as_ref(), files);
    let derived = derived_owners_for_files(project_root, files, meta, &options, &mut status);

    let mut by_file = BTreeMap::new();
    for file in files {
        by_file.insert(
            file.clone(),
            FileOwnership {
                declared: declared.get(file).cloned().unwrap_or_default(),
                derived: derived.get(file).cloned().unwrap_or_default(),
            },
        );
    }
    let degraded_sources = degraded_sources(&status, &by_file);
    let mut doc = ownership_frontmatter(status.partial, &degraded_sources);
    doc.push_str("# Code Ownership\n\n");
    if status.partial {
        doc.push_str(
            "> Ownership is partial because blame hit the configured file cap or timeout.\n\n",
        );
    }

    if by_file
        .values()
        .all(|ownership| ownership.declared.is_empty() && ownership.derived.is_empty())
    {
        doc.push_str("Unknown ownership. No CODEOWNERS declarations or git blame contributors were available.\n");
        return Ok(doc);
    }

    write_modules(&mut doc, &by_file, file_modules);
    write_files(&mut doc, &by_file);
    Ok(doc)
}

fn read_codeowners(project_root: &Path) -> anyhow::Result<Option<Codeowners>> {
    for relative in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"] {
        let path = project_root.join(relative);
        match std::fs::read_to_string(&path) {
            Ok(raw) => return Ok(Some(parse_codeowners(&raw))),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(None)
}

fn parse_codeowners(raw: &str) -> Codeowners {
    let entries = raw
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let mut parts = line.split_whitespace();
            let pattern = parts.next()?.to_string();
            let owners = parts
                .take_while(|part| !part.starts_with('#'))
                .map(str::to_string)
                .collect::<Vec<_>>();
            (!owners.is_empty()).then_some(CodeownersEntry { pattern, owners })
        })
        .collect();
    Codeowners { entries }
}

fn declared_owners_for_files(
    codeowners: Option<&Codeowners>,
    files: &[String],
) -> BTreeMap<String, Vec<String>> {
    let mut out = BTreeMap::new();
    let Some(codeowners) = codeowners else {
        return out;
    };
    for file in files {
        if let Some(entry) = codeowners
            .entries
            .iter()
            .rev()
            .find(|entry| codeowners_pattern_matches(&entry.pattern, file))
        {
            out.insert(file.clone(), entry.owners.clone());
        }
    }
    out
}

fn codeowners_pattern_matches(pattern: &str, file: &str) -> bool {
    let normalized = pattern.trim_start_matches('/');
    if normalized.ends_with('/') {
        return file.starts_with(normalized);
    }
    if normalized.contains('*') || normalized.contains('?') || normalized.contains('[') {
        if pattern.starts_with('/') || normalized.contains('/') {
            return match glob::Pattern::new(normalized) {
                Ok(glob) => glob.matches(file),
                Err(error) => {
                    log::warn!(
                        "failed to parse CODEOWNERS pattern {pattern:?} as glob {normalized:?} for file {file:?}: {error}"
                    );
                    false
                }
            };
        }
        return Path::new(file)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| match glob::Pattern::new(normalized) {
                Ok(glob) => glob.matches(name),
                Err(error) => {
                    log::warn!(
                        "failed to parse CODEOWNERS pattern {pattern:?} as basename glob {normalized:?} for file {file:?} name {name:?}: {error}"
                    );
                    false
                }
            });
    }
    if normalized.contains('/') {
        file == normalized || file.starts_with(&format!("{normalized}/"))
    } else {
        Path::new(file).file_name().and_then(|name| name.to_str()) == Some(normalized)
    }
}

fn derived_owners_for_files(
    project_root: &Path,
    files: &[String],
    meta: &mut OwnershipMeta,
    options: &OwnershipOptions,
    status: &mut OwnershipStatus,
) -> BTreeMap<String, Vec<OwnershipContributor>> {
    let Ok(mut repo) = gix::discover(project_root) else {
        return BTreeMap::new();
    };
    repo.object_cache_size_if_unset(4 * 1024 * 1024);
    let Ok(head) = repo.head_id() else {
        return BTreeMap::new();
    };
    let head = head.detach();
    let repo = Arc::new(repo.into_sync());
    status.blame_available = true;

    let started = Instant::now();
    let mut out = BTreeMap::new();
    let mut blame_misses = 0usize;
    for file in files {
        if started.elapsed() >= options.blame_timeout {
            status.partial = true;
            break;
        }
        let Some(content_hash) = content_hash(project_root, file) else {
            continue;
        };
        if let Some(cached) = meta
            .files
            .get(file)
            .filter(|cached| cached.content_hash == content_hash)
        {
            out.insert(file.clone(), cached.contributors.clone());
            continue;
        }
        if blame_misses >= options.blame_file_cap {
            status.partial = true;
            break;
        }
        blame_misses += 1;
        let remaining_timeout = options.blame_timeout.saturating_sub(started.elapsed());
        match blame_file_contributors_with_timeout(Arc::clone(&repo), head, file, remaining_timeout)
        {
            BlameContributorsOutcome::Success(contributors) => {
                meta.files.insert(
                    file.clone(),
                    CachedBlameSummary {
                        content_hash,
                        contributors: contributors.clone(),
                    },
                );
                out.insert(file.clone(), contributors);
            }
            BlameContributorsOutcome::Timeout => {
                status.partial = true;
                break;
            }
            BlameContributorsOutcome::Error(error) => {
                log::debug!("codewiki ownership git blame failed for `{file}`: {error}");
                status.partial = true;
                status.blame_errors = true;
            }
        }
    }
    out
}

fn content_hash(project_root: &Path, file: &str) -> Option<String> {
    hasher::file_content_hash(&project_root.join(file)).ok()
}

fn blame_file_contributors_with_timeout(
    repo: Arc<gix::ThreadSafeRepository>,
    head: gix::ObjectId,
    file: &str,
    timeout: Duration,
) -> BlameContributorsOutcome {
    let file = file.to_string();
    match run_with_timeout(timeout, move |cancelled| {
        if cancelled.load(Ordering::Relaxed) {
            anyhow::bail!("git blame cancelled before start");
        }
        let repo = repo.to_thread_local();
        if cancelled.load(Ordering::Relaxed) {
            anyhow::bail!("git blame cancelled before blame");
        }
        let outcome = blame_file_contributors(&repo, head, &file);
        if cancelled.load(Ordering::Relaxed) {
            anyhow::bail!("git blame cancelled after blame");
        }
        outcome
    }) {
        Some(Ok(contributors)) => BlameContributorsOutcome::Success(contributors),
        Some(Err(error)) => BlameContributorsOutcome::Error(error),
        None => BlameContributorsOutcome::Timeout,
    }
}

fn run_with_timeout<T, F>(timeout: Duration, work: F) -> Option<T>
where
    T: Send + 'static,
    F: FnOnce(Arc<AtomicBool>) -> T + Send + 'static,
{
    let (sender, receiver) = mpsc::channel();
    let cancelled = Arc::new(AtomicBool::new(false));
    let worker_cancelled = Arc::clone(&cancelled);
    let handle = thread::spawn(move || {
        if worker_cancelled.load(Ordering::Relaxed) {
            return;
        }
        let _ = sender.send(work(worker_cancelled));
    });
    match recv_with_timeout(receiver, timeout, &cancelled) {
        WorkerRecv::Completed(value) => {
            let _ = handle.join();
            Some(value)
        }
        WorkerRecv::TimedOut => {
            cancelled.store(true, Ordering::Relaxed);
            // recv_with_timeout has already set `cancelled`, so the worker exits
            // at its next cooperative cancellation check. Rust threads cannot be
            // forcibly killed, so a worker blocked inside a long-running gix
            // blame operation may keep running past this timeout; joining it
            // here could stall ownership reporting indefinitely, which is why
            // the handle is intentionally leaked via mem::forget instead.
            // TODO: restructure blame work into cancellable tasks or a child
            // process so timed-out work can be reaped rather than leaked.
            std::mem::forget(handle);
            None
        }
        WorkerRecv::Disconnected => {
            let _ = handle.join();
            None
        }
    }
}

fn recv_with_timeout<T>(
    receiver: mpsc::Receiver<T>,
    timeout: Duration,
    cancelled: &AtomicBool,
) -> WorkerRecv<T> {
    match receiver.recv_timeout(timeout) {
        Ok(value) => WorkerRecv::Completed(value),
        Err(mpsc::RecvTimeoutError::Timeout) => {
            cancelled.store(true, Ordering::Relaxed);
            WorkerRecv::TimedOut
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => WorkerRecv::Disconnected,
    }
}

fn blame_file_contributors(
    repo: &gix::Repository,
    head: gix::ObjectId,
    file: &str,
) -> anyhow::Result<Vec<OwnershipContributor>> {
    let outcome = repo.blame_file(
        file.as_bytes().as_bstr(),
        head,
        gix::repository::blame_file::Options::default(),
    )?;
    let mut line_counts: BTreeMap<String, (String, Option<String>, usize)> = BTreeMap::new();
    for entry in outcome.entries {
        let commit = repo.find_commit(entry.commit_id)?;
        let author = commit.author()?;
        let name = author.name.to_string();
        let email = (!author.email.is_empty()).then(|| author.email.to_string());
        let contributor_id = contributor_id(&name, email.as_deref());
        let line_count = entry.len.get() as usize;
        match line_counts.entry(contributor_id) {
            Entry::Occupied(mut stored) => {
                let (stored_name, stored_email, lines) = stored.get_mut();
                retain_deterministic_identity(stored_name, stored_email, &name, &email);
                *lines += line_count;
            }
            Entry::Vacant(stored) => {
                stored.insert((name, email, line_count));
            }
        }
    }
    let mut contributors = line_counts
        .into_iter()
        .map(
            |(contributor_id, (name, email, lines))| OwnershipContributor {
                contributor_id,
                name,
                email,
                lines,
            },
        )
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.contributor_id.cmp(&right.contributor_id))
    });
    contributors.truncate(5);
    Ok(contributors)
}

fn contributor_id(name: &str, email: Option<&str>) -> String {
    let identity = email
        .map(str::trim)
        .filter(|email| !email.is_empty())
        .unwrap_or(name)
        .trim()
        .to_ascii_lowercase();
    let digest = Sha256::digest(identity.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn retain_deterministic_identity(
    stored_name: &mut String,
    stored_email: &mut Option<String>,
    name: &str,
    email: &Option<String>,
) {
    if name < stored_name.as_str() {
        *stored_name = name.to_string();
    }
    match (stored_email.as_mut(), email) {
        (Some(stored), Some(next)) if next < stored => *stored = next.clone(),
        (None, Some(next)) => *stored_email = Some(next.clone()),
        _ => {}
    }
}

fn degraded_sources(
    status: &OwnershipStatus,
    by_file: &BTreeMap<String, FileOwnership>,
) -> Vec<String> {
    let mut sources = Vec::new();
    if !status.codeowners_available {
        sources.push("codeowners_unavailable".to_string());
    }
    if !status.blame_available {
        sources.push("git_blame_unavailable".to_string());
    }
    if status.blame_errors {
        sources.push("git_blame_errors".to_string());
    }
    if status.partial {
        sources.push("git_blame_partial".to_string());
    }
    if by_file
        .values()
        .all(|ownership| ownership.declared.is_empty() && ownership.derived.is_empty())
    {
        sources.push("ownership_unknown".to_string());
    }
    sources
}

fn ownership_frontmatter(partial: bool, degraded_sources: &[String]) -> String {
    #[derive(Serialize)]
    struct Frontmatter<'a> {
        title: &'static str,
        #[serde(rename = "type")]
        kind: &'static str,
        provenance: Vec<&'a str>,
        generated_by: &'static str,
        trust: &'static str,
        freshness: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        degraded: Option<bool>,
        #[serde(skip_serializing_if = "Vec::is_empty")]
        degraded_sources: Vec<&'a str>,
        #[serde(skip_serializing_if = "is_false")]
        partial: bool,
    }

    let data = Frontmatter {
        title: "Code Ownership",
        kind: "code_ownership",
        provenance: Vec::new(),
        generated_by: gobby_core::codewiki_contract::GENERATED_BY_CODEWIKI,
        trust: gobby_core::codewiki_contract::TRUST_GENERATED,
        freshness: gobby_core::codewiki_contract::FRESHNESS_INDEXED,
        degraded: (!degraded_sources.is_empty()).then_some(true),
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
        partial,
    };
    let yaml = serde_yaml::to_string(&data).expect("ownership frontmatter is YAML-serializable");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);
    format!("---\n{yaml}---\n\n")
}

fn is_false(value: &bool) -> bool {
    !*value
}

fn write_modules(
    doc: &mut String,
    by_file: &BTreeMap<String, FileOwnership>,
    file_modules: &HashMap<String, String>,
) {
    let mut modules: BTreeMap<String, Vec<(&String, &FileOwnership)>> = BTreeMap::new();
    for (file, ownership) in by_file {
        modules
            .entry(file_modules.get(file).cloned().unwrap_or_default())
            .or_default()
            .push((file, ownership));
    }
    doc.push_str("## Modules\n\n");
    for (module, files) in modules {
        let label = if module.is_empty() {
            "[[code/repo|Repository Overview]]".to_string()
        } else {
            module_wikilink(&module)
        };
        let primary = aggregate_primary(&files);
        let contributors = aggregate_contributors(&files);
        let _ = writeln!(doc, "### Module: {label}\n");
        write_owner_line(doc, "Primary owners", &primary);
        write_contributor_line(doc, &contributors);
        doc.push('\n');
    }
}

fn write_files(doc: &mut String, by_file: &BTreeMap<String, FileOwnership>) {
    doc.push_str("## Files\n\n");
    for (file, ownership) in by_file {
        let _ = writeln!(doc, "### {}\n", file_wikilink(file));
        if ownership.declared.is_empty() && ownership.derived.is_empty() {
            doc.push_str("Unknown ownership.\n\n");
            continue;
        }
        write_owner_line(doc, "Declared owners", &ownership.declared);
        write_contributor_line(doc, &ownership.derived);
        doc.push('\n');
    }
}

fn aggregate_primary(files: &[(&String, &FileOwnership)]) -> Vec<String> {
    let mut owners = BTreeSet::new();
    for (_, ownership) in files {
        if !ownership.declared.is_empty() {
            owners.extend(ownership.declared.iter().cloned());
        } else if let Some(top) = ownership.derived.first() {
            owners.insert(top.name.clone());
        }
    }
    owners.into_iter().collect()
}

fn aggregate_contributors(files: &[(&String, &FileOwnership)]) -> Vec<OwnershipContributor> {
    let mut counts: BTreeMap<String, (String, Option<String>, usize)> = BTreeMap::new();
    for (_, ownership) in files {
        for contributor in &ownership.derived {
            counts
                .entry(contributor.contributor_id.clone())
                .and_modify(|(stored_name, stored_email, lines)| {
                    retain_deterministic_identity(
                        stored_name,
                        stored_email,
                        &contributor.name,
                        &contributor.email,
                    );
                    *lines += contributor.lines;
                })
                .or_insert_with(|| {
                    (
                        contributor.name.clone(),
                        contributor.email.clone(),
                        contributor.lines,
                    )
                });
        }
    }
    let mut contributors = counts
        .into_iter()
        .map(
            |(contributor_id, (name, email, lines))| OwnershipContributor {
                contributor_id,
                name,
                email,
                lines,
            },
        )
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.contributor_id.cmp(&right.contributor_id))
    });
    contributors.truncate(5);
    contributors
}

fn write_owner_line(doc: &mut String, label: &str, owners: &[String]) {
    if owners.is_empty() {
        let _ = writeln!(doc, "{label}: unknown");
    } else {
        let _ = writeln!(doc, "{label}: {}", owners.join(", "));
    }
}

fn write_contributor_line(doc: &mut String, contributors: &[OwnershipContributor]) {
    if contributors.is_empty() {
        doc.push_str("Top contributors: unknown\n");
        return;
    }
    let rendered = contributors
        .iter()
        .map(|contributor| {
            format!(
                "{} ({} {})",
                contributor.name,
                contributor.lines,
                if contributor.lines == 1 {
                    "line"
                } else {
                    "lines"
                }
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    let _ = writeln!(doc, "Top contributors: {rendered}");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::path::Path;
    use std::process::Command;
    use std::time::Duration;

    #[test]
    fn codewiki_ownership_codeowners_only_maps_declared_owners() {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src/api")).expect("create src/api");
        std::fs::write(project.path().join("CODEOWNERS"), "/src/api/ @platform\n")
            .expect("write CODEOWNERS");
        std::fs::write(project.path().join("src/api/mod.rs"), "pub fn api() {}\n")
            .expect("write source");

        let mut meta = OwnershipMeta::default();
        let doc = build_ownership_doc(
            project.path(),
            &["src/api/mod.rs".to_string()],
            &modules([("src/api/mod.rs", "src/api")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 10,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("type: code_ownership"));
        assert!(doc.contains("degraded: true"));
        assert!(doc.contains("git_blame_unavailable"));
        assert!(doc.contains("Module: [[code/modules/src/api|src/api]]"));
        assert!(doc.contains("Declared owners: @platform"));
        assert!(doc.contains("src/api/mod.rs"));
    }

    #[test]
    fn codewiki_ownership_derives_top_committers_from_gix_blame() {
        let project = git_project_with_history();
        let mut meta = OwnershipMeta::default();

        let doc = build_ownership_doc(
            project.path(),
            &["src/lib.rs".to_string()],
            &modules([("src/lib.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 10,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("Top contributors: Alice"));
        assert!(doc.contains("Bob"));
        assert!(!doc.contains("git_blame_unavailable"));
        assert_eq!(meta.files.len(), 1);
        let serialized = serde_json::to_string(&meta).expect("serialize ownership meta");
        assert!(serialized.contains("contributor_id"));
        assert!(!serialized.contains("alice@example.com"));
        assert!(!serialized.contains("bob@example.com"));
    }

    #[test]
    fn codewiki_ownership_requires_cached_contributor_ids() {
        let error = serde_json::from_str::<OwnershipMeta>(
            r#"{
"files": {
"src/lib.rs": {
                  "content_hash": "hash",
                  "contributors": [
                    {"name": "Alice", "email": "alice@example.test", "lines": 4},
                    {"name": "Bob", "email": "bob@example.test", "lines": 2}
                  ]
                }
}
}"#,
        )
        .expect_err("legacy ownership meta without contributor_id is rejected");

        assert!(error.to_string().contains("missing field `contributor_id`"));
    }

    #[test]
    fn codewiki_ownership_declared_owners_take_primary_precedence() {
        let project = git_project_with_history();
        std::fs::write(project.path().join("CODEOWNERS"), "src/lib.rs @declared\n")
            .expect("write CODEOWNERS");
        let mut meta = OwnershipMeta::default();

        let doc = build_ownership_doc(
            project.path(),
            &["src/lib.rs".to_string()],
            &modules([("src/lib.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 10,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("Declared owners: @declared"));
        assert!(doc.contains("Top contributors: Alice"));
        assert!(!doc.contains("Primary owners: Alice"));
    }

    #[test]
    fn codewiki_ownership_without_sources_degrades_to_unknown() {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        std::fs::write(project.path().join("src/lib.rs"), "pub fn lib() {}\n")
            .expect("write source");
        let mut meta = OwnershipMeta::default();

        let doc = build_ownership_doc(
            project.path(),
            &["src/lib.rs".to_string()],
            &modules([("src/lib.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 10,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("degraded: true"));
        assert!(doc.contains("codeowners_unavailable"));
        assert!(doc.contains("git_blame_unavailable"));
        assert!(doc.contains("Unknown ownership"));
    }

    #[test]
    fn codewiki_ownership_file_cap_marks_partial() {
        let project = git_project_with_two_files();
        let mut meta = OwnershipMeta::default();

        let doc = build_ownership_doc(
            project.path(),
            &["src/a.rs".to_string(), "src/b.rs".to_string()],
            &modules([("src/a.rs", "src"), ("src/b.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 1,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("partial: true"));
        assert!(doc.contains("Ownership is partial"));
        assert_eq!(meta.files.len(), 1);
    }

    #[test]
    fn codewiki_ownership_file_cap_counts_only_cache_misses() {
        let project = git_project_with_two_files();
        let cached_hash = content_hash(project.path(), "src/a.rs").expect("hash a");
        let cached_contributor = OwnershipContributor {
            contributor_id: "cached".to_string(),
            name: "Cached Owner".to_string(),
            email: None,
            lines: 1,
        };
        let mut meta = OwnershipMeta {
            files: BTreeMap::from([(
                "src/a.rs".to_string(),
                CachedBlameSummary {
                    content_hash: cached_hash,
                    contributors: vec![cached_contributor],
                },
            )]),
        };

        let doc = build_ownership_doc(
            project.path(),
            &["src/a.rs".to_string(), "src/b.rs".to_string()],
            &modules([("src/a.rs", "src"), ("src/b.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 1,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(!doc.contains("partial: true"));
        assert!(!doc.contains("Ownership is partial"));
        assert!(meta.files.contains_key("src/a.rs"));
        assert!(meta.files.contains_key("src/b.rs"));
    }

    #[test]
    fn codewiki_ownership_blame_error_marks_partial_without_caching() {
        let project = git_project_with_history();
        std::fs::write(
            project.path().join("src/untracked.rs"),
            "pub fn untracked() {}\n",
        )
        .expect("write untracked source");
        let mut meta = OwnershipMeta::default();

        let doc = build_ownership_doc(
            project.path(),
            &["src/untracked.rs".to_string()],
            &modules([("src/untracked.rs", "src")]),
            &mut meta,
            OwnershipOptions {
                blame_file_cap: 10,
                blame_timeout: Duration::from_secs(10),
            },
        )
        .expect("ownership doc");

        assert!(doc.contains("partial: true"));
        assert!(doc.contains("git_blame_errors"));
        assert!(meta.files.is_empty());
    }

    #[test]
    fn codewiki_ownership_recv_timeout_returns_none() {
        let (_sender, receiver) = mpsc::channel::<usize>();
        let cancelled = AtomicBool::new(false);
        let result = recv_with_timeout(receiver, Duration::from_millis(1), &cancelled);

        assert!(matches!(result, WorkerRecv::TimedOut));
        assert!(cancelled.load(Ordering::Relaxed));
    }

    #[test]
    fn codewiki_ownership_run_with_timeout_joins_completed_worker() {
        let result = run_with_timeout(Duration::from_secs(1), |_| 42);

        assert_eq!(result, Some(42));
    }

    fn modules<const N: usize>(items: [(&str, &str); N]) -> HashMap<String, String> {
        items
            .into_iter()
            .map(|(file, module)| (file.to_string(), module.to_string()))
            .collect()
    }

    fn git_project_with_history() -> tempfile::TempDir {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        git(project.path(), &["init"]);
        std::fs::write(
            project.path().join("src/lib.rs"),
            "pub fn one() {}\npub fn two() {}\n",
        )
        .expect("write source");
        git(project.path(), &["add", "src/lib.rs"]);
        git_author(project.path(), "Alice", "alice@example.test", "initial");
        std::fs::write(
            project.path().join("src/lib.rs"),
            "pub fn one() {}\npub fn two_changed() {}\n",
        )
        .expect("rewrite source");
        git(project.path(), &["add", "src/lib.rs"]);
        git_author(project.path(), "Bob", "bob@example.test", "update");
        project
    }

    fn git_project_with_two_files() -> tempfile::TempDir {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src")).expect("create src");
        git(project.path(), &["init"]);
        std::fs::write(project.path().join("src/a.rs"), "pub fn a() {}\n").expect("write a");
        std::fs::write(project.path().join("src/b.rs"), "pub fn b() {}\n").expect("write b");
        git(project.path(), &["add", "src/a.rs", "src/b.rs"]);
        git_author(project.path(), "Alice", "alice@example.test", "initial");
        project
    }

    fn git_author(repo: &Path, name: &str, email: &str, message: &str) {
        let status = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args([
                "-c",
                &format!("user.name={name}"),
                "-c",
                &format!("user.email={email}"),
                "commit",
                "-m",
                message,
            ])
            .status()
            .expect("run git commit");
        assert!(status.success(), "git commit failed");
    }

    fn git(repo: &Path, args: &[&str]) {
        let status = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(args)
            .status()
            .expect("run git");
        assert!(status.success(), "git {args:?} failed");
    }
}
