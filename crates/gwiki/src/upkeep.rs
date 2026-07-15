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
//! near-duplicate at or above [`NEAR_DUPLICATE_UPDATE_COSINE`] is updated, the
//! review band creates a new page flagged for review, and a missing backend
//! skips the check with a note.
//! The deterministic tail runs even without AI: catalog regeneration, an
//! `upkeep_completed` log entry, a compile-status reconcile for reviewed
//! sources that formed no cluster, and a run report under
//! [`REPORT_RELATIVE_PATH`].

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::compile::{CompileRequest, WikiCompileOptions, compile_to_wiki_with_options, select};
use crate::explainer::ExplainerGenerator;
use crate::links::{LinkKind, canonical_target_key, extract_links, is_entity_key};
use crate::lint::page_match_keys;
use crate::search::SearchScope;
use crate::search::semantic::{SemanticSearchBackend, SemanticSearchRequest};
use crate::session::{ResearchScope, ResearchSession};
use crate::sources::{CompileStatus, SourceManifest, SourceRecord};
use crate::support::text::degradation_label;
use crate::synthesis::{ArticleKind, PageWriteKind};
use crate::{ScopeIdentity, WikiError, catalog, lint, paths};

/// Default budget: concept pages synthesized per run.
pub(crate) const DEFAULT_MAX_PAGES: usize = 10;
/// Default minimum digest mentions before an unresolved target forms a cluster.
pub(crate) const DEFAULT_MIN_MENTIONS: usize = 2;
/// Default budget: accepted sources compiled into one concept page.
pub(crate) const DEFAULT_MAX_SOURCES_PER_PAGE: usize = 12;
/// Cosine similarity at or above which upkeep updates the matched page
/// instead of creating a new one.
const NEAR_DUPLICATE_UPDATE_COSINE: f64 = 0.90;
/// Cosine similarity band lower bound: a hit in
/// [`NEAR_DUPLICATE_REVIEW_COSINE`, [`NEAR_DUPLICATE_UPDATE_COSINE`]) still
/// creates a page but flags the cluster for human review.
const NEAR_DUPLICATE_REVIEW_COSINE: f64 = 0.80;
/// Semantic hits requested per near-duplicate probe.
const NEAR_DUPLICATE_SEARCH_LIMIT: usize = 8;
/// Vault-relative path of the run report written by non-dry runs.
pub(crate) const REPORT_RELATIVE_PATH: &str = "meta/upkeep/last-run.json";
/// Default days a page stays `stale` before upkeep archives it. A starting
/// point until Part A loop distributions tune the threshold (strategy §3.4).
pub(crate) const DEFAULT_ARCHIVE_AFTER_DAYS: u64 = 45;
const MIN_CLUSTER_REMAINING_SECONDS: u64 = 1210;

/// Durable registry of concept mentions the heal pass unwrapped from digest
/// bodies. It preserves the concept-synthesis work-queue signal across runs so
/// unwrapping a red-link to plain text does not lose the mention (#17703).
const HEALED_MENTIONS_PATH: &str = "meta/upkeep/concept-mentions.json";
/// Frontmatter tag marking entity concept pages synthesized by upkeep.
const ENTITY_TAG: &str = "entity";

/// Budgets and toggles for one upkeep run.
#[derive(Debug, Clone)]
pub struct Options {
    pub max_pages: usize,
    pub min_mentions: usize,
    pub max_sources_per_page: usize,
    pub dry_run: bool,
    pub daemon_synthesis_available: bool,
    /// Set when a Lane B generator is driving synthesis: a generation failure
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
    /// Quarantined candidates promoted this run: corroborated by at least
    /// [`CANDIDATE_PROMOTION_BACKLINKS`] other knowledge pages (#17727).
    pub candidates_promoted: Vec<PathBuf>,
    /// Quarantined candidates discarded this run: no page links to them
    /// anymore, so they were archived (#17727).
    pub candidates_discarded: Vec<PathBuf>,
    pub notes: Vec<String>,
}

/// Accumulated mentions for one case-folded unresolved target.
#[derive(Default)]
struct ClusterAccumulator {
    mentions: usize,
    variant_counts: BTreeMap<String, usize>,
    source_indices: BTreeSet<usize>,
}

struct Cluster {
    key: String,
    primary: String,
    variants: Vec<String>,
    mentions: usize,
    source_indices: Vec<usize>,
}

/// How the update-over-create layers resolved for one cluster.
enum PageDisposition {
    Update {
        page: PathBuf,
        near_duplicate: Option<NearDuplicateMatch>,
    },
    Create {
        near_duplicate: Option<NearDuplicateMatch>,
        review_flag: bool,
        note: Option<String>,
    },
}

pub fn run(
    research_scope: ResearchScope,
    scope: ScopeIdentity,
    options: &Options,
    semantic: Option<SemanticProbe<'_>>,
    generator: Option<ExplainerGenerator<'_>>,
    timestamp: &str,
) -> Result<UpkeepReport, WikiError> {
    run_with_clock(
        research_scope,
        scope,
        options,
        semantic,
        generator,
        timestamp,
        Instant::now,
    )
}

