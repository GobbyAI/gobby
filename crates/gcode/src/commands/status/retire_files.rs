//! Explicit retirement is separate from age- and Git-protected ordinary GC.

mod backend;
mod manifest;

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

use anyhow::{Context as _, ensure};
use postgres::Client;
use serde::{Deserialize, Serialize};

use crate::config::Context;
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lease_project_lock};
use crate::output;

use super::content_gc::{
    ContentGcCandidate, content_is_unreferenced, delete_candidate_projections,
    delete_unreferenced_content_row,
};
use manifest::{Manifest, RetiredFile};

#[derive(Debug, Deserialize, Serialize)]
struct Receipt {
    backends: backend::BackendIdentity,
    version: u32,
    manifest_digest: String,
    source_inventory_digest: String,
    project_id: String,
    machine_id: String,
    root_path: String,
    mode: String,
    complete: bool,
    files: BTreeMap<String, FileReceipt>,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct FileReceipt {
    versions: BTreeMap<String, String>,
    shell: String,
    error: Option<String>,
}

pub(crate) fn run(
    ctx: &Context,
    manifest_path: &Path,
    apply: bool,
    receipt_path: Option<&Path>,
) -> anyhow::Result<()> {
    let (manifest, manifest_digest) = manifest::read(manifest_path)?;
    let mut receipt = Receipt {
        backends: manifest.backends.clone(),
        version: 1,
        manifest_digest,
        source_inventory_digest: manifest.source_inventory_digest.clone(),
        project_id: manifest.project_id.clone(),
        machine_id: manifest.machine_id.clone(),
        root_path: manifest.root_path.to_string_lossy().into_owned(),
        mode: if apply { "apply" } else { "validate" }.to_string(),
        complete: false,
        files: BTreeMap::new(),
    };
    let mut conn = if apply {
        db::connect_readwrite(&ctx.database_url)?
    } else {
        db::connect_readonly(&ctx.database_url)?
    };
    let mut lock_conn = if apply {
        Some(db::connect_readwrite(&ctx.database_url)?)
    } else {
        None
    };
    let _lock = match lock_conn.as_mut() {
        Some(connection) => Some(
            lease_project_lock(
                connection,
                &ctx.project_id,
                IndexLockPolicy::maintenance_try(),
            )?
            .context("project index is busy; retry this same manifest")?,
        ),
        None => None,
    };
    validate_context(&mut conn, ctx, &manifest)?;
    // Validate the complete set before the first deletion. Repeat each file's
    // identity check while holding the same lock immediately before deleting it.
    for file in &manifest.files {
        let candidates = validate_file(&mut conn, &manifest, file)?;
        let current: BTreeSet<_> = candidates.iter().map(|row| row.id.as_str()).collect();
        receipt.files.insert(
            file.file_path.clone(),
            FileReceipt {
                versions: file
                    .versions
                    .iter()
                    .map(|version| {
                        (
                            version.id.clone(),
                            if current.contains(version.id.as_str()) {
                                "ready"
                            } else {
                                "already_absent"
                            }
                            .to_string(),
                        )
                    })
                    .collect(),
                shell: "pending".to_string(),
                error: None,
            },
        );
    }
    if !apply {
        receipt.complete = true;
        return output::print_json(&receipt);
    }
    // Inventory every graph scope before the first mutation, including files
    // later in the manifest. Repeat this check under the same project lock.
    for file in &manifest.files {
        let current = receipt.files[&file.file_path]
            .versions
            .iter()
            .filter(|(_, state)| state.as_str() == "ready")
            .map(|(id, _)| id.as_str())
            .collect();
        validate_projections(ctx, file, &current)?;
    }
    let receipt_path = receipt_path.context("--apply requires --receipt")?;
    validate_receipt_path(receipt_path, &receipt)?;
    write_receipt(receipt_path, &receipt)?;
    for file in &manifest.files {
        let result = retire_file(&mut conn, ctx, &manifest, file, &mut receipt, receipt_path);
        if let Err(error) = result {
            receipt
                .files
                .entry(file.file_path.clone())
                .or_default()
                .error = Some(format!("{error:#}"));
            write_receipt(receipt_path, &receipt)?;
            output::print_json(&receipt)?;
            return Err(error);
        }
    }
    // A writer that disregards the shared lock must not turn a partial result
    // into success. A fresh selector or version is rejected even on retry.
    validate_context(&mut conn, ctx, &manifest)?;
    for file in &manifest.files {
        ensure!(
            validate_file(&mut conn, &manifest, file)?.is_empty(),
            "retired content reappeared"
        );
    }
    receipt.complete = true;
    write_receipt(receipt_path, &receipt)?;
    output::print_json(&receipt)
}

fn validate_context(conn: &mut Client, ctx: &Context, manifest: &Manifest) -> anyhow::Result<()> {
    ensure!(
        backend::BackendIdentity::from_context(ctx)? == manifest.backends,
        "acquired grant backends differ from retirement inventory"
    );
    let database: String = conn.query_one("SELECT current_database()", &[])?.get(0);
    ensure!(
        database == manifest.backends.postgres_database,
        "connected database differs from inventory"
    );
    ensure!(
        ctx.project_id == manifest.project_id,
        "manifest project differs from command project"
    );
    ensure!(
        gobby_core::machine::read_local_machine_id()? == manifest.machine_id,
        "manifest machine differs from this machine"
    );
    manifest::no_symlinks(&manifest.root_path)?;
    ensure!(
        ctx.project_root.canonicalize()? == manifest.root_path,
        "manifest root differs from canonical command root"
    );
    ensure!(
        manifest.root_path.is_dir(),
        "manifest checkout root is absent"
    );
    let present: bool = conn
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM project_checkouts
         WHERE machine_id = $1 AND project_id = $2 AND root_path = $3)",
            &[
                &db::id_param(&manifest.machine_id)?,
                &db::id_param(&manifest.project_id)?,
                &manifest.root_path.to_string_lossy().as_ref(),
            ],
        )?
        .get(0);
    if !present {
        let identity = crate::config::resolve_project_identity(&manifest.root_path)?;
        ensure!(
            matches!(
                identity.source,
                crate::config::ProjectIdentitySource::IsolatedOverlay
                    | crate::config::ProjectIdentitySource::IsolatedRoot
                    | crate::config::ProjectIdentitySource::LinkedWorktree
            ),
            "manifest does not identify this machine's registered primary checkout"
        );
        ensure!(
            identity.project_id == manifest.project_id && identity.root == manifest.root_path,
            "synthetic index identity differs from retirement manifest"
        );
        let recorded: bool = conn
            .query_one(
                "SELECT EXISTS(SELECT 1 FROM code_indexed_project_states
             WHERE machine_id=$1 AND project_id=$2 AND root_path=$3)",
                &[
                    &db::id_param(&manifest.machine_id)?,
                    &db::id_param(&manifest.project_id)?,
                    &manifest.root_path.to_string_lossy().as_ref(),
                ],
            )?
            .get(0);
        ensure!(
            recorded,
            "synthetic index is not recorded for this machine and root"
        );
    }
    Ok(())
}

