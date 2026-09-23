//! Import-community evidence over the stored partition: a size-ordered list, or the
//! communities one selector names, with members bound to the pinned index snapshot.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet};

use crate::codewiki_facts::CommunityFact;

use super::contracts::{
    CommunitiesSelector, CommunityBoundary, CommunityEvidence, CommunityMember, Completeness,
    DEFAULT_RESULT_LIMIT, EvidenceItem, EvidenceWarning,
};
use super::source::{canonical_hash, validate_repo_path};
use super::{EvidenceError, EvidenceLibrary, QueryResult, Result};

/// Singletons are partition noise; a selector asks for them with `min_size: 1`.
const DEFAULT_MIN_SIZE: usize = 2;
const DEFAULT_MAX_MEMBERS: usize = 50;
const MAX_MEMBERS: usize = 500;

pub(super) fn execute(
    library: &EvidenceLibrary,
    selector: &CommunitiesSelector,
) -> Result<QueryResult> {
    validate_selector(selector)?;
    let stored =
        library
            .facts
            .project_communities()
            .map_err(|error| EvidenceError::IndexUnavailable {
                reason: format!("{error:#}"),
            })?;
    let mut warnings = Vec::new();
    if stored.communities.is_empty() && !stored.refreshed {
        warnings.push(EvidenceWarning {
            code: "community_partition_missing".to_string(),
            message: crate::communities::MISSING_PARTITION_HINT.to_string(),
            path: None,
        });
    }

    let detail =
        selector.community_id.is_some() || selector.label.is_some() || selector.path.is_some();
    let min_size = selector.min_size.unwrap_or(DEFAULT_MIN_SIZE);
    let mut rows = resolve(&stored.communities, selector)
        .into_iter()
        .filter(|row| row.size >= min_size)
        .collect::<Vec<_>>();
    if detail && rows.len() > 1 {
        warnings.push(EvidenceWarning {
            code: "community_selector_ambiguous".to_string(),
            message: format!(
                "{} communities match the selector; every match is returned",
                rows.len()
            ),
            path: None,
        });
    }
    rows.sort_by_key(|row| (Reverse(row.size), row.community_id));
    let limit = selector.limit.unwrap_or(DEFAULT_RESULT_LIMIT);
    let truncated = rows.len() > limit;
    rows.truncate(limit);

    let labels = stored
        .communities
        .iter()
        .map(|row| (row.community_id, row.label.as_str()))
        .collect::<BTreeMap<_, _>>();
    let max_members = selector.max_members.unwrap_or(DEFAULT_MAX_MEMBERS);
    let mut items = Vec::with_capacity(rows.len());
    for row in rows {
        // A list omits members, so any community with members reads as truncated.
        let (members, members_truncated) = if detail {
            snapshot_members(library, row, max_members, &mut warnings)
        } else {
            (Vec::new(), !row.members.is_empty())
        };
        items.push(EvidenceItem::Community(CommunityEvidence {
            evidence_id: format!(
                "com:{}",
                canonical_hash(&(&library.binding, row.community_id, &row.member_signature))?
            ),
            community_id: row.community_id,
            label: row.label.clone(),
            label_source: row.label_source.clone(),
            label_confidence: row.label_confidence,
            label_stale: row.label_stale,
            size: row.size,
            cohesion: row.cohesion,
            internal_edges: row.internal_edges,
            member_signature: row.member_signature.clone(),
            members,
            members_truncated,
            representatives: row.representatives.clone(),
            boundary: row
                .boundary
                .iter()
                .map(|&(other_community_id, import_count)| CommunityBoundary {
                    other_community_id,
                    label: labels
                        .get(&other_community_id)
                        .map(|label| (*label).to_string())
                        .unwrap_or_default(),
                    import_count,
                })
                .collect(),
        }));
    }

    let completeness = if truncated {
        Completeness::TruncatedIndex
    } else if items.is_empty() {
        Completeness::CompleteEmpty
    } else {
        Completeness::Complete
    };
    Ok(QueryResult {
        items,
        completeness,
        lane: "communities".to_string(),
        hybrid: None,
        warnings,
        result_limit: limit,
        graph_depth: None,
    })
}