fn run_with_clock(
    research_scope: ResearchScope,
    scope: ScopeIdentity,
    options: &Options,
    mut semantic: Option<SemanticProbe<'_>>,
    mut generator: Option<ExplainerGenerator<'_>>,
    timestamp: &str,
    mut now: impl FnMut() -> Instant,
) -> Result<UpkeepReport, WikiError> {
    let started_at = now();
    let deadline = options
        .time_budget_seconds
        .and_then(|seconds| started_at.checked_add(Duration::from_secs(seconds)));
    let vault_root = research_scope.root().to_path_buf();
    let mut notes: Vec<String> = Vec::new();

    // Rename pages whose filenames collide with agent instruction files
    // (claude.md == CLAUDE.md on case-insensitive filesystems, #17645) before
    // lint runs, so clustering and page matching see the migrated layout.
    migrate_reserved_pages(&vault_root, options.dry_run, &mut notes)?;

    let manifest = SourceManifest::read(&vault_root)?;
    let records: Vec<SourceRecord> = manifest.entries.clone();
    let pending_indices: BTreeSet<usize> = records
        .iter()
        .enumerate()
        .filter(|(_, entry)| entry.compile_status == CompileStatus::Pending)
        .map(|(index, _)| index)
        .collect();
    let pending_before = pending_indices.len();

    // Digest page (knowledge/sources/<id>.md) -> manifest record index.
    // Unresolved targets mentioned by ANY digest seed clusters — the contract
    // counts digest mentions, not pending ones. Compile status only drives the
    // drain bookkeeping below; without this, targets whose mentioning digests
    // were already reconciled (or consumed by another cluster) could never get
    // an entity page and the vault's broken links would never converge.
    let mut digest_records: BTreeMap<PathBuf, usize> = BTreeMap::new();
    for (index, record) in records.iter().enumerate() {
        match paths::derived_markdown_path(record) {
            Ok(path) => {
                digest_records.insert(path, index);
            }
            Err(error) => {
                notes.push(format!(
                    "skipping source `{}` during upkeep digest scan: {error}",
                    record.id
                ));
            }
        }
    }

    let lint_report = lint::run(&vault_root, scope.clone())?;
    let mut accumulators: BTreeMap<String, ClusterAccumulator> = BTreeMap::new();
    let mut counted_mentions: BTreeSet<(usize, String)> = BTreeSet::new();
    for issue in &lint_report.broken_links {
        let Some(&source_index) = digest_records.get(&issue.path) else {
            continue;
        };
        // Path-shaped targets (file paths, digest links) never mint entity
        // pages — librarian classifies them as repair debt or compile-pending
        // convergence (#17652). Their mentioning digests reconcile as
        // reviewed-no-synthesis below.
        let key = canonical_target_key(&issue.target);
        if !is_entity_key(&key) {
            continue;
        }
        counted_mentions.insert((source_index, key.clone()));
        let accumulator = accumulators.entry(key).or_default();
        accumulator.mentions += 1;
        *accumulator
            .variant_counts
            .entry(issue.target.clone())
            .or_default() += 1;
        accumulator.source_indices.insert(source_index);
    }

    // Healed mentions (concept red-links a prior run unwrapped to plain text)
    // keep seeding clusters so concept discovery accumulates across upkeep
    // cycles even though the digest bodies no longer carry the unresolved
    // `[[Entity]]` links. This durable registry replaces the permanent broken
    // links the vault used to accrue as its concept-synthesis work-queue: a
    // digest link and its healed record never both count for the same
    // (digest, entity) pair (#17703).
    let mut healed_mentions = HealedMentions::read(&vault_root)?;
    for (digest_path, &source_index) in &digest_records {
        for entity in healed_mentions.entities_for(digest_path) {
            let key = canonical_target_key(entity);
            if !is_entity_key(&key) || !counted_mentions.insert((source_index, key.clone())) {
                continue;
            }
            let accumulator = accumulators.entry(key).or_default();
            accumulator.mentions += 1;
            *accumulator
                .variant_counts
                .entry(entity.clone())
                .or_default() += 1;
            accumulator.source_indices.insert(source_index);
        }
    }

    let mut candidates: Vec<Cluster> = accumulators
        .into_iter()
        .filter(|(_, accumulator)| accumulator.mentions >= options.min_mentions)
        .map(|(key, accumulator)| {
            let mut variants = accumulator.variant_counts.into_iter().collect::<Vec<_>>();
            variants.sort_by(|(left_target, left_count), (right_target, right_count)| {
                right_count
                    .cmp(left_count)
                    .then_with(|| left_target.cmp(right_target))
            });
            let variants = variants
                .into_iter()
                .map(|(variant, _)| variant)
                .collect::<Vec<_>>();
            Cluster {
                key,
                primary: variants[0].clone(),
                variants,
                mentions: accumulator.mentions,
                source_indices: accumulator.source_indices.into_iter().collect(),
            }
        })
        .collect();
    candidates.sort_by(|left, right| {
        right
            .mentions
            .cmp(&left.mentions)
            .then_with(|| left.key.cmp(&right.key))
    });

    // Sources in any candidate cluster stay pending until their cluster is
    // compiled; everything else was reviewed without synthesis and reconciles.
    let clustered_indices: BTreeSet<usize> = candidates
        .iter()
        .flat_map(|cluster| cluster.source_indices.iter().copied())
        .collect();

    let processed_count = candidates.len().min(options.max_pages);
    let skipped_over_budget: Vec<SkippedCluster> = candidates[processed_count..]
        .iter()
        .map(|cluster| SkippedCluster {
            target: cluster.primary.clone(),
            mentions: cluster.mentions,
        })
        .collect();

    // Match keys for existing knowledge pages (concepts and topics; source
    // digests are excluded so an entity name never "matches" a digest stub).
    let existing_pages: Vec<(PathBuf, BTreeSet<String>)> = lint::collect_pages(&vault_root)?
        .iter()
        .filter(|page| {
            page.relative_path.starts_with("knowledge/concepts")
                || page.relative_path.starts_with("knowledge/topics")
        })
        .map(|page| (page.relative_path.clone(), page_match_keys(page)))
        .collect();

    let mut report = UpkeepReport {
        command: "upkeep",
        scope: scope.clone(),
        timestamp: timestamp.to_string(),
        dry_run: options.dry_run,
        max_pages: options.max_pages,
        min_mentions: options.min_mentions,
        max_sources_per_page: options.max_sources_per_page,
        pending_before,
        pending_after: pending_before,
        pages_created: 0,
        pages_updated: 0,
        failures: 0,
        clusters: Vec::new(),
        budget_exhausted: false,
        deferred_clusters: Vec::new(),
        skipped_over_budget: skipped_over_budget.clone(),
        reconciled_no_synthesis: Vec::new(),
        archived_pages: Vec::new(),
        candidates_promoted: Vec::new(),
        candidates_discarded: Vec::new(),
        notes: Vec::new(),
    };

    for (cluster_index, cluster) in candidates[..processed_count].iter().enumerate() {
        let budget_exhausted = deadline.is_some_and(|deadline| {
            deadline.saturating_duration_since(now())
                < Duration::from_secs(MIN_CLUSTER_REMAINING_SECONDS)
        });
        if budget_exhausted {
            report.budget_exhausted = true;
            report.deferred_clusters = candidates[cluster_index..processed_count]
                .iter()
                .map(|cluster| SkippedCluster {
                    target: cluster.primary.clone(),
                    mentions: cluster.mentions,
                })
                .collect();
            if !options.dry_run {
                report.notes = notes.clone();
                report.pending_after = pending_source_count(&vault_root)?;
                write_report(&vault_root, &report)?;
            }
            break;
        }
        let disposition = resolve_page_disposition(cluster, &existing_pages, &mut semantic);
        let (target_page, near_duplicate, review_flag, note) = match disposition {
            PageDisposition::Update {
                page,
                near_duplicate,
            } => (Some(page), near_duplicate, false, None),
            PageDisposition::Create {
                near_duplicate,
                review_flag,
                note,
            } => (None, near_duplicate, review_flag, note),
        };
        if let Some(note) = note
            && !notes.contains(&note)
        {
            notes.push(note);
        }

        let mut selected: Vec<&SourceRecord> = cluster
            .source_indices
            .iter()
            .map(|&index| &records[index])
            .collect();
        selected.sort_by(|left, right| left.id.cmp(&right.id));
        let sources_truncated = selected.len().saturating_sub(options.max_sources_per_page);
        selected.truncate(options.max_sources_per_page);
        let source_ids: Vec<String> = selected.iter().map(|record| record.id.clone()).collect();

        let mut outcome = ClusterOutcome {
            target: cluster.primary.clone(),
            key: cluster.key.clone(),
            mentions: cluster.mentions,
            variants: cluster.variants.clone(),
            source_ids,
            sources_truncated,
            action: String::new(),
            page_path: target_page.clone(),
            near_duplicate,
            review_flag,
            error: None,
        };

        if options.dry_run {
            outcome.action = if target_page.is_some() {
                "planned_update".to_string()
            } else {
                "planned_create".to_string()
            };
            report.clusters.push(outcome);
            continue;
        }

        // Reborrow the generator with a per-iteration lifetime.
        let cluster_generator: Option<ExplainerGenerator<'_>> = match generator.as_mut() {
            Some(generate) => Some(&mut **generate),
            None => None,
        };
        match compile_cluster(
            &vault_root,
            &research_scope,
            options,
            cluster,
            &selected,
            target_page,
            cluster_generator,
        ) {
            Ok((page_path, write_kind)) => {
                outcome.page_path = Some(page_path.clone());
                outcome.action = match write_kind {
                    PageWriteKind::Created => {
                        report.pages_created += 1;
                        "created".to_string()
                    }
                    PageWriteKind::Overwritten => {
                        report.pages_updated += 1;
                        "updated".to_string()
                    }
                };
                // Quarantine audit trail (#17727): a created concept page
                // enters quarantine (proposed); a near-duplicate update
                // resolved the would-be candidate into an existing page
                // (merged). Plain key-match updates are ordinary recompiles,
                // not candidate events.
                let candidate_event = match write_kind {
                    PageWriteKind::Created => Some((
                        crate::log::ACTION_CANDIDATE_PROPOSED,
                        format!(
                            "{}: proposed from cluster `{}` ({} mentions)",
                            page_path.display(),
                            cluster.primary,
                            cluster.mentions,
                        ),
                    )),
                    PageWriteKind::Overwritten => outcome.near_duplicate.as_ref().map(|near| {
                        (
                            crate::log::ACTION_CANDIDATE_MERGED,
                            format!(
                                "{}: cluster `{}` resolved into existing page (cosine {:.2})",
                                page_path.display(),
                                cluster.primary,
                                near.score,
                            ),
                        )
                    }),
                };
                if let Some((action, summary)) = candidate_event {
                    crate::log::append_logs(
                        &vault_root,
                        None,
                        &crate::log::LogEntry {
                            timestamp: timestamp.to_string(),
                            scope: scope.clone(),
                            action: action.to_string(),
                            summary,
                            artifacts: vec![page_path],
                        },
                    )?;
                }
            }
            Err(error) => {
                // Per-page failure: record it and keep draining. The cluster's
                // sources stay pending for the next run.
                report.failures += 1;
                outcome.action = "failed".to_string();
                outcome.error = Some(error.to_string());
            }
        }
        report.clusters.push(outcome);
        if !options.dry_run {
            report.notes = notes.clone();
            report.pending_after = pending_source_count(&vault_root)?;
            write_report(&vault_root, &report)?;
        }
    }

    // Heal the vault's broken-link debt: any `[[Entity]]` concept link still
    // unresolved AFTER this run's synthesis is unwrapped to plain text and its
    // entity recorded in the durable mentions registry, so `broken_link_count`
    // converges to ~0 while concept discovery keeps accumulating across runs
    // (#17703). Re-linting after synthesis keeps freshly created concept pages
    // linked (their targets now resolve) without approximating which keys the
    // librarian actually minted.
    if !options.dry_run {
        let post_synthesis = lint::run(&vault_root, scope.clone())?;
        let mut unresolved_by_digest: BTreeMap<PathBuf, BTreeSet<String>> = BTreeMap::new();
        for issue in &post_synthesis.broken_links {
            if !digest_records.contains_key(&issue.path) {
                continue;
            }
            let key = canonical_target_key(&issue.target);
            if !is_entity_key(&key) {
                continue;
            }
            unresolved_by_digest
                .entry(issue.path.clone())
                .or_default()
                .insert(key);
        }
        for (digest_path, unresolved_keys) in &unresolved_by_digest {
            for entity in
                unwrap_unresolved_concept_links(&vault_root, digest_path, unresolved_keys)?
            {
                healed_mentions.record(digest_path, entity);
            }
        }
        healed_mentions.retain_digests(digest_records.keys());
        healed_mentions.write(&vault_root)?;
    }

    // Candidate governance (#17727): corroboration from other knowledge pages
    // promotes a quarantined candidate out of quarantine; a candidate no page
    // links to anymore is an orphan and is discarded via the archive
    // transition. Runs after healing so this run's own link rewrites count.
    let (candidates_promoted, candidates_discarded) = if options.dry_run {
        (Vec::new(), Vec::new())
    } else {
        govern_candidates(&vault_root, &scope, timestamp)?
    };

    // Archive long-stale pages before catalog regeneration so the regenerated
    // indexes already reflect the exclusions.
    let archived_pages = archive_long_stale_pages(&vault_root, &scope, options)?;

    // Only pending sources reconcile: a compiled digest feeding a cluster is
    // evidence reuse, not a drain-state change.
    let mut reconciled_no_synthesis: Vec<String> = pending_indices
        .iter()
        .filter(|index| !clustered_indices.contains(index))
        .map(|&index| records[index].id.clone())
        .collect();
    reconciled_no_synthesis.sort_unstable();
    if !options.dry_run && !reconciled_no_synthesis.is_empty() {
        let reconcile_ids: BTreeSet<&str> =
            reconciled_no_synthesis.iter().map(String::as_str).collect();
        SourceManifest::update(&vault_root, |manifest| {
            let mut changed = false;
            for entry in &mut manifest.entries {
                if reconcile_ids.contains(entry.id.as_str())
                    && entry.compile_status != CompileStatus::Compiled
                {
                    entry.compile_status = CompileStatus::Compiled;
                    changed = true;
                }
            }
            Ok(changed)
        })?;
    }

    let pending_after = if options.dry_run {
        pending_before
    } else {
        SourceManifest::read(&vault_root)?
            .entries
            .iter()
            .filter(|entry| entry.compile_status == CompileStatus::Pending)
            .count()
    };

    report.pending_after = pending_after;
    report.reconciled_no_synthesis = reconciled_no_synthesis;
    report.archived_pages = archived_pages;
    report.candidates_promoted = candidates_promoted;
    report.candidates_discarded = candidates_discarded;
    report.notes = notes;

    if !options.dry_run {
        catalog::regenerate(&vault_root, &scope)?;
        write_report(&vault_root, &report)?;
        crate::log::append_logs(
            &vault_root,
            None,
            &crate::log::LogEntry {
                timestamp: timestamp.to_string(),
                scope,
                action: crate::log::ACTION_UPKEEP_COMPLETED.to_string(),
                summary: format!(
                    "created={} updated={} failed={} archived={} reconciled={} pending_after={}",
                    report.pages_created,
                    report.pages_updated,
                    report.failures,
                    report.archived_pages.len(),
                    report.reconciled_no_synthesis.len(),
                    report.pending_after,
                ),
                artifacts: vec![PathBuf::from(REPORT_RELATIVE_PATH)],
            },
        )?;
    }

    Ok(report)
}