fn validate_file(
    conn: &mut Client,
    manifest: &Manifest,
    file: &RetiredFile,
) -> anyhow::Result<Vec<ContentGcCandidate>> {
    manifest::require_absent(&manifest.root_path, &file.file_path)?;
    let project_id = db::id_param(&manifest.project_id)?;
    let references: i64 = conn.query_one(
        "SELECT COUNT(*) FROM code_indexed_file_states WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file.file_path],
    )?.get(0);
    ensure!(
        references == 0,
        "file remains referenced by a machine: {}",
        file.file_path
    );
    // Missing versions are legitimate retries, but an identity that now belongs
    // to another file is never evidence that this manifest already completed.
    for expected in &file.versions {
        let symbols = expected
            .symbol_ids
            .iter()
            .map(|id| db::id_param(id))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let foreign: bool = conn
            .query_one(
                "SELECT EXISTS(SELECT 1 FROM code_indexed_files WHERE id=$1
                AND (project_id<>$2 OR file_path<>$3 OR content_hash<>$4))
              OR EXISTS(SELECT 1 FROM code_symbols WHERE id=ANY($5)
                AND (project_id<>$2 OR file_path<>$3 OR file_content_hash<>$4))",
                &[
                    &db::id_param(&expected.id)?,
                    &project_id,
                    &file.file_path,
                    &expected.content_hash,
                    &symbols,
                ],
            )?
            .get(0);
        ensure!(
            !foreign,
            "manifest identity belongs to another content version"
        );
    }
    let rows = conn.query(
        "SELECT f.id::text, f.content_hash, f.graph_synced, f.vectors_synced,
            f.language, f.symbol_count, f.byte_size,
            EXISTS(SELECT 1 FROM code_content_chunks k WHERE k.project_id=f.project_id AND k.file_path=f.file_path AND k.content_hash=f.content_hash) AS has_content_chunks,
            (EXISTS(SELECT 1 FROM code_symbols s WHERE s.project_id=f.project_id AND s.file_path=f.file_path AND s.file_content_hash=f.content_hash)
             OR EXISTS(SELECT 1 FROM code_imports i WHERE i.project_id=f.project_id AND i.source_file=f.file_path AND i.content_hash=f.content_hash)
             OR EXISTS(SELECT 1 FROM code_calls c WHERE c.project_id=f.project_id AND c.file_path=f.file_path AND c.content_hash=f.content_hash)
             OR EXISTS(SELECT 1 FROM code_inheritance h WHERE h.project_id=f.project_id AND h.file_path=f.file_path AND h.content_hash=f.content_hash)) AS has_graph_facts,
            ARRAY(SELECT s.id::text FROM code_symbols s WHERE s.project_id=f.project_id AND s.file_path=f.file_path AND s.file_content_hash=f.content_hash ORDER BY s.id) AS symbol_ids
         FROM code_indexed_files f WHERE f.project_id = $1 AND f.file_path = $2 ORDER BY f.id",
        &[&project_id, &file.file_path],
    )?;
    let mut candidates = Vec::new();
    for row in rows {
        let id: String = row.get("id");
        let expected = file
            .versions
            .iter()
            .find(|version| version.id == id)
            .with_context(|| format!("unexpected content version for {}: {id}", file.file_path))?;
        let content_hash: String = row.get("content_hash");
        let symbol_ids: Vec<String> = row.get("symbol_ids");
        ensure!(
            content_hash == expected.content_hash,
            "content identity changed: {id}"
        );
        ensure!(
            symbol_ids.iter().collect::<BTreeSet<_>>()
                == expected.symbol_ids.iter().collect::<BTreeSet<_>>(),
            "symbol membership changed: {id}"
        );
        if expected.is_tombstone() {
            // Overlay deletion markers are canonical non-hash identities, but
            // they must never carry content or projection facts.
            ensure!(
                row.get::<_, String>("language") == crate::visibility::TOMBSTONE_LANGUAGE
                    && row.get::<_, i32>("symbol_count") == 0
                    && row.get::<_, i32>("byte_size") == 0
                    && !row.get::<_, bool>("has_graph_facts")
                    && !row.get::<_, bool>("has_content_chunks"),
                "tombstone has nonempty content facts: {id}"
            );
        }
        candidates.push(ContentGcCandidate {
            id,
            project_id: manifest.project_id.clone(),
            file_path: file.file_path.clone(),
            content_hash,
            symbol_ids,
            has_graph_facts: row.get("has_graph_facts"),
            graph_synced: row.get("graph_synced"),
            vectors_synced: row.get("vectors_synced"),
        });
    }
    Ok(candidates)
}

