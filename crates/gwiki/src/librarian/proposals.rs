use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::json;

use crate::{WikiError, health};

use super::semantic::{LINK_CLUSTER_MIN_MENTIONS, NEAR_DUPLICATE_COSINE, SemanticGapScan};
use super::{
    LibrarianArtifacts, ProposalsReport, SuggestedPatchDiff, SuggestedTask, render_text,
    unique_paths,
};

const LIBRARIAN_DIR: &str = "meta/librarian";

pub(super) fn suggested_tasks(
    uncited_sources: &[health::HealthSourceIssue],
    stale_pages: &[PathBuf],
    missing_citations: &[PathBuf],
    broken_links: &[PathBuf],
    weak_provenance: &[PathBuf],
    semantic: &SemanticGapScan,
) -> Vec<SuggestedTask> {
    let mut tasks = Vec::new();
    push_task(
        &mut tasks,
        !stale_pages.is_empty(),
        "Refresh stale wiki pages",
        "Review stale pages and refresh source support before accepting canonical edits.",
        stale_pages,
    );
    let source_ids = uncited_sources
        .iter()
        .map(|source| source.source_id.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    push_task(
        &mut tasks,
        !missing_citations.is_empty() || !source_ids.is_empty(),
        "Add missing citations for unsupported claims",
        &format!("Unsupported claims need citation review. Uncited sources: {source_ids}"),
        missing_citations,
    );
    push_task(
        &mut tasks,
        !broken_links.is_empty(),
        "Repair broken wiki links",
        "Genuinely dead links (purged digest targets, path-shaped targets, or entity \
         mentions no digest sustains) should be retargeted or removed after human \
         review. Pending entity mentions that upkeep converges on are excluded.",
        broken_links,
    );
    push_task(
        &mut tasks,
        !weak_provenance.is_empty(),
        "Strengthen weak provenance",
        "Attach source-to-section provenance before relying on these pages.",
        weak_provenance,
    );
    let near_duplicate_pairs = semantic
        .near_duplicates
        .iter()
        .map(|pair| {
            format!(
                "{} ~ {} ({:.2})",
                pair.left.display(),
                pair.right.display(),
                pair.score
            )
        })
        .collect::<Vec<_>>()
        .join("; ");
    push_task(
        &mut tasks,
        !semantic.near_duplicates.is_empty(),
        "Merge or disambiguate near-duplicate pages",
        &format!(
            "Knowledge page pairs with cosine similarity >= {NEAR_DUPLICATE_COSINE}: {near_duplicate_pairs}"
        ),
        &unique_paths(
            semantic
                .near_duplicates
                .iter()
                .flat_map(|pair| [pair.left.clone(), pair.right.clone()]),
        ),
    );
    let cluster_summary = semantic
        .unresolved_clusters
        .iter()
        .map(|cluster| format!("{} ({} mentions)", cluster.target, cluster.mentions))
        .collect::<Vec<_>>()
        .join(", ");
    push_task(
        &mut tasks,
        !semantic.unresolved_clusters.is_empty(),
        "Create pages for repeatedly mentioned link targets",
        &format!(
            "Unresolved link targets mentioned at least {LINK_CLUSTER_MIN_MENTIONS} times with no page behind them: {cluster_summary}"
        ),
        &semantic
            .unresolved_clusters
            .iter()
            .map(|cluster| PathBuf::from(&cluster.target))
            .collect::<Vec<_>>(),
    );
    tasks
}

pub(super) fn push_task(
    tasks: &mut Vec<SuggestedTask>,
    include: bool,
    title: &str,
    description: &str,
    paths: &[PathBuf],
) {
    if include {
        tasks.push(SuggestedTask {
            title: title.to_string(),
            description: description.to_string(),
            paths: paths.to_vec(),
        });
    }
}

pub(super) fn suggested_patch_diffs(
    stale_pages: &[PathBuf],
    missing_citations: &[PathBuf],
) -> Vec<SuggestedPatchDiff> {
    unique_paths(stale_pages.iter().chain(missing_citations).cloned())
        .into_iter()
        .map(|path| SuggestedPatchDiff {
            path: path.clone(),
            summary: "Add citation refresh notes after human acceptance".to_string(),
            diff: format!(
                "--- a/{0}\n+++ b/{0}\n@@\n+<!-- librarian proposal: refresh citations and stale claims before accepting -->\n",
                path.display()
            ),
            applies_to_canonical_content: true,
            requires_acceptance: true,
        })
        .collect()
}

pub(super) fn artifacts() -> LibrarianArtifacts {
    LibrarianArtifacts {
        proposals_json: PathBuf::from("meta/librarian/proposals.json"),
        proposals_markdown: PathBuf::from("meta/librarian/proposals.md"),
        audit_annotations_json: PathBuf::from("meta/librarian/audit-annotations.json"),
        stale_pages_json: PathBuf::from("meta/librarian/stale-pages.json"),
    }
}

pub(super) fn persist_report(vault_root: &Path, report: &ProposalsReport) -> Result<(), WikiError> {
    let dir = vault_root.join(LIBRARIAN_DIR);
    std::fs::create_dir_all(&dir).map_err(|source| WikiError::Io {
        action: "create librarian metadata directory",
        path: Some(dir.clone()),
        source,
    })?;
    write_json(vault_root, &report.artifacts.proposals_json, report)?;
    write_text(
        vault_root,
        &report.artifacts.proposals_markdown,
        &render_text(report),
    )?;
    write_json(
        vault_root,
        &report.artifacts.audit_annotations_json,
        &json!({
            "missing_citations": report.checks.iter().find(|check| check.name == "missing_citations"),
            "weak_provenance": report.checks.iter().find(|check| check.name == "weak_provenance"),
        }),
    )?;
    write_json(
        vault_root,
        &report.artifacts.stale_pages_json,
        &json!({
            "stale_pages": report.checks.iter().find(|check| check.name == "stale_pages"),
        }),
    )
}

pub(super) fn write_json<T: Serialize>(
    vault_root: &Path,
    relative: &Path,
    value: &T,
) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    let bytes = serde_json::to_vec_pretty(value).map_err(|source| WikiError::Json {
        action: "serialize librarian metadata",
        path: Some(path.clone()),
        source,
    })?;
    std::fs::write(&path, bytes).map_err(|source| WikiError::Io {
        action: "write librarian metadata",
        path: Some(path),
        source,
    })
}

pub(super) fn write_text(vault_root: &Path, relative: &Path, text: &str) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    std::fs::write(&path, text).map_err(|source| WikiError::Io {
        action: "write librarian metadata",
        path: Some(path),
        source,
    })
}