fn reserved_migration_dirs() -> [(&'static str, &'static str); 3] {
    [
        (
            ArticleKind::Concept.directory(),
            ArticleKind::Concept.reserved_suffix(),
        ),
        (
            ArticleKind::Topic.directory(),
            ArticleKind::Topic.reserved_suffix(),
        ),
        (
            ArticleKind::Source.directory(),
            ArticleKind::Source.reserved_suffix(),
        ),
    ]
}

/// Rename vault pages whose filename stem case-insensitively matches an agent
/// instruction filename (`claude.md` == `CLAUDE.md` on APFS), then retarget
/// path-form links across the vault. Titles and aliases are untouched, so
/// title-addressed links like `[[Claude]]` keep resolving. Dry runs only
/// report what would change.
fn migrate_reserved_pages(
    vault_root: &Path,
    dry_run: bool,
    notes: &mut Vec<String>,
) -> Result<(), WikiError> {
    use gobby_core::vault::reserved::is_reserved_instruction_stem;

    let mut renames: Vec<(PathBuf, PathBuf)> = Vec::new();
    let mut planned_destinations: BTreeSet<PathBuf> = BTreeSet::new();
    for (directory, suffix) in reserved_migration_dirs() {
        let absolute = vault_root.join(directory);
        let Ok(entries) = fs::read_dir(&absolute) else {
            continue;
        };
        let mut paths: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
        paths.sort();
        for path in paths {
            if path.extension().and_then(|ext| ext.to_str()) != Some("md") {
                continue;
            }
            let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
                continue;
            };
            if !is_reserved_instruction_stem(stem) {
                continue;
            }
            let base = format!("{}-{suffix}", stem.to_ascii_lowercase());
            let mut new_stem = None;
            for index in 1usize..=99 {
                let candidate = if index == 1 {
                    base.clone()
                } else {
                    format!("{base}-{index}")
                };
                let candidate_relative = PathBuf::from(directory).join(format!("{candidate}.md"));
                if absolute.join(format!("{candidate}.md")).exists()
                    || planned_destinations.contains(&candidate_relative)
                {
                    continue;
                }
                planned_destinations.insert(candidate_relative);
                new_stem = Some(candidate);
                break;
            }
            let Some(new_stem) = new_stem else {
                notes.push(format!(
                    "reserved-slug migration: no free name for {directory}/{stem}.md; left in place"
                ));
                continue;
            };
            renames.push((
                PathBuf::from(directory).join(format!("{stem}.md")),
                PathBuf::from(directory).join(format!("{new_stem}.md")),
            ));
        }
    }
    if renames.is_empty() {
        return Ok(());
    }

    if dry_run {
        for (old, new) in &renames {
            notes.push(format!(
                "reserved-slug migration (dry run): would rename {} -> {}",
                old.display(),
                new.display()
            ));
        }
        return Ok(());
    }

    let mut completed: Vec<(PathBuf, PathBuf)> = Vec::new();
    for (old, new) in &renames {
        if let Err(error) = fs::rename(vault_root.join(old), vault_root.join(new)) {
            if let Err(cleanup_error) =
                retarget_completed_reserved_renames(vault_root, &completed, notes)
            {
                notes.push(format!(
                    "reserved-slug migration: failed to retarget completed renames after rename failure: {cleanup_error}"
                ));
            }
            return Err(WikiError::Io {
                action: "rename reserved-slug page",
                path: Some(vault_root.join(old)),
                source: error,
            });
        }
        completed.push((old.clone(), new.clone()));
        notes.push(format!(
            "reserved-slug migration: renamed {} -> {} (agent instruction filename collision)",
            old.display(),
            new.display()
        ));
    }
    retarget_completed_reserved_renames(vault_root, &completed, notes)
}

fn retarget_completed_reserved_renames(
    vault_root: &Path,
    renames: &[(PathBuf, PathBuf)],
    notes: &mut Vec<String>,
) -> Result<(), WikiError> {
    if renames.is_empty() {
        return Ok(());
    }
    let rewritten = retarget_renamed_links(vault_root, renames)?;
    if rewritten > 0 {
        notes.push(format!(
            "reserved-slug migration: retargeted {rewritten} path-form links"
        ));
    }
    Ok(())
}

/// Rewrite path-form wikilinks and markdown links that point at renamed pages.
/// Returns the number of links rewritten.
fn retarget_renamed_links(
    vault_root: &Path,
    renames: &[(PathBuf, PathBuf)],
) -> Result<usize, WikiError> {
    use crate::links::{LinkKind, extract_links};

    // Old canonical target key (with and without the .md suffix) -> new
    // extensionless vault path.
    let mut targets: BTreeMap<String, String> = BTreeMap::new();
    for (old, new) in renames {
        let old_page = old.to_string_lossy().replace('\\', "/");
        let old_stemless = old.with_extension("").to_string_lossy().replace('\\', "/");
        let new_stemless = new.with_extension("").to_string_lossy().replace('\\', "/");
        targets.insert(canonical_target_key(&old_page), new_stemless.clone());
        targets.insert(canonical_target_key(&old_stemless), new_stemless);
    }

    let mut markdown_files: Vec<PathBuf> = Vec::new();
    collect_retarget_files(vault_root, vault_root, &mut markdown_files)?;
    markdown_files.sort();

    let mut rewritten = 0usize;
    for path in markdown_files {
        let Ok(markdown) = fs::read_to_string(&path) else {
            continue;
        };
        let links = extract_links(&markdown, std::iter::empty::<&str>());
        let mut edits: Vec<(usize, usize, String)> = Vec::new();
        for link in &links {
            let Some(new_target) = targets.get(&canonical_target_key(&link.normalized_target))
            else {
                continue;
            };
            let keeps_extension = link.target.to_ascii_lowercase().ends_with(".md");
            let destination = if keeps_extension {
                format!("{new_target}.md")
            } else {
                new_target.clone()
            };
            let anchor = link
                .anchor
                .as_deref()
                .map(|anchor| format!("#{anchor}"))
                .unwrap_or_default();
            let replacement = match link.kind {
                LinkKind::Wikilink => match link.alias.as_deref() {
                    Some(alias) => format!("[[{destination}{anchor}|{alias}]]"),
                    None => format!("[[{destination}{anchor}]]"),
                },
                LinkKind::Markdown => format!(
                    "[{}]({destination}{anchor})",
                    link.alias.as_deref().unwrap_or(new_target)
                ),
            };
            edits.push((link.byte_start, link.byte_end, replacement));
        }
        if edits.is_empty() {
            continue;
        }
        let mut updated = markdown.clone();
        for (byte_start, byte_end, replacement) in edits.into_iter().rev() {
            updated.replace_range(byte_start..byte_end, &replacement);
            rewritten += 1;
        }
        fs::write(&path, updated).map_err(|error| WikiError::Io {
            action: "retarget renamed page links",
            path: Some(path.clone()),
            source: error,
        })?;
    }
    Ok(rewritten)
}

