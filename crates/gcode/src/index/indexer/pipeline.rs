use std::collections::HashMap;
use std::time::Instant;

use postgres::Client;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::api;
use crate::index::{parser, walker};

use super::file::{
    ExplicitFileRoute, create_semantic_resolver_if_needed, explicit_file_route, index_content_only,
    index_file,
};
use super::lifecycle::{
    attach_projection_sync, cleanup_deleted_file_projections, current_file_hashes,
    get_orphan_files, get_stale_files, refresh_project_stats,
};
use super::overlay::index_overlay_files;
use super::types::{IndexOutcome, IndexRequest};
use super::util::{
    DEFAULT_EXCLUDES, filter_discovered_paths, relative_path, requested_relative_path,
};

pub fn index_files(request: IndexRequest, ctx: &Context) -> anyhow::Result<IndexOutcome> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    index_files_with_connection(&mut conn, request, ctx)
}

fn index_files_with_connection(
    conn: &mut Client,
    request: IndexRequest,
    ctx: &Context,
) -> anyhow::Result<IndexOutcome> {
    if matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }) {
        return index_overlay_files(conn, &request, ctx);
    }
    if request.explicit_files.is_empty() {
        index_discovered_files(conn, &request, ctx)
    } else {
        index_explicit_files_with_connection(conn, &request, ctx)
    }
}

fn index_discovered_files(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
) -> anyhow::Result<IndexOutcome> {
    let project_id = ctx.project_id.as_str();
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let mut outcome = IndexOutcome::new(project_id);

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();
    let (mut candidates, mut content_only) = walker::discover_files(root_path, &excludes);
    if let Some(filter) = request.path_filter.as_deref() {
        candidates = filter_discovered_paths(root_path, filter, candidates);
        content_only = filter_discovered_paths(root_path, filter, content_only);
    }
    let import_context = parser::build_import_resolution_context(root_path, &candidates);
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &candidates, request.require_cpp_semantics)?;

    // Build current hash map for incremental detection and orphan cleanup.
    let current_hashes = current_file_hashes(root_path, &candidates, &content_only);
    let stale: Option<HashMap<String, ()>> = if !request.full {
        Some(get_stale_files(conn, project_id, &current_hashes))
    } else {
        None
    };

    // Clean orphans only during whole-project scans. Filtered scans do not know
    // about files outside the requested subtree.
    if request.path_filter.is_none() {
        let orphans = get_orphan_files(conn, project_id, &current_hashes);
        for orphan in &orphans {
            cleanup_deleted_file_projections(ctx, orphan, &mut outcome);
            api::delete_file_facts(conn, project_id, orphan)?;
        }
    }

    let eligible_files = candidates.len() + content_only.len();
    outcome.scanned_files = eligible_files;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    for path in &candidates {
        let rel = match relative_path(path, root_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            outcome.skipped_files += 1;
            continue;
        }

        match index_file(
            conn,
            path,
            project_id,
            root_path,
            &excludes,
            &import_context,
            semantic_resolver.as_deref_mut(),
        )? {
            Some(counts) => outcome.add_counts(counts),
            None => {
                outcome.skipped_files += 1;
            }
        }
    }

    for path in &content_only {
        let rel = relative_path(path, root_path).unwrap_or_default();
        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            outcome.skipped_files += 1;
            continue;
        }
        match index_content_only(conn, path, project_id, root_path, &excludes)? {
            Some(counts) => outcome.add_counts(counts),
            None => outcome.skipped_files += 1,
        }
    }
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        root_path,
        project_id,
        start.elapsed().as_millis() as u64,
        Some(eligible_files),
    );
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

fn index_explicit_files_with_connection(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
) -> anyhow::Result<IndexOutcome> {
    let project_id = ctx.project_id.as_str();
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let mut outcome = IndexOutcome::new(project_id);
    outcome.scanned_files = request.explicit_files.len();

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();
    let (candidates, _content_only) = walker::discover_files(root_path, &excludes);
    let import_context = parser::build_import_resolution_context(root_path, &candidates);
    let mut routed_files = Vec::new();
    let mut ast_files = Vec::new();

    for fp in &request.explicit_files {
        let abs = if fp.is_absolute() {
            fp.clone()
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            let rel = requested_relative_path(root_path, fp);
            cleanup_deleted_file_projections(ctx, &rel, &mut outcome);
            api::delete_file_facts(conn, project_id, &rel)?;
            continue;
        }

        match explicit_file_route(root_path, &abs, &excludes) {
            ExplicitFileRoute::Ast => {
                ast_files.push(abs.clone());
                routed_files.push((abs, ExplicitFileRoute::Ast));
            }
            ExplicitFileRoute::ContentOnly => {
                routed_files.push((abs, ExplicitFileRoute::ContentOnly));
            }
            ExplicitFileRoute::Skip => {
                outcome.skipped_files += 1;
            }
        }
    }

    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &ast_files, request.require_cpp_semantics)?;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    let routed_file_count = routed_files.len();
    for (abs, route) in routed_files {
        match route {
            ExplicitFileRoute::Ast => {
                if let Some(count) = index_file(
                    conn,
                    &abs,
                    project_id,
                    root_path,
                    &excludes,
                    &import_context,
                    semantic_resolver.as_deref_mut(),
                )? {
                    outcome.add_counts(count);
                } else {
                    outcome.skipped_files += 1;
                }
            }
            ExplicitFileRoute::ContentOnly => {
                match index_content_only(conn, &abs, project_id, root_path, &excludes)? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            _ => unreachable!("skip routes are filtered before indexing"),
        }
    }
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        root_path,
        project_id,
        start.elapsed().as_millis() as u64,
        Some(routed_file_count),
    );
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}
