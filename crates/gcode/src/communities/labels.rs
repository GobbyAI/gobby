//! Deterministic community labels and the candidate list the model pass chooses from.
//!
//! Every label here is derived from member paths alone, so a community always
//! has a name before plan section 6 asks a model for a better one. Plan 3.2
//! persists these, so nothing in the binary reads them yet.
#![allow(dead_code)]

use std::collections::BTreeMap;

use super::partition::PartitionCommunity;

/// Version of the deterministic labeling rules: [`derive_label`] plus the
/// partition-wide [`dedupe_labels`] pass. Refresh folds it into the stored
/// partition signature, so a bump makes each project's next `gcode index`
/// rewrite its labels once.
pub(crate) const LABEL_ALGORITHM_VERSION: u32 = 2;

/// The deterministic label for one community.
///
/// A singleton is named by its path. Otherwise the deepest proper directory
/// prefix covering at least half the members wins, ties going to the wider
/// prefix and then the lexicographically smallest; failing that the top-level
/// prefix with the most coverage; failing that the member with the highest
/// in-degree.
pub(crate) fn derive_label(members: &[String], in_degree: &BTreeMap<String, usize>) -> String {
    if let [only] = members {
        return only.clone();
    }

    let coverage = prefix_coverage(members);
    let majority = coverage
        .iter()
        .filter(|(_, covered)| **covered * 2 >= members.len())
        .max_by(|(left, left_covered), (right, right_covered)| {
            depth(left)
                .cmp(&depth(right))
                .then_with(|| left_covered.cmp(right_covered))
                .then_with(|| right.cmp(left))
        });
    if let Some((prefix, _)) = majority {
        return prefix.clone();
    }

    let plurality = coverage
        .iter()
        .filter(|(prefix, _)| depth(prefix) == 1)
        .max_by(|(left, left_covered), (right, right_covered)| {
            left_covered
                .cmp(right_covered)
                .then_with(|| right.cmp(left))
        });
    if let Some((prefix, _)) = plurality {
        return prefix.clone();
    }

    members
        .iter()
        .max_by(|left, right| {
            degree(in_degree, left)
                .cmp(&degree(in_degree, right))
                .then_with(|| right.cmp(left))
        })
        .cloned()
        .unwrap_or_default()
}

/// Suffix repeated labels with ` #2`, ` #3` in partition order.
///
/// Communities arrive largest first, so the largest keeps the bare label.
pub(crate) fn dedupe_labels(labels: Vec<String>) -> Vec<String> {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    labels
        .into_iter()
        .map(|label| {
            let seen_count = seen.entry(label.clone()).or_default();
            *seen_count += 1;
            match *seen_count {
                1 => label,
                ordinal => format!("{label} #{ordinal}"),
            }
        })
        .collect()
}

/// Up to four distinct naming candidates, led by the deterministic label.
///
/// The others are the busiest member's file stem, the most common leading
/// identifier token among member stems, and the deepest directory segment every
/// member shares. `community` already carries the in-degree map, so it is not
/// passed again.
pub(crate) fn label_candidates(community: &PartitionCommunity) -> Vec<String> {
    let mut candidates: Vec<String> = Vec::with_capacity(4);
    for candidate in [
        derive_label(&community.members, &community.in_degree),
        busiest_stem(&community.members, &community.in_degree),
        common_token(&community.members),
        shared_segment(&community.members),
    ] {
        if !candidate.is_empty() && !candidates.contains(&candidate) {
            candidates.push(candidate);
        }
    }
    candidates
}

/// How many members each proper directory prefix of a member path covers.
fn prefix_coverage(members: &[String]) -> BTreeMap<String, usize> {
    let mut coverage: BTreeMap<String, usize> = BTreeMap::new();
    for member in members {
        let mut prefix = String::new();
        for segment in directories(member) {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(segment);
            *coverage.entry(prefix.clone()).or_default() += 1;
        }
    }
    coverage
}

/// The directory segments of `path`, without its file name.
fn directories(path: &str) -> Vec<&str> {
    let mut segments = path.split('/').collect::<Vec<_>>();
    segments.pop();
    segments
}

fn depth(prefix: &str) -> usize {
    prefix.split('/').count()
}

fn degree(in_degree: &BTreeMap<String, usize>, member: &str) -> usize {
    in_degree.get(member).copied().unwrap_or(0)
}

/// The file name of `path` without its extension.
fn stem(path: &str) -> &str {
    let name = path.rsplit('/').next().unwrap_or(path);
    name.split_once('.').map_or(name, |(stem, _)| stem)
}

/// The stem of the most imported member, ties to the smallest path.
fn busiest_stem(members: &[String], in_degree: &BTreeMap<String, usize>) -> String {
    members
        .iter()
        .max_by(|left, right| {
            degree(in_degree, left)
                .cmp(&degree(in_degree, right))
                .then_with(|| right.cmp(left))
        })
        .map(|member| stem(member).to_string())
        .unwrap_or_default()
}

/// The leading `_`-separated token shared by the most member stems, ties to the
/// smallest token. A token only one member carries names nothing, so it is empty.
fn common_token(members: &[String]) -> String {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for member in members {
        let token = stem(member).split('_').next().unwrap_or_default();
        if !token.is_empty() {
            *counts.entry(token).or_default() += 1;
        }
    }
    counts
        .into_iter()
        .filter(|(_, count)| *count > 1)
        .max_by(|(left, left_count), (right, right_count)| {
            left_count.cmp(right_count).then_with(|| right.cmp(left))
        })
        .map(|(token, _)| token.to_string())
        .unwrap_or_default()
}

/// The deepest directory segment every member shares.
fn shared_segment(members: &[String]) -> String {
    let mut shared: Option<Vec<&str>> = None;
    for member in members {
        let segments = directories(member);
        shared = Some(match shared {
            None => segments,
            Some(common) => common
                .into_iter()
                .zip(segments)
                .take_while(|(left, right)| left == right)
                .map(|(segment, _)| segment)
                .collect(),
        });
    }
    shared
        .and_then(|segments| segments.last().map(|segment| (*segment).to_string()))
        .unwrap_or_default()
}
