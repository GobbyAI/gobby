//! The project-wide file import partition.
//!
//! [`build_partition`] turns the identity and import rows loaded by
//! [`super::identity`] into one community per cluster of files that import each
//! other, following Graphify's split and cohesion rules. Plan section 2.3
//! remaps the ids against the stored partition and 3.2 persists it, so nothing
//! in the binary reads these types yet.
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fmt::{self, Write as _};

use gobby_core::graph_analytics::{
    AnalyticsEdge, AnalyticsGraph, AnalyticsNode, GraphInputError, communities,
};
use sha2::{Digest, Sha256};

use super::identity::ImportIdentity;

/// Node kind for the file nodes handed to the analytics kernel.
const FILE_KIND: &str = "file";
/// Edge kind for the folded undirected import edges.
const IMPORTS_KIND: &str = "IMPORTS";
/// Graphify's oversized floor: a community above `max(10, file_count / 4)` splits once.
const OVERSIZED_FLOOR: usize = 10;
/// Graphify's member floor for the low-cohesion pass.
const LOW_COHESION_MIN_MEMBERS: usize = 50;
/// Graphify's 0.05 cohesion threshold as integer math: `E / (n(n-1)/2) < 0.05`
/// is `40 * E < n * (n - 1)`, which never rounds a reachable threshold away.
const LOW_COHESION_NUMERATOR: u128 = 40;

/// One community of the file import partition.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PartitionCommunity {
    /// Member file paths, ascending.
    pub members: Vec<String>,
    /// Member pairs with at least one import between them, in either direction.
    pub internal_edges: usize,
    /// `internal_edges / (n(n-1)/2)`, and `1.0` for a singleton.
    pub cohesion: f64,
    /// Distinct project-wide importers of each member.
    pub in_degree: BTreeMap<String, usize>,
    /// First 16 hex digits of sha256 over the NUL-terminated sorted members.
    pub member_signature: String,
}

/// One folded import direction: how many distinct module names take `importer` to `provider`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DirectedImport {
    pub importer: String,
    pub provider: String,
    pub count: usize,
}

/// A whole project's file import partition.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ProjectPartition {
    /// Communities ordered by `(size desc, members[0] asc)`.
    pub communities: Vec<PartitionCommunity>,
    /// Folded imports ordered by `(importer, provider)`.
    pub directed: Vec<DirectedImport>,
    /// Visible files in the project, community members or not.
    pub file_count: usize,
    /// sha256 over every stored per-community column and the folded imports.
    pub partition_signature: String,
}

/// Why a partition could not be built.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum PartitionError {
    /// The analytics kernel rejected the graph this module handed it.
    InvalidGraph(GraphInputError),
    /// Identity resolved a module to a file outside the partition's node set.
    ProviderNotVisible { module: String, file: String },
}

impl fmt::Display for PartitionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidGraph(error) => write!(f, "import graph rejected: {error}"),
            Self::ProviderNotVisible { module, file } => {
                write!(f, "module {module} resolved to non-visible file {file}")
            }
        }
    }
}

impl std::error::Error for PartitionError {}

/// Partition the project's visible files by the imports between them.
pub(crate) fn build_partition(
    identity: &ImportIdentity,
    rows: &[(String, String)],
) -> Result<ProjectPartition, PartitionError> {
    let files = sorted_files(identity);
    let file_set = files.iter().map(String::as_str).collect::<HashSet<_>>();
    let directed = directed_imports(identity, &file_set, rows)?;
    let undirected = fold_undirected(&directed);
    let in_degree = in_degrees(&directed);

    let groups = leiden_groups(&files, &undirected)?;
    let groups = split_oversized(groups, files.len(), &undirected)?;
    let mut groups = split_low_cohesion(groups, &undirected)?;
    groups.sort_by(|left, right| {
        right
            .len()
            .cmp(&left.len())
            .then_with(|| left[0].cmp(&right[0]))
    });

    let communities = groups
        .into_iter()
        .map(|members| {
            let internal = internal_edges(&members, &undirected);
            let scoped = members
                .iter()
                .map(|member| (member.clone(), in_degree.get(member).copied().unwrap_or(0)))
                .collect::<BTreeMap<_, _>>();
            PartitionCommunity {
                member_signature: member_signature(&members),
                cohesion: cohesion(members.len(), internal),
                internal_edges: internal,
                in_degree: scoped,
                members,
            }
        })
        .collect::<Vec<_>>();

    Ok(ProjectPartition {
        partition_signature: partition_signature(&communities, &directed),
        communities,
        directed,
        file_count: files.len(),
    })
}

