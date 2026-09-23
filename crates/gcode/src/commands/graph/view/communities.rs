//! Stored project import-community list and detail views.

use std::collections::{BTreeMap, HashMap, HashSet};

use anyhow::Context as _;

use crate::cli::GraphViewArgs;
use crate::cli_error::CliError;
use crate::communities::{self, MISSING_PARTITION_HINT, StoredCommunity};
use crate::config::Context;
use crate::output::Format;

use super::render::{
    NodeKey, ViewCommunity, ViewEdgeInput, ViewNodeInput, ViewPayload, ViewSeed,
    build_view_payload, print_view,
};

const MEMBER_LIMIT: usize = 50;
const NEIGHBOR_LIMIT: usize = 12;

fn build_list_payload(
    ctx: &Context,
    args: &GraphViewArgs,
    rows: &[StoredCommunity],
) -> anyhow::Result<ViewPayload> {
    let listed = rows
        .iter()
        .filter(|row| row.member_count >= args.effective_min_size())
        .collect::<Vec<_>>();
    let listed_ids = listed
        .iter()
        .map(|row| row.community_id)
        .collect::<HashSet<_>>();
    let nodes = listed
        .iter()
        .map(|row| ViewNodeInput {
            key: community_key(row.community_id),
            name: row.label.clone(),
            kind: "community".into(),
            file: None,
            community: None,
        })
        .collect();
    let communities = listed
        .iter()
        .map(|row| view_community(row, Vec::new()))
        .collect();
    let mut counts = BTreeMap::<(i32, i32), usize>::new();
    for row in &listed {
        for &(neighbor_id, count) in &row.boundary {
            if listed_ids.contains(&neighbor_id) {
                let pair = ordered_pair(row.community_id, neighbor_id);
                counts.entry(pair).or_insert(count);
            }
        }
    }
    let edges = counts
        .keys()
        .map(|&(source, target)| ViewEdgeInput {
            source: community_key(source),
            target: community_key(target),
            rel: "IMPORTS".into(),
        })
        .collect();
    let hint = rows.is_empty().then(|| MISSING_PARTITION_HINT.to_string());
    let mut payload = build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        ViewSeed {
            id: ctx.project_id.clone(),
            name: ctx.project_root.display().to_string(),
            kind: "project".into(),
            file: None,
        },
        0,
        false,
        false,
        hint,
        nodes,
        edges,
        communities,
    )?;
    apply_edge_counts(&mut payload, &counts);
    Ok(payload)
}

fn build_detail_payload(
    ctx: &Context,
    args: &GraphViewArgs,
    selector: &str,
    rows: &[StoredCommunity],
) -> anyhow::Result<ViewPayload> {
    let selected = resolve_selector(rows, selector)?;
    let selected_key = community_key(selected.community_id);
    let selected_id = selected_key.canonical();
    let members = selected_members(selected);
    let member_truncated = members.len() < selected.members.len();
    let mut nodes = Vec::with_capacity(1 + members.len() + NEIGHBOR_LIMIT);
    nodes.push(ViewNodeInput {
        key: selected_key.clone(),
        name: selected.label.clone(),
        kind: "community".into(),
        file: None,
        community: None,
    });
    let mut community_nodes = Vec::with_capacity(members.len());
    let mut edges = Vec::with_capacity(members.len() + NEIGHBOR_LIMIT);
    for member in &members {
        let key = NodeKey::file(member.clone());
        community_nodes.push(key.canonical());
        nodes.push(ViewNodeInput {
            key: key.clone(),
            name: member.clone(),
            kind: "file".into(),
            file: Some(member.clone()),
            community: Some(selected_id.clone()),
        });
        edges.push(ViewEdgeInput {
            source: selected_key.clone(),
            target: key,
            rel: "CONTAINS".into(),
        });
    }

    let by_id = rows
        .iter()
        .map(|row| (row.community_id, row))
        .collect::<HashMap<_, _>>();
    let mut neighbors = selected
        .boundary
        .iter()
        .filter_map(|&(id, count)| by_id.get(&id).map(|row| (*row, count)))
        .collect::<Vec<_>>();
    neighbors.sort_by(|(left, left_count), (right, right_count)| {
        right_count
            .cmp(left_count)
            .then_with(|| left.community_id.cmp(&right.community_id))
    });
    let neighbor_truncated = neighbors.len() > NEIGHBOR_LIMIT;
    neighbors.truncate(NEIGHBOR_LIMIT);
    let mut counts = BTreeMap::new();
    for (neighbor, count) in neighbors {
        let neighbor_key = community_key(neighbor.community_id);
        nodes.push(ViewNodeInput {
            key: neighbor_key.clone(),
            name: neighbor.label.clone(),
            kind: "community".into(),
            file: None,
            community: None,
        });
        edges.push(ViewEdgeInput {
            source: selected_key.clone(),
            target: neighbor_key,
            rel: "IMPORTS".into(),
        });
        counts.insert((selected.community_id, neighbor.community_id), count);
    }

    let mut payload = build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        ViewSeed {
            id: selected_id,
            name: selected.label.clone(),
            kind: "community".into(),
            file: None,
        },
        0,
        neighbor_truncated,
        member_truncated,
        None,
        nodes,
        edges,
        vec![view_community(selected, community_nodes)],
    )?;
    apply_edge_counts(&mut payload, &counts);
    Ok(payload)
}

