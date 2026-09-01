use std::collections::HashSet;
use std::path::Path;
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
    attach_projection_sync, current_file_state, get_orphan_files, get_stale_files,
    refresh_project_stats,
};
use super::local_imports::{
    resolve_local_import_calls, resolve_local_import_inheritance,
    resolve_project_local_import_calls,
};
use super::overlay::index_overlay_files;
use super::types::{IndexOptions, IndexOutcome, IndexProgressSink, IndexRequest, IndexTarget};
use super::util::{
    effective_excludes, filter_discovered_paths, relative_path, requested_relative_path,
    unsupported_file_types,
};

pub fn index_files(
    request: IndexRequest,
    ctx: &Context,
    options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    index_files_with_connection(&mut conn, request, ctx, options)
}

fn index_files_with_connection(
    conn: &mut Client,
    request: IndexRequest,
    ctx: &Context,
    mut options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    if matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }) {
        return index_overlay_files(conn, &request, ctx);
    }
    if request.explicit_files.is_empty() {
        index_discovered_files(conn, &request, ctx, &mut options)
    } else {
        index_explicit_files_with_connection(conn, &request, ctx, &mut options)
    }
}

fn index_discovered_files(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
    options: &mut IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let project_id = ctx.project_id.as_str();
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let target = IndexTarget {
        project_id,
        root_path,
        mode: api::IndexWriteMode::Primary,
    };
    let mut outcome = IndexOutcome::new(project_id);
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    api::upsert_project_seed(
        conn,
        &machine_id,
        project_id,
        root_path,
        api::IndexWriteMode::Primary,
    )?;

    let excludes = effective_excludes(&ctx.indexing.extra_excludes);
    let (mut candidates, mut content_only) =
        walker::discover_files_with_options(root_path, &excludes, discovery_options(ctx));
    if let Some(filter) = request.path_filter.as_deref() {
        candidates = filter_discovered_paths(root_path, filter, candidates);
        content_only = filter_discovered_paths(root_path, filter, content_only);
    }
    outcome.set_unsupported_file_types(unsupported_file_types(root_path, &content_only));
    let discovered_files = candidates.len() + content_only.len();
    let import_context = parser::build_import_resolution_context(root_path, &candidates);
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &candidates, request.require_cpp_semantics)?;

    // Build current file state for incremental detection and orphan cleanup.
    let current_files = current_file_state(root_path, &candidates, &content_only);
    let stale: Option<HashSet<String>> = if !request.full {
        Some(get_stale_files(
            conn,
            &machine_id,
            project_id,
            &current_files.hashes,
        )?)
    } else {
        None
    };

    // Clean orphans only during whole-project scans. Filtered scans do not know
    // about files outside the requested subtree.
    if request.path_filter.is_none() {
        let orphans =
            get_orphan_files(conn, &machine_id, project_id, &current_files.present_paths)?;
        for orphan in &orphans {
            api::delete_file_state(
                conn,
                &machine_id,
                project_id,
                orphan,
                root_path,
                api::IndexWriteMode::Primary,
            )?;
        }
    }

    let eligible_files = if let Some(stale_map) = stale.as_ref() {
        candidates
            .iter()
            .chain(content_only.iter())
            .filter_map(|path| relative_path(path, root_path).ok())
            .filter(|rel| stale_map.contains(rel))
            .count()
    } else {
        discovered_files
    };
    outcome.scanned_files = discovered_files;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    let mut adopted_paths = Vec::new();
    let mut progress = ActiveIndexProgress::new(options.progress.take(), eligible_files);
    for path in &candidates {
        let rel = match relative_path(path, root_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        if let Some(ref stale_map) = stale
            && !stale_map.contains(&rel)
        {
            outcome.skipped_files += 1;
            continue;
        }
        if !request.full
            && let Some(content_hash) = current_files.hashes.get(&rel)
            && api::adopt_file_state(
                conn,
                &machine_id,
                project_id,
                &rel,
                content_hash,
                root_path,
                api::IndexWriteMode::Primary,
            )?
        {
            adopted_paths.push(rel.clone());
            outcome.skipped_files += 1;
            progress.advance(&rel);
            continue;
        }

        match index_file(
            conn,
            path,
            target,
            &excludes,
            &import_context,
            semantic_resolver.as_deref_mut(),
        )? {
            Some(counts) => outcome.add_counts(counts),
            None => {
                api::delete_file_state(
                    conn,
                    &machine_id,
                    project_id,
                    &rel,
                    root_path,
                    api::IndexWriteMode::Primary,
                )?;
                outcome.skipped_files += 1;
            }
        }
        progress.advance(&rel);
    }

    for path in &content_only {
        let rel = match relative_path(path, root_path) {
            Ok(r) => r,
            Err(_) => continue,
        };
        if let Some(ref stale_map) = stale
            && !stale_map.contains(&rel)
        {
            outcome.skipped_files += 1;
            continue;
        }
        if !request.full
            && let Some(content_hash) = current_files.hashes.get(&rel)
            && api::adopt_file_state(
                conn,
                &machine_id,
                project_id,
                &rel,
                content_hash,
                root_path,
                api::IndexWriteMode::Primary,
            )?
        {
            adopted_paths.push(rel.clone());
            outcome.skipped_files += 1;
            progress.advance(&rel);
            continue;
        }
        match index_content_only(conn, path, target, &excludes)? {
            Some(counts) => outcome.add_counts(counts),
            None => outcome.skipped_files += 1,
        }
        progress.advance(&rel);
    }
    // Resolve cross-file local-import calls now that every file's symbols are in
    // the hub. Order-independent and bounded by this run's changed files.
    resolve_local_import_calls(conn, project_id, &outcome.indexed_file_paths)?;
    if request.full && request.path_filter.is_none() {
        resolve_project_local_import_calls(conn, project_id)?;
    }
    let mut trigger_paths = outcome.indexed_file_paths.clone();
    trigger_paths.extend(adopted_paths);
    let promoted_owners = resolve_local_import_inheritance(conn, project_id, &trigger_paths)?;
    outcome.record_promotion_owners(promoted_owners);
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        &machine_id,
        target,
        start.elapsed().as_millis() as u64,
        Some(eligible_files),
        (request.full && request.path_filter.is_none()).then_some(env!("CARGO_PKG_VERSION")),
    )?;
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

