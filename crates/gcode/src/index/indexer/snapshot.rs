//! Complete indexing of eligible immutable Git snapshot blobs.

use std::collections::HashSet;
use std::time::Instant;

use anyhow::Context as _;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::evidence::Snapshot;
use crate::index::{api, languages, parser, walker};

use super::file::{
    create_semantic_resolver_if_needed, write_content_only_file_facts, write_parsed_file_facts,
};
use super::lifecycle::{get_orphan_files, refresh_project_stats};
use super::local_imports::{resolve_local_import_calls, resolve_local_import_inheritance};
use super::overlay::write_tombstone;
use super::sink::PostgresCodeFactSink;
use super::types::{IndexOutcome, IndexTarget, OverlayIndexMetadata};

pub(crate) fn index_snapshot(ctx: &Context, commit_oid: &str) -> anyhow::Result<IndexOutcome> {
    let start = Instant::now();
    let root = &ctx.project_root;
    let snapshot = Snapshot::prepare(root, &ctx.project_id, commit_oid)?;
    snapshot.verify_materialized(root)?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let (project_id, mode) = match &ctx.index_scope {
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } => {
            crate::config::validate_parent_code_index(&mut conn, &ctx.index_scope)?;
            (overlay_project_id.as_str(), api::IndexWriteMode::Overlay)
        }
        _ => (ctx.project_id.as_str(), api::IndexWriteMode::Primary),
    };
    let target = IndexTarget {
        project_id,
        root_path: root,
        mode,
    };
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    api::upsert_project_seed(&mut conn, &machine_id, project_id, root, mode)?;
    let mut outcome = IndexOutcome::new(project_id);
    if let ProjectIndexScope::Overlay {
        overlay_project_id,
        overlay_root,
        parent_project_id,
        parent_root,
    } = &ctx.index_scope
    {
        outcome.overlay = Some(OverlayIndexMetadata {
            overlay_project_id: overlay_project_id.clone(),
            overlay_root: overlay_root.to_string_lossy().into_owned(),
            parent_project_id: parent_project_id.clone(),
            parent_root: parent_root.to_string_lossy().into_owned(),
        });
    }
    let paths = snapshot
        .eligible_entries()
        .map(|entry| root.join(&entry.path))
        .collect::<Vec<_>>();
    let present = snapshot
        .eligible_entries()
        .map(|entry| entry.path.clone())
        .collect::<HashSet<_>>();
    let import_context = parser::build_import_resolution_context(root, &paths);
    let mut semantic_resolver = create_semantic_resolver_if_needed(root, &paths, false)?;
    outcome.scanned_files = paths.len();
    outcome.durations.discovery_ms = start.elapsed().as_millis() as u64;
    let indexing_start = Instant::now();

    // Every eligible file gets its own selector, including files inherited from
    // the parent. An Ask snapshot must not depend on mutable parent index state.
    for entry in snapshot.eligible_entries() {
        let source = snapshot.read_blob(&entry.path)?;
        let hash = entry
            .content_hash
            .as_deref()
            .context("eligible snapshot blob has no hash")?;
        let path = root.join(&entry.path);
        // Retain the verified bytes for content indexing when this language has
        // no parser. Never reread the mutable path to write its content facts.
        let parsed = parser::parse_source_with_semantic(
            &path,
            project_id,
            root,
            source.clone(),
            &import_context,
            semantic_resolver.as_deref_mut(),
        )?;
        let mut tx = conn
            .transaction()
            .context("start snapshot file transaction")?;
        let mut sink = PostgresCodeFactSink::new(&mut tx, project_id, root, mode)?;
        let counts = if let Some(parsed) = parsed {
            let language = languages::detect_language(&entry.path).unwrap_or("unknown");
            write_parsed_file_facts(
                &mut sink,
                project_id,
                &entry.path,
                language,
                hash,
                source.len(),
                &parsed,
            )?
        } else {
            write_content_only_file_facts(
                &mut sink,
                project_id,
                &entry.path,
                &walker::content_language(&path),
                hash,
                source.len(),
                &source,
            )?
        };
        tx.commit().context("commit snapshot file transaction")?;
        outcome.add_counts(counts);
    }

    for orphan in get_orphan_files(&mut conn, &machine_id, project_id, &present)? {
        api::delete_file_state(&mut conn, &machine_id, project_id, &orphan, root, mode)?;
    }
    if let ProjectIndexScope::Overlay {
        parent_project_id, ..
    } = &ctx.index_scope
    {
        // Hide parent-only, excluded, and later-added files from overlay reads.
        for path in db::list_indexed_file_paths(&mut conn, parent_project_id)? {
            if !present.contains(&path) {
                write_tombstone(&mut conn, project_id, root, &path)?;
                outcome.tombstones_indexed += 1;
            }
        }
    }
    resolve_local_import_calls(&mut conn, project_id, &outcome.indexed_file_paths)?;
    let promoted =
        resolve_local_import_inheritance(&mut conn, project_id, &outcome.indexed_file_paths)?;
    outcome.record_promotion_owners(promoted);
    // Fail closed if materialization changed during parsing or fact writes.
    snapshot.verify_materialized(root)?;
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;
    refresh_project_stats(
        &mut conn,
        &machine_id,
        target,
        start.elapsed().as_millis() as u64,
        Some(present.len()),
        Some(env!("CARGO_PKG_VERSION")),
        true,
    )?;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;
    Ok(outcome)
}
