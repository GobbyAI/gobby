use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;

use crate::frontmatter::parse_frontmatter;
use crate::links::{canonical_target_key, is_concept_worthy};
use crate::paths::derived_markdown_path;
use crate::provenance::ProvenanceGraph;
use crate::search::SearchScope;
use crate::search::semantic::SemanticSearchBackend;
use crate::sources::SourceManifest;
use crate::support::scope::scope_includes_page;
use crate::support::services::RuntimeServices;
use crate::{ScopeIdentity, WikiError, audit, health, lint};

mod proposals;
mod semantic;
#[cfg(test)]
mod tests;

use proposals::{artifacts, persist_report, suggested_patch_diffs, suggested_tasks};
pub(crate) use semantic::load_distinct_pairs;
use semantic::{SemanticGapScan, semantic_gap_scan, semantic_gaps_check};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Options {
    pub require_postgres_index: bool,
    pub shared_code_graph_available: bool,
    pub semantic_available: bool,
    pub model_available: bool,
}

impl Options {
    pub fn offline() -> Self {
        Self {
            require_postgres_index: false,
            shared_code_graph_available: false,
            semantic_available: false,
            model_available: false,
        }
    }

    /// Build options from live service probes. PostgreSQL stays a hard
    /// requirement for probed runs; the optional flags reflect what the
    /// resolver actually found.
    pub(crate) fn probed(services: &RuntimeServices, model_available: bool) -> Self {
        Self {
            require_postgres_index: true,
            shared_code_graph_available: services.shared_code_graph_available(),
            semantic_available: services.semantic_available(),
            model_available,
        }
    }
}

