//! Content search split at its disk check. `content_candidates` runs the database half,
//! which the hub can serve for a node's project. `page_content` applies the caller's
//! disk check and pages the result.

use crate::commands::indexed_paths;
use crate::commands::search::{
    filtered_fetch_cap_hint, path_filter_post_filter_hint, path_matches_filters,
};
use crate::commands::token_budget;
use crate::config::Context;
use crate::models::ContentSearchHit;
use crate::node::ContentCandidates;
use crate::search::fts;

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
    conn: &mut postgres::Client,
    ctx: &Context,
    query: &str,
    options: ContentQuery<'_>,
) -> anyhow::Result<ContentCandidates> {
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let has_path_filters = !expanded_paths.is_empty();
    let fetch_limit = if has_path_filters {
        fts::FILTERED_FETCH_CAP
    } else {
        // The SQL LIMIT binds an i64, so a wire offset near usize::MAX clamps rather than wraps.
        options
            .offset
            .saturating_add(options.limit)
            .saturating_mul(3)
            .clamp(200, i64::MAX as usize)
    };
    let fetched = fts::search_content_visible(
        conn,
        query,
        ctx,
        options.language,
        &expanded_paths,
        fetch_limit,
    )?;
    let cap_hint = (has_path_filters && fetched.len() >= fts::FILTERED_FETCH_CAP)
        .then(filtered_fetch_cap_hint);
    let path_hint =
        fts::path_filter_requires_post_filter(&expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(cap_hint, path_hint);
    let fetched: Vec<_> = fetched
        .into_iter()
        .filter(|r| {
            options
                .language
                .is_none_or(|lang| r.language.as_deref() == Some(lang))
                && path_matches_filters(&path_patterns, &r.file_path)
        })
        .collect();
    let file_paths: Vec<String> = fetched.iter().map(|r| r.file_path.clone()).collect();
    let indexed = indexed_paths::indexed_file_paths(conn, ctx, &file_paths);
    let results = fetched
        .into_iter()
        .filter(|r| indexed.contains(&r.file_path))
        .collect();
    let db_total = if has_path_filters {
        None
    } else {
        Some(fts::count_content_visible(
            conn,
            query,
            ctx,
            options.language,
            &expanded_paths,
        )?)
    };
    Ok(ContentCandidates {
        results,
        db_total,
        hint,
    })
}

/// Disk half of a content search: drops hits whose file `exists` rejects, then pages.
pub(crate) fn page_content(
    candidates: ContentCandidates,
    offset: usize,
    limit: usize,
    exists: impl Fn(&str) -> bool,
) -> SearchContentPage {
    let ContentCandidates {
        results,
        db_total,
        hint,
    } = candidates;
    let present: Vec<ContentSearchHit> = results
        .into_iter()
        .filter(|hit| exists(&hit.file_path))
        .collect();
    let total = db_total.unwrap_or(present.len());
    let results: Vec<ContentSearchHit> = present.into_iter().skip(offset).take(limit).collect();
    // An empty page sits past the last present hit. The total still counts entries that are
    // gone from disk, so pointing a cursor back at this offset would never advance.
    let next = offset + results.len();
    SearchContentPage {
        next_offset: (!results.is_empty() && next < total).then_some(next),
        results,
        total,
        hint,
    }
}
