//! Effective pin: copies an ancestor checkout's clean selectors into an overlay's base rows.

#[cfg(test)]
use std::cell::Cell;
use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use postgres::types::ToSql;
use postgres::{Client, ToStatement, Transaction};
use serde::Serialize;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::git::{git_output, parse_ls_tree};
use crate::output::{self, Format};
use crate::visibility::catalog::{self, EffectiveSelector};

/// Outcome of one pin. `reason` is set only for a semantic cold pin.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct PinReport {
    pub(crate) warm: bool,
    pub(crate) reason: Option<&'static str>,
    pub(crate) pinned: Vec<String>,
    pub(crate) gaps: Vec<String>,
}

/// Ancestor identity and selected commit from the isolation marker.
pub(crate) struct PinSource<'a> {
    pub(crate) project_id: &'a str,
    pub(crate) root: &'a Path,
    pub(crate) base_commit: &'a str,
}

/// Replaces the overlay's pin rows with the ancestor's clean effective selectors.
///
/// Semantic misses (no provenance, no live checkout, unresolvable commit, failed
/// `ls-tree`) durably write a cold pin and return `Ok`. Storage failures return `Err`
/// and leave the prior rows in place.
pub(crate) fn apply_pin(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
    source: Option<PinSource<'_>>,
) -> anyhow::Result<PinReport> {
    let Some(source) = source else {
        return write_cold(
            conn,
            machine_id,
            overlay_project_id,
            "no_pin_provenance",
            Vec::new(),
        );
    };
    let tree = match read_tree(source.root, source.base_commit) {
        Ok(tree) => tree,
        Err(reason) => return write_cold(conn, machine_id, overlay_project_id, reason, Vec::new()),
    };
    let tracked: BTreeSet<String> = tree.keys().cloned().collect();
    if !has_live_checkout(conn, machine_id, source.project_id)? {
        let gaps = derive_gaps(&tracked, &BTreeSet::new());
        return write_cold(
            conn,
            machine_id,
            overlay_project_id,
            "no_live_checkout",
            gaps,
        );
    }

    let pins: Vec<EffectiveSelector> =
        catalog::effective_selectors(conn, machine_id, source.project_id)?
            .into_iter()
            .filter(|selector| {
                selector.git_blob_id.as_deref().is_some_and(|blob| {
                    tree.get(&selector.file_path).map(String::as_str) == Some(blob)
                })
            })
            .collect();
    let pinned: Vec<String> = pins.iter().map(|pin| pin.file_path.clone()).collect();
    let gaps = derive_gaps(&tracked, &pinned.iter().cloned().collect());
    replace_pins(conn, machine_id, overlay_project_id, &pins, &gaps)?;
    Ok(PinReport {
        warm: true,
        reason: None,
        pinned,
        gaps,
    })
}

/// Tracked paths the overlay still lacks a pin for. The one gap rule child indexing and
/// reconciliation share.
pub(crate) fn derive_gaps(tracked: &BTreeSet<String>, pinned: &BTreeSet<String>) -> Vec<String> {
    tracked.difference(pinned).cloned().collect()
}

pub fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let marker = gobby_core::project::read_isolation_marker(&ctx.project_root);
    let source = marker
        .as_ref()
        .and_then(|marker| marker.pin.as_ref())
        .map(|pin| PinSource {
            project_id: &pin.ancestor_checkout_identity,
            root: Path::new(&pin.ancestor_checkout_path),
            base_commit: &pin.base_commit,
        });
    let overlay_project_id = match &ctx.index_scope {
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } => overlay_project_id,
        ProjectIndexScope::Single => &ctx.project_id,
    };
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let report = apply_pin(&mut conn, &machine_id, overlay_project_id, source)?;
    match format {
        Format::Json => output::print_json(&report),
        Format::Text => output::print_text(&pin_text(&report)),
    }
}

fn pin_text(report: &PinReport) -> String {
    match report.reason {
        None => format!(
            "warm pin: {} pinned, {} gaps",
            report.pinned.len(),
            report.gaps.len()
        ),
        Some(reason) => format!("cold pin: {reason}; {} gaps", report.gaps.len()),
    }
}

