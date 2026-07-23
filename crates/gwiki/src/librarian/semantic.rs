use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::frontmatter::parse_frontmatter;
use crate::links::{canonical_target_key, is_concept_worthy};
use crate::lint;
use crate::search::semantic::SemanticSearchRequest;
use crate::support::text::degradation_label;

use super::{CheckReport, Options, SemanticProbe, available_check, optional_check, unique_paths};

pub(super) const DISTINCT_PAIRS_RELATIVE_PATH: &str = "meta/librarian/distinct-pairs.json";
pub(super) const NEAR_DUPLICATE_COSINE: f64 = 0.90;
const NEAR_DUPLICATE_SEARCH_LIMIT: usize = 8;
const NEAR_DUPLICATE_QUERY_CHARS: usize = 600;
pub(super) const LINK_CLUSTER_MIN_MENTIONS: usize = 2;

/// A knowledge-page pair whose semantic similarity crossed
/// [`NEAR_DUPLICATE_COSINE`]; paths are ordered so each pair is unique.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct NearDuplicatePair {
    pub(super) left: PathBuf,
    pub(super) right: PathBuf,
    pub(super) score: f64,
}

/// An unresolved link target mentioned at least
/// [`LINK_CLUSTER_MIN_MENTIONS`] times with no page behind it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct UnresolvedLinkCluster {
    pub(super) target: String,
    pub(super) mentions: usize,
}

#[derive(Debug, Clone, Default)]
pub(super) struct SemanticGapScan {
    pub(super) near_duplicates: Vec<NearDuplicatePair>,
    pub(super) unresolved_clusters: Vec<UnresolvedLinkCluster>,
    failure: Option<String>,
}

impl SemanticGapScan {
    pub(super) fn unavailable() -> Self {
        Self::default()
    }

    pub(super) fn failed(message: impl Into<String>) -> Self {
        Self {
            failure: Some(message.into()),
            ..Self::default()
        }
    }