fn retire_file(
    conn: &mut Client,
    ctx: &Context,
    manifest: &Manifest,
    file: &RetiredFile,
    receipt: &mut Receipt,
    receipt_path: &Path,
) -> anyhow::Result<()> {
    validate_context(conn, ctx, manifest)?;
    let candidates = validate_file(conn, manifest, file)?;
    let current = candidates
        .iter()
        .map(|candidate| candidate.id.as_str())
        .collect();
    validate_projections(ctx, file, &current)?;
    ensure!(
        ctx.falkordb.is_some(),
        "graph backend is required to verify exact retirement"
    );
    ensure!(
        ctx.qdrant.is_some(),
        "vector backend is required to verify exact retirement"
    );
    for candidate in candidates {
        // Re-read every remaining identity and symbol membership under the GC
        // lock; a partial retry never admits a newly indexed content version.
        let current = validate_file(conn, manifest, file)?;
        ensure!(
            current.iter().any(|row| row == &candidate),
            "content changed before retirement"
        );
        ensure!(
            content_is_unreferenced(conn, &candidate.id)?,
            "content became referenced"
        );
        receipt
            .files
            .entry(file.file_path.clone())
            .or_default()
            .versions
            .insert(candidate.id.clone(), "deleting".to_string());
        write_receipt(receipt_path, receipt)?;
        // An admitted retained version may have stale graph edges even when its
        // SQL facts are empty. Exact hash deletion remains safe in that case.
        let mut projection_candidate = candidate.clone();
        projection_candidate.has_graph_facts = true;
        delete_candidate_projections(ctx, &projection_candidate)?;
        ensure!(
            delete_unreferenced_content_row(conn, &candidate.id)?,
            "content became referenced after projection cleanup; marked for resync"
        );
        receipt
            .files
            .entry(file.file_path.clone())
            .or_default()
            .versions
            .insert(candidate.id, "deleted".to_string());
        write_receipt(receipt_path, receipt)?;
    }
    ensure!(
        validate_file(conn, manifest, file)?.is_empty(),
        "retained content still exists"
    );
    code_graph::with_code_graph(ctx, |graph| graph.delete_empty_file_node(&file.file_path))?;
    receipt
        .files
        .entry(file.file_path.clone())
        .or_default()
        .shell = "absent".to_string();
    write_receipt(receipt_path, receipt)
}