fn index_explicit_files_with_connection(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
    options: &mut IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let project_id = ctx.project_id.as_str();
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let target = IndexTarget {
        project_id,
        root_path,
        mode: api::IndexWriteMode::Primary,
    };
    let mut outcome = IndexOutcome::new(project_id);
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    api::upsert_project_seed(
        conn,
        &machine_id,
        project_id,
        root_path,
        api::IndexWriteMode::Primary,
    )?;
    outcome.scanned_files = request.explicit_files.len();

    let excludes = effective_excludes(&ctx.indexing.extra_excludes);
    let mut routed_files = Vec::new();
    let mut ast_files = Vec::new();
    let mut content_only_files = Vec::new();
    for fp in &request.explicit_files {
        let abs = if fp.is_absolute() {
            fp.clone()
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            let rel = requested_relative_path(root_path, fp);
            api::delete_file_state(
                conn,
                &machine_id,
                project_id,
                &rel,
                root_path,
                api::IndexWriteMode::Primary,
            )?;
            continue;
        }

        let route = explicit_route_with_discovery_options(
            root_path,
            &abs,
            &excludes,
            discovery_options(ctx),
        );

        match route {
            ExplicitFileRoute::Ast => {
                ast_files.push(abs.clone());
                routed_files.push((abs, ExplicitFileRoute::Ast));
            }
            ExplicitFileRoute::ContentOnly => {
                content_only_files.push(abs.clone());
                routed_files.push((abs, ExplicitFileRoute::ContentOnly));
            }
            ExplicitFileRoute::Skip => {
                let Ok(rel) = relative_path(&abs, root_path) else {
                    outcome.skipped_files += 1;
                    continue;
                };
                api::delete_file_state(
                    conn,
                    &machine_id,
                    project_id,
                    &rel,
                    root_path,
                    api::IndexWriteMode::Primary,
                )?;
                outcome.skipped_files += 1;
            }
        }
    }
    outcome.set_unsupported_file_types(unsupported_file_types(root_path, &content_only_files));

    let mut seen_import_candidates = std::collections::HashSet::new();
    let mut import_candidates = db::list_indexed_file_paths(conn, project_id)?
        .into_iter()
        .map(|path| root_path.join(path))
        .filter(|path| seen_import_candidates.insert(path.clone()))
        .collect::<Vec<_>>();
    for path in &ast_files {
        if seen_import_candidates.insert(path.clone()) {
            import_candidates.push(path.clone());
        }
    }
    let import_context = parser::build_import_resolution_context(root_path, &import_candidates);

    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &ast_files, request.require_cpp_semantics)?;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    let routed_file_count = routed_files.len();
    let mut adopted_paths = Vec::new();
    let mut progress = ActiveIndexProgress::new(options.progress.take(), routed_file_count);
    for (abs, route) in routed_files {
        let rel = relative_path(&abs, root_path).ok();
        if !request.full
            && let Some(rel) = rel.as_deref()
            && let Ok(content_hash) = crate::index::hasher::file_content_hash(&abs)
            && api::adopt_file_state(
                conn,
                &machine_id,
                project_id,
                rel,
                &content_hash,
                root_path,
                api::IndexWriteMode::Primary,
            )?
        {
            adopted_paths.push(rel.to_string());
            outcome.skipped_files += 1;
            progress.advance(rel);
            continue;
        }
        match route {
            ExplicitFileRoute::Ast => {
                if let Some(count) = index_file(
                    conn,
                    &abs,
                    target,
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
                match index_content_only(conn, &abs, target, &excludes)? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            _ => unreachable!("skip routes are filtered before indexing"),
        }
        if let Some(rel) = rel {
            progress.advance(&rel);
        }
    }
    // Resolve cross-file local-import calls now that every file's symbols are in
    // the hub. Order-independent and bounded by this run's changed files.
    resolve_local_import_calls(conn, project_id, &outcome.indexed_file_paths)?;
    let mut trigger_paths = outcome.indexed_file_paths.clone();
    trigger_paths.extend(adopted_paths);
    let promoted_owners = resolve_local_import_inheritance(conn, project_id, &trigger_paths)?;
    outcome.record_promotion_owners(promoted_owners);
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        &machine_id,
        target,
        start.elapsed().as_millis() as u64,
        Some(routed_file_count),
        None,
    )?;
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

struct ActiveIndexProgress<'a> {
    sink: Option<&'a mut dyn IndexProgressSink>,
    started: bool,
}

