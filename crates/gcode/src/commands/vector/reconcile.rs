//! Read-only inventory of the code-symbol vector projection against PostgreSQL.
//!
//! The dry run scrolls the project's Qdrant collection and reads PostgreSQL. It
//! writes nothing and deletes nothing. Orphan classes are evidence only.

use std::collections::BTreeMap;

use crate::config::Context;
use crate::output::Format;

#[cfg(test)]
#[path = "reconcile/tests.rs"]
mod tests;

/// Why a point ID absent from PostgreSQL is an orphan. The first matching class wins.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum OrphanClass {
    LegacyPayload,
    LegacyIdScheme,
    ReparsedVersion,
    Unattributed,
}

/// Why a version has symbols whose points are absent from Qdrant.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum MissingClass {
    SyncedMissingCurrent,
    SyncedMissingRetained,
    PendingCurrent,
    PendingRetained,
}

/// Versions in one missing class, with the count of their absent symbol points.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct MissingGroup {
    pub(crate) version_ids: Vec<String>,
    pub(crate) symbols: usize,
}

/// The full dry-run inventory for one project.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Inventory {
    pub(crate) points_scanned: usize,
    pub(crate) pg_symbols: usize,
    pub(crate) orphans: BTreeMap<OrphanClass, Vec<String>>,
    pub(crate) missing: BTreeMap<MissingClass, MissingGroup>,
    pub(crate) inventory_digest: String,
}

pub(crate) fn inventory(_ctx: &Context) -> anyhow::Result<Inventory> {
    todo!("red: vector reconcile inventory is not implemented yet")
}

pub(crate) fn inventory_digest(_orphan_ids: &[String], _synced_missing_version_ids: &[String]) -> String {
    todo!("red: vector reconcile digest is not implemented yet")
}

pub(crate) fn run(_ctx: &Context, _format: Format) -> anyhow::Result<()> {
    todo!("red: vector reconcile run is not implemented yet")
}
