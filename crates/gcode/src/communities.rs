//! Project-wide file import communities.
//!
//! [`identity`] resolves module names to the visible files that provide them and
//! loads the import rows a partition is built from. [`partition`] turns those
//! rows into communities and [`labels`] names them. Plan sections 2.3 and 3.2
//! add the id remapping and the persistence beside them.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::SystemTime;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::indexer::CommunityRefreshReport;
use chrono::{DateTime, Utc};
use postgres::Client;

use self::identity::load_project_imports;
use self::labels::LABEL_ALGORITHM_VERSION;
use self::partition::{PartitionCommunity, ProjectPartition, build_partition};
use self::remap::{AssignedCommunity, PriorCommunity, assign_ids};

#[allow(dead_code)] // consumed by the 3.2 persistence stage
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum LabelSource {
    Deterministic,
    Model,
}

#[allow(dead_code)] // consumed by the 3.2 persistence stage
impl LabelSource {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Deterministic => "deterministic",
            Self::Model => "model",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<Self> {
        match value {
            "deterministic" => Some(Self::Deterministic),
            "model" => Some(Self::Model),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct StoredCommunity {
    pub machine_id: String,
    pub project_id: String,
    pub community_id: i32,
    pub member_count: usize,
    pub members: Vec<String>,
    pub representatives: Vec<String>,
    pub internal_edges: usize,
    pub cohesion: f64,
    pub boundary: Vec<(i32, usize)>,
    pub member_signature: String,
    pub label_deterministic: String,
    pub label: String,
    pub label_source: LabelSource,
    pub label_confidence: Option<f64>,
    pub label_model: Option<String>,
    pub label_candidates: Vec<String>,
    pub labeled_signature: Option<String>,
    pub labeled_at: Option<SystemTime>,
    pub label_attempted_at: Option<SystemTime>,
    pub refreshed_at: SystemTime,
    pub label_stale: bool,
}

#[allow(dead_code, reason = "consumed by the stored-community read surfaces")]
pub(crate) const MISSING_PARTITION_HINT: &str = "No stored import communities for this \
project; run `gcode index` to compute them. Node `community` stays null until then; this \
view never computes a partition inline.";

pub(crate) fn refresh_project_communities(
    conn: &mut Client,
    ctx: &Context,
) -> anyhow::Result<CommunityRefreshReport> {
    let imports = load_project_imports(conn, ctx)?;
    let partition = build_partition(&imports.identity, &imports.rows)?;
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let target_project_id = storage_project_id(ctx);
    let mut replace = db::begin_replace(conn, &machine_id, target_project_id)?;
    let target_signature = replace.partition_signature().map(str::to_owned);
    if let ProjectIndexScope::Overlay {
        parent_project_id, ..
    } = &ctx.index_scope
    {
        replace.seed_from_parent(parent_project_id)?;
    }
    let stored_signature = format!(
        "{LABEL_ALGORITHM_VERSION}:{}",
        partition.partition_signature
    );
    if target_signature.as_deref() == Some(stored_signature.as_str()) {
        replace.skip()?;
        return Ok(CommunityRefreshReport {
            communities: partition.communities.len(),
            skipped_unchanged: true,
            ..CommunityRefreshReport::default()
        });
    }

    let prior = replace
        .prior()
        .iter()
        .map(prior_community)
        .collect::<Vec<_>>();
    let (assigned, watermark) = assign_ids(&partition, &prior, replace.watermark());
    let new_ids = assigned
        .iter()
        .filter(|community| community.matched_prior.is_none())
        .count();
    let matched_ids = assigned
        .iter()
        .filter_map(|community| community.matched_prior)
        .collect::<HashSet<_>>();
    let retired_ids = prior
        .iter()
        .filter(|community| !matched_ids.contains(&community.community_id))
        .count();
    let rows = stored_rows(&machine_id, target_project_id, &partition, &assigned);
    let changed = rows
        .iter()
        .filter(|row| {
            replace
                .prior()
                .iter()
                .find(|prior| prior.community_id == row.community_id)
                .is_some_and(|prior| stored_content_changed(prior, row))
        })
        .count();
    let communities = rows.len();
    replace.commit(rows, watermark, &stored_signature)?;
    Ok(CommunityRefreshReport {
        communities,
        changed,
        new_ids,
        retired_ids,
        skipped_unchanged: false,
    })
}

#[allow(dead_code, reason = "consumed by the stored-community read surfaces")]
pub(crate) fn read_for_context(
    conn: &mut Client,
    ctx: &Context,
) -> anyhow::Result<Vec<StoredCommunity>> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let target_project_id = storage_project_id(ctx);
    let mut rows = db::read_project_communities(conn, &machine_id, target_project_id)?;
    if rows.is_empty()
        && db::read_partition_signature(conn, &machine_id, target_project_id)?.is_none()
        && let ProjectIndexScope::Overlay {
            parent_project_id, ..
        } = &ctx.index_scope
    {
        rows = db::read_project_communities(conn, &machine_id, parent_project_id)?;
    }
    for row in &mut rows {
        row.label_stale = row.label_source == LabelSource::Model
            && row.labeled_signature.as_deref() != Some(row.member_signature.as_str());
        if row.label_stale {
            row.label = row.label_deterministic.clone();
            row.label_source = LabelSource::Deterministic;
            row.label_confidence = None;
            row.label_model = None;
        }
    }
    Ok(rows)
}

#[allow(dead_code, reason = "consumed by the stored-community read surfaces")]
pub(crate) fn partition_refreshed(conn: &mut Client, ctx: &Context) -> anyhow::Result<bool> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let target_project_id = storage_project_id(ctx);
    if db::read_partition_signature(conn, &machine_id, target_project_id)?.is_some() {
        return Ok(true);
    }
    if let ProjectIndexScope::Overlay {
        parent_project_id, ..
    } = &ctx.index_scope
    {
        return Ok(db::read_partition_signature(conn, &machine_id, parent_project_id)?.is_some());
    }
    Ok(false)
}

fn storage_project_id(ctx: &Context) -> &str {
    match &ctx.index_scope {
        ProjectIndexScope::Single => &ctx.project_id,
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } => overlay_project_id,
    }
}

