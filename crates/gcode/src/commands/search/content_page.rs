//! Content search split at its disk check. `content_candidates` runs the database half,
//! which the hub can serve for a node's project. `page_content` applies the caller's
//! disk check and pages the result.

use crate::config::Context;
use crate::models::ContentSearchHit;
use crate::node::ContentCandidates;

/// Filters and paging shared by the local CLI and the hub.
#[derive(Debug, Clone, Copy)]
pub(crate) struct ContentQuery<'a> {
    pub language: Option<&'a str>,
    pub paths: &'a [String],
    pub limit: usize,
    pub offset: usize,
}

/// One page of content hits after the disk check.
#[derive(Debug)]
pub(crate) struct SearchContentPage {
    pub results: Vec<ContentSearchHit>,
    pub total: usize,
    pub next_offset: Option<usize>,
    pub hint: Option<String>,
}

/// Database half of a content search. Reads no file on disk.
pub(crate) fn content_candidates(
    _conn: &mut postgres::Client,
    _ctx: &Context,
    _query: &str,
    _options: ContentQuery<'_>,
) -> anyhow::Result<ContentCandidates> {
    todo!("1.3: FTS fetch, language and path filters, visibility, db total")
}

/// Disk half of a content search: drops hits whose file `exists` rejects, then pages.
pub(crate) fn page_content(
    _candidates: ContentCandidates,
    _offset: usize,
    _limit: usize,
    _exists: impl Fn(&str) -> bool,
) -> SearchContentPage {
    todo!("1.3: drop missing files, take total from db_total or the filtered count, page")
}
