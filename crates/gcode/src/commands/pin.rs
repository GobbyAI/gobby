//! Effective pin: copies an ancestor checkout's clean selectors into an overlay's base rows.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use postgres::Client;
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
    replace_pins(conn, machine_id, overlay_project_id, &pins)?;
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
    replace_pins(conn, machine_id, overlay_project_id, &[])?;
    Ok(PinReport {
        warm: false,
        reason: Some(reason),
        pinned: Vec::new(),
        gaps,
    })
}

fn replace_pins(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
    pins: &[EffectiveSelector],
) -> anyhow::Result<()> {
    let machine_id = db::id_param(machine_id)?;
    let overlay_project_id = db::id_param(overlay_project_id)?;
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_overlay_pins WHERE machine_id = $1 AND overlay_project_id = $2",
        &[&machine_id, &overlay_project_id],
    )?;
    let insert = tx.prepare(
        "INSERT INTO code_overlay_pins (
            machine_id, overlay_project_id, file_path, source_project_id,
            content_hash, git_blob_id
         ) VALUES ($1, $2, $3, $4, $5, $6)",
    )?;
    for pin in pins {
        tx.execute(
            &insert,
            &[
                &machine_id,
                &overlay_project_id,
                &pin.file_path,
                &db::id_param(&pin.source_project_id)?,
                &pin.content_hash,
                &pin.git_blob_id,
            ],
        )?;
    }
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests;
