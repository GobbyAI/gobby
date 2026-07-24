//! `gwiki upkeep` conductor: drain pending manifest sources into entity
//! concept pages.
//!
//! The conductor clusters case-folded unresolved wikilink targets mentioned by
//! pending source digests, then synthesizes one concept page per cluster
//! through the regular compile pipeline (`target_kind = concept`, observed
//! case variants as `aliases`, an `entity` tag). Update-over-create is layered:
//! mentions of an already-covered entity never cluster at all, because
//! case-insensitive link resolution (stem/title/alias) binds them to the
//! existing page upstream; an existing page whose key still matches a cluster
//! is recompiled in place as defense-in-depth; otherwise a semantic
//! near-duplicate at or above `NEAR_DUPLICATE_UPDATE_COSINE` is updated, the
//! review band creates a new page flagged for review, and a missing backend
//! skips the check with a note.
//! The deterministic tail runs even without AI: catalog regeneration, an
//! `upkeep_completed` log entry, a compile-status reconcile for reviewed
//! sources that formed no cluster, and a run report under
//! [`REPORT_RELATIVE_PATH`].

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::explainer::ExplainerGenerator;
use crate::links::{canonical_target_key, concept_rejection_reason, is_entity_key};
use crate::lint::page_match_keys;
use crate::search::SearchScope;
use crate::search::semantic::SemanticSearchBackend;
use crate::session::ResearchScope;
use crate::{ScopeIdentity, WikiError, lint};

mod reserved_pages;
mod runner;

/// Default budget: concept pages synthesized per run.
pub(crate) const DEFAULT_MAX_PAGES: usize = 10;
/// Default minimum digest mentions before an unresolved target forms a cluster.
pub(crate) const DEFAULT_MIN_MENTIONS: usize = 2;
/// Default budget: accepted sources compiled into one concept page.
pub(crate) const DEFAULT_MAX_SOURCES_PER_PAGE: usize = 12;
/// Vault-relative path of the run report written by non-dry runs.
pub(crate) const REPORT_RELATIVE_PATH: &str = "meta/upkeep/last-run.json";
/// Default days a page stays `stale` before upkeep archives it. A starting
/// point until Part A loop distributions tune the threshold (strategy §3.4).
pub(crate) const DEFAULT_ARCHIVE_AFTER_DAYS: u64 = 45;

/// Budgets and toggles for one upkeep run.
#[derive(Debug, Clone)]
pub struct Options {
    pub max_pages: usize,
    pub min_mentions: usize,
    pub max_sources_per_page: usize,
    pub dry_run: bool,
    pub daemon_synthesis_available: bool,
    /// Set when a tool-loop generator is driving synthesis: a generation failure
    /// then fails the cluster instead of writing a skeleton page.
    pub hard_fail_on_generation_failure: bool,
    /// Days a page stays `stale` (per its `stale_at` demotion timestamp)
    /// before upkeep archives it.
    pub archive_after_days: u64,
    /// Optional wall-clock budget for the whole upkeep operation.
    pub time_budget_seconds: Option<u64>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            max_pages: DEFAULT_MAX_PAGES,
            min_mentions: DEFAULT_MIN_MENTIONS,
            max_sources_per_page: DEFAULT_MAX_SOURCES_PER_PAGE,
            dry_run: false,
            daemon_synthesis_available: false,
            hard_fail_on_generation_failure: false,
            archive_after_days: DEFAULT_ARCHIVE_AFTER_DAYS,
            time_budget_seconds: None,
        }
    }
}

/// Live semantic search access for the near-duplicate layer.
pub struct SemanticProbe<'a> {
    pub backend: &'a mut dyn SemanticSearchBackend,
    pub search_scope: SearchScope,
}