/// Tracked blob paths at `commit`, or the semantic reason it cannot be read.
fn read_tree(root: &Path, commit: &str) -> Result<HashMap<String, String>, &'static str> {
    let peeled = format!("{commit}^{{commit}}");
    git_output(root, &["rev-parse", "--verify", "--quiet", peeled.as_str()])
        .map_err(|_| "unresolvable_commit")?;
    let listing =
        git_output(root, &["ls-tree", "-r", "-z", commit]).map_err(|_| "git_ls_tree_failed")?;
    Ok(parse_ls_tree(&listing))
}

fn has_live_checkout(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
) -> anyhow::Result<bool> {
    let machine_id = db::id_param(machine_id)?;
    let project_id = db::id_param(project_id)?;
    Ok(conn
        .query_opt(
            "SELECT 1 FROM project_checkouts WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &project_id],
        )?
        .is_some())
}

fn write_cold(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
    reason: &'static str,
    gaps: Vec<String>,
) -> anyhow::Result<PinReport> {
    replace_pins(conn, machine_id, overlay_project_id, &[], &gaps)?;
    Ok(PinReport {
        warm: false,
        reason: Some(reason),
        pinned: Vec::new(),
        gaps,
    })
}

// Hub statements issued by `replace_pins`, counted in test builds only.
#[cfg(test)]
thread_local! {
    static HUB_STATEMENTS: Cell<usize> = const { Cell::new(0) };
}

/// Runs one statement on the pin transaction, counting it as a hub round trip in test builds.
fn execute_counted<T: ?Sized + ToStatement>(
    tx: &mut Transaction<'_>,
    query: &T,
    params: &[&(dyn ToSql + Sync)],
) -> Result<u64, postgres::Error> {
    #[cfg(test)]
    HUB_STATEMENTS.with(|count| count.set(count.get() + 1));
    tx.execute(query, params)
}

fn replace_pins(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
    pins: &[EffectiveSelector],
    gaps: &[String],
) -> anyhow::Result<()> {
    let machine_id = db::id_param(machine_id)?;
    let overlay_project_id = db::id_param(overlay_project_id)?;
    let file_paths = pins
        .iter()
        .map(|pin| pin.file_path.as_str())
        .collect::<Vec<_>>();
    let source_project_ids = pins
        .iter()
        .map(|pin| db::id_param(&pin.source_project_id))
        .collect::<anyhow::Result<Vec<_>>>()?;
    let content_hashes = pins
        .iter()
        .map(|pin| pin.content_hash.as_str())
        .collect::<Vec<_>>();
    let git_blob_ids = pins
        .iter()
        .map(|pin| pin.git_blob_id.as_deref())
        .collect::<Vec<_>>();
    // One DELETE and one unnest INSERT per table, whatever the row count.
    let mut tx = conn.transaction()?;
    execute_counted(
        &mut tx,
        "DELETE FROM code_overlay_pins WHERE machine_id = $1 AND overlay_project_id = $2",
        &[&machine_id, &overlay_project_id],
    )?;
    execute_counted(
        &mut tx,
        "DELETE FROM code_overlay_pin_gaps WHERE machine_id = $1 AND overlay_project_id = $2",
        &[&machine_id, &overlay_project_id],
    )?;
    execute_counted(
        &mut tx,
        "INSERT INTO code_overlay_pins (
            machine_id, overlay_project_id, file_path, source_project_id,
            content_hash, git_blob_id
         )
         SELECT $1::uuid, $2::uuid, pins.file_path, pins.source_project_id,
                pins.content_hash, pins.git_blob_id
         FROM unnest(
            $3::text[], $4::uuid[], $5::text[], $6::text[]
         ) AS pins(file_path, source_project_id, content_hash, git_blob_id)",
        &[
            &machine_id,
            &overlay_project_id,
            &file_paths,
            &source_project_ids,
            &content_hashes,
            &git_blob_ids,
        ],
    )?;
    execute_counted(
        &mut tx,
        "INSERT INTO code_overlay_pin_gaps (machine_id, overlay_project_id, file_path)
         SELECT $1::uuid, $2::uuid, gaps.file_path
         FROM unnest($3::text[]) AS gaps(file_path)",
        &[&machine_id, &overlay_project_id, &gaps],
    )?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
pub(crate) use crate::db::persisted_gaps;

#[cfg(test)]
mod tests;
