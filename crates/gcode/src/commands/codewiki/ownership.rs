use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write;
use std::path::Path;
use std::time::{Duration, Instant};

use gix::bstr::ByteSlice;
use serde::{Deserialize, Serialize};

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
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    pub lines: usize,
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
    status.blame_available = true;

    let started = Instant::now();
    let mut out = BTreeMap::new();
    for (index, file) in files.iter().enumerate() {
        if index >= options.blame_file_cap || started.elapsed() >= options.blame_timeout {
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
        let contributors = blame_file_contributors(&repo, head.detach(), file).unwrap_or_default();
        meta.files.insert(
            file.clone(),
            CachedBlameSummary {
                content_hash,
                contributors: contributors.clone(),
            },
        );
        out.insert(file.clone(), contributors);
    }
    out
}

fn content_hash(project_root: &Path, file: &str) -> Option<String> {
    hasher::file_content_hash(&project_root.join(file)).ok()
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
    let mut line_counts: BTreeMap<(String, Option<String>), usize> = BTreeMap::new();
    for entry in outcome.entries {
        let commit = repo.find_commit(entry.commit_id)?;
        let author = commit.author()?;
        let name = author.name.to_string();
        let email = (!author.email.is_empty()).then(|| author.email.to_string());
        *line_counts.entry((name, email)).or_default() += entry.len.get() as usize;
    }
    let mut contributors = line_counts
        .into_iter()
        .map(|((name, email), lines)| OwnershipContributor { name, email, lines })
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.email.cmp(&right.email))
    });
    contributors.truncate(5);
    Ok(contributors)
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
        source: Vec<&'a str>,
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
        source: Vec::new(),
        provenance: Vec::new(),
        generated_by: "gcode-codewiki",
        trust: "generated",
        freshness: "indexed",
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
    let mut counts: BTreeMap<(String, Option<String>), usize> = BTreeMap::new();
    for (_, ownership) in files {
        for contributor in &ownership.derived {
            *counts
                .entry((contributor.name.clone(), contributor.email.clone()))
                .or_default() += contributor.lines;
        }
    }
    let mut contributors = counts
        .into_iter()
        .map(|((name, email), lines)| OwnershipContributor { name, email, lines })
        .collect::<Vec<_>>();
    contributors.sort_by(|left, right| {
        right
            .lines
            .cmp(&left.lines)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.email.cmp(&right.email))
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
