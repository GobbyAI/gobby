//! Projected overlay pins whose overlay points disagree with the pinned source symbols.
//!
//! A pin marked projected claims that the overlay collection holds exactly the source
//! symbols at the pinned content hash. The pending selector revisits a pin only once
//! its source selector moves, so a pin whose source still names its hash would never be
//! re-projected. This module names the pins whose points are missing or stale so the
//! repair can re-project them; the projection prunes the stale points it finds.

use std::collections::{BTreeMap, HashMap, HashSet};

use postgres::GenericClient;

use crate::config::Context;
use crate::db;
use crate::projection::sync::pinned::UNSHADOWED;
use crate::vector::code_symbols::{self, VectorLifecycleError};

/// Source symbol IDs that this overlay's unshadowed pins keep. Their points belong to the
/// pinned source, so reconcile must not classify them as orphans.
pub(super) fn pinned_keep_ids(
    conn: &mut impl GenericClient,
    ctx: &Context,
    scope: Option<&[String]>,
) -> anyhow::Result<HashSet<String>> {
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let overlay = db::id_param(&ctx.project_id)?;
    let scope_paths: Option<Vec<String>> = scope.map(<[String]>::to_vec);
    let rows = conn.query(
        &format!(
            "SELECT s.id::text FROM code_overlay_pins p \
             JOIN code_symbols s ON s.project_id = p.source_project_id \
               AND s.file_path = p.file_path AND s.file_content_hash = p.content_hash \
             WHERE p.machine_id = $1 AND p.overlay_project_id = $2 AND {UNSHADOWED} \
               AND ($3::text[] IS NULL OR p.file_path = ANY($3))",
            UNSHADOWED = UNSHADOWED,
        ),
        &[&machine_id, &overlay, &scope_paths],
    )?;
    Ok(rows.iter().map(|row| row.get(0)).collect())
}

/// File paths of projected overlay pins whose overlay points are missing a pinned source
/// symbol or hold a point the pinned version does not name. Shadowed pins are excluded:
/// an owned overlay state row serves the path.
pub(super) fn drifted_pin_paths(
    ctx: &Context,
    scope: Option<&[String]>,
) -> anyhow::Result<Vec<String>> {
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let overlay = db::id_param(&ctx.project_id)?;
    // NULL scope reads every pinned path; a list reads only those file paths.
    let scope_paths: Option<Vec<String>> = scope.map(<[String]>::to_vec);
    // Probe before scrolling: a project with no projected pins has nothing to compare,
    // so its overlay is never scrolled.
    let pinned = conn.query_one(
        &format!("SELECT EXISTS (SELECT 1 {})", projected_pin_symbols_from()),
        &[&machine_id, &overlay, &scope_paths],
    )?;
    if !pinned.get::<_, bool>(0) {
        return Ok(Vec::new());
    }
    // Scroll Qdrant before reading the pinned symbol rows, as the inventory does, so a
    // point present at scroll time still has its source symbol row.
    let points = code_symbols::scroll_point_identities(qdrant, &ctx.project_id, scope)?;
    let mut points_by_path: HashMap<&str, HashSet<&str>> = HashMap::new();
    for point in &points {
        if let Some(path) = point.file_path.as_deref() {
            points_by_path
                .entry(path)
                .or_default()
                .insert(point.id.as_str());
        }
    }

    let rows = conn.query(
        &format!(
            "SELECT s.id::text, p.file_path {}",
            projected_pin_symbols_from()
        ),
        &[&machine_id, &overlay, &scope_paths],
    )?;

    let mut symbols_by_path: BTreeMap<String, HashSet<String>> = BTreeMap::new();
    for row in &rows {
        let id: String = row.get(0);
        let path: String = row.get(1);
        symbols_by_path.entry(path).or_default().insert(id);
    }
    Ok(symbols_by_path
        .into_iter()
        .filter(|(path, symbol_ids)| {
            let point_ids = points_by_path.get(path.as_str());
            let missing = symbol_ids
                .iter()
                .any(|id| !point_ids.is_some_and(|ids| ids.contains(id.as_str())));
            let stale = point_ids.is_some_and(|ids| ids.iter().any(|id| !symbol_ids.contains(*id)));
            missing || stale
        })
        .map(|(path, _)| path)
        .collect())
}

/// The projected pins of this overlay whose source symbols are stored, from the `FROM`
/// keyword on. Callers prepend their select list and bind the machine, the overlay
/// project and the optional scope paths.
fn projected_pin_symbols_from() -> String {
    format!(
        "FROM code_overlay_pins p \
         JOIN code_symbols s ON s.project_id = p.source_project_id \
           AND s.file_path = p.file_path AND s.file_content_hash = p.content_hash \
         WHERE p.machine_id = $1 AND p.overlay_project_id = $2 \
           AND p.vector_projected IS TRUE AND {UNSHADOWED} \
           AND ($3::text[] IS NULL OR p.file_path = ANY($3))",
        UNSHADOWED = UNSHADOWED,
    )
}