/// One unresolved-target cluster and what upkeep did with it.
#[derive(Debug, Clone, Serialize)]
pub struct ClusterOutcome {
    /// Primary observed variant (most mentions, ties broken lexically).
    pub target: String,
    /// Case-folded cluster key.
    pub key: String,
    pub mentions: usize,
    /// Observed case variants, most-mentioned first.
    pub variants: Vec<String>,
    /// Pending source ids compiled into the page, selection-ordered.
    pub source_ids: Vec<String>,
    /// Sources dropped by the per-page budget.
    pub sources_truncated: usize,
    /// `created` / `updated` / `failed`, or `planned_create` /
    /// `planned_update` on dry runs.
    pub action: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub page_path: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub near_duplicate: Option<NearDuplicateMatch>,
    /// Set when the near-duplicate score landed in the create-and-review band.
    pub review_flag: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct NearDuplicateMatch {
    pub page: PathBuf,
    pub score: f64,
}

/// A candidate cluster left unprocessed by the `max_pages` budget.
#[derive(Debug, Clone, Serialize)]
pub struct SkippedCluster {
    pub target: String,
    pub mentions: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct UpkeepReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub timestamp: String,
    pub dry_run: bool,
    pub max_pages: usize,
    pub min_mentions: usize,
    pub max_sources_per_page: usize,
    pub pending_before: usize,
    pub pending_after: usize,
    pub pages_created: usize,
    pub pages_updated: usize,
    pub failures: usize,
    pub clusters: Vec<ClusterOutcome>,
    pub budget_exhausted: bool,
    /// Candidate clusters deferred by the wall-clock budget.
    pub deferred_clusters: Vec<SkippedCluster>,
    /// Candidate clusters beyond the page budget; their sources stay pending.
    pub skipped_over_budget: Vec<SkippedCluster>,
    /// Pending sources reviewed without joining any cluster, flipped to
    /// `compiled` so the queue drains.
    pub reconciled_no_synthesis: Vec<String>,
    /// Long-stale pages archived this run (would-be archives on dry runs).
    pub archived_pages: Vec<PathBuf>,
    /// Concept pages rejected by deterministic key-quality rules. Dry runs
    /// report the same entries without applying lifecycle transitions.
    pub unworthy_archived: Vec<UnworthyConceptArchive>,
    /// Quarantined candidates promoted this run: corroborated by at least
    /// [`CANDIDATE_PROMOTION_BACKLINKS`] other knowledge pages (#17727).
    pub candidates_promoted: Vec<PathBuf>,
    /// Quarantined candidates discarded this run: no page links to them
    /// anymore, so they were archived (#17727).
    pub candidates_discarded: Vec<PathBuf>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UnworthyConceptArchive {
    pub page: PathBuf,
    pub key: String,
    pub reason: String,
}

pub fn run(
    research_scope: ResearchScope,
    scope: ScopeIdentity,
    options: &Options,
    semantic: Option<SemanticProbe<'_>>,
    generator: Option<ExplainerGenerator<'_>>,
    timestamp: &str,
) -> Result<UpkeepReport, WikiError> {
    runner::run_with_clock(
        research_scope,
        scope,
        options,
        semantic,
        generator,
        timestamp,
        std::time::Instant::now,
    )
}

fn archive_long_stale_pages(
    vault_root: &Path,
    scope: &ScopeIdentity,
    options: &Options,
) -> Result<Vec<PathBuf>, WikiError> {
    use crate::frontmatter::WikiLifecycle;

    let now = chrono::Utc::now();
    let max_age =
        chrono::Duration::days(i64::try_from(options.archive_after_days).unwrap_or(i64::MAX));
    let mut archived = Vec::new();
    for page in lint::collect_pages(vault_root)? {
        let frontmatter = &page.parsed.frontmatter;
        if frontmatter.lifecycle != Some(WikiLifecycle::Stale) {
            continue;
        }
        let Some(stale_at) = frontmatter
            .unknown
            .get("stale_at")
            .and_then(serde_json::Value::as_str)
            .and_then(|value| chrono::DateTime::parse_from_rfc3339(value).ok())
        else {
            continue;
        };
        let age = now.signed_duration_since(stale_at.with_timezone(&chrono::Utc));
        if age < max_age {
            continue;
        }
        if !options.dry_run {
            crate::lifecycle::apply_lifecycle_transition(
                vault_root,
                scope,
                &page.relative_path,
                WikiLifecycle::Archived,
                &format!("upkeep: stale for {} days", age.num_days()),
            )?;
        }
        archived.push(page.relative_path.clone());
    }
    archived.sort();
    Ok(archived)
}

fn find_unworthy_concepts(vault_root: &Path) -> Result<Vec<UnworthyConceptArchive>, WikiError> {
    use crate::frontmatter::WikiLifecycle;

    let mut archived = Vec::new();
    for page in lint::collect_pages(vault_root)? {
        if !page
            .relative_path
            .starts_with(Path::new("knowledge/concepts"))
            || page.parsed.frontmatter.lifecycle == Some(WikiLifecycle::Archived)
        {
            continue;
        }
        // Catalog-generated folder contexts share `_context.md` with the
        // structural-artifact key. Their `generated_by` stamp distinguishes
        // them from a synthesized junk concept at the same path.
        if page
            .relative_path
            .file_name()
            .and_then(|name| name.to_str())
            == Some("_context.md")
        {
            let generated_by_catalog =
                page.parsed.frontmatter.generated_by.as_deref() == Some("gwiki-catalog");
            if page.parsed.frontmatter.title.is_none() || generated_by_catalog {
                continue;
            }
        }

        let rejected = page_match_keys(&page)
            .into_iter()
            .filter(|key| is_entity_key(key))
            .map(|key| concept_rejection_reason(&key).map(|reason| (key, reason.to_string())))
            .collect::<Option<Vec<_>>>();
        let Some(rejected) = rejected else {
            continue;
        };
        let Some((key, reason)) = rejected.into_iter().next() else {
            continue;
        };
        archived.push(UnworthyConceptArchive {
            page: page.relative_path,
            key,
            reason,
        });
    }
    archived.sort_by(|left, right| {
        left.page
            .cmp(&right.page)
            .then_with(|| left.key.cmp(&right.key))
    });
    Ok(archived)
}

fn archive_unworthy_concepts(
    vault_root: &Path,
    scope: &ScopeIdentity,
    dry_run: bool,
    archived: Vec<UnworthyConceptArchive>,
) -> Result<Vec<UnworthyConceptArchive>, WikiError> {
    use crate::frontmatter::WikiLifecycle;

    if !dry_run {
        for candidate in &archived {
            crate::lifecycle::apply_lifecycle_transition(
                vault_root,
                scope,
                &candidate.page,
                WikiLifecycle::Archived,
                &format!(
                    "upkeep: unworthy concept key `{}` ({})",
                    candidate.key, candidate.reason
                ),
            )?;
        }
    }
    Ok(archived)
}

const CANDIDATE_PROMOTION_BACKLINKS: usize = 2;

/// Promote corroborated candidates and discard orphaned ones (#17727).
///
/// Promotion clears the `candidate` frontmatter flag when at least
/// [`CANDIDATE_PROMOTION_BACKLINKS`] other knowledge pages link to the
/// candidate. Discard archives a candidate that no page in the vault links to
/// anymore (its digest mentions were healed away or its sources pruned) —
/// the page stays on disk at its stable path but leaves every default
/// surface. Both paths append their own `log.md` audit entries.
fn govern_candidates(
    vault_root: &Path,
    scope: &ScopeIdentity,
    timestamp: &str,
) -> Result<(Vec<PathBuf>, Vec<PathBuf>), WikiError> {
    use crate::frontmatter::WikiLifecycle;

    let pages = lint::collect_pages(vault_root)?;
    let candidates: Vec<(usize, BTreeSet<String>)> = pages
        .iter()
        .enumerate()
        .filter(|(_, page)| {
            page.parsed.frontmatter.candidate
                && page.parsed.frontmatter.lifecycle != Some(WikiLifecycle::Archived)
        })
        .map(|(index, page)| (index, page_match_keys(page)))
        .collect();
    if candidates.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }

    let mut key_to_slot: BTreeMap<&str, usize> = BTreeMap::new();
    for (slot, (_, keys)) in candidates.iter().enumerate() {
        for key in keys {
            key_to_slot.entry(key.as_str()).or_insert(slot);
        }
    }

    let mut knowledge_referrers: Vec<BTreeSet<&Path>> = vec![BTreeSet::new(); candidates.len()];
    let mut any_referrers: Vec<BTreeSet<&Path>> = vec![BTreeSet::new(); candidates.len()];
    for (page_index, page) in pages.iter().enumerate() {
        let is_knowledge_page = page.relative_path.starts_with("knowledge/concepts")
            || page.relative_path.starts_with("knowledge/topics");
        for link in &page.parsed.links {
            let key = canonical_target_key(&link.target);
            let Some(&slot) = key_to_slot.get(key.as_str()) else {
                continue;
            };
            if candidates[slot].0 == page_index {
                continue;
            }
            any_referrers[slot].insert(page.relative_path.as_path());
            if is_knowledge_page {
                knowledge_referrers[slot].insert(page.relative_path.as_path());
            }
        }
    }

    let mut promoted = Vec::new();
    let mut discarded = Vec::new();
    for (slot, (page_index, _)) in candidates.iter().enumerate() {
        let relative = &pages[*page_index].relative_path;
        if knowledge_referrers[slot].len() >= CANDIDATE_PROMOTION_BACKLINKS {
            let reason = format!(
                "corroborated by {} knowledge pages",
                knowledge_referrers[slot].len()
            );
            if crate::lifecycle::promote_candidate_page(vault_root, scope, relative, &reason)? {
                promoted.push(relative.clone());
            }
        } else if any_referrers[slot].is_empty() {
            crate::lifecycle::apply_lifecycle_transition(
                vault_root,
                scope,
                relative,
                WikiLifecycle::Archived,
                "candidate discarded: no remaining backlinks",
            )?;
            crate::log::append_logs(
                vault_root,
                None,
                &crate::log::LogEntry {
                    timestamp: timestamp.to_string(),
                    scope: scope.clone(),
                    action: crate::log::ACTION_CANDIDATE_DISCARDED.to_string(),
                    summary: format!("{}: no remaining backlinks", relative.display()),
                    artifacts: vec![relative.clone()],
                },
            )?;
            discarded.push(relative.clone());
        }
    }
    promoted.sort();
    discarded.sort();
    Ok((promoted, discarded))
}

fn write_report(vault_root: &Path, report: &UpkeepReport) -> Result<(), WikiError> {
    let path = vault_root.join(REPORT_RELATIVE_PATH);
    let temp_path = path.with_extension("json.tmp");
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create upkeep report directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let json = serde_json::to_string_pretty(report).map_err(|error| WikiError::Json {
        action: "serialize upkeep report",
        path: Some(path.clone()),
        source: error,
    })?;
    fs::write(&temp_path, json).map_err(|error| WikiError::Io {
        action: "write upkeep report checkpoint",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    fs::rename(&temp_path, &path).map_err(|error| WikiError::Io {
        action: "publish upkeep report checkpoint",
        path: Some(path),
        source: error,
    })
}

pub fn render_text(report: &UpkeepReport) -> String {
    let mut text = format!(
        "Wiki upkeep {}\nScope: {}\nPending: {} -> {}\nPages: {} created, {} updated, {} failed\nReconciled without synthesis: {}\n",
        if report.dry_run { "(dry run)" } else { "run" },
        report.scope,
        report.pending_before,
        report.pending_after,
        report.pages_created,
        report.pages_updated,
        report.failures,
        report.reconciled_no_synthesis.len(),
    );
    for cluster in &report.clusters {
        text.push_str("- ");
        text.push_str(&cluster.target);
        text.push_str(": ");
        text.push_str(&cluster.action);
        if let Some(page) = &cluster.page_path {
            text.push_str(" -> ");
            text.push_str(&page.display().to_string());
        }
        if cluster.review_flag {
            text.push_str(" [review]");
        }
        if let Some(error) = &cluster.error {
            text.push_str(" (");
            text.push_str(error);
            text.push(')');
        }
        text.push('\n');
    }
    if !report.skipped_over_budget.is_empty() {
        text.push_str(&format!(
            "Skipped over budget: {}\n",
            report
                .skipped_over_budget
                .iter()
                .map(|cluster| cluster.target.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if report.budget_exhausted {
        text.push_str(&format!(
            "Time budget exhausted; deferred clusters: {}\n",
            report
                .deferred_clusters
                .iter()
                .map(|cluster| cluster.target.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if !report.candidates_promoted.is_empty() {
        text.push_str(&format!(
            "Candidates promoted: {}\n",
            display_paths(&report.candidates_promoted)
        ));
    }
    if !report.candidates_discarded.is_empty() {
        text.push_str(&format!(
            "Candidates discarded: {}\n",
            display_paths(&report.candidates_discarded)
        ));
    }
    if !report.unworthy_archived.is_empty() {
        text.push_str(if report.dry_run {
            "Unworthy concepts to archive:\n"
        } else {
            "Unworthy concepts archived:\n"
        });
        for archived in &report.unworthy_archived {
            text.push_str(&format!(
                "- {} [{}]: {}\n",
                archived.page.display(),
                archived.key,
                archived.reason
            ));
        }
    }
    for note in &report.notes {
        text.push_str("Note: ");
        text.push_str(note);
        text.push('\n');
    }
    text
}

fn display_paths(paths: &[PathBuf]) -> String {
    paths
        .iter()
        .map(|path| path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(test)]
mod tests;