fn prior_community(stored: &StoredCommunity) -> PriorCommunity {
    PriorCommunity {
        community_id: stored.community_id,
        members: stored.members.clone(),
        member_signature: stored.member_signature.clone(),
        label: stored.label.clone(),
        label_deterministic: stored.label_deterministic.clone(),
        label_source: stored.label_source,
        label_confidence: stored.label_confidence,
        label_model: stored.label_model.clone(),
        labeled_signature: stored.labeled_signature.clone(),
        labeled_at: stored.labeled_at.map(DateTime::<Utc>::from),
        label_attempted_at: stored.label_attempted_at.map(DateTime::<Utc>::from),
    }
}

fn stored_rows(
    machine_id: &str,
    project_id: &str,
    partition: &ProjectPartition,
    assigned: &[AssignedCommunity],
) -> Vec<StoredCommunity> {
    let boundary = community_boundaries(partition, assigned);
    let refreshed_at = SystemTime::now();
    assigned
        .iter()
        .map(|assigned| {
            let community = &partition.communities[assigned.partition_index];
            StoredCommunity {
                machine_id: machine_id.to_string(),
                project_id: project_id.to_string(),
                community_id: assigned.community_id,
                member_count: community.members.len(),
                members: community.members.clone(),
                representatives: representatives(community),
                internal_edges: community.internal_edges,
                cohesion: community.cohesion,
                boundary: boundary
                    .get(&assigned.community_id)
                    .cloned()
                    .unwrap_or_default(),
                member_signature: community.member_signature.clone(),
                label_deterministic: assigned.label.label_deterministic.clone(),
                label: assigned.label.label.clone(),
                label_source: assigned.label.label_source,
                label_confidence: assigned.label.label_confidence,
                label_model: assigned.label.label_model.clone(),
                label_candidates: assigned.label.label_candidates.clone(),
                labeled_signature: assigned.label.labeled_signature.clone(),
                labeled_at: assigned.label.labeled_at.map(SystemTime::from),
                label_attempted_at: assigned.label.label_attempted_at.map(SystemTime::from),
                refreshed_at,
                label_stale: false,
            }
        })
        .collect()
}

fn representatives(community: &PartitionCommunity) -> Vec<String> {
    let mut members = community.members.clone();
    members.sort_by(|left, right| {
        community
            .in_degree
            .get(right)
            .copied()
            .unwrap_or(0)
            .cmp(&community.in_degree.get(left).copied().unwrap_or(0))
            .then_with(|| left.cmp(right))
    });
    members.truncate(5);
    members
}

fn community_boundaries(
    partition: &ProjectPartition,
    assigned: &[AssignedCommunity],
) -> BTreeMap<i32, Vec<(i32, usize)>> {
    let community_by_member = assigned
        .iter()
        .flat_map(|assigned| {
            partition.communities[assigned.partition_index]
                .members
                .iter()
                .map(move |member| (member.as_str(), assigned.community_id))
        })
        .collect::<HashMap<_, _>>();
    let mut pair_counts = BTreeMap::<(i32, i32), usize>::new();
    for edge in &partition.directed {
        let Some(&source) = community_by_member.get(edge.importer.as_str()) else {
            continue;
        };
        let Some(&target) = community_by_member.get(edge.provider.as_str()) else {
            continue;
        };
        if source == target {
            continue;
        }
        let pair = if source < target {
            (source, target)
        } else {
            (target, source)
        };
        *pair_counts.entry(pair).or_default() += edge.count;
    }
    let mut boundary = BTreeMap::<i32, Vec<(i32, usize)>>::new();
    for ((left, right), import_count) in pair_counts {
        boundary
            .entry(left)
            .or_default()
            .push((right, import_count));
        boundary
            .entry(right)
            .or_default()
            .push((left, import_count));
    }
    boundary
}

fn stored_content_changed(left: &StoredCommunity, right: &StoredCommunity) -> bool {
    left.members != right.members
        || left.representatives != right.representatives
        || left.internal_edges != right.internal_edges
        || left.cohesion != right.cohesion
        || left.boundary != right.boundary
        || left.member_signature != right.member_signature
        || left.label_deterministic != right.label_deterministic
        || left.label != right.label
        || left.label_source != right.label_source
        || left.label_confidence != right.label_confidence
        || left.label_model != right.label_model
        || left.label_candidates != right.label_candidates
        || left.labeled_signature != right.labeled_signature
        || left.labeled_at != right.labeled_at
        || left.label_attempted_at != right.label_attempted_at
}

pub(crate) mod identity;
pub(crate) mod labels;
pub(crate) mod partition;
pub(crate) mod remap;

#[cfg(test)]
#[path = "communities/refresh_tests.rs"]
mod refresh_tests;