    pub(super) fn items(&self) -> Vec<PathBuf> {
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

pub(super) fn semantic_gaps_check(options: &Options, scan: &SemanticGapScan) -> CheckReport {
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

pub(super) fn semantic_gap_scan(
    pages: &[lint::WikiPage],
    broken_links: &[lint::LinkIssue],
    probe: SemanticProbe<'_>,
    distinct_pairs: &BTreeSet<(String, String)>,
) -> SemanticGapScan {
    match near_duplicate_pairs(pages, probe, distinct_pairs) {
        Ok(near_duplicates) => SemanticGapScan {
            near_duplicates,
            unresolved_clusters: unresolved_link_clusters(broken_links),
            failure: None,
        },
        Err(failure) => SemanticGapScan::failed(failure),
    }
}

pub(super) fn near_duplicate_pairs(
    pages: &[lint::WikiPage],
    probe: SemanticProbe<'_>,
    distinct_pairs: &BTreeSet<(String, String)>,
) -> Result<Vec<NearDuplicatePair>, String> {
    let SemanticProbe {
        backend,
        search_scope,
    } = probe;
    let mut best_scores: BTreeMap<(PathBuf, PathBuf), f64> = BTreeMap::new();
    for page in pages
        .iter()
        .filter(|page| is_knowledge_page(page) && !is_folder_context(&page.relative_path))
    {
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
                || is_folder_context(&hit.path)
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
        .filter(|((left, right), _)| {
            !expected_similarity_pair(left, right, &pages_by_path, distinct_pairs)
        })
        .map(|((left, right), score)| NearDuplicatePair { left, right, score })
        .collect())
}

/// True for pairs whose high similarity is structural rather than a merge
/// signal (#17643): a synthesis is expected to score near the source digests
/// it cites, and session digests are distinct manifest records that must not
/// merge even when adjacent sessions worked the same topic. Reviewed
/// disambiguation verdicts (#17782) are also honored so blessed-distinct
/// pairs stop resurfacing. Pages missing from the lookup (stale semantic
/// index) keep their pairs.
pub(super) fn expected_similarity_pair(
    left: &Path,
    right: &Path,
    pages_by_path: &BTreeMap<&Path, &lint::WikiPage>,
    distinct_pairs: &BTreeSet<(String, String)>,
) -> bool {
    if distinct_pairs.contains(&normalized_page_pair(
        &left.display().to_string(),
        &right.display().to_string(),
    )) {
        return true;
    }
    let left_page = pages_by_path.get(left).copied();
    let right_page = pages_by_path.get(right).copied();
    if left_page.is_some_and(is_redirect_page) || right_page.is_some_and(is_redirect_page) {
        return true;
    }
    if left_page.is_some_and(is_session_digest) && right_page.is_some_and(is_session_digest) {
        return true;
    }
    cites_source_digest(left_page, right) || cites_source_digest(right_page, left)
}

/// Reviewed disambiguation verdicts (#17782): normalized page-path pairs the
/// near-duplicate scan must not re-flag. Stored at
/// [`DISTINCT_PAIRS_RELATIVE_PATH`] — a librarian meta artifact rather than
/// page frontmatter, because entity recompiles regenerate frontmatter and
/// would silently drop an in-page marker. A missing or unparseable file is an
/// empty set.
pub(super) fn load_distinct_pairs(vault_root: &Path) -> BTreeSet<(String, String)> {
    #[derive(serde::Deserialize)]
    struct DistinctPairsFile {
        #[serde(default)]
        pairs: Vec<DistinctPairEntry>,
    }
    #[derive(serde::Deserialize)]
    struct DistinctPairEntry {
        left: String,
        right: String,
    }
    let Ok(raw) = std::fs::read_to_string(vault_root.join(DISTINCT_PAIRS_RELATIVE_PATH)) else {
        return BTreeSet::new();
    };
    let Ok(parsed) = serde_json::from_str::<DistinctPairsFile>(&raw) else {
        return BTreeSet::new();
    };
    parsed
        .pairs
        .into_iter()
        .map(|entry| normalized_page_pair(&entry.left, &entry.right))
        .collect()
}

/// Order-insensitive pair key tolerant of `.md`-suffixed and extensionless
/// spellings on either side.
pub(super) fn normalized_page_pair(left: &str, right: &str) -> (String, String) {
    let left = canonical_page_key(left);
    let right = canonical_page_key(right);
    if left <= right {
        (left, right)
    } else {
        (right, left)
    }
}

pub(super) fn canonical_page_key(path: &str) -> String {
    canonical_target_key(path.strip_suffix(".md").unwrap_or(path))
}

/// A source digest recording a coding session (`source_kind: session`).
pub(super) fn is_session_digest(page: &lint::WikiPage) -> bool {
    page.parsed.frontmatter.source_kind == Some(crate::models::WikiSourceKind::Session)
}

pub(super) fn is_redirect_page(page: &lint::WikiPage) -> bool {
    parse_frontmatter(&page.markdown)
        .ok()
        .is_some_and(|parsed| parsed.metadata.unknown.contains_key("redirect"))
}

/// True when `citing` links to the source digest at `source` — the `Sources:`
/// line every synthesized page carries for each of its input digests.
pub(super) fn cites_source_digest(citing: Option<&lint::WikiPage>, source: &Path) -> bool {
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

pub(super) fn is_knowledge_page(page: &lint::WikiPage) -> bool {
    page.relative_path.starts_with("knowledge")
}

/// Generated per-folder navigation files (`_context.md`, #17730). They share
/// one template, so sibling folders' contexts always score as near-duplicates
/// — and merging generated navigation is meaningless, so the scan skips them
/// on both the probe and hit sides (#17782).
pub(super) fn is_folder_context(path: &Path) -> bool {
    path.file_name().is_some_and(|name| name == "_context.md")
}

pub(super) fn near_duplicate_query(page: &lint::WikiPage) -> String {
    let body = parse_frontmatter(&page.markdown)
        .map(|parsed| parsed.body)
        .unwrap_or(page.markdown.as_str());
    let trimmed = body.trim();
    match trimmed.char_indices().nth(NEAR_DUPLICATE_QUERY_CHARS) {
        Some((index, _)) => trimmed[..index].to_string(),
        None => trimmed.to_string(),
    }
}

pub(super) fn unresolved_link_clusters(
    broken_links: &[lint::LinkIssue],
) -> Vec<UnresolvedLinkCluster> {
    let mut clusters: BTreeMap<String, UnresolvedLinkCluster> = BTreeMap::new();
    for issue in broken_links {
        // An absolute filesystem path can never be a vault page, so it is
        // pure noise as a page-creation candidate (#17649); the broken-links
        // repair check still covers such dead links.
        if Path::new(&issue.target).is_absolute() {
            continue;
        }
        let key = canonical_target_key(&issue.target);
        if !is_concept_worthy(&key) {
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