fn resolve_selector<'a>(
    rows: &'a [StoredCommunity],
    selector: &str,
) -> anyhow::Result<&'a StoredCommunity> {
    if let Ok(id) = selector.parse::<i32>() {
        return decide(
            selector,
            rows.iter().filter(|row| row.community_id == id).collect(),
        );
    }

    let exact_label = rows
        .iter()
        .filter(|row| row.label.eq_ignore_ascii_case(selector))
        .collect::<Vec<_>>();
    if !exact_label.is_empty() {
        return decide(selector, exact_label);
    }
    let exact_deterministic = rows
        .iter()
        .filter(|row| row.label_deterministic == selector)
        .collect::<Vec<_>>();
    if !exact_deterministic.is_empty() {
        return decide(selector, exact_deterministic);
    }
    let member = rows
        .iter()
        .filter(|row| row.members.iter().any(|path| path == selector))
        .collect::<Vec<_>>();
    if !member.is_empty() {
        return decide(selector, member);
    }
    if selector.contains(['/', '\\']) {
        return Err(no_match(selector));
    }

    let needle = selector.to_lowercase();
    let substring = rows
        .iter()
        .filter(|row| {
            row.label.to_lowercase().contains(&needle)
                || row.label_deterministic.to_lowercase().contains(&needle)
        })
        .collect::<Vec<_>>();
    decide(selector, substring)
}

pub(super) fn run_list(ctx: &Context, args: &GraphViewArgs, format: Format) -> anyhow::Result<()> {
    let mut conn = crate::db::connect_readonly(&ctx.database_url)?;
    let rows = communities::read_for_context(&mut conn, ctx)?;
    let payload = build_list_payload(ctx, args, &rows).context("build communities list view")?;
    print_view(&payload, format)
}

pub(super) fn run_detail(
    ctx: &Context,
    args: &GraphViewArgs,
    selector: &str,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = crate::db::connect_readonly(&ctx.database_url)?;
    let rows = communities::read_for_context(&mut conn, ctx)?;
    let payload =
        build_detail_payload(ctx, args, selector, &rows).context("build community detail view")?;
    print_view(&payload, format)
}

fn community_key(id: i32) -> NodeKey {
    NodeKey::community(id.to_string())
}

fn ordered_pair(left: i32, right: i32) -> (i32, i32) {
    if left < right {
        (left, right)
    } else {
        (right, left)
    }
}

fn view_community(row: &StoredCommunity, nodes: Vec<String>) -> ViewCommunity {
    ViewCommunity {
        id: community_key(row.community_id).canonical(),
        label: row.label.clone(),
        size: row.member_count,
        cohesion: row.cohesion,
        label_source: row.label_source.as_str().into(),
        label_stale: row.label_stale,
        nodes,
        first_member: row.members.first().cloned().unwrap_or_default(),
    }
}

fn selected_members(row: &StoredCommunity) -> Vec<String> {
    let member_set = row
        .members
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let mut selected = Vec::with_capacity(MEMBER_LIMIT.min(row.members.len()));
    let mut seen = HashSet::new();
    for representative in &row.representatives {
        if member_set.contains(representative.as_str()) && seen.insert(representative.as_str()) {
            selected.push(representative.clone());
        }
    }
    let mut remaining = row.members.iter().collect::<Vec<_>>();
    remaining.sort();
    for member in remaining {
        if selected.len() == MEMBER_LIMIT {
            break;
        }
        if seen.insert(member.as_str()) {
            selected.push(member.clone());
        }
    }
    selected.truncate(MEMBER_LIMIT);
    selected
}

fn decide<'a>(
    selector: &str,
    matches: Vec<&'a StoredCommunity>,
) -> anyhow::Result<&'a StoredCommunity> {
    match matches.as_slice() {
        [] => Err(no_match(selector)),
        [row] => Ok(*row),
        _ => {
            let rendered = matches
                .iter()
                .take(5)
                .map(|row| format!("community:{} ({})", row.community_id, row.label))
                .collect::<Vec<_>>()
                .join(", ");
            Err(usage_error(format!(
                "community selector '{selector}' is ambiguous: {rendered}"
            )))
        }
    }
}

fn no_match(selector: &str) -> anyhow::Error {
    usage_error(format!("no community matches selector '{selector}'"))
}

fn usage_error(message: String) -> anyhow::Error {
    anyhow::Error::new(CliError {
        code: "usage",
        message,
        recovery: None,
        exit_status: 2,
    })
}

fn apply_edge_counts(payload: &mut ViewPayload, counts: &BTreeMap<(i32, i32), usize>) {
    for edge in &mut payload.edges {
        if edge.rel != "IMPORTS" {
            continue;
        }
        let source = edge.source.strip_prefix("community:");
        let target = edge.target.strip_prefix("community:");
        let Some((source, target)) = source.zip(target) else {
            continue;
        };
        let Ok(source) = source.parse::<i32>() else {
            continue;
        };
        let Ok(target) = target.parse::<i32>() else {
            continue;
        };
        edge.count = counts
            .get(&(source, target))
            .or_else(|| counts.get(&ordered_pair(source, target)))
            .copied();
    }
}

#[cfg(test)]
#[path = "communities/tests.rs"]
mod tests;
