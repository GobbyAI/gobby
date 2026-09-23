//! Stable community ids and label carry-forward across partition rebuilds.
//!
//! Plan section 3.2 consumes these assignments when it persists community rows,
//! so production callers arrive in the next stage.
#![allow(dead_code)]

use std::cmp::Ordering;
use std::collections::HashSet;

use chrono::{DateTime, Utc};

use super::LabelSource;
use super::labels::{dedupe_labels, derive_label, label_candidates};
use super::partition::{PartitionCommunity, ProjectPartition};

#[derive(Clone, Debug)]
pub(crate) struct PriorCommunity {
    pub community_id: i32,
    pub members: Vec<String>,
    pub member_signature: String,
    pub label: String,
    pub label_deterministic: String,
    pub label_source: LabelSource,
    pub label_confidence: Option<f64>,
    pub label_model: Option<String>,
    pub labeled_signature: Option<String>,
    pub labeled_at: Option<DateTime<Utc>>,
    pub label_attempted_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct LabelCarry {
    pub label: String,
    pub label_deterministic: String,
    pub label_source: LabelSource,
    pub label_confidence: Option<f64>,
    pub label_model: Option<String>,
    pub labeled_signature: Option<String>,
    pub labeled_at: Option<DateTime<Utc>>,
    pub label_attempted_at: Option<DateTime<Utc>>,
    pub label_candidates: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct AssignedCommunity {
    pub community_id: i32,
    pub matched_prior: Option<i32>,
    pub partition_index: usize,
    pub label: LabelCarry,
}

#[derive(Clone, Copy, Debug)]
struct MatchCandidate {
    new_index: usize,
    prior_index: usize,
    old_id: i32,
    overlap: usize,
    union: usize,
}

pub(crate) fn assign_ids(
    partition: &ProjectPartition,
    prior: &[PriorCommunity],
    watermark: i32,
) -> (Vec<AssignedCommunity>, i32) {
    let mut candidates = match_candidates(partition, prior);
    candidates.sort_by(compare_candidates);

    let mut matched_prior = vec![false; prior.len()];
    let mut prior_by_new = vec![None; partition.communities.len()];
    for candidate in candidates {
        if prior_by_new[candidate.new_index].is_none() && !matched_prior[candidate.prior_index] {
            prior_by_new[candidate.new_index] = Some(candidate.prior_index);
            matched_prior[candidate.prior_index] = true;
        }
    }

    let mut next_id = prior
        .iter()
        .map(|community| community.community_id)
        .max()
        .unwrap_or(watermark)
        .max(watermark);
    let mut new_watermark = watermark;
    let deterministic = dedupe_labels(
        partition
            .communities
            .iter()
            .map(|community| derive_label(&community.members, &community.in_degree))
            .collect(),
    );
    let assigned = partition
        .communities
        .iter()
        .zip(deterministic)
        .enumerate()
        .map(|(partition_index, (community, deterministic))| {
            let matched = prior_by_new[partition_index].map(|index| &prior[index]);
            let community_id = match matched {
                Some(previous) => previous.community_id,
                None => {
                    next_id += 1;
                    new_watermark = next_id;
                    next_id
                }
            };
            AssignedCommunity {
                community_id,
                matched_prior: matched.map(|previous| previous.community_id),
                partition_index,
                label: carry_label(community, matched, deterministic),
            }
        })
        .collect();

    (assigned, new_watermark)
}

fn match_candidates(partition: &ProjectPartition, prior: &[PriorCommunity]) -> Vec<MatchCandidate> {
    let mut candidates = Vec::new();
    for (new_index, current) in partition.communities.iter().enumerate() {
        for (prior_index, previous) in prior.iter().enumerate() {
            let overlap = overlap(&current.members, &previous.members);
            if overlap == 0 {
                continue;
            }
            candidates.push(MatchCandidate {
                new_index,
                prior_index,
                old_id: previous.community_id,
                overlap,
                union: current.members.len() + previous.members.len() - overlap,
            });
        }
    }
    candidates
}

fn overlap(current: &[String], previous: &[String]) -> usize {
    let previous: HashSet<&str> = previous.iter().map(String::as_str).collect();
    current
        .iter()
        .filter(|member| previous.contains(member.as_str()))
        .count()
}

fn compare_candidates(left: &MatchCandidate, right: &MatchCandidate) -> Ordering {
    let left_ratio = left.overlap as u128 * right.union as u128;
    let right_ratio = right.overlap as u128 * left.union as u128;
    right_ratio
        .cmp(&left_ratio)
        .then_with(|| right.overlap.cmp(&left.overlap))
        .then_with(|| left.old_id.cmp(&right.old_id))
        .then_with(|| left.new_index.cmp(&right.new_index))
}

/// `deterministic` is the derived label after the partition-wide ` #N` dedupe, so
/// every arm writes the current ordinal. It also leads the candidates, so the
/// labeler cannot re-admit the bare name as a deterministic pick.
fn carry_label(
    current: &PartitionCommunity,
    previous: Option<&PriorCommunity>,
    deterministic: String,
) -> LabelCarry {
    let mut candidates = label_candidates(current);
    if let Some(first) = candidates.first_mut() {
        first.clone_from(&deterministic);
    }
    match previous {
        None => LabelCarry {
            label: deterministic.clone(),
            label_deterministic: deterministic,
            label_source: LabelSource::Deterministic,
            label_confidence: None,
            label_model: None,
            labeled_signature: None,
            labeled_at: None,
            label_attempted_at: None,
            label_candidates: candidates,
        },
        Some(previous) if previous.member_signature == current.member_signature => LabelCarry {
            // A derived label takes the current ordinal; a model label or a
            // gate-skipped candidate pick (6.3) is carried.
            label: if previous.label_source == LabelSource::Deterministic
                && previous.label == previous.label_deterministic
            {
                deterministic.clone()
            } else {
                previous.label.clone()
            },
            label_deterministic: deterministic,
            label_source: previous.label_source,
            label_confidence: previous.label_confidence,
            label_model: previous.label_model.clone(),
            labeled_signature: previous.labeled_signature.clone(),
            labeled_at: previous.labeled_at,
            label_attempted_at: previous.label_attempted_at,
            label_candidates: candidates,
        },
        Some(previous) if previous.label_source == LabelSource::Deterministic => LabelCarry {
            label: deterministic.clone(),
            label_deterministic: deterministic,
            label_source: LabelSource::Deterministic,
            label_confidence: None,
            label_model: None,
            labeled_signature: None,
            labeled_at: None,
            label_attempted_at: previous.label_attempted_at,
            label_candidates: candidates,
        },
        Some(previous) => LabelCarry {
            label: previous.label.clone(),
            label_deterministic: deterministic,
            label_source: LabelSource::Model,
            label_confidence: previous.label_confidence,
            label_model: previous.label_model.clone(),
            labeled_signature: previous.labeled_signature.clone(),
            labeled_at: previous.labeled_at,
            label_attempted_at: previous.label_attempted_at,
            label_candidates: candidates,
        },
    }
}

#[cfg(test)]
#[path = "remap/tests.rs"]
mod tests;
