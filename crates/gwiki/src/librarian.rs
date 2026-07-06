use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::{Value, json};

use crate::frontmatter::parse_frontmatter;
use crate::links::canonical_target_key;
use crate::provenance::ProvenanceGraph;
use crate::search::SearchScope;
use crate::search::semantic::{SemanticSearchBackend, SemanticSearchRequest};
use crate::support::scope::scope_includes_page;
use crate::support::services::RuntimeServices;
use crate::support::text::degradation_label;
use crate::{ScopeIdentity, WikiError, audit, health, lint};

const LIBRARIAN_DIR: &str = "meta/librarian";

/// Cosine similarity at or above which two knowledge pages count as
/// near-duplicates.
const NEAR_DUPLICATE_COSINE: f64 = 0.90;
/// Semantic hits requested per probed knowledge page.
const NEAR_DUPLICATE_SEARCH_LIMIT: usize = 8;
/// Query text drawn from the head of each page body for near-duplicate
/// probing.
const NEAR_DUPLICATE_QUERY_CHARS: usize = 600;
/// Minimum mentions before an unresolved link target forms a cluster.
const LINK_CLUSTER_MIN_MENTIONS: usize = 2;

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
    let broken_links = unique_paths(
        lint_report
            .broken_links
            .iter()
            .map(|issue| issue.path.clone()),
    );
    let weak_provenance = weak_provenance_pages(&pages, &provenance);
    let outdated_codewiki = if options.shared_code_graph_available {
        outdated_codewiki_pages(&pages)
    } else {
        Vec::new()
    };
    let semantic_scan = if options.semantic_available {
        match semantic {
            Some(probe) => semantic_gap_scan(&pages, &lint_report.broken_links, probe),
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
        available_check("broken_links", broken_links.clone()),
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

    persist_report(vault_root, &report)?;
    Ok(report)
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

/// A knowledge-page pair whose semantic similarity crossed
/// [`NEAR_DUPLICATE_COSINE`]; paths are ordered so each pair is unique.
#[derive(Debug, Clone, PartialEq)]
struct NearDuplicatePair {
    left: PathBuf,
    right: PathBuf,
    score: f64,
}

/// An unresolved link target mentioned at least
/// [`LINK_CLUSTER_MIN_MENTIONS`] times with no page behind it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct UnresolvedLinkCluster {
    target: String,
    mentions: usize,
}

#[derive(Debug, Clone, Default)]
struct SemanticGapScan {
    near_duplicates: Vec<NearDuplicatePair>,
    unresolved_clusters: Vec<UnresolvedLinkCluster>,
    failure: Option<String>,
}

impl SemanticGapScan {
    fn unavailable() -> Self {
        Self::default()
    }

    fn failed(message: impl Into<String>) -> Self {
        Self {
            failure: Some(message.into()),
            ..Self::default()
        }
    }

    fn items(&self) -> Vec<PathBuf> {
        unique_paths(
            self.near_duplicates
                .iter()
                .flat_map(|pair| [pair.left.clone(), pair.right.clone()])
                .chain(
                    self.unresolved_clusters
                        .iter()
                        .map(|cluster| PathBuf::from(&cluster.target)),
                ),
        )
    }
}

fn semantic_gaps_check(options: &Options, scan: &SemanticGapScan) -> CheckReport {
    if !options.semantic_available {
        return optional_check(
            "semantic_gaps",
            false,
            "Qdrant or embeddings are unavailable; skipped semantic gap detection",
            Vec::new(),
        );
    }
    if let Some(failure) = &scan.failure {
        return CheckReport {
            name: "semantic_gaps",
            available: false,
            note: Some(failure.clone()),
            items: Vec::new(),
        };
    }
    available_check("semantic_gaps", scan.items())
}

fn semantic_gap_scan(
    pages: &[lint::WikiPage],
    broken_links: &[lint::LinkIssue],
    probe: SemanticProbe<'_>,
) -> SemanticGapScan {
    match near_duplicate_pairs(pages, probe) {
        Ok(near_duplicates) => SemanticGapScan {
            near_duplicates,
            unresolved_clusters: unresolved_link_clusters(broken_links),
            failure: None,
        },
        Err(failure) => SemanticGapScan::failed(failure),
    }
}

fn near_duplicate_pairs(
    pages: &[lint::WikiPage],
    probe: SemanticProbe<'_>,
) -> Result<Vec<NearDuplicatePair>, String> {
    let SemanticProbe {
        backend,
        search_scope,
    } = probe;
    let mut best_scores: BTreeMap<(PathBuf, PathBuf), f64> = BTreeMap::new();
    for page in pages.iter().filter(|page| is_knowledge_page(page)) {
        let query = near_duplicate_query(page);
        if query.is_empty() {
            continue;
        }
        let outcome = backend
            .search_semantic(SemanticSearchRequest {
                query,
                scope: search_scope.clone(),
                limit: NEAR_DUPLICATE_SEARCH_LIMIT,
            })
            .map_err(|error| format!("semantic gap detection failed: {error}"))?;
        if let Some(degradation) = outcome.degradation {
            return Err(format!(
                "semantic gap detection degraded: {}",
                degradation_label(&degradation)
            ));
        }
        for hit in outcome.hits {
            if hit.score < NEAR_DUPLICATE_COSINE
                || hit.path == page.relative_path
                || !hit.path.starts_with("knowledge")
            {
                continue;
            }
            let pair = if hit.path < page.relative_path {
                (hit.path.clone(), page.relative_path.clone())
            } else {
                (page.relative_path.clone(), hit.path.clone())
            };
            let entry = best_scores.entry(pair).or_insert(hit.score);
            if hit.score > *entry {
                *entry = hit.score;
            }
        }
    }
    let pages_by_path: BTreeMap<&Path, &lint::WikiPage> = pages
        .iter()
        .map(|page| (page.relative_path.as_path(), page))
        .collect();
    Ok(best_scores
        .into_iter()
        .filter(|((left, right), _)| !expected_similarity_pair(left, right, &pages_by_path))
        .map(|((left, right), score)| NearDuplicatePair { left, right, score })
        .collect())
}

/// True for pairs whose high similarity is structural rather than a merge
/// signal (#17643): a synthesis is expected to score near the source digests
/// it cites, and session digests are distinct manifest records that must not
/// merge even when adjacent sessions worked the same topic. Pages missing
/// from the lookup (stale semantic index) keep their pairs.
fn expected_similarity_pair(
    left: &Path,
    right: &Path,
    pages_by_path: &BTreeMap<&Path, &lint::WikiPage>,
) -> bool {
    let left_page = pages_by_path.get(left).copied();
    let right_page = pages_by_path.get(right).copied();
    if left_page.is_some_and(is_session_digest) && right_page.is_some_and(is_session_digest) {
        return true;
    }
    cites_source_digest(left_page, right) || cites_source_digest(right_page, left)
}

/// A source digest recording a coding session (`source_kind: session`).
fn is_session_digest(page: &lint::WikiPage) -> bool {
    page.parsed.frontmatter.source_kind == Some(crate::models::WikiSourceKind::Session)
}

/// True when `citing` links to the source digest at `source` — the `Sources:`
/// line every synthesized page carries for each of its input digests.
fn cites_source_digest(citing: Option<&lint::WikiPage>, source: &Path) -> bool {
    if !source.starts_with("knowledge/sources") {
        return false;
    }
    let Some(page) = citing else {
        return false;
    };
    // Wikilink targets name the page without its `.md` file extension; match
    // both spellings so markdown-style links to the file also count.
    let source_keys = [
        canonical_target_key(&source.display().to_string()),
        canonical_target_key(&source.with_extension("").display().to_string()),
    ];
    page.parsed
        .links
        .iter()
        .any(|link| source_keys.contains(&canonical_target_key(&link.normalized_target)))
}

fn is_knowledge_page(page: &lint::WikiPage) -> bool {
    page.relative_path.starts_with("knowledge")
}

fn near_duplicate_query(page: &lint::WikiPage) -> String {
    let body = parse_frontmatter(&page.markdown)
        .map(|parsed| parsed.body)
        .unwrap_or(page.markdown.as_str());
    let trimmed = body.trim();
    match trimmed.char_indices().nth(NEAR_DUPLICATE_QUERY_CHARS) {
        Some((index, _)) => trimmed[..index].to_string(),
        None => trimmed.to_string(),
    }
}

fn unresolved_link_clusters(broken_links: &[lint::LinkIssue]) -> Vec<UnresolvedLinkCluster> {
    let mut clusters: BTreeMap<String, UnresolvedLinkCluster> = BTreeMap::new();
    for issue in broken_links {
        let key = canonical_target_key(&issue.target);
        if key.is_empty() {
            continue;
        }
        clusters
            .entry(key)
            .and_modify(|cluster| cluster.mentions += 1)
            .or_insert_with(|| UnresolvedLinkCluster {
                target: issue.target.clone(),
                mentions: 1,
            });
    }
    clusters
        .into_values()
        .filter(|cluster| cluster.mentions >= LINK_CLUSTER_MIN_MENTIONS)
        .collect()
}

fn weak_provenance_pages(pages: &[lint::WikiPage], provenance: &ProvenanceGraph) -> Vec<PathBuf> {
    let mut paths = pages
        .iter()
        .filter(|page| page_is_codewiki(page))
        .filter(|page| !provenance_mentions_page(provenance, &page.relative_path))
        .map(|page| page.relative_path.clone())
        .collect::<Vec<_>>();
    paths.sort();
    paths
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

fn suggested_tasks(
    uncited_sources: &[health::HealthSourceIssue],
    stale_pages: &[PathBuf],
    missing_citations: &[PathBuf],
    broken_links: &[PathBuf],
    weak_provenance: &[PathBuf],
    outdated_codewiki: &[PathBuf],
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
        "Broken links should be retargeted or removed after human review.",
        broken_links,
    );
    push_task(
        &mut tasks,
        !weak_provenance.is_empty(),
        "Strengthen weak provenance",
        "Attach source-to-section provenance before relying on these pages.",
        weak_provenance,
    );
    push_task(
        &mut tasks,
        !outdated_codewiki.is_empty(),
        "Refresh outdated codewiki pages",
        "Codewiki pages are stale; regenerate or accept a reviewed patch.",
        outdated_codewiki,
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

fn push_task(
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

fn suggested_patch_diffs(
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

fn unique_paths(paths: impl Iterator<Item = PathBuf>) -> Vec<PathBuf> {
    paths.collect::<BTreeSet<_>>().into_iter().collect()
}

fn artifacts() -> LibrarianArtifacts {
    LibrarianArtifacts {
        proposals_json: PathBuf::from("meta/librarian/proposals.json"),
        proposals_markdown: PathBuf::from("meta/librarian/proposals.md"),
        audit_annotations_json: PathBuf::from("meta/librarian/audit-annotations.json"),
        stale_pages_json: PathBuf::from("meta/librarian/stale-pages.json"),
    }
}

fn persist_report(vault_root: &Path, report: &ProposalsReport) -> Result<(), WikiError> {
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
            "outdated_codewiki": report.checks.iter().find(|check| check.name == "outdated_codewiki"),
        }),
    )
}

fn write_json<T: Serialize>(
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

fn write_text(vault_root: &Path, relative: &Path, text: &str) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    std::fs::write(&path, text).map_err(|source| WikiError::Io {
        action: "write librarian metadata",
        path: Some(path),
        source,
    })
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use gobby_core::config::{EmbeddingConfig, FalkorConfig, QdrantConfig};

    use crate::ScopeIdentity;
    use crate::markdown::parse_markdown;
    use crate::sources::{SourceDraft, SourceManifest};
    use crate::support::test_env::EnvGuard;

    use super::*;

    #[test]
    fn librarian_detects_and_proposes_without_rewriting_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/uncited",
                "2026-05-29T12:00:00Z",
                "uncited source",
            )
            .with_citation("Uncited Example"),
        )
        .expect("source registered");
        write_page(
            root,
            "knowledge/topics/stale.md",
            "---\ntitle: Stale\nstale: true\n---\n# Stale\nUnsupported operational claim.\nSee [[Missing]].\n",
        );
        write_page(
            root,
            "code/example.md",
            "---\ntitle: Example code\ngenerated_by: gcode-codewiki\nsource_spans:\n  - path: src/lib.rs\n    start_line: 1\n    end_line: 1\ncodewiki_status: stale\n---\n# Example code\nDocuments old code.\n",
        );

        let original_page =
            std::fs::read_to_string(root.join("knowledge/topics/stale.md")).expect("read page");
        let report = run(
            root,
            ScopeIdentity::project("project-1"),
            Options {
                shared_code_graph_available: true,
                ..Options::offline()
            },
            None,
        )
        .expect("librarian runs");

        assert_eq!(
            report.check("stale_pages").items,
            vec![PathBuf::from("knowledge/topics/stale.md")]
        );
        assert_eq!(
            report.check("missing_citations").items,
            vec![
                PathBuf::from("code/example.md"),
                PathBuf::from("knowledge/topics/stale.md"),
            ]
        );
        assert_eq!(
            report.check("broken_links").items,
            vec![PathBuf::from("knowledge/topics/stale.md")]
        );
        assert_eq!(
            report.check("weak_provenance").items,
            vec![PathBuf::from("code/example.md")]
        );
        assert!(report.check("outdated_codewiki").available);
        assert_eq!(
            report.check("outdated_codewiki").items,
            vec![PathBuf::from("code/example.md")]
        );
        assert!(report.check("outdated_codewiki").note.is_none());
        assert!(!report.check("patch_suggestions").available);
        assert!(
            report
                .suggested_tasks
                .iter()
                .any(|task| task.title.contains("Refresh stale wiki pages"))
        );
        assert!(
            report
                .suggested_patch_diffs
                .iter()
                .all(|diff| diff.applies_to_canonical_content)
        );
        assert_eq!(
            std::fs::read_to_string(root.join("knowledge/topics/stale.md")).expect("read page"),
            original_page
        );
        assert!(root.join("meta/librarian/proposals.json").exists());
        assert!(root.join("meta/librarian/audit-annotations.json").exists());
        assert!(root.join("meta/librarian/stale-pages.json").exists());
        assert!(
            report
                .suggested_tasks
                .iter()
                .any(|task| task.description.contains(&source.id))
        );
    }

    #[test]
    fn librarian_filters_codewiki_checks_to_selected_scope() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/topic.md",
            "# Topic\nSupported claim.\n",
        );
        write_page(
            root,
            "code/example.md",
            "---\ntitle: Example code\ngenerated_by: gcode-codewiki\ncodewiki_status: stale\n---\n# Example code\nDocuments old code.\n",
        );

        let report = run(
            root,
            ScopeIdentity::topic("ops"),
            Options {
                shared_code_graph_available: true,
                ..Options::offline()
            },
            None,
        )
        .expect("librarian runs");

        assert!(report.check("weak_provenance").items.is_empty());
        assert!(report.check("outdated_codewiki").items.is_empty());
    }

    #[test]
    fn librarian_degrades_each_optional_check_independently() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/page.md",
            "---\ntitle: Page\n---\n# Page\nSupported enough. [source](https://example.com)\n",
        );

        let report = run(root, ScopeIdentity::topic("ops"), Options::offline(), None)
            .expect("librarian runs");

        assert!(report.check("stale_pages").available);
        assert!(report.check("missing_citations").available);
        assert!(report.check("broken_links").available);
        assert!(report.check("weak_provenance").available);
        assert!(!report.check("outdated_codewiki").available);
        assert_eq!(
            report.check("outdated_codewiki").note.as_deref(),
            Some("shared code graph is unavailable; skipped outdated codewiki detection")
        );
        assert!(!report.check("semantic_gaps").available);
        assert!(!report.check("patch_suggestions").available);
    }

    #[test]
    fn librarian_outdated_codewiki_unavailable_without_shared_graph_even_when_stale() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/example.md",
            "---\ntitle: Example code\ngenerated_by: gcode-codewiki\nsource_spans:\n  - path: src/lib.rs\n    start_line: 1\n    end_line: 1\ncodewiki_status: stale\n---\n# Example code\nDocuments old code.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops"), Options::offline(), None)
            .expect("librarian runs");

        let check = report.check("outdated_codewiki");
        assert!(!check.available);
        assert!(check.items.is_empty());
        assert_eq!(
            check.note.as_deref(),
            Some("shared code graph is unavailable; skipped outdated codewiki detection")
        );
    }

    #[test]
    fn librarian_codewiki_path_checks_are_sorted() {
        let pages = vec![
            codewiki_page("code/z.md", true),
            codewiki_page("code/a.md", true),
        ];

        assert_eq!(
            weak_provenance_pages(&pages, &ProvenanceGraph::default()),
            vec![PathBuf::from("code/a.md"), PathBuf::from("code/z.md")]
        );
        assert_eq!(
            outdated_codewiki_pages(&pages),
            vec![PathBuf::from("code/a.md"), PathBuf::from("code/z.md")]
        );
    }

    #[test]
    fn librarian_probed_options_reflect_runtime_services() {
        let services = RuntimeServices {
            postgres_configured: true,
            falkor: Some(FalkorConfig {
                host: "localhost".to_string(),
                port: 6379,
                password: None,
            }),
            qdrant: Some(QdrantConfig {
                url: Some("http://localhost:6333".to_string()),
                api_key: None,
            }),
            embedding: Some(EmbeddingConfig {
                api_base: "http://localhost:1234/v1".to_string(),
                model: "embed-model".to_string(),
                api_key: None,
                query_prefix: None,
                timeout_seconds: 30,
            }),
            semantic_embedding: None,
        };

        assert_eq!(
            Options::probed(&services, true),
            Options {
                require_postgres_index: true,
                shared_code_graph_available: true,
                semantic_available: true,
                model_available: true,
            }
        );
        assert_eq!(
            Options::probed(&RuntimeServices::detached(), false),
            Options {
                require_postgres_index: true,
                ..Options::offline()
            }
        );
    }

    #[test]
    fn librarian_semantic_gaps_report_near_duplicates_and_link_clusters() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/async-rust.md",
            "# Async Rust\nSee [[Widget Factory]] twice: [[Widget Factory]] and once [[Solo Target]].\n",
        );
        write_page(
            root,
            "knowledge/topics/rust-async.md",
            "# Rust Async\nNearly the same content about async Rust.\n",
        );

        let mut backend = FixedSemanticBackend {
            hits: vec![
                semantic_hit("knowledge/topics/rust-async.md", 0.95),
                semantic_hit("code/files/dup.md", 0.99),
                semantic_hit("knowledge/topics/other.md", 0.50),
            ],
        };
        let report = run(
            root,
            ScopeIdentity::topic("ops"),
            Options {
                semantic_available: true,
                ..Options::offline()
            },
            Some(SemanticProbe {
                backend: &mut backend,
                search_scope: SearchScope::topic("ops"),
            }),
        )
        .expect("librarian runs");

        let check = report.check("semantic_gaps");
        assert!(check.available);
        assert!(check.note.is_none());
        assert_eq!(
            check.items,
            vec![
                PathBuf::from("Widget Factory"),
                PathBuf::from("knowledge/topics/async-rust.md"),
                PathBuf::from("knowledge/topics/rust-async.md"),
            ]
        );
        let merge_task = report
            .suggested_tasks
            .iter()
            .find(|task| task.title.contains("near-duplicate"))
            .expect("near-duplicate task");
        assert!(
            merge_task.description.contains("rust-async.md"),
            "{merge_task:?}"
        );
        assert!(merge_task.description.contains("0.95"), "{merge_task:?}");
        let create_task = report
            .suggested_tasks
            .iter()
            .find(|task| task.title.contains("repeatedly mentioned"))
            .expect("link cluster task");
        assert!(
            create_task
                .description
                .contains("Widget Factory (2 mentions)"),
            "{create_task:?}"
        );
        assert!(
            !create_task.description.contains("Solo Target"),
            "single mentions must not cluster: {create_task:?}"
        );
    }

    #[test]
    fn librarian_semantic_gaps_fail_closed_without_backend_or_on_degradation() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/page.md",
            "# Page\nSee [[Widget Factory]] and [[Widget Factory]].\n",
        );
        let options = Options {
            semantic_available: true,
            ..Options::offline()
        };

        let report =
            run(root, ScopeIdentity::topic("ops"), options.clone(), None).expect("librarian runs");
        let check = report.check("semantic_gaps");
        assert!(!check.available);
        assert!(check.items.is_empty());
        assert!(
            check
                .note
                .as_deref()
                .is_some_and(|note| note.contains("no semantic backend")),
            "{check:?}"
        );

        let mut degraded_backend = DegradedSemanticBackend;
        let report = run(
            root,
            ScopeIdentity::topic("ops"),
            options,
            Some(SemanticProbe {
                backend: &mut degraded_backend,
                search_scope: SearchScope::topic("ops"),
            }),
        )
        .expect("librarian runs");
        let check = report.check("semantic_gaps");
        assert!(!check.available);
        assert!(check.items.is_empty());
        assert!(
            check
                .note
                .as_deref()
                .is_some_and(|note| note.contains("degraded")),
            "{check:?}"
        );
    }

    #[test]
    fn unresolved_link_clusters_fold_case_and_require_multiple_mentions() {
        let issues = vec![
            link_issue("a.md", "Widget Factory"),
            link_issue("b.md", "widget factory"),
            link_issue("a.md", "Solo"),
        ];

        assert_eq!(
            unresolved_link_clusters(&issues),
            vec![UnresolvedLinkCluster {
                target: "Widget Factory".to_string(),
                mentions: 2,
            }]
        );
    }

    #[test]
    fn near_duplicates_skip_sources_cited_by_the_synthesis() {
        let concept = knowledge_page(
            "knowledge/concepts/gcode.md",
            "---\ntitle: gcode\nsource_kind: concept\n---\n# gcode\n\nSources: [[knowledge/sources/src-1-session-aaa|Session: aaa]]\n\ngcode is the code index CLI.\n",
        );
        let digest = knowledge_page(
            "knowledge/sources/src-1-session-aaa.md",
            "---\nsource_kind: session\n---\n# Session: aaa\n\ngcode is the code index CLI.\n",
        );
        let mut backend = FixedSemanticBackend {
            hits: vec![
                semantic_hit("knowledge/concepts/gcode.md", 0.95),
                semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.95),
            ],
        };

        let pairs = near_duplicate_pairs(
            &[concept, digest],
            SemanticProbe {
                backend: &mut backend,
                search_scope: SearchScope::topic("ops"),
            },
        )
        .expect("scan succeeds");

        assert_eq!(pairs, Vec::new());
    }

    #[test]
    fn near_duplicates_skip_session_digest_pairs() {
        let first = knowledge_page(
            "knowledge/sources/src-1-session-aaa.md",
            "---\nsource_kind: session\n---\n# Session: aaa\n\nWorked the wiki upkeep pipeline.\n",
        );
        let second = knowledge_page(
            "knowledge/sources/src-2-session-bbb.md",
            "---\nsource_kind: session\n---\n# Session: bbb\n\nContinued the wiki upkeep pipeline.\n",
        );
        let mut backend = FixedSemanticBackend {
            hits: vec![
                semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.92),
                semantic_hit("knowledge/sources/src-2-session-bbb.md", 0.92),
            ],
        };

        let pairs = near_duplicate_pairs(
            &[first, second],
            SemanticProbe {
                backend: &mut backend,
                search_scope: SearchScope::topic("ops"),
            },
        )
        .expect("scan succeeds");

        assert_eq!(pairs, Vec::new());
    }

    #[test]
    fn near_duplicates_keep_concept_pairs() {
        let left = knowledge_page(
            "knowledge/concepts/gobby-core.md",
            "---\nsource_kind: concept\n---\n# gobby-core\n\nShared Rust library crate.\n",
        );
        let right = knowledge_page(
            "knowledge/concepts/gcore.md",
            "---\nsource_kind: concept\n---\n# gcore\n\nShared Rust library crate.\n",
        );
        let mut backend = FixedSemanticBackend {
            hits: vec![
                semantic_hit("knowledge/concepts/gobby-core.md", 0.93),
                semantic_hit("knowledge/concepts/gcore.md", 0.93),
            ],
        };

        let pairs = near_duplicate_pairs(
            &[left, right],
            SemanticProbe {
                backend: &mut backend,
                search_scope: SearchScope::topic("ops"),
            },
        )
        .expect("scan succeeds");

        assert_eq!(
            pairs,
            vec![NearDuplicatePair {
                left: PathBuf::from("knowledge/concepts/gcore.md"),
                right: PathBuf::from("knowledge/concepts/gobby-core.md"),
                score: 0.93,
            }]
        );
    }

    #[test]
    fn near_duplicates_keep_source_pairs_the_synthesis_does_not_cite() {
        let concept = knowledge_page(
            "knowledge/concepts/gcode.md",
            "---\nsource_kind: concept\n---\n# gcode\n\nSources: [[knowledge/sources/src-9-session-zzz|Session: zzz]]\n\ngcode is the code index CLI.\n",
        );
        let digest = knowledge_page(
            "knowledge/sources/src-1-session-aaa.md",
            "---\nsource_kind: session\n---\n# Session: aaa\n\ngcode is the code index CLI.\n",
        );
        let mut backend = FixedSemanticBackend {
            hits: vec![
                semantic_hit("knowledge/concepts/gcode.md", 0.94),
                semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.94),
            ],
        };

        let pairs = near_duplicate_pairs(
            &[concept, digest],
            SemanticProbe {
                backend: &mut backend,
                search_scope: SearchScope::topic("ops"),
            },
        )
        .expect("scan succeeds");

        assert_eq!(
            pairs,
            vec![NearDuplicatePair {
                left: PathBuf::from("knowledge/concepts/gcode.md"),
                right: PathBuf::from("knowledge/sources/src-1-session-aaa.md"),
                score: 0.94,
            }]
        );
    }

    struct FixedSemanticBackend {
        hits: Vec<crate::search::WikiSearchResult>,
    }

    impl SemanticSearchBackend for FixedSemanticBackend {
        fn search_semantic(
            &mut self,
            _request: SemanticSearchRequest,
        ) -> Result<crate::search::semantic::SemanticSearchOutcome, crate::search::SearchError>
        {
            Ok(crate::search::semantic::SemanticSearchOutcome {
                hits: self.hits.clone(),
                degradation: None,
            })
        }
    }

    struct DegradedSemanticBackend;

    impl SemanticSearchBackend for DegradedSemanticBackend {
        fn search_semantic(
            &mut self,
            _request: SemanticSearchRequest,
        ) -> Result<crate::search::semantic::SemanticSearchOutcome, crate::search::SearchError>
        {
            Ok(crate::search::semantic::SemanticSearchOutcome {
                hits: Vec::new(),
                degradation: Some(gobby_core::degradation::DegradationKind::PartialData {
                    component: "semantic".to_string(),
                    message: "qdrant unreachable".to_string(),
                }),
            })
        }
    }

    fn semantic_hit(path: &str, score: f64) -> crate::search::WikiSearchResult {
        crate::search::WikiSearchResult {
            id: path.to_string(),
            title: None,
            scope: SearchScope::topic("ops"),
            path: PathBuf::from(path),
            source_path: PathBuf::from(path),
            hit_kind: crate::search::SearchHitKind::Document,
            snippet: String::new(),
            score,
            sources: Vec::new(),
            explanations: Vec::new(),
            chunk: None,
            provenance: crate::search::SearchProvenance {
                document_path: PathBuf::from(path),
                source_path: PathBuf::from(path),
                source_kind: "document".to_string(),
                content_hash: None,
            },
        }
    }

    fn link_issue(path: &str, target: &str) -> lint::LinkIssue {
        lint::LinkIssue {
            path: PathBuf::from(path),
            line: 1,
            target: target.to_string(),
            kind: "wikilink".to_string(),
        }
    }

    #[test]
    #[serial_test::serial]
    fn librarian_requires_configured_postgres_index() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "knowledge/topics/page.md",
            "# Page\n\nSupported enough.\n",
        );
        let _database_url = EnvGuard::set("GWIKI_DATABASE_URL", "postgresql://127.0.0.1:1/gwiki");

        let error = run(root, ScopeIdentity::topic("ops"), Options::default(), None)
            .expect_err("PostgreSQL is required");

        assert!(
            error
                .to_string()
                .contains("failed to connect to PostgreSQL for gwiki librarian"),
            "{error}"
        );
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }

    fn knowledge_page(relative: &str, markdown: &str) -> lint::WikiPage {
        let relative_path = PathBuf::from(relative);
        lint::WikiPage {
            path: relative_path.clone(),
            relative_path: relative_path.clone(),
            parsed: parse_markdown(relative_path, markdown, Vec::<String>::new())
                .expect("parse test page"),
            markdown: markdown.to_string(),
            has_frontmatter: markdown.starts_with("---"),
        }
    }

    fn codewiki_page(relative: &str, stale: bool) -> lint::WikiPage {
        let markdown = format!(
            "---\ntitle: Code\ngenerated_by: gcode-codewiki\ncodewiki_status: {}\n---\n# Code\n",
            if stale { "stale" } else { "fresh" }
        );
        let relative_path = PathBuf::from(relative);
        lint::WikiPage {
            path: relative_path.clone(),
            relative_path: relative_path.clone(),
            parsed: parse_markdown(relative_path, &markdown, Vec::<String>::new())
                .expect("parse test page"),
            markdown,
            has_frontmatter: true,
        }
    }
}
