//! Background recovery for pinned overlay paths.
//!
//! A pin names one retained source version of an overlay path. Recovery writes
//! only the pin row and this overlay's namespace, never the source's sync
//! columns. Each completion is fenced on the pin's content hash and attempt
//! stamp, and an owned overlay file-state row for the path shadows the pin.

use std::time::SystemTime;

use postgres::GenericClient;

use crate::db;
use crate::graph::code_graph::CodeGraph;
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, CodeSymbolVectorLifecycleOutput, VectorLifecycleError,
};

/// One recovery attempt's stamp on a pin row.
pub(crate) struct PinAttempt {
    source_project_id: String,
    content_hash: String,
    attempted_at: SystemTime,
}

/// Counts from one pinned graph write.
pub(crate) struct PinnedGraphWrite {
    pub(crate) relationships_written: usize,
    pub(crate) symbols_synced: usize,
}

#[derive(Clone, Copy)]
enum Stage {
    Vector,
    Graph,
}

impl Stage {
    fn projected(self) -> &'static str {
        match self {
            Self::Vector => "vector_projected",
            Self::Graph => "graph_projected",
        }
    }

    fn attempted_at(self) -> &'static str {
        match self {
            Self::Vector => "vector_attempted_at",
            Self::Graph => "graph_attempted_at",
        }
    }
}

/// Binds `$1` machine, `$2` overlay, `$3` path. A pin is shadowed when this
/// machine owns an overlay file-state row for the same path.
pub(crate) const UNSHADOWED: &str = "NOT EXISTS (
    SELECT 1 FROM code_indexed_file_states s
    WHERE s.machine_id = p.machine_id
      AND s.project_id = p.overlay_project_id
      AND s.file_path = p.file_path
)";