pub(super) fn validate_selector(selector: &CommunitiesSelector) -> Result<()> {
    let keys = [
        selector.community_id.is_some(),
        selector.label.is_some(),
        selector.path.is_some(),
    ];
    if keys.into_iter().filter(|set| *set).count() > 1 {
        return Err(invalid(
            "select a community by at most one of community_id, label, or path",
        ));
    }
    if selector
        .max_members
        .is_some_and(|max| !(1..=MAX_MEMBERS).contains(&max))
    {
        return Err(invalid(format!(
            "max_members must be between 1 and {MAX_MEMBERS}"
        )));
    }
    if selector.limit == Some(0) {
        return Err(invalid("communities limit must be positive"));
    }
    if selector
        .label
        .as_deref()
        .is_some_and(|label| label.trim().is_empty())
    {
        return Err(invalid("community label must not be empty"));
    }
    if let Some(path) = &selector.path {
        validate_repo_path(path)?;
    }
    Ok(())
}

fn invalid(detail: impl Into<String>) -> EvidenceError {
    EvidenceError::InvalidSelector {
        detail: detail.into(),
    }
}

/// An id or a member path names communities exactly. A label tries the display label
/// (ASCII case-insensitive), then the deterministic label, then a substring of either;
/// member paths never match a label.
fn resolve<'a>(
    rows: &'a [CommunityFact],
    selector: &CommunitiesSelector,
) -> Vec<&'a CommunityFact> {
    if let Some(id) = selector.community_id {
        return rows.iter().filter(|row| row.community_id == id).collect();
    }
    if let Some(path) = &selector.path {
        return rows
            .iter()
            .filter(|row| row.members.iter().any(|member| member == path))
            .collect();
    }
    let Some(label) = selector.label.as_deref().map(str::trim) else {
        return rows.iter().collect();
    };
    let exact = rows
        .iter()
        .filter(|row| row.label.eq_ignore_ascii_case(label))
        .collect::<Vec<_>>();
    if !exact.is_empty() {
        return exact;
    }
    let deterministic = rows
        .iter()
        .filter(|row| row.label_deterministic == label)
        .collect::<Vec<_>>();
    if !deterministic.is_empty() {
        return deterministic;
    }
    let needle = label.to_lowercase();
    rows.iter()
        .filter(|row| {
            row.label.to_lowercase().contains(&needle)
                || row.label_deterministic.to_lowercase().contains(&needle)
        })
        .collect()
}

/// Representatives first, then the other members by path. A member absent from the
/// pinned snapshot is dropped with a warning instead of being reported without a hash.
fn snapshot_members(
    library: &EvidenceLibrary,
    row: &CommunityFact,
    max_members: usize,
    warnings: &mut Vec<EvidenceWarning>,
) -> (Vec<CommunityMember>, bool) {
    let member_paths = row
        .members
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let mut ordered = Vec::with_capacity(member_paths.len());
    for representative in &row.representatives {
        let path = representative.as_str();
        if member_paths.contains(path) && !ordered.contains(&path) {
            ordered.push(path);
        }
    }
    let rest = member_paths
        .iter()
        .copied()
        .filter(|path| !ordered.contains(path))
        .collect::<Vec<_>>();
    ordered.extend(rest);

    let mut present = Vec::with_capacity(ordered.len());
    for path in ordered {
        match library.files.get(path) {
            Some(file) => present.push(CommunityMember {
                path: path.to_string(),
                content_hash: file.content_hash.clone(),
            }),
            None => warnings.push(EvidenceWarning {
                code: "community_member_not_in_snapshot".to_string(),
                message: format!(
                    "community {} member {path} is not in the pinned index snapshot",
                    row.community_id
                ),
                path: Some(path.to_string()),
            }),
        }
    }
    let truncated = present.len() > max_members;
    present.truncate(max_members);
    (present, truncated)
}