fn validate_projections(
    ctx: &Context,
    file: &RetiredFile,
    current: &BTreeSet<&str>,
) -> anyhow::Result<()> {
    let symbols = file
        .versions
        .iter()
        .filter(|version| current.contains(version.id.as_str()))
        .flat_map(|version| {
            version
                .symbol_ids
                .iter()
                .map(|id| (id.clone(), version.content_hash.clone()))
        })
        .collect();
    let hashes = file
        .versions
        .iter()
        .filter(|version| current.contains(version.id.as_str()) && !version.is_tombstone())
        .map(|version| version.content_hash.clone())
        .collect();
    code_graph::with_code_graph(ctx, |graph| {
        graph.validate_file_retirement(&file.file_path, &symbols, &hashes)
    })?;
    // A completed receipt never authorizes projections recreated after their
    // SQL content was deleted. Count exact IDs only; retrieve no vector data.
    let absent_symbols = file
        .versions
        .iter()
        .filter(|version| !current.contains(version.id.as_str()))
        .flat_map(|version| version.symbol_ids.iter().cloned())
        .collect::<Vec<_>>();
    if !absent_symbols.is_empty() {
        let qdrant = ctx
            .qdrant
            .as_ref()
            .context("vector backend is required to verify retirement")?;
        ensure!(
            crate::vector::code_symbols::count_symbol_vectors(
                qdrant,
                &ctx.project_id,
                &absent_symbols
            )? == 0,
            "retired content still has vector points: {}",
            file.file_path
        );
    }
    Ok(())
}

fn validate_receipt_path(path: &Path, receipt: &Receipt) -> anyhow::Result<()> {
    manifest::no_symlinks(path)?;
    let parent = path
        .parent()
        .context("receipt requires a parent directory")?;
    let filename = path.file_name().context("receipt requires a file name")?;
    ensure!(
        parent.canonicalize()?.join(filename) == path,
        "receipt path must be canonical"
    );
    ensure!(
        !path.starts_with(&receipt.root_path),
        "receipt must stay outside repository indexing"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(parent)?;
        // SAFETY: getuid has no arguments and no memory-safety preconditions.
        let uid = unsafe { libc::getuid() };
        ensure!(
            metadata.is_dir() && metadata.uid() == uid && metadata.mode() & 0o077 == 0,
            "receipt parent must be a private owned directory"
        );
    }
    if path.exists() {
        let file = File::open(path)?;
        manifest::require_private_file(&file)?;
        let previous: Receipt = serde_json::from_reader(file)?;
        ensure!(
            previous.manifest_digest == receipt.manifest_digest
                && previous.backends == receipt.backends
                && previous.source_inventory_digest == receipt.source_inventory_digest
                && previous.project_id == receipt.project_id
                && previous.machine_id == receipt.machine_id
                && previous.root_path == receipt.root_path,
            "receipt belongs to another retirement manifest"
        );
    }
    Ok(())
}

fn write_receipt(path: &Path, receipt: &Receipt) -> anyhow::Result<()> {
    validate_receipt_path(path, receipt)?;
    let parent = path.parent().context("receipt requires parent")?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(file.as_file_mut(), receipt)?;
    file.write_all(b"\n")?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|error| error.error)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
#[path = "retire_files/tests.rs"]
mod tests;