impl Default for Options {
    fn default() -> Self {
        Self {
            require_postgres_index: true,
            ..Self::offline()
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ProposalsReport {
    pub scope: ScopeIdentity,
    pub checks: Vec<CheckReport>,
    pub suggested_tasks: Vec<SuggestedTask>,
    pub suggested_patch_diffs: Vec<SuggestedPatchDiff>,
    pub artifacts: LibrarianArtifacts,
    pub dependency_classification: DependencyClassification,
}

impl ProposalsReport {
    #[cfg(test)]
    fn check(&self, name: &str) -> &CheckReport {
        self.checks
            .iter()
            .find(|check| check.name == name)
            .unwrap_or_else(|| panic!("missing check {name}"))
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckReport {
    pub name: &'static str,
    pub available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    pub items: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SuggestedTask {
    pub title: String,
    pub description: String,
    pub paths: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SuggestedPatchDiff {
    pub path: PathBuf,
    pub summary: String,
    pub diff: String,
    pub applies_to_canonical_content: bool,
    pub requires_acceptance: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LibrarianArtifacts {
    pub proposals_json: PathBuf,
    pub proposals_markdown: PathBuf,
    pub audit_annotations_json: PathBuf,
    pub stale_pages_json: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
pub struct DependencyClassification {
    pub hard: Vec<&'static str>,
    pub optional: Vec<&'static str>,
    pub multimodal: &'static str,
}

/// Live semantic search access for near-duplicate detection.
pub struct SemanticProbe<'a> {
    pub backend: &'a mut dyn SemanticSearchBackend,
    pub search_scope: SearchScope,
}

pub fn run(
    vault_root: &Path,
    scope: ScopeIdentity,
    options: Options,
    semantic: Option<SemanticProbe<'_>>,
) -> Result<ProposalsReport, WikiError> {
    let _postgres_index = if options.require_postgres_index {
        Some(crate::support::postgres::require_postgres_index(
            "gwiki librarian",
        )?)
    } else {
        None
    };
    let health_report = health::inspect(vault_root, scope.clone())?;
    let audit_report =
        audit::run_with_options(vault_root, scope.clone(), audit::AuditOptions::from_env())?;
    let lint_report = lint::run(vault_root, scope.clone())?;
    let pages = lint::collect_pages(vault_root)?
        .into_iter()
        .filter(|page| scope_includes_page(&scope, &page.relative_path))
        .collect::<Vec<_>>();
    let provenance = ProvenanceGraph::load_from_vault(vault_root)?;

    let stale_pages = health_report.stale_pages.clone();
    let missing_citations = unique_paths(
        audit_report
            .unsupported_claims
            .iter()
            .map(|claim| claim.path.clone()),
    );
    // Broken digest links are mostly upkeep's convergence fuel, not repair
    // debt — proposing repair for them invites mass de-linking that would
    // destroy upkeep's work queue (#17640). Classify them the way upkeep
    // consumes them and only forward genuinely dead links.
    let manifest = SourceManifest::read(vault_root)?;
    let mut digest_pages = BTreeSet::new();
    let mut manifest_ids = BTreeSet::new();
    for record in &manifest.entries {
        digest_pages.insert(derived_markdown_path(record)?);
        manifest_ids.insert(record.id.to_lowercase());
    }
    let broken_link_scan =
        classify_broken_links(&lint_report.broken_links, &digest_pages, &manifest_ids);
    let broken_links = broken_link_scan.repair_pages.clone();
    let weak_provenance = weak_provenance_pages(&pages, &provenance);
    let outdated_codewiki = if options.shared_code_graph_available {
        outdated_codewiki_pages(&pages)
    } else {
        Vec::new()
    };
    let semantic_scan = if options.semantic_available {
        match semantic {
            Some(probe) => {
                let distinct_pairs = load_distinct_pairs(vault_root);
                semantic_gap_scan(&pages, &lint_report.broken_links, probe, &distinct_pairs)
            }
            None => SemanticGapScan::failed(
                "semantic services resolved as available but no semantic backend was supplied",
            ),
        }
    } else {
        SemanticGapScan::unavailable()
    };

    let mut checks = vec![
        available_check("stale_pages", stale_pages.clone()),
        available_check("missing_citations", missing_citations.clone()),
        broken_links_check(&broken_link_scan),
        available_check("weak_provenance", weak_provenance.clone()),
    ];
    checks.push(optional_check(
        "outdated_codewiki",
        options.shared_code_graph_available,
        "shared code graph is unavailable; skipped outdated codewiki detection",
        outdated_codewiki.clone(),
    ));
    checks.push(semantic_gaps_check(&options, &semantic_scan));
    checks.push(optional_check(
        "patch_suggestions",
        options.model_available,
        "model provider is unavailable; emitted deterministic task proposals only",
        Vec::new(),
    ));

    let suggested_tasks = suggested_tasks(
        &health_report.uncited_sources,
        &stale_pages,
        &missing_citations,
        &broken_links,
        &weak_provenance,
        &outdated_codewiki,
        &semantic_scan,
    );
    let suggested_patch_diffs = suggested_patch_diffs(&stale_pages, &missing_citations);
    let artifacts = artifacts();
    let report = ProposalsReport {
        scope,
        checks,
        suggested_tasks,
        suggested_patch_diffs,
        artifacts,
        dependency_classification: DependencyClassification {
            hard: vec!["PostgreSQL index", "vault"],
            optional: vec![
                "FalkorDB/shared code graph",
                "Qdrant+embeddings",
                "model provider",
            ],
            multimodal: "none; transcription, vision, and video providers are not used",
        },
    };

    promote_reviewed_lifecycle(vault_root, &report, &pages)?;
    persist_report(vault_root, &report)?;
    Ok(report)
}

/// A clean lint+librarian pass promotes `draft` pages to `reviewed`.
///
/// Promotion is conservative: it requires every page-hygiene check to have
/// actually run (`patch_suggestions` availability only gates patch output,
/// not page hygiene) and only touches pages that already opted into the
/// lifecycle by carrying a `lifecycle` field — legacy pages without one are
/// never mass-stamped. Page content is untouched; only the lifecycle
/// frontmatter key changes, and each promotion appends a `log.md` entry.
fn promote_reviewed_lifecycle(
    vault_root: &Path,
    report: &ProposalsReport,
    pages: &[lint::WikiPage],
) -> Result<(), WikiError> {
    use crate::frontmatter::WikiLifecycle;

    let hygiene_complete = report
        .checks
        .iter()
        .all(|check| check.available || check.name == "patch_suggestions");
    if !hygiene_complete {
        return Ok(());
    }
    let implicated: BTreeSet<&Path> = report
        .checks
        .iter()
        .flat_map(|check| check.items.iter().map(PathBuf::as_path))
        .collect();
    for page in pages {
        if page.parsed.frontmatter.lifecycle != Some(WikiLifecycle::Draft) {
            continue;
        }
        if implicated.contains(page.relative_path.as_path()) {
            continue;
        }
        crate::lifecycle::apply_lifecycle_transition(
            vault_root,
            &report.scope,
            &page.relative_path,
            WikiLifecycle::Reviewed,
            "librarian: clean lint+librarian pass",
        )?;
    }
    Ok(())
}

pub fn render_text(report: &ProposalsReport) -> String {
    let mut text = format!("Librarian proposals\nScope: {}\n", report.scope);
    for check in &report.checks {
        let status = if check.available {
            "available"
        } else {
            "unavailable"
        };
        text.push_str(&format!(
            "\n## {} ({status})\n{} item(s)\n",
            check.name,
            check.items.len()
        ));
        if let Some(note) = &check.note {
            text.push_str(note);
            text.push('\n');
        }
        for path in &check.items {
            text.push_str("- ");
            text.push_str(&path.display().to_string());
            text.push('\n');
        }
    }
    text.push_str("\n## Suggested tasks\n");
    for task in &report.suggested_tasks {
        text.push_str("- ");
        text.push_str(&task.title);
        text.push('\n');
    }
    text
}

fn available_check(name: &'static str, items: Vec<PathBuf>) -> CheckReport {
    CheckReport {
        name,
        available: true,
        note: None,
        items,
    }
}

fn unique_paths(paths: impl Iterator<Item = PathBuf>) -> Vec<PathBuf> {
    paths.collect::<BTreeSet<_>>().into_iter().collect()
}

fn optional_check(
    name: &'static str,
    available: bool,
    unavailable_note: &'static str,
    items: Vec<PathBuf>,
) -> CheckReport {
    CheckReport {
        name,
        available,
        note: (!available).then(|| unavailable_note.to_string()),
        items: if available { items } else { Vec::new() },
    }
}

/// Split of lint's broken links into genuinely dead links (repair debt) and
/// pending mentions that upkeep or compile converge on without repair
/// (#17640).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct BrokenLinkScan {
    /// Pages carrying at least one genuinely dead link.
    repair_pages: Vec<PathBuf>,
    /// Entity targets with enough digest mentions for an upkeep cluster.
    pending_clusters: usize,
    /// Digest mentions of entity targets still below the cluster threshold.
    pending_singleton_mentions: usize,
    /// Links to registered source digests that are not compiled yet.
    pending_compile_mentions: usize,
}

impl BrokenLinkScan {
    fn pending_note(&self) -> Option<String> {
        (self.pending_clusters > 0
            || self.pending_singleton_mentions > 0
            || self.pending_compile_mentions > 0)
            .then(|| {
                format!(
                    "excluded self-healing links — pending synthesis: {} cluster(s), \
                     {} singleton mention(s); pending compile: {} digest link(s)",
                    self.pending_clusters,
                    self.pending_singleton_mentions,
                    self.pending_compile_mentions
                )
            })
    }
}

fn broken_links_check(scan: &BrokenLinkScan) -> CheckReport {
    let mut check = available_check("broken_links", scan.repair_pages.clone());
    check.note = scan.pending_note();
    check
}

/// Manifest id behind a `knowledge/sources/...` target key, if the key is
/// digest-shaped.
fn source_digest_id(key: &str) -> Option<&str> {
    let rest = key.strip_prefix("knowledge/sources/")?;
    let id = rest.strip_suffix(".md").unwrap_or(rest);
    (!id.is_empty() && !id.contains('/')).then_some(id)
}

/// Classify lint's broken links the way upkeep will consume them: entity
/// mentions on digest pages seed upkeep clusters (`upkeep::run` counts the
/// same mentions), so they — and every other mention of a target some digest
/// sustains — are self-healing synthesis fuel, not repair debt. Links to
/// registered but uncompiled source digests materialize on compile. Only
/// genuinely dead links remain: purged or never-registered digest targets,
/// path-shaped targets no entity page can satisfy, and entity mentions no
/// digest ever made.
fn classify_broken_links(
    broken_links: &[lint::LinkIssue],
    digest_pages: &BTreeSet<PathBuf>,
    manifest_ids: &BTreeSet<String>,
) -> BrokenLinkScan {
    let mut digest_mentions: BTreeMap<String, usize> = BTreeMap::new();
    for issue in broken_links {
        if !digest_pages.contains(&issue.path) {
            continue;
        }
        let key = canonical_target_key(&issue.target);
        if is_concept_worthy(&key) {
            *digest_mentions.entry(key).or_default() += 1;
        }
    }

    let mut scan = BrokenLinkScan::default();
    let mut repair_pages: BTreeSet<PathBuf> = BTreeSet::new();
    for issue in broken_links {
        let key = canonical_target_key(&issue.target);
        if let Some(id) = source_digest_id(&key) {
            if manifest_ids.contains(id) {
                scan.pending_compile_mentions += 1;
            } else {
                repair_pages.insert(issue.path.clone());
            }
            continue;
        }
        if is_concept_worthy(&key) && digest_mentions.contains_key(&key) {
            continue;
        }
        repair_pages.insert(issue.path.clone());
    }

    for mentions in digest_mentions.values() {
        if *mentions >= crate::upkeep::DEFAULT_MIN_MENTIONS {
            scan.pending_clusters += 1;
        } else {
            scan.pending_singleton_mentions += mentions;
        }
    }
    scan.repair_pages = repair_pages.into_iter().collect();
    scan
}

fn weak_provenance_pages(pages: &[lint::WikiPage], provenance: &ProvenanceGraph) -> Vec<PathBuf> {
    let mut paths = pages
        .iter()
        .filter(|page| page_is_codewiki(page))
        .filter(|page| !page_records_provenance(page))
        .filter(|page| !provenance_mentions_page(provenance, &page.relative_path))
        .map(|page| page.relative_path.clone())
        .collect::<Vec<_>>();
    paths.sort();
    paths
}

/// Codewiki pages stamp their own provenance in frontmatter — the source files
/// each page was generated from. A non-empty `provenance` entry IS
/// source-to-section provenance by construction; only pages missing it fall
/// back to the ingest provenance graph (#17781). An empty list (`provenance:
/// []`) records nothing and stays weak.
fn page_records_provenance(page: &lint::WikiPage) -> bool {
    match &page.parsed.frontmatter.provenance {
        Some(Value::Array(entries)) => !entries.is_empty(),
        Some(Value::Null) | None => false,
        Some(_) => true,
    }
}

fn provenance_mentions_page(provenance: &ProvenanceGraph, path: &Path) -> bool {
    let path = path.to_string_lossy();
    provenance
        .links()
        .iter()
        .any(|link| link.section.page_path.to_string_lossy() == path)
}

fn outdated_codewiki_pages(pages: &[lint::WikiPage]) -> Vec<PathBuf> {
    let mut paths = pages
        .iter()
        .filter(|page| page_is_codewiki(page))
        .filter(|page| frontmatter_flag(&page.markdown, "codewiki_status", "stale"))
        .map(|page| page.relative_path.clone())
        .collect::<Vec<_>>();
    paths.sort();
    paths
}

fn page_is_codewiki(page: &lint::WikiPage) -> bool {
    page.parsed
        .frontmatter
        .generated_by
        .as_deref()
        .is_some_and(|generated_by| generated_by.contains("codewiki"))
}

fn frontmatter_flag(markdown: &str, key: &str, expected: &str) -> bool {
    parse_frontmatter(markdown)
        .ok()
        .and_then(|parsed| parsed.metadata.unknown.get(key).cloned())
        .and_then(|value| match value {
            Value::String(value) => Some(value == expected),
            Value::Bool(value) => Some(value && expected == "true"),
            _ => None,
        })
        .unwrap_or(false)
}
