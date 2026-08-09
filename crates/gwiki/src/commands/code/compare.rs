use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use anyhow::Context as _;
use serde::Serialize;

use super::CODEWIKI_META_PATH;
use super::runtime as output;
use super::types::{CodewikiDocMeta, CodewikiMeta};
use crate::commands::code::runtime::normalize_storage_path_str;

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct CodewikiCommitMetadata {
    commit: Option<String>,
    dirty: Option<bool>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct CodewikiCompareDoc {
    path: String,
    commit: Option<String>,
    dirty: Option<bool>,
    source_hashes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct CodewikiChangedDoc {
    path: String,
    base: CodewikiDocState,
    current: CodewikiDocState,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
struct CodewikiDocState {
    commit: Option<String>,
    dirty: Option<bool>,
    source_hashes: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct CodewikiCompareSummary {
    base: CodewikiCommitMetadata,
    current: CodewikiCommitMetadata,
    added: Vec<CodewikiCompareDoc>,
    removed: Vec<CodewikiCompareDoc>,
    changed: Vec<CodewikiChangedDoc>,
}

pub(crate) fn compare_summary_text(summary: &CodewikiCompareSummary) -> String {
    let mut lines = vec![format!(
        "CodeWiki compare: {} added, {} removed, {} changed",
        summary.added.len(),
        summary.removed.len(),
        summary.changed.len()
    )];
    lines.extend(summary.added.iter().map(|doc| format!("+ {}", doc.path)));
    lines.extend(summary.removed.iter().map(|doc| format!("- {}", doc.path)));
    lines.extend(summary.changed.iter().map(|doc| format!("~ {}", doc.path)));
    lines.join("\n")
}

pub(crate) fn compare_to(
    project_root: &Path,
    out: Option<&str>,
    compare_target: &str,
) -> anyhow::Result<CodewikiCompareSummary> {
    let paths = compare_paths(project_root, out)?;
    let target = parse_compare_target(compare_target, &paths.default_git_meta)?;
    let current = read_current_meta(&paths.current_meta)?;
    let resolved_ref = resolve_commit(project_root, target.base_ref)?;
    let baseline = read_baseline_meta(
        project_root,
        target.base_ref,
        &resolved_ref,
        &target.git_meta,
    )?;

    let all_paths = baseline
        .docs
        .keys()
        .chain(current.docs.keys())
        .cloned()
        .collect::<BTreeSet<_>>();
    let mut added = Vec::new();
    let mut removed = Vec::new();
    let mut changed = Vec::new();

    for path in all_paths {
        match (baseline.docs.get(&path), current.docs.get(&path)) {
            (None, Some(doc)) => added.push(compare_doc(path, doc)),
            (Some(doc), None) => removed.push(compare_doc(path, doc)),
            (Some(base_doc), Some(current_doc)) => {
                let base = doc_state(base_doc);
                let current = doc_state(current_doc);
                if base != current {
                    changed.push(CodewikiChangedDoc {
                        path,
                        base,
                        current,
                    });
                }
            }
            (None, None) => unreachable!("path came from baseline or current metadata"),
        }
    }

    Ok(CodewikiCompareSummary {
        base: commit_metadata(&baseline),
        current: commit_metadata(&current),
        added,
        removed,
        changed,
    })
}

struct ComparePaths {
    current_meta: PathBuf,
    default_git_meta: String,
}

struct CompareTarget<'a> {
    base_ref: &'a str,
    git_meta: String,
}

fn compare_paths(project_root: &Path, out: Option<&str>) -> anyhow::Result<ComparePaths> {
    let out = output::resolve_output_path(project_root, out);
    let relative = out.strip_prefix(project_root).map_err(|_| {
        anyhow::anyhow!("codewiki --compare-to requires --out to be inside the source repository")
    })?;
    let mut normalized = PathBuf::new();
    for component in relative.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                anyhow::bail!(
                    "codewiki --compare-to requires --out to be inside the source repository"
                );
            }
        }
    }
    let relative_meta = normalized.join(CODEWIKI_META_PATH);
    let git_meta = normalize_storage_path_str(
        relative_meta
            .to_str()
            .context("codewiki --compare-to metadata path is not valid UTF-8")?,
    );
    Ok(ComparePaths {
        current_meta: out.join(CODEWIKI_META_PATH),
        default_git_meta: git_meta,
    })
}