/// The partition's node set: every visible file, ascending.
fn sorted_files(identity: &ImportIdentity) -> Vec<String> {
    let mut files = identity.visible_files.iter().cloned().collect::<Vec<_>>();
    files.sort();
    files
}

/// Fold `rows` into one entry per ordered `(importer, provider)` pair.
///
/// `files` is the caller's node set rather than the identity's visible set: a
/// split pass resolves against the whole project while its node set is one
/// community, and a provider outside the declared set is the invariant
/// violation [`PartitionError::ProviderNotVisible`] reports.
fn directed_imports(
    identity: &ImportIdentity,
    files: &HashSet<&str>,
    rows: &[(String, String)],
) -> Result<Vec<DirectedImport>, PartitionError> {
    let mut counts: BTreeMap<(String, String), usize> = BTreeMap::new();
    for (source, module) in rows.iter().cloned().collect::<BTreeSet<_>>() {
        if !files.contains(source.as_str()) {
            continue;
        }
        let Some(provider) = identity.unique_provider(&module) else {
            continue;
        };
        if provider == source {
            continue;
        }
        if !files.contains(provider.as_str()) {
            return Err(PartitionError::ProviderNotVisible {
                module,
                file: provider,
            });
        }
        *counts.entry((source, provider)).or_default() += 1;
    }
    Ok(counts
        .into_iter()
        .map(|((importer, provider), count)| DirectedImport {
            importer,
            provider,
            count,
        })
        .collect())
}

/// Fold both directions of each file pair into one weighted undirected edge.
fn fold_undirected(directed: &[DirectedImport]) -> BTreeMap<(String, String), usize> {
    let mut folded: BTreeMap<(String, String), usize> = BTreeMap::new();
    for import in directed {
        let key = if import.importer <= import.provider {
            (import.importer.clone(), import.provider.clone())
        } else {
            (import.provider.clone(), import.importer.clone())
        };
        *folded.entry(key).or_default() += import.count;
    }
    folded
}

/// Distinct project-wide importers of each file that has any.
fn in_degrees(directed: &[DirectedImport]) -> BTreeMap<String, usize> {
    let mut degrees: BTreeMap<String, usize> = BTreeMap::new();
    for import in directed {
        *degrees.entry(import.provider.clone()).or_default() += 1;
    }
    degrees
}

/// One Leiden pass over `files` and the edges induced on them.
fn leiden_groups(
    files: &[String],
    undirected: &BTreeMap<(String, String), usize>,
) -> Result<Vec<Vec<String>>, PartitionError> {
    let inside = files.iter().map(String::as_str).collect::<HashSet<_>>();
    let graph = AnalyticsGraph {
        nodes: files
            .iter()
            .map(|file| AnalyticsNode {
                id: file.clone(),
                kind: FILE_KIND.to_string(),
                weight: 1.0,
            })
            .collect(),
        edges: undirected
            .iter()
            .filter(|((left, right), _)| {
                inside.contains(left.as_str()) && inside.contains(right.as_str())
            })
            .map(|((left, right), count)| AnalyticsEdge {
                source: left.clone(),
                target: right.clone(),
                kind: IMPORTS_KIND.to_string(),
                weight: *count as f64,
            })
            .collect(),
    };

    Ok(communities(&graph)
        .map_err(PartitionError::InvalidGraph)?
        .into_iter()
        .map(|community| {
            let mut members = community
                .nodes
                .into_iter()
                .map(|node| node.id)
                .collect::<Vec<_>>();
            members.sort();
            members
        })
        .collect())
}

/// Split every community above `max(10, file_count / 4)` once; children are never re-split.
fn split_oversized(
    groups: Vec<Vec<String>>,
    file_count: usize,
    undirected: &BTreeMap<(String, String), usize>,
) -> Result<Vec<Vec<String>>, PartitionError> {
    let max_size = OVERSIZED_FLOOR.max(file_count / 4);
    resplit(groups, undirected, |members| members.len() > max_size)
}