pub(crate) fn begin_vector_attempt(
    conn: &mut impl GenericClient,
    overlay_project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<PinAttempt>> {
    begin_attempt(conn, Stage::Vector, overlay_project_id, file_path)
}

pub(crate) fn begin_graph_attempt(
    conn: &mut impl GenericClient,
    overlay_project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<PinAttempt>> {
    begin_attempt(conn, Stage::Graph, overlay_project_id, file_path)
}

fn begin_attempt(
    conn: &mut impl GenericClient,
    stage: Stage,
    overlay_project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<PinAttempt>> {
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let overlay = db::id_param(overlay_project_id)?;
    let row = conn.query_opt(
        &format!(
            "UPDATE code_overlay_pins p
             SET {projected} = false, {at} = NOW()
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2 AND p.file_path = $3
               AND {UNSHADOWED}
             RETURNING p.source_project_id::text AS source_project_id,
                       p.content_hash, p.{at} AS attempted_at",
            projected = stage.projected(),
            at = stage.attempted_at(),
            UNSHADOWED = UNSHADOWED,
        ),
        &[&machine_id, &overlay, &file_path],
    )?;
    row.map(|row| {
        Ok(PinAttempt {
            source_project_id: row.try_get("source_project_id")?,
            content_hash: row.try_get("content_hash")?,
            attempted_at: row.try_get("attempted_at")?,
        })
    })
    .transpose()
}

/// Marks the pin projected only if it still names the attempted version, carries
/// the same attempt stamp, and no overlay file-state row shadows the path.
fn finish_attempt(
    conn: &mut impl GenericClient,
    stage: Stage,
    overlay_project_id: &str,
    file_path: &str,
    attempt: &PinAttempt,
) -> anyhow::Result<bool> {
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let overlay = db::id_param(overlay_project_id)?;
    Ok(conn
        .query_opt(
            &format!(
                "UPDATE code_overlay_pins p
                 SET {projected} = true
                 WHERE p.machine_id = $1 AND p.overlay_project_id = $2 AND p.file_path = $3
                   AND p.content_hash = $4
                   AND p.{at} IS NOT DISTINCT FROM $5
                   AND {UNSHADOWED}
                 RETURNING p.file_path",
                projected = stage.projected(),
                at = stage.attempted_at(),
                UNSHADOWED = UNSHADOWED,
            ),
            &[
                &machine_id,
                &overlay,
                &file_path,
                &attempt.content_hash,
                &attempt.attempted_at,
            ],
        )?
        .is_some())
}

/// Deletes the pin whose source left the checkout, fenced on the same attempt
/// stamp as `finish_attempt`. Returns false when a newer pin or index won.
fn retire_missing_source(
    conn: &mut impl GenericClient,
    overlay_project_id: &str,
    file_path: &str,
    attempt: &PinAttempt,
) -> anyhow::Result<bool> {
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let overlay = db::id_param(overlay_project_id)?;
    Ok(conn
        .query_opt(
            &format!(
                "DELETE FROM code_overlay_pins p
                 WHERE p.machine_id = $1 AND p.overlay_project_id = $2 AND p.file_path = $3
                   AND p.content_hash = $4
                   AND p.{at} IS NOT DISTINCT FROM $5
                   AND {UNSHADOWED}
                 RETURNING p.file_path",
                at = Stage::Vector.attempted_at(),
                UNSHADOWED = UNSHADOWED,
            ),
            &[
                &machine_id,
                &overlay,
                &file_path,
                &attempt.content_hash,
                &attempt.attempted_at,
            ],
        )?
        .is_some())
}

/// Outcome of one pinned vector sync attempt.
pub(crate) enum PinnedVectorSync {
    Synced(CodeSymbolVectorLifecycleOutput),
    /// A newer pin or index won the race; this attempt was not recorded.
    Superseded,
    /// The pinned source moved after indexing. The attempt stays unfinished, so
    /// the file stays pending for the newer index.
    SourceChanged,
    /// The pinned source left the checkout. Its pin row is retired, so no
    /// later pass retries it; content GC then drops the unreferenced rows.
    SourceMissing,
}

/// Projects the pinned source version into this overlay's vector namespace.
pub(crate) fn sync_vector_attempt(
    conn: &mut impl GenericClient,
    overlay_project_id: &str,
    lifecycle: &mut CodeSymbolVectorLifecycle,
    file_path: &str,
    attempt: &PinAttempt,
) -> anyhow::Result<PinnedVectorSync> {
    let symbols = code_symbols::fetch_symbols_for_file_version(
        conn,
        &attempt.source_project_id,
        file_path,
        &attempt.content_hash,
    )?;
    let output = match lifecycle.sync_file_symbols(file_path, &symbols) {
        Ok(output) => output,
        Err(VectorLifecycleError::IndexedSourceChanged { .. }) => {
            return Ok(PinnedVectorSync::SourceChanged);
        }
        Err(VectorLifecycleError::IndexedSourceMissing { .. }) => {
            return Ok(
                if retire_missing_source(conn, overlay_project_id, file_path, attempt)? {
                    PinnedVectorSync::SourceMissing
                } else {
                    PinnedVectorSync::Superseded
                },
            );
        }
        Err(err) => return Err(err.into()),
    };
    // Prune only after the fence: a superseded attempt must not delete points that a
    // newer attempt wrote for the same path.
    if !finish_attempt(conn, Stage::Vector, overlay_project_id, file_path, attempt)? {
        return Ok(PinnedVectorSync::Superseded);
    }
    code_symbols::prune_stale_file_points(
        lifecycle,
        conn,
        overlay_project_id,
        file_path,
        &symbols,
    )?;
    Ok(PinnedVectorSync::Synced(output))
}

/// Writes the pinned source version's graph facts into this overlay's graph
/// namespace. `Ok(None)` means a newer pin state superseded the attempt.
pub(crate) fn sync_graph_attempt(
    graph: &mut CodeGraph<'_>,
    conn: &mut impl GenericClient,
    overlay_project_id: &str,
    file_path: &str,
    attempt: &PinAttempt,
) -> anyhow::Result<Option<PinnedGraphWrite>> {
    let facts = db::read_version_graph_facts(
        conn,
        &attempt.source_project_id,
        file_path,
        &attempt.content_hash,
    )?;
    let no_facts = facts.imports.is_empty()
        && facts.definitions.is_empty()
        && facts.calls.is_empty()
        && facts.inheritance.is_empty();
    let relationships_written = if no_facts {
        graph.sync_no_fact_file(&facts.file_path, &facts.content_hash)?;
        0
    } else {
        graph.sync_file(
            &facts.file_path,
            &facts.content_hash,
            &facts.imports,
            &facts.definitions,
            &facts.calls,
            &facts.inheritance,
            false,
        )?
    };
    if finish_attempt(conn, Stage::Graph, overlay_project_id, file_path, attempt)? {
        Ok(Some(PinnedGraphWrite {
            relationships_written,
            symbols_synced: facts.definitions.len(),
        }))
    } else {
        Ok(None)
    }
}