fn parse_compare_target<'a>(
    target: &'a str,
    default_git_meta: &str,
) -> anyhow::Result<CompareTarget<'a>> {
    let (base_ref, git_meta) = match target.split_once(':') {
        Some((base_ref, explicit_git_meta)) => {
            (base_ref, normalize_explicit_git_meta(explicit_git_meta)?)
        }
        None => (target, default_git_meta.to_string()),
    };
    if base_ref.is_empty() {
        anyhow::bail!("codewiki compare ref '' does not resolve to a commit");
    }
    Ok(CompareTarget { base_ref, git_meta })
}

fn normalize_explicit_git_meta(path: &str) -> anyhow::Result<String> {
    // Deliberately unconditional (not normalize_storage_path_str): this is a
    // user-typed compare target, so Windows-style separators must be honored
    // on every platform and `..\` must always read as traversal.
    let normalized_separators = path.replace('\\', "/");
    let mut normalized = PathBuf::new();
    for component in Path::new(&normalized_separators).components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                anyhow::bail!(
                    "codewiki --compare-to metadata path must be repository-relative: '{path}'"
                );
            }
        }
    }
    if normalized.as_os_str().is_empty() {
        anyhow::bail!("codewiki --compare-to metadata path must be repository-relative: '{path}'");
    }
    Ok(normalized
        .to_str()
        .context("codewiki --compare-to metadata path is not valid UTF-8")?
        .to_string())
}

fn resolve_commit(project_root: &Path, base_ref: &str) -> anyhow::Result<String> {
    let commit_ref = format!("{base_ref}^{{commit}}");
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(project_root)
        .args(["rev-parse", "--verify", "--end-of-options", &commit_ref])
        .output()
        .with_context(|| {
            format!("failed to run git rev-parse for codewiki --compare-to '{base_ref}'")
        })?;
    if !output.status.success() {
        anyhow::bail!("codewiki compare ref '{base_ref}' does not resolve to a commit");
    }
    let commit = String::from_utf8(output.stdout)
        .context("git rev-parse returned a non-UTF-8 commit ID")?
        .trim()
        .to_string();
    if commit.is_empty() {
        anyhow::bail!("codewiki compare ref '{base_ref}' does not resolve to a commit");
    }
    Ok(commit)
}

fn read_current_meta(path: &Path) -> anyhow::Result<CodewikiMeta> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            anyhow::bail!(
                "codewiki compare current metadata is absent at {}",
                path.display()
            );
        }
        Err(err) => {
            return Err(err).with_context(|| {
                format!(
                    "failed to read codewiki compare current metadata at {}",
                    path.display()
                )
            });
        }
    };
    serde_json::from_str(&raw).map_err(|err| {
        anyhow::anyhow!(
            "codewiki compare current metadata is malformed at {}: {err}",
            path.display()
        )
    })
}

fn read_baseline_meta(
    project_root: &Path,
    base_ref: &str,
    resolved_ref: &str,
    git_meta: &str,
) -> anyhow::Result<CodewikiMeta> {
    let object = format!("{resolved_ref}:{git_meta}");
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(project_root)
        .args(["show", &object])
        .output()
        .with_context(|| {
            format!("failed to run git show for codewiki --compare-to '{base_ref}'")
        })?;
    if !output.status.success() {
        anyhow::bail!(
            "codewiki compare baseline metadata is absent at ref '{base_ref}': {git_meta}"
        );
    }
    let raw = String::from_utf8(output.stdout).map_err(|err| {
        anyhow::anyhow!(
            "codewiki compare baseline metadata is malformed at ref '{base_ref}': \
             {git_meta}: {err}"
        )
    })?;
    serde_json::from_str(&raw).map_err(|err| {
        anyhow::anyhow!(
            "codewiki compare baseline metadata is malformed at ref '{base_ref}': \
             {git_meta}: {err}"
        )
    })
}

fn commit_metadata(meta: &CodewikiMeta) -> CodewikiCommitMetadata {
    CodewikiCommitMetadata {
        commit: meta.commit.clone(),
        dirty: meta.commit_dirty,
    }
}

fn compare_doc(path: String, doc: &CodewikiDocMeta) -> CodewikiCompareDoc {
    let state = doc_state(doc);
    CodewikiCompareDoc {
        path,
        commit: state.commit,
        dirty: state.dirty,
        source_hashes: state.source_hashes,
    }
}

fn doc_state(doc: &CodewikiDocMeta) -> CodewikiDocState {
    CodewikiDocState {
        commit: doc.commit.clone(),
        dirty: doc.commit_dirty,
        source_hashes: doc.source_hashes.clone(),
    }
}