/// Collect vault markdown files eligible for link retargeting, skipping raw
/// captures (verbatim source data), vault state, and hidden directories.
fn collect_retarget_files(
    vault_root: &Path,
    directory: &Path,
    files: &mut Vec<PathBuf>,
) -> Result<(), WikiError> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Ok(());
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if path.is_dir() {
            let is_vault_top_level = directory == vault_root;
            if name.starts_with('.')
                || (is_vault_top_level && (name == "raw" || name == gobby_core::vault::STATE_ROOT))
            {
                continue;
            }
            collect_retarget_files(vault_root, &path, files)?;
        } else if path.extension().and_then(|ext| ext.to_str()) == Some("md") {
            files.push(path);
        }
    }
    Ok(())
}

/// Synthesize one cluster through the compile pipeline against an ephemeral
/// research session; the caller's vault checkpoint is never touched.
fn compile_cluster(
    vault_root: &Path,
    research_scope: &ResearchScope,
    options: &Options,
    cluster: &Cluster,
    selected: &[&SourceRecord],
    target_page: Option<PathBuf>,
    generator: Option<ExplainerGenerator<'_>>,
) -> Result<(PathBuf, PageWriteKind), WikiError> {
    let mut notes = Vec::with_capacity(selected.len());
    for record in selected {
        notes.push(select::accepted_note_from_source(vault_root, record)?);
    }
    let mut session = ResearchSession::new(
        cluster.primary.clone(),
        research_scope.clone(),
        Vec::new(),
        1,
        None,
    )?;
    session.accepted_notes = notes;

    // Quarantine (#17727): a freshly minted concept page starts as an
    // untrusted candidate; an update keeps the target's existing quarantine
    // state so a rewrite never silently promotes a candidate.
    let mark_candidate = match &target_page {
        None => true,
        Some(page) => crate::lifecycle::page_is_candidate(vault_root, page),
    };
    // An Update disposition is upkeep's own identity decision: near-duplicate
    // merges (and case-variant key matches) deliberately compile a cluster
    // into an existing page whose title is not the cluster topic, so the
    // targeted-compile identity check (#17804) must not veto it.
    let allow_target_identity_mismatch = target_page.is_some();

    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: cluster.primary.clone(),
            outline: Vec::new(),
            target_page,
            write_intent: true,
        },
        WikiCompileOptions {
            target_kind: ArticleKind::Concept,
            daemon_synthesis_available: options.daemon_synthesis_available,
            hard_fail_on_generation_failure: options.hard_fail_on_generation_failure,
            aliases: cluster.variants.clone(),
            extra_tags: vec![ENTITY_TAG.to_string()],
            persist_checkpoint: false,
            mark_candidate,
            allow_target_identity_mismatch,
        },
        generator,
    )?;

    let write_kind = outcome
        .page_writes
        .first()
        .map(|write| write.kind)
        .unwrap_or(PageWriteKind::Created);
    // Report vault-relative paths so the run report stays portable.
    let article_path = outcome
        .article_path
        .strip_prefix(vault_root)
        .map(Path::to_path_buf)
        .unwrap_or(outcome.article_path);
    Ok((article_path, write_kind))
}

/// Update-over-create layering: exact/alias match first, then the semantic
/// near-duplicate bands, defaulting to create.
/// Archive long-stale pages: lifecycle `stale` whose `stale_at` demotion
/// timestamp is at least `archive_after_days` old. Archived files stay at
/// their stable paths (the publisher and SourceManifest keep resolving them);
/// the shared exclusion predicate removes them from catalog indexes, agent
/// exports, and default retrieval. Dry runs report would-be archives without
/// touching the vault. Pages stale without a `stale_at` cannot age yet — the
/// next health demotion records one.
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

fn resolve_page_disposition(
    cluster: &Cluster,
    existing_pages: &[(PathBuf, BTreeSet<String>)],
    semantic: &mut Option<SemanticProbe<'_>>,
) -> PageDisposition {
    if let Some((page, _)) = existing_pages
        .iter()
        .find(|(_, keys)| keys.contains(&cluster.key))
    {
        return PageDisposition::Update {
            page: page.clone(),
            near_duplicate: None,
        };
    }

    let Some(probe) = semantic.as_mut() else {
        return PageDisposition::Create {
            near_duplicate: None,
            review_flag: false,
            note: Some("semantic backend unavailable; near-duplicate checks skipped".to_string()),
        };
    };
    let outcome = match probe.backend.search_semantic(SemanticSearchRequest {
        query: cluster.primary.clone(),
        scope: probe.search_scope.clone(),
        limit: NEAR_DUPLICATE_SEARCH_LIMIT,
    }) {
        Ok(outcome) => outcome,
        Err(error) => {
            return PageDisposition::Create {
                near_duplicate: None,
                review_flag: false,
                note: Some(format!(
                    "near-duplicate check failed for `{}`: {error}",
                    cluster.primary
                )),
            };
        }
    };
    if let Some(degradation) = outcome.degradation {
        return PageDisposition::Create {
            near_duplicate: None,
            review_flag: false,
            note: Some(format!(
                "near-duplicate check degraded: {}",
                degradation_label(&degradation)
            )),
        };
    }

    let best = outcome
        .hits
        .into_iter()
        .filter(|hit| {
            hit.path.starts_with("knowledge") && !hit.path.starts_with("knowledge/sources")
        })
        .max_by(|left, right| left.score.total_cmp(&right.score));
    match best {
        Some(hit) if hit.score >= NEAR_DUPLICATE_UPDATE_COSINE => PageDisposition::Update {
            near_duplicate: Some(NearDuplicateMatch {
                page: hit.path.clone(),
                score: hit.score,
            }),
            page: hit.path,
        },
        Some(hit) if hit.score >= NEAR_DUPLICATE_REVIEW_COSINE => PageDisposition::Create {
            near_duplicate: Some(NearDuplicateMatch {
                page: hit.path,
                score: hit.score,
            }),
            review_flag: true,
            note: None,
        },
        _ => PageDisposition::Create {
            near_duplicate: None,
            review_flag: false,
            note: None,
        },
    }
}

/// Corroborating backlinks from other knowledge (concept/topic) pages
/// required to clear a candidate's quarantine (#17727). Digest mentions
/// already gated the page's creation (`min_mentions`), so corroboration is
/// counted from knowledge pages only — a fresh candidate's own digest
/// backlinks never auto-promote it.
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

/// Persisted map of digest relative path -> concept entities the heal pass has
/// unwrapped from that digest body. Consumed by clustering so concept discovery
/// still accumulates mentions across runs once the red-links are gone (#17703).
#[derive(Debug, Default, Serialize, serde::Deserialize)]
struct HealedMentions {
    #[serde(default)]
    digests: BTreeMap<String, Vec<String>>,
}

impl HealedMentions {
    fn read(vault_root: &Path) -> Result<Self, WikiError> {
        let path = vault_root.join(HEALED_MENTIONS_PATH);
        match fs::read_to_string(&path) {
            Ok(text) => serde_json::from_str(&text).map_err(|error| WikiError::Json {
                action: "parse healed concept-mentions registry",
                path: Some(path),
                source: error,
            }),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(WikiError::Io {
                action: "read healed concept-mentions registry",
                path: Some(path),
                source: error,
            }),
        }
    }

    fn write(&self, vault_root: &Path) -> Result<(), WikiError> {
        let path = vault_root.join(HEALED_MENTIONS_PATH);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| WikiError::Io {
                action: "create healed concept-mentions directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })?;
        }
        let json = serde_json::to_string_pretty(self).map_err(|error| WikiError::Json {
            action: "serialize healed concept-mentions registry",
            path: Some(path.clone()),
            source: error,
        })?;
        fs::write(&path, json).map_err(|error| WikiError::Io {
            action: "write healed concept-mentions registry",
            path: Some(path),
            source: error,
        })
    }

