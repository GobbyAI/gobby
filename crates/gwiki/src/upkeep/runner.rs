use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Serialize;

use super::reserved_pages::migrate_reserved_pages;
use super::{
    ClusterOutcome, NearDuplicateMatch, Options, REPORT_RELATIVE_PATH, SemanticProbe,
    SkippedCluster, UpkeepReport, archive_long_stale_pages, archive_unworthy_concepts,
    collect_upkeep_pages, find_unworthy_concepts, govern_candidates, write_report,
};
use crate::compile::{CompileRequest, WikiCompileOptions, compile_to_wiki_with_options, select};
use crate::explainer::ExplainerGenerator;
use crate::links::{
    LinkKind, canonical_target_key, extract_links, is_concept_worthy, is_entity_key,
};
use crate::lint::page_match_keys;
use crate::search::semantic::SemanticSearchRequest;
use crate::session::{ResearchScope, ResearchSession};
use crate::sources::{CompileStatus, SourceManifest, SourceRecord};
use crate::support::text::degradation_label;
use crate::synthesis::{ArticleKind, PageWriteKind};
use crate::{ScopeIdentity, WikiError, catalog, lint, paths};

/// Cosine similarity at or above which upkeep updates the matched page
/// instead of creating a new one.
const NEAR_DUPLICATE_UPDATE_COSINE: f64 = 0.90;
/// Cosine similarity band lower bound: a hit in
/// [`NEAR_DUPLICATE_REVIEW_COSINE`, [`NEAR_DUPLICATE_UPDATE_COSINE`]) still
/// creates a page but flags the cluster for human review.
const NEAR_DUPLICATE_REVIEW_COSINE: f64 = 0.80;
/// Semantic hits requested per near-duplicate probe.
const NEAR_DUPLICATE_SEARCH_LIMIT: usize = 8;
/// Reserve roughly 20 minutes for tail work; below this threshold the first
/// cluster check defers every remaining cluster instead of starting new work.
const MIN_CLUSTER_REMAINING_SECONDS: u64 = 1210;

/// Durable registry of concept mentions the heal pass unwrapped from digest
/// bodies. It preserves the concept-synthesis work-queue signal across runs so
/// unwrapping a red-link to plain text does not lose the mention (#17703).
pub(super) const HEALED_MENTIONS_PATH: &str = "meta/upkeep/concept-mentions.json";
/// Frontmatter tag marking entity concept pages synthesized by upkeep.
const ENTITY_TAG: &str = "entity";

/// Accumulated mentions for one case-folded unresolved target.
#[derive(Default)]
struct ClusterAccumulator {
    mentions: usize,
    variant_counts: BTreeMap<String, usize>,
    source_indices: BTreeSet<usize>,
}

pub(super) struct Cluster {
    pub(super) key: String,
    pub(super) primary: String,
    pub(super) variants: Vec<String>,
    pub(super) mentions: usize,
    pub(super) source_indices: Vec<usize>,
}

/// How the update-over-create layers resolved for one cluster.
pub(super) enum PageDisposition {
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

pub(super) fn run_with_clock(
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
        if !is_concept_worthy(&key) {
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
            if !is_concept_worthy(&key) || !counted_mentions.insert((source_index, key.clone())) {
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
    let pages = collect_upkeep_pages(&vault_root)?;
    let existing_pages: Vec<(PathBuf, BTreeSet<String>)> = pages
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
        unworthy_archived: Vec::new(),
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
    // Discover before candidate governance so dry-run and applied reports
    // agree even when governance archives an orphaned junk candidate. The
    // lifecycle transition itself remains a post-govern step below.
    let unworthy_concepts = find_unworthy_concepts(&pages);
    let (candidates_promoted, candidates_discarded) = if options.dry_run {
        (Vec::new(), Vec::new())
    } else {
        govern_candidates(&vault_root, &scope, timestamp)?
    };

    let unworthy_archived =
        archive_unworthy_concepts(&vault_root, &scope, options.dry_run, unworthy_concepts)?;

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
    report.unworthy_archived = unworthy_archived;
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
                    "created={} updated={} failed={} archived={} unworthy_archived={} reconciled={} pending_after={}",
                    report.pages_created,
                    report.pages_updated,
                    report.failures,
                    report.archived_pages.len(),
                    report.unworthy_archived.len(),
                    report.reconciled_no_synthesis.len(),
                    report.pending_after,
                ),
                artifacts: vec![PathBuf::from(REPORT_RELATIVE_PATH)],
            },
        )?;
    }

    Ok(report)
}

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

pub(super) fn resolve_page_disposition(
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
