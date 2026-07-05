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

use serde::Serialize;

use crate::compile::{CompileRequest, WikiCompileOptions, compile_to_wiki_with_options, select};
use crate::explainer::ExplainerGenerator;
use crate::links::canonical_target_key;
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
    /// Candidate clusters beyond the page budget; their sources stay pending.
    pub skipped_over_budget: Vec<SkippedCluster>,
    /// Pending sources reviewed without joining any cluster, flipped to
    /// `compiled` so the queue drains.
    pub reconciled_no_synthesis: Vec<String>,
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
    mut semantic: Option<SemanticProbe<'_>>,
    mut generator: Option<ExplainerGenerator<'_>>,
    timestamp: &str,
) -> Result<UpkeepReport, WikiError> {
    let vault_root = research_scope.root().to_path_buf();
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
        digest_records.insert(paths::derived_markdown_path(record)?, index);
    }

    let lint_report = lint::run(&vault_root, scope.clone())?;
    let mut accumulators: BTreeMap<String, ClusterAccumulator> = BTreeMap::new();
    for issue in &lint_report.broken_links {
        let Some(&source_index) = digest_records.get(&issue.path) else {
            continue;
        };
        let key = canonical_target_key(&issue.target);
        if key.is_empty() {
            continue;
        }
        let accumulator = accumulators.entry(key).or_default();
        accumulator.mentions += 1;
        *accumulator
            .variant_counts
            .entry(issue.target.clone())
            .or_default() += 1;
        accumulator.source_indices.insert(source_index);
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

    let mut notes: Vec<String> = Vec::new();
    let mut clusters: Vec<ClusterOutcome> = Vec::new();
    let mut pages_created = 0usize;
    let mut pages_updated = 0usize;
    let mut failures = 0usize;

    for cluster in &candidates[..processed_count] {
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
            clusters.push(outcome);
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
                outcome.page_path = Some(page_path);
                outcome.action = match write_kind {
                    PageWriteKind::Created => {
                        pages_created += 1;
                        "created".to_string()
                    }
                    PageWriteKind::Overwritten => {
                        pages_updated += 1;
                        "updated".to_string()
                    }
                };
            }
            Err(error) => {
                // Per-page failure: record it and keep draining. The cluster's
                // sources stay pending for the next run.
                failures += 1;
                outcome.action = "failed".to_string();
                outcome.error = Some(error.to_string());
            }
        }
        clusters.push(outcome);
    }

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

    let report = UpkeepReport {
        command: "upkeep",
        scope: scope.clone(),
        timestamp: timestamp.to_string(),
        dry_run: options.dry_run,
        max_pages: options.max_pages,
        min_mentions: options.min_mentions,
        max_sources_per_page: options.max_sources_per_page,
        pending_before,
        pending_after,
        pages_created,
        pages_updated,
        failures,
        clusters,
        skipped_over_budget,
        reconciled_no_synthesis,
        notes,
    };

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
                    "created={} updated={} failed={} reconciled={} pending_after={}",
                    report.pages_created,
                    report.pages_updated,
                    report.failures,
                    report.reconciled_no_synthesis.len(),
                    report.pending_after,
                ),
                artifacts: vec![PathBuf::from(REPORT_RELATIVE_PATH)],
            },
        )?;
    }

    Ok(report)
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

/// Case-folded lookup keys under which an existing page counts as the cluster
/// target: relative path (sans extension), file stem, frontmatter title, and
/// every frontmatter alias.
fn page_match_keys(page: &lint::WikiPage) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    if let Some(relative) = page.relative_path.with_extension("").to_str() {
        keys.insert(canonical_target_key(relative));
    }
    if let Some(stem) = page
        .relative_path
        .file_stem()
        .and_then(|stem| stem.to_str())
    {
        keys.insert(canonical_target_key(stem));
    }
    if let Some(title) = &page.parsed.frontmatter.title {
        keys.insert(canonical_target_key(title));
    }
    for alias in &page.parsed.frontmatter.aliases {
        keys.insert(canonical_target_key(alias));
    }
    keys.remove("");
    keys
}

fn write_report(vault_root: &Path, report: &UpkeepReport) -> Result<(), WikiError> {
    let path = vault_root.join(REPORT_RELATIVE_PATH);
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
    fs::write(&path, json).map_err(|error| WikiError::Io {
        action: "write upkeep report",
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
    for note in &report.notes {
        text.push_str("Note: ");
        text.push_str(note);
        text.push('\n');
    }
    text
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
}