    fn entities_for(&self, digest_path: &Path) -> &[String] {
        self.digests
            .get(&digest_key(digest_path))
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    fn record(&mut self, digest_path: &Path, entity: String) {
        let entities = self.digests.entry(digest_key(digest_path)).or_default();
        if !entities.contains(&entity) {
            entities.push(entity);
            entities.sort();
        }
    }

    fn retain_digests<'a>(&mut self, live: impl IntoIterator<Item = &'a PathBuf>) {
        let live: BTreeSet<String> = live.into_iter().map(|path| digest_key(path)).collect();
        self.digests.retain(|key, _| live.contains(key));
    }
}

/// Canonical string key for a digest relative path (forward slashes) so the
/// registry stays stable across platforms.
fn digest_key(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Unwrap every unresolved `[[Entity]]` wikilink whose canonical key is in
/// `unresolved_keys` to its plain-text display, returning the raw entity names
/// (link targets) that were unwrapped so the caller can record them in the
/// mentions registry. Resolved links and non-wikilinks are left untouched;
/// links inside code spans are never matched (`extract_links` skips them).
fn unwrap_unresolved_concept_links(
    vault_root: &Path,
    digest_relative: &Path,
    unresolved_keys: &BTreeSet<String>,
) -> Result<Vec<String>, WikiError> {
    let path = vault_root.join(digest_relative);
    let markdown = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read digest for concept-link heal",
                path: Some(path),
                source: error,
            });
        }
    };
    let mut edits: Vec<(usize, usize, String)> = Vec::new();
    let mut recorded: Vec<String> = Vec::new();
    for link in extract_links(&markdown, std::iter::empty::<&str>()) {
        if link.kind != LinkKind::Wikilink {
            continue;
        }
        if !unresolved_keys.contains(&canonical_target_key(&link.normalized_target)) {
            continue;
        }
        let display = link.alias.clone().unwrap_or_else(|| link.target.clone());
        edits.push((link.byte_start, link.byte_end, display));
        recorded.push(link.target);
    }
    if edits.is_empty() {
        return Ok(Vec::new());
    }
    let mut updated = markdown;
    for (byte_start, byte_end, replacement) in edits.into_iter().rev() {
        updated.replace_range(byte_start..byte_end, &replacement);
    }
    fs::write(&path, updated).map_err(|error| WikiError::Io {
        action: "write healed digest concept links",
        path: Some(path),
        source: error,
    })?;
    recorded.sort();
    recorded.dedup();
    Ok(recorded)
}

