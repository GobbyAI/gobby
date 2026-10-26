//! Node half of the Primary index split (#24673, plan 1.4).
//!
//! The node discovers files, hashes them and parses facts without touching the
//! database. The hub's database half (`node::hub::plan_index` and `apply_index`)
//! writes them. These stubs fix the seams the tests drive; bodies are the implementation's.

use crate::config::Context;
use crate::index::indexer::{IndexOptions, IndexOutcome, IndexRequest};
use crate::node::ParsedFileFacts;

/// Parses the named paths into facts for an apply request.
pub(crate) fn parse_facts(
    _ctx: &Context,
    _request: &IndexRequest,
    _paths: &[String],
) -> anyhow::Result<Vec<ParsedFileFacts>> {
    todo!("1.4 node half: parse_file_facts and parse_content_only_facts")
}

/// A local `gcode index`, run as the node half followed by the database half in one process.
pub(crate) fn index_files_in_two_halves(
    _request: IndexRequest,
    _ctx: &Context,
    _options: IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    todo!("1.4 local recomposition: parse then apply")
}