/// Split every low-cohesion community once; children are never re-split.
fn split_low_cohesion(
    groups: Vec<Vec<String>>,
    undirected: &BTreeMap<(String, String), usize>,
) -> Result<Vec<Vec<String>>, PartitionError> {
    resplit(groups, undirected, |members| {
        is_low_cohesion(members.len(), internal_edges(members, undirected))
    })
}

/// One re-partition pass. A result of a single group is discarded, so a
/// community Leiden declines to divide survives the pass intact. `should_split`
/// owns whatever it needs to decide, so the oversized pass never pays for an
/// edge scan it does not read.
fn resplit(
    groups: Vec<Vec<String>>,
    undirected: &BTreeMap<(String, String), usize>,
    should_split: impl Fn(&[String]) -> bool,
) -> Result<Vec<Vec<String>>, PartitionError> {
    let mut split = Vec::with_capacity(groups.len());
    for group in groups {
        if should_split(&group) {
            let children = leiden_groups(&group, undirected)?;
            if children.len() > 1 {
                split.extend(children);
                continue;
            }
        }
        split.push(group);
    }
    Ok(split)
}

/// `n >= 50` and `cohesion < 0.05`, the latter as `40 * E < n * (n - 1)` in `u128`.
fn is_low_cohesion(member_count: usize, internal_edges: usize) -> bool {
    if member_count < LOW_COHESION_MIN_MEMBERS {
        return false;
    }
    let members = member_count as u128;
    LOW_COHESION_NUMERATOR * (internal_edges as u128) < members * (members - 1)
}

/// Member pairs with at least one import between them, in either direction.
fn internal_edges(members: &[String], undirected: &BTreeMap<(String, String), usize>) -> usize {
    let inside = members.iter().map(String::as_str).collect::<HashSet<_>>();
    undirected
        .keys()
        .filter(|(left, right)| inside.contains(left.as_str()) && inside.contains(right.as_str()))
        .count()
}

/// `internal_edges / (n(n-1)/2)`, and `1.0` for a singleton, matching Graphify.
fn cohesion(member_count: usize, internal_edges: usize) -> f64 {
    if member_count <= 1 {
        return 1.0;
    }
    let pairs = member_count * (member_count - 1) / 2;
    internal_edges as f64 / pairs as f64
}

/// sha256 over the ascending members, each followed by NUL, truncated to 16 hex digits.
fn member_signature(members: &[String]) -> String {
    let mut hasher = Sha256::new();
    for member in members {
        hasher.update(member.as_bytes());
        hasher.update(b"\0");
    }
    hex_digest(&hasher.finalize())[..16].to_string()
}

/// sha256 over every column plan 3.2 stores, so its skip predicate cannot freeze
/// an edge-only change: per community the member signature, the internal edge
/// count, and a digest of the in-degree map, then every folded directed import.
fn partition_signature(communities: &[PartitionCommunity], directed: &[DirectedImport]) -> String {
    let mut hasher = Sha256::new();
    for community in communities {
        hasher.update(community.member_signature.as_bytes());
        hasher.update(b"\0");
        hasher.update(community.internal_edges.to_string().as_bytes());
        hasher.update(b"\0");
        hasher.update(in_degree_digest(&community.in_degree).as_bytes());
        hasher.update(b"\0");
    }
    for import in directed {
        hasher.update(import.importer.as_bytes());
        hasher.update(b"\0");
        hasher.update(import.provider.as_bytes());
        hasher.update(b"\0");
        hasher.update(import.count.to_string().as_bytes());
        hasher.update(b"\0");
    }
    hex_digest(&hasher.finalize())
}

/// Lowercase hex for a digest, the same shape `gobby_core::indexing` writes.
fn hex_digest(bytes: &[u8]) -> String {
    let mut hex = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(&mut hex, "{byte:02x}").expect("writing to a String cannot fail");
    }
    hex
}

/// sha256 over each `(member, in_degree)` pair in member order.
fn in_degree_digest(in_degree: &BTreeMap<String, usize>) -> String {
    let mut hasher = Sha256::new();
    for (member, degree) in in_degree {
        hasher.update(member.as_bytes());
        hasher.update(b"\0");
        hasher.update(degree.to_string().as_bytes());
        hasher.update(b"\0");
    }
    hex_digest(&hasher.finalize())
}

#[cfg(test)]
#[path = "partition_tests.rs"]
mod tests;
