//! Node half of the Primary index split (#24673, plan 1.4).
//!
//! The node discovers files, hashes them and parses facts without touching the
//! database. The hub's database half (`node::hub::plan_index` and `apply_index`)
//! writes them.

use crate::config::Context;
use crate::db;
use crate::index::indexer::primary;
use crate::index::indexer::scan::NodeScan;
use crate::index::indexer::{IndexOptions, IndexOutcome, IndexRequest};
use crate::node::ParsedFileFacts;

/// Parses the named paths into facts for an apply request.
pub(crate) fn parse_facts(
    ctx: &Context,
    request: &IndexRequest,
    paths: &[String],
) -> anyhow::Result<Vec<ParsedFileFacts>> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let mut scan = NodeScan::for_request(&mut conn, request, ctx)?;
    scan.parse_paths(paths)
}

/// A local `gcode index`: the node half and the database half, run in one process.
pub(crate) fn index_files_in_two_halves(
    request: IndexRequest,
    ctx: &Context,
    mut options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    primary::index_with_connection(&mut conn, &request, ctx, &mut options)
}