impl<'a> ActiveIndexProgress<'a> {
    fn new(sink: Option<&'a mut dyn IndexProgressSink>, total: usize) -> Self {
        let mut progress = Self {
            sink,
            started: total > 0,
        };
        if progress.started
            && let Some(sink) = progress.sink.as_deref_mut()
        {
            sink.start(total);
        }
        progress
    }

    fn advance(&mut self, file_path: &str) {
        if let Some(sink) = self.sink.as_deref_mut() {
            sink.advance(file_path);
        }
    }
}

impl Drop for ActiveIndexProgress<'_> {
    fn drop(&mut self) {
        if self.started
            && let Some(sink) = self.sink.as_deref_mut()
        {
            sink.finish();
        }
    }
}

fn discovery_options(ctx: &Context) -> walker::DiscoveryOptions {
    walker::DiscoveryOptions {
        respect_gitignore: ctx.indexing.respect_gitignore,
    }
}

pub(super) fn explicit_route_with_discovery_options(
    root_path: &Path,
    abs: &Path,
    excludes: &[&str],
    options: walker::DiscoveryOptions,
) -> ExplicitFileRoute {
    if !options.respect_gitignore {
        return explicit_file_route(root_path, abs, excludes);
    }
    match walker::classify_explicit_file_with_options(root_path, abs, excludes, options) {
        Some(walker::FileClassification::Ast) => ExplicitFileRoute::Ast,
        Some(walker::FileClassification::ContentOnly) => ExplicitFileRoute::ContentOnly,
        None => ExplicitFileRoute::Skip,
    }
}

#[cfg(test)]
mod progress_tests {
    use super::*;

    #[derive(Default)]
    struct RecordingProgress {
        events: Vec<String>,
    }

    impl IndexProgressSink for RecordingProgress {
        fn start(&mut self, total: usize) {
            self.events.push(format!("start:{total}"));
        }

        fn advance(&mut self, file_path: &str) {
            self.events.push(format!("advance:{file_path}"));
        }

        fn finish(&mut self) {
            self.events.push("finish".to_string());
        }
    }

    #[test]
    fn index_progress_reports_start_advance_and_finish() {
        let mut sink = RecordingProgress::default();
        {
            let mut progress = ActiveIndexProgress::new(Some(&mut sink), 2);
            progress.advance("src/one.rs");
            progress.advance("src/two.rs");
        }

        assert_eq!(
            sink.events,
            vec![
                "start:2",
                "advance:src/one.rs",
                "advance:src/two.rs",
                "finish"
            ]
        );
    }

    #[test]
    fn index_progress_zero_total_is_noop() {
        let mut sink = RecordingProgress::default();
        {
            let _progress = ActiveIndexProgress::new(Some(&mut sink), 0);
        }

        assert!(sink.events.is_empty());
    }
}