fn pending_source_count(vault_root: &Path) -> Result<usize, WikiError> {
    Ok(SourceManifest::read(vault_root)?
        .entries
        .iter()
        .filter(|entry| entry.compile_status == CompileStatus::Pending)
        .count())
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
mod tests {
    use super::*;
    use crate::search::semantic::SemanticSearchOutcome;
    use crate::search::{SearchHitKind, SearchProvenance, SearchScope, WikiSearchResult};
    use crate::sources::{IngestionMethod, SourceKind};

    const TIMESTAMP: &str = "unix-ms:1750000000000";

    fn scope() -> ScopeIdentity {
        ScopeIdentity::topic("upkeep-test")
    }

    fn research_scope(root: &Path) -> ResearchScope {
        ResearchScope::topic("upkeep-test", root)
    }

    fn pending_record(id: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: format!("{id}.md"),
            canonical_location: format!("canonical:{id}"),
            kind: SourceKind::Markdown,
            fetched_at: TIMESTAMP.to_string(),
            content_hash: format!("{id}-hash"),
            title: Some(id.to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }

    fn write_file(root: &Path, relative: &str, content: &str) {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent");
        fs::write(path, content).expect("write file");
    }

    #[test]
    fn archive_long_stale_pages_archives_only_aged_stale_pages() {
        use crate::frontmatter::{WikiLifecycle, parse_frontmatter};

        let temp = tempfile::tempdir().expect("tempdir");
        write_file(
            temp.path(),
            "knowledge/concepts/old-stale.md",
            "---\ntitle: Old\nlifecycle: stale\nstale_at: 2020-01-01T00:00:00Z\n---\n\nBody.\n",
        );
        let recent_stale_at = chrono::Utc::now().to_rfc3339();
        let recent = format!(
            "---\ntitle: Recent\nlifecycle: stale\nstale_at: {recent_stale_at}\n---\n\nBody.\n"
        );
        write_file(temp.path(), "knowledge/concepts/recent-stale.md", &recent);
        write_file(
            temp.path(),
            "knowledge/concepts/unstamped-stale.md",
            "---\ntitle: Unstamped\nlifecycle: stale\n---\n\nBody.\n",
        );

        let archived = archive_long_stale_pages(temp.path(), &scope(), &Options::default())
            .expect("archive pass");

        assert_eq!(
            archived,
            vec![PathBuf::from("knowledge/concepts/old-stale.md")]
        );
        let markdown = std::fs::read_to_string(temp.path().join("knowledge/concepts/old-stale.md"))
            .expect("read archived page");
        let parsed = parse_frontmatter(&markdown).expect("parse archived page");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Archived));
        assert!(parsed.metadata.unknown.contains_key("archived_at"));
        // The file stays at its stable path.
        assert!(temp.path().join("knowledge/concepts/old-stale.md").exists());

        let recent_after =
            std::fs::read_to_string(temp.path().join("knowledge/concepts/recent-stale.md"))
                .expect("read recent page");
        assert_eq!(recent_after, recent);

        let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert_eq!(log.matches("lifecycle_transition:").count(), 1, "{log}");
        assert!(log.contains("stale -> archived"), "{log}");
    }

    #[test]
    fn archive_long_stale_pages_dry_run_reports_without_writing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let markdown =
            "---\ntitle: Old\nlifecycle: stale\nstale_at: 2020-01-01T00:00:00Z\n---\n\nBody.\n";
        write_file(temp.path(), "knowledge/concepts/old-stale.md", markdown);
        let options = Options {
            dry_run: true,
            ..Options::default()
        };

        let archived =
            archive_long_stale_pages(temp.path(), &scope(), &options).expect("dry-run pass");

        assert_eq!(
            archived,
            vec![PathBuf::from("knowledge/concepts/old-stale.md")]
        );
        let after = std::fs::read_to_string(temp.path().join("knowledge/concepts/old-stale.md"))
            .expect("read page");
        assert_eq!(after, markdown);
        assert!(!temp.path().join("log.md").exists());
    }

    /// Register a pending source: manifest entry, raw body, and digest page.
    fn seed_source(root: &Path, id: &str, digest_body: &str) {
        let record = pending_record(id);
        write_file(
            root,
            &format!("raw/{id}.md"),
            &format!("# {id}\n\nRaw source body for {id}.\n"),
        );
        write_file(root, &format!("knowledge/sources/{id}.md"), digest_body);
        SourceManifest::update(root, |manifest| {
            manifest.entries.push(record.clone());
            Ok(true)
        })
        .expect("seed manifest entry");
    }

    fn compile_status_of(root: &Path, id: &str) -> CompileStatus {
        SourceManifest::read(root)
            .expect("read manifest")
            .entries
            .iter()
            .find(|entry| entry.id == id)
            .unwrap_or_else(|| panic!("manifest entry {id}"))
            .compile_status
            .clone()
    }

    fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
        let mut files = BTreeMap::new();
        snapshot_into(root, root, &mut files);
        files
    }

    fn snapshot_into(root: &Path, directory: &Path, files: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(directory).expect("read dir") {
            let entry = entry.expect("dir entry");
            let path = entry.path();
            if path.is_dir() {
                snapshot_into(root, &path, files);
            } else {
                files.insert(
                    path.strip_prefix(root).expect("relative").to_path_buf(),
                    fs::read(&path).expect("read file"),
                );
            }
        }
    }

    struct FixedSemanticBackend {
        hits: Vec<WikiSearchResult>,
    }

    impl SemanticSearchBackend for FixedSemanticBackend {
        fn search_semantic(
            &mut self,
            _request: SemanticSearchRequest,
        ) -> Result<SemanticSearchOutcome, crate::search::SearchError> {
            Ok(SemanticSearchOutcome {
                hits: self.hits.clone(),
                degradation: None,
            })
        }
    }

    fn semantic_hit(path: &str, score: f64) -> WikiSearchResult {
        WikiSearchResult {
            id: path.to_string(),
            title: None,
            scope: SearchScope::topic("upkeep-test"),
            path: PathBuf::from(path),
            source_path: PathBuf::from(path),
            hit_kind: SearchHitKind::Document,
            snippet: String::new(),
            score,
            sources: Vec::new(),
            explanations: Vec::new(),
            chunk: None,
            provenance: SearchProvenance {
                document_path: PathBuf::from(path),
                source_path: PathBuf::from(path),
                source_kind: "document".to_string(),
                content_hash: None,
            },
        }
    }

    #[test]
    fn upkeep_drains_case_variant_cluster_into_one_entity_concept_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "Uses [[gcode]] for symbol search.\n");
        seed_source(
            root,
            "src-b",
            "Prefers [[Gcode]]; still [[gcode]] underneath.\n",
        );

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert_eq!(report.pending_before, 2);
        assert_eq!(report.pending_after, 0);
        assert_eq!(report.pages_created, 1);
        assert_eq!(report.clusters.len(), 1);
        let cluster = &report.clusters[0];
        assert_eq!(cluster.action, "created");
        assert_eq!(cluster.target, "gcode");
        assert_eq!(cluster.variants, vec!["gcode", "Gcode"]);
        assert_eq!(cluster.mentions, 3);
        assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
        assert_eq!(
            cluster.page_path.as_deref(),
            Some(Path::new("knowledge/concepts/gcode.md"))
        );

        let page = fs::read_to_string(root.join("knowledge/concepts/gcode.md"))
            .expect("concept page written");
        assert!(page.contains("Gcode"), "aliases carry observed variants");
        assert!(page.contains("entity"), "entity tag rendered: {page}");
        assert!(
            page.contains("knowledge/sources/src-a") && page.contains("knowledge/sources/src-b"),
            "provenance links point at the source digests: {page}"
        );

        assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);

        let index = fs::read_to_string(root.join("_index.md")).expect("index regenerated");
        assert!(index.contains("knowledge/concepts/gcode"), "{index}");

        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("page_created:"), "{log}");
        assert!(log.contains("upkeep_completed:"), "{log}");

        assert!(root.join(REPORT_RELATIVE_PATH).exists());
        assert!(
            report
                .notes
                .iter()
                .any(|note| note.contains("semantic backend unavailable")),
            "missing backend is noted: {:?}",
            report.notes
        );
        assert!(
            root.join(crate::vault::STATE_ROOT)
                .join("research-session.json")
                .try_exists()
                .is_ok_and(|exists| !exists),
            "upkeep must not persist a research checkpoint"
        );
    }

    #[test]
    fn upkeep_created_concept_pages_enter_candidate_quarantine() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "Uses [[gcode]] for symbol search.\n");
        seed_source(root, "src-b", "Prefers [[gcode]] everywhere.\n");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        let page = fs::read_to_string(root.join("knowledge/concepts/gcode.md"))
            .expect("concept page written");
        assert!(
            page.contains("lifecycle: draft") && page.contains("candidate: true"),
            "created concept page starts quarantined: {page}"
        );

        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("candidate_proposed:"), "{log}");

        // Digest backlinks gated the page's creation; they are not
        // corroboration, so the fresh candidate is neither promoted nor
        // discarded by the same run.
        assert!(report.candidates_promoted.is_empty());
        assert!(report.candidates_discarded.is_empty());
    }

    #[test]
    fn govern_candidates_promotes_candidates_with_knowledge_backlinks() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_file(
            root,
            "knowledge/concepts/widget.md",
            "---\ntitle: Widget\nlifecycle: draft\ncandidate: true\n---\n\n# Widget\n\nBody.\n",
        );
        write_file(
            root,
            "knowledge/concepts/gadget.md",
            "---\ntitle: Gadget\n---\n\nPairs with [[Widget]].\n",
        );
        write_file(
            root,
            "knowledge/topics/assembly.md",
            "---\ntitle: Assembly\n---\n\nStarts from a [[Widget]].\n",
        );

        let (promoted, discarded) =
            govern_candidates(root, &scope(), TIMESTAMP).expect("governance pass");

        assert_eq!(
            promoted,
            vec![PathBuf::from("knowledge/concepts/widget.md")]
        );
        assert!(discarded.is_empty());
        let page =
            fs::read_to_string(root.join("knowledge/concepts/widget.md")).expect("read page");
        assert!(!page.contains("candidate: true"), "{page}");
        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("candidate_promoted:"), "{log}");
    }

    #[test]
    fn govern_candidates_discards_orphans_and_keeps_digest_backed_candidates() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_file(
            root,
            "knowledge/concepts/orphan.md",
            "---\ntitle: Orphan\nlifecycle: draft\ncandidate: true\n---\n\n# Orphan\n\nBody.\n",
        );
        write_file(
            root,
            "knowledge/concepts/mentioned.md",
            "---\ntitle: Mentioned\nlifecycle: draft\ncandidate: true\n---\n\n# Mentioned\n\nBody.\n",
        );
        write_file(
            root,
            "knowledge/sources/digest.md",
            "---\ntitle: Digest\n---\n\nStill cites [[Mentioned]].\n",
        );

        let (promoted, discarded) =
            govern_candidates(root, &scope(), TIMESTAMP).expect("governance pass");

        assert!(promoted.is_empty());
        assert_eq!(
            discarded,
            vec![PathBuf::from("knowledge/concepts/orphan.md")]
        );

        // The orphan is archived in place; the digest-backed candidate stays
        // quarantined awaiting knowledge-page corroboration.
        let orphan =
            fs::read_to_string(root.join("knowledge/concepts/orphan.md")).expect("orphan page");
        assert!(orphan.contains("lifecycle: archived"), "{orphan}");
        let mentioned = fs::read_to_string(root.join("knowledge/concepts/mentioned.md"))
            .expect("mentioned page");
        assert!(mentioned.contains("candidate: true"), "{mentioned}");

        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("candidate_discarded:"), "{log}");
        assert!(log.contains("lifecycle_transition:"), "{log}");
    }

    #[test]
    fn upkeep_clusters_targets_mentioned_only_by_compiled_digests() {
        // Regression: convergence must not depend on drain state. A target
        // whose mentioning digests were already reconciled (by a run whose
        // generation failed, or by consumption in another cluster) still gets
        // its entity page; only the drain bookkeeping is pending-scoped.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "Runs on [[PostgreSQL]].\n");
        seed_source(root, "src-b", "Migrated to [[PostgreSQL]] storage.\n");
        SourceManifest::update(root, |manifest| {
            for entry in &mut manifest.entries {
                entry.compile_status = CompileStatus::Compiled;
            }
            Ok(true)
        })
        .expect("mark sources compiled");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert_eq!(report.pending_before, 0);
        assert_eq!(report.pending_after, 0);
        assert!(
            report.reconciled_no_synthesis.is_empty(),
            "compiled digests feeding a cluster are evidence reuse, not a drain change"
        );
        assert_eq!(report.pages_created, 1);
        assert_eq!(report.clusters.len(), 1);
        let cluster = &report.clusters[0];
        assert_eq!(cluster.target, "PostgreSQL");
        assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
        assert_eq!(
            cluster.page_path.as_deref(),
            Some(Path::new("knowledge/concepts/postgresql.md"))
        );
    }

    #[test]
    fn upkeep_skips_path_shaped_targets_while_entity_targets_still_cluster() {
        // Path-shaped targets ([[code/files/foo.md]], [[knowledge/sources/...]])
        // must not form clusters: slugify would flatten '/' into '-' and mint
        // junk pages like knowledge/concepts/code-files-foo-md.md (#17652).
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(
            root,
            "src-a",
            "See [[code/files/foo.md]] and [[FalkorDB]].\n",
        );
        seed_source(
            root,
            "src-b",
            "Also [[code/files/foo.md]] plus [[FalkorDB]].\n",
        );
        // Mentions only a digest-shaped target with no live manifest record:
        // never clusters, reconciles as reviewed-no-synthesis.
        seed_source(root, "src-c", "Digest link [[knowledge/sources/src-zz]].\n");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert_eq!(
            report.clusters.len(),
            1,
            "path-shaped targets must not form clusters"
        );
        let cluster = &report.clusters[0];
        assert_eq!(cluster.target, "FalkorDB");
        assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
        assert_eq!(report.pages_created, 1);
        assert!(
            !root
                .join("knowledge/concepts/code-files-foo-md.md")
                .exists(),
            "path-shaped target must not mint a junk entity page"
        );
        assert!(
            !root
                .join("knowledge/concepts/knowledge-sources-src-zz.md")
                .exists(),
            "digest-shaped target must not mint a junk entity page"
        );
        assert!(root.join("knowledge/concepts/falkordb.md").exists());
        assert_eq!(
            report.reconciled_no_synthesis,
            vec!["src-c"],
            "a digest mentioning only path-shaped targets reconciles as reviewed-no-synthesis"
        );
    }

    #[test]
    fn upkeep_dry_run_leaves_vault_bytes_unchanged() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "Mentions [[gcode]].\n");
        seed_source(root, "src-b", "Mentions [[Gcode]] again.\n");
        let before = snapshot(root);

        let report = run(
            research_scope(root),
            scope(),
            &Options {
                dry_run: true,
                ..Options::default()
            },
            None,
            None,
            TIMESTAMP,
        )
        .expect("dry run");

        assert_eq!(snapshot(root), before, "dry run must not write vault bytes");
        assert_eq!(report.clusters.len(), 1);
        assert_eq!(report.clusters[0].action, "planned_create");
        assert_eq!(report.pending_after, report.pending_before);
        assert!(report.reconciled_no_synthesis.is_empty() || report.pending_after == 2);
    }

    #[test]
    fn upkeep_skips_sources_with_invalid_digest_paths() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "No entity mentions here.\n");
        SourceManifest::update(root, |manifest| {
            manifest.entries.push(pending_record("../bad"));
            Ok(true)
        })
        .expect("seed invalid manifest entry");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run skips bad digest path");

        assert!(
            report
                .notes
                .iter()
                .any(|note| note.contains("skipping source `../bad`")),
            "{:?}",
            report.notes
        );
    }

    #[test]
    fn upkeep_migrates_reserved_instruction_filename_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "No entity mentions here.\n");
        write_file(
            root,
            "knowledge/concepts/claude.md",
            "---\ntitle: \"Claude\"\n---\n\n# Claude\n\nBody.\n",
        );
        // A page the catalog does not regenerate, holding both link forms.
        write_file(
            root,
            "knowledge/topics/tour.md",
            "---\ntitle: \"Tour\"\n---\n\nSee [[knowledge/concepts/claude|Claude]] and \
             [Claude](knowledge/concepts/claude.md).\n",
        );

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert!(
            !root.join("knowledge/concepts/claude.md").exists(),
            "reserved filename must be renamed"
        );
        assert!(root.join("knowledge/concepts/claude-concept.md").exists());
        let migrated = fs::read_to_string(root.join("knowledge/concepts/claude-concept.md"))
            .expect("migrated page");
        assert!(migrated.contains("title: \"Claude\""), "{migrated}");
        let tour = fs::read_to_string(root.join("knowledge/topics/tour.md")).expect("tour");
        assert!(
            tour.contains("[[knowledge/concepts/claude-concept|Claude]]"),
            "{tour}"
        );
        assert!(
            tour.contains("[Claude](knowledge/concepts/claude-concept.md)"),
            "{tour}"
        );
        let index = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("index");
        assert!(
            index.contains("knowledge/concepts/claude-concept"),
            "{index}"
        );
        assert!(
            report
                .notes
                .iter()
                .any(|note| note.contains("claude-concept")),
            "{:?}",
            report.notes
        );
    }

    #[test]
    fn upkeep_reserved_filename_migration_claims_batch_destinations() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "No entity mentions here.\n");
        write_file(
            root,
            "knowledge/concepts/claude.md",
            "---\ntitle: \"Claude\"\n---\n\n# Claude\n",
        );
        write_file(
            root,
            "knowledge/concepts/claude-concept.md",
            "---\ntitle: \"Existing Claude Concept\"\n---\n\n# Existing Claude Concept\n",
        );
        write_file(
            root,
            "knowledge/concepts/agents.md",
            "---\ntitle: \"Agents\"\n---\n\n# Agents\n",
        );

        run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert!(root.join("knowledge/concepts/claude-concept.md").exists());
        assert!(root.join("knowledge/concepts/claude-concept-2.md").exists());
        assert!(root.join("knowledge/concepts/agents-concept.md").exists());
        assert!(!root.join("knowledge/concepts/claude.md").exists());
        assert!(!root.join("knowledge/concepts/agents.md").exists());
        let migrated = fs::read_to_string(root.join("knowledge/concepts/claude-concept-2.md"))
            .expect("migrated claude page");
        assert!(migrated.contains("title: \"Claude\""), "{migrated}");
        let existing = fs::read_to_string(root.join("knowledge/concepts/claude-concept.md"))
            .expect("existing claude concept page");
        assert!(existing.contains("Existing Claude Concept"), "{existing}");
    }

    #[test]
    fn upkeep_dry_run_only_reports_reserved_filename_migration() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "No entity mentions here.\n");
        write_file(
            root,
            "knowledge/concepts/gemini.md",
            "---\ntitle: \"Gemini\"\n---\n\n# Gemini\n\nBody.\n",
        );
        let before = snapshot(root);

        let report = run(
            research_scope(root),
            scope(),
            &Options {
                dry_run: true,
                ..Options::default()
            },
            None,
            None,
            TIMESTAMP,
        )
        .expect("dry run");

        assert_eq!(snapshot(root), before, "dry run must not write vault bytes");
        assert!(
            report
                .notes
                .iter()
                .any(|note| note.contains("would rename") && note.contains("gemini")),
            "{:?}",
            report.notes
        );
    }

    #[test]
    fn upkeep_leaves_alias_resolved_mentions_to_the_existing_entity_page() {
        // Case-insensitive link resolution (stem/title/alias) already binds
        // [[GCode]]/[[gcode]] to the existing aliased page, so no cluster
        // forms, no duplicate concept page is created, and the sources
        // reconcile as reviewed-without-synthesis.
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "About [[GCode]].\n");
        seed_source(root, "src-b", "More on [[gcode]].\n");
        write_file(
            root,
            "knowledge/concepts/code-index.md",
            "---\ntitle: Code Index\naliases:\n  - Gcode\n---\n\n# Code Index\n\nExisting body.\n",
        );

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert!(report.clusters.is_empty());
        assert_eq!(report.pages_created, 0);
        assert_eq!(report.pages_updated, 0);
        assert!(
            !root.join("knowledge/concepts/gcode.md").exists(),
            "no duplicate page created for an already-covered entity"
        );
        assert_eq!(
            report.reconciled_no_synthesis,
            vec!["src-a".to_string(), "src-b".to_string()]
        );
        assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);
    }

    #[test]
    fn exact_alias_match_layer_updates_without_touching_the_semantic_probe() {
        // Defense-in-depth for the first update-over-create layer: if a
        // cluster key ever reaches disposition while an existing page carries
        // that key, upkeep updates the page and skips the near-dup probe.
        let cluster = Cluster {
            key: "gcode".to_string(),
            primary: "Gcode".to_string(),
            variants: vec!["Gcode".to_string()],
            mentions: 2,
            source_indices: Vec::new(),
        };
        let existing = vec![(
            PathBuf::from("knowledge/concepts/code-index.md"),
            BTreeSet::from(["code index".to_string(), "gcode".to_string()]),
        )];
        let mut semantic: Option<SemanticProbe<'_>> = None;

        match resolve_page_disposition(&cluster, &existing, &mut semantic) {
            PageDisposition::Update {
                page,
                near_duplicate,
            } => {
                assert_eq!(page, PathBuf::from("knowledge/concepts/code-index.md"));
                assert!(near_duplicate.is_none());
            }
            PageDisposition::Create { .. } => panic!("exact match must update, not create"),
        }
    }

    #[test]
    fn upkeep_near_duplicate_hit_chooses_update_over_create() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "The [[Gobby Daemon]] never sleeps.\n");
        seed_source(root, "src-b", "Restart the [[gobby daemon]] nightly.\n");
        write_file(
            root,
            "knowledge/concepts/long-running-service.md",
            "---\ntitle: Long Running Service\n---\n\n# Long Running Service\n\nBody.\n",
        );

        let mut backend = FixedSemanticBackend {
            hits: vec![semantic_hit(
                "knowledge/concepts/long-running-service.md",
                0.95,
            )],
        };
        let probe = SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("upkeep-test"),
        };
        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            Some(probe),
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        let cluster = &report.clusters[0];
        assert_eq!(cluster.action, "updated");
        assert_eq!(
            cluster.page_path.as_deref(),
            Some(Path::new("knowledge/concepts/long-running-service.md"))
        );
        let near = cluster.near_duplicate.as_ref().expect("near-dup recorded");
        assert_eq!(
            near.page,
            PathBuf::from("knowledge/concepts/long-running-service.md")
        );
        assert!((near.score - 0.95).abs() < f64::EPSILON);
        assert!(!cluster.review_flag);
        assert!(
            !root.join("knowledge/concepts/gobby-daemon.md").exists(),
            "near-duplicate update must not create a sibling page"
        );

        // The would-be candidate resolved into an existing page: the audit
        // trail records a merge, and the target page is not quarantined.
        let log = fs::read_to_string(root.join("log.md")).expect("log written");
        assert!(log.contains("candidate_merged:"), "{log}");
        let target = fs::read_to_string(root.join("knowledge/concepts/long-running-service.md"))
            .expect("target page");
        assert!(
            !target.contains("candidate: true"),
            "merge target must not enter quarantine: {target}"
        );
    }

    #[test]
    fn upkeep_review_band_creates_page_flagged_for_review() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "The [[Gobby Daemon]] never sleeps.\n");
        seed_source(root, "src-b", "Restart the [[Gobby Daemon]] nightly.\n");
        write_file(
            root,
            "knowledge/concepts/long-running-service.md",
            "---\ntitle: Long Running Service\n---\n\n# Long Running Service\n\nBody.\n",
        );

        let mut backend = FixedSemanticBackend {
            hits: vec![semantic_hit(
                "knowledge/concepts/long-running-service.md",
                0.85,
            )],
        };
        let probe = SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("upkeep-test"),
        };
        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            Some(probe),
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        let cluster = &report.clusters[0];
        assert_eq!(cluster.action, "created");
        assert!(cluster.review_flag, "review band flags the new page");
        assert!(cluster.near_duplicate.is_some());
        assert_eq!(
            cluster.page_path.as_deref(),
            Some(Path::new("knowledge/concepts/gobby-daemon.md"))
        );
    }

    #[test]
    fn upkeep_respects_page_and_source_budgets() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // Cluster "alpha": three sources; cluster "beta"/"gamma": one source each.
        seed_source(root, "src-a1", "On [[alpha]].\n");
        seed_source(root, "src-a2", "On [[alpha]].\n");
        seed_source(root, "src-a3", "On [[alpha]].\n");
        seed_source(root, "src-b", "On [[beta]] and [[beta]].\n");
        seed_source(root, "src-c", "On [[gamma]] and [[gamma]].\n");

        let report = run(
            research_scope(root),
            scope(),
            &Options {
                max_pages: 1,
                max_sources_per_page: 2,
                ..Options::default()
            },
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        // "alpha" wins the budget slot (3 mentions beats 2).
        assert_eq!(report.clusters.len(), 1);
        let cluster = &report.clusters[0];
        assert_eq!(cluster.target, "alpha");
        assert_eq!(cluster.source_ids, vec!["src-a1", "src-a2"]);
        assert_eq!(cluster.sources_truncated, 1);
        assert_eq!(report.skipped_over_budget.len(), 2);

        // Budget-skipped and truncated sources stay pending for the next run.
        assert_eq!(compile_status_of(root, "src-a1"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-a2"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-a3"), CompileStatus::Pending);
        assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Pending);
        assert_eq!(compile_status_of(root, "src-c"), CompileStatus::Pending);
        assert!(report.reconciled_no_synthesis.is_empty());
    }

    #[test]
    fn time_budget_checkpoints_completed_clusters_and_defers_pending_work() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a1", "On [[alpha]].\n");
        seed_source(root, "src-a2", "On [[alpha]].\n");
        seed_source(root, "src-b1", "On [[beta]].\n");
        seed_source(root, "src-b2", "On [[beta]].\n");

        let started_at = Instant::now();
        let clock_calls = std::cell::Cell::new(0_usize);
        let report = run_with_clock(
            research_scope(root),
            scope(),
            &Options {
                time_budget_seconds: Some(1320),
                ..Options::default()
            },
            None,
            None,
            TIMESTAMP,
            || {
                let call = clock_calls.get();
                clock_calls.set(call + 1);
                if call < 2 {
                    started_at
                } else {
                    started_at + Duration::from_secs(111)
                }
            },
        )
        .expect("time-budgeted upkeep run");

        assert_eq!(report.clusters.len(), 1);
        assert_eq!(report.clusters[0].target, "alpha");
        assert!(report.budget_exhausted);
        assert_eq!(report.deferred_clusters.len(), 1);
        assert_eq!(report.deferred_clusters[0].target, "beta");
        assert_eq!(compile_status_of(root, "src-a1"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-a2"), CompileStatus::Compiled);
        assert_eq!(compile_status_of(root, "src-b1"), CompileStatus::Pending);
        assert_eq!(compile_status_of(root, "src-b2"), CompileStatus::Pending);

        let report_path = root.join(REPORT_RELATIVE_PATH);
        let checkpoint: serde_json::Value = serde_json::from_str(
            &fs::read_to_string(&report_path).expect("read upkeep checkpoint"),
        )
        .expect("parse upkeep checkpoint");
        assert_eq!(checkpoint["clusters"].as_array().map(Vec::len), Some(1));
        assert_eq!(checkpoint["budget_exhausted"], true);
        assert_eq!(checkpoint["deferred_clusters"][0]["target"], "beta");
        assert!(!report_path.with_extension("json.tmp").exists());
    }

    #[test]
    fn upkeep_reconciles_unclustered_sources_as_reviewed_no_synthesis() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "On [[gcode]].\n");
        seed_source(root, "src-b", "On [[gcode]] too.\n");
        // Below min_mentions and no unresolved links at all: both reconcile.
        seed_source(root, "src-solo", "One [[Solo Target]] mention only.\n");
        seed_source(root, "src-plain", "No links here at all.\n");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        assert_eq!(
            report.reconciled_no_synthesis,
            vec!["src-plain".to_string(), "src-solo".to_string()]
        );
        assert_eq!(compile_status_of(root, "src-solo"), CompileStatus::Compiled);
        assert_eq!(
            compile_status_of(root, "src-plain"),
            CompileStatus::Compiled
        );
        assert_eq!(report.pending_after, 0);
    }

    #[test]
    fn upkeep_records_per_page_failure_and_continues() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(root, "src-a", "On [[alpha]] and [[alpha]].\n");
        seed_source(root, "src-b", "On [[beta]] and [[beta]].\n");
        // Break cluster "alpha": its only raw source vanishes before the run.
        fs::remove_file(root.join("raw/src-a.md")).expect("remove raw source");

        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run survives per-page failure");

        assert_eq!(report.failures, 1);
        assert_eq!(report.pages_created, 1);
        let failed = report
            .clusters
            .iter()
            .find(|cluster| cluster.action == "failed")
            .expect("failed cluster recorded");
        assert_eq!(failed.target, "alpha");
        assert!(
            failed
                .error
                .as_deref()
                .is_some_and(|error| !error.is_empty())
        );
        let created = report
            .clusters
            .iter()
            .find(|cluster| cluster.action == "created")
            .expect("other cluster still processed");
        assert_eq!(created.target, "beta");

        // The failed cluster's source stays pending; the drained one flips.
        assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Pending);
        assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);
    }

    #[test]
    fn upkeep_heals_unresolved_concept_links_to_plain_text() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // Two mentions cluster and synthesize (link kept resolved); the lone
        // mention stays unresolved and must be unwrapped to plain text with its
        // entity recorded for future runs.
        seed_source(root, "src-a", "Built on [[PostgreSQL]].\n");
        seed_source(root, "src-b", "Also uses [[PostgreSQL]].\n");
        seed_source(root, "src-solo", "## Connections\n\n- [[Ephemeral Idea]]\n");

        run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");

        let clustered = fs::read_to_string(root.join("knowledge/sources/src-a.md"))
            .expect("read clustered digest");
        assert!(
            clustered.contains("[[PostgreSQL]]"),
            "synthesized concept link is kept resolved: {clustered}"
        );
        assert!(root.join("knowledge/concepts/postgresql.md").exists());

        let solo = fs::read_to_string(root.join("knowledge/sources/src-solo.md"))
            .expect("read solo digest");
        assert!(
            !solo.contains("[[Ephemeral Idea]]"),
            "unresolved singleton link is unwrapped: {solo}"
        );
        assert!(
            solo.contains("- Ephemeral Idea"),
            "unwrapped mention stays as plain text: {solo}"
        );

        let registry =
            fs::read_to_string(root.join(HEALED_MENTIONS_PATH)).expect("read healed registry");
        assert!(
            registry.contains("Ephemeral Idea"),
            "healed entity recorded for later clustering: {registry}"
        );
    }

    #[test]
    fn upkeep_healed_mentions_seed_a_later_cluster() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // Run 1: a lone mention is healed to plain text and recorded.
        seed_source(root, "src-a", "First note on [[Emerging Topic]].\n");
        let first = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("first upkeep run");
        assert_eq!(first.pages_created, 0, "a singleton does not synthesize");
        assert!(
            !fs::read_to_string(root.join("knowledge/sources/src-a.md"))
                .expect("read src-a")
                .contains("[[Emerging Topic]]"),
            "singleton healed to plain text on the first run"
        );

        // A second digest mentions the same concept on a later run.
        seed_source(root, "src-b", "Second note on [[Emerging Topic]].\n");
        let report = run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("second upkeep run");

        // The healed mention from run 1 plus the fresh mention now cluster, so
        // decoupling the work-queue from the red-links preserves discovery.
        assert_eq!(report.pages_created, 1);
        let cluster = report
            .clusters
            .iter()
            .find(|outcome| outcome.key == "emerging topic")
            .expect("emerging topic cluster");
        assert_eq!(cluster.mentions, 2);
        assert!(cluster.source_ids.contains(&"src-a".to_string()));
        assert!(cluster.source_ids.contains(&"src-b".to_string()));
    }

    #[test]
    fn upkeep_heal_skips_concept_links_inside_code_spans() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        seed_source(
            root,
            "src-a",
            "Mentions `[[Not A Link]]` inline and [[Real Mention]] outside.\n",
        );
        run(
            research_scope(root),
            scope(),
            &Options::default(),
            None,
            None,
            TIMESTAMP,
        )
        .expect("upkeep run");
        let digest =
            fs::read_to_string(root.join("knowledge/sources/src-a.md")).expect("read digest");
        assert!(
            digest.contains("`[[Not A Link]]`"),
            "code-span content is left verbatim: {digest}"
        );
        assert!(
            !digest.contains("[[Real Mention]]"),
            "real unresolved link is unwrapped: {digest}"
        );
    }
}
