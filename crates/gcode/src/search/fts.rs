//! PostgreSQL pg_search BM25 query sanitization and execution.
//!
//! The module name stays `fts` to keep command wiring stable; runtime keyword
//! search is pg_search BM25 against Gobby's PostgreSQL hub.

mod common;
mod content;
mod counts;
mod graph;
mod symbols;

#[cfg(test)]
mod tests;

pub use common::{
    FILTERED_FETCH_CAP, ResolvedGraphSymbol, compile_patterns, expand_paths,
    path_filter_falls_back, sanitize_pg_search_query,
};
pub use content::{search_content, search_content_visible};
pub use counts::{count_content, count_content_visible, count_text, count_text_visible};
pub use graph::resolve_graph_symbol;
pub use symbols::{
    VisibleSearchOutcome, search_symbols_by_name, search_symbols_by_name_visible,
    search_symbols_exact_first, search_symbols_exact_first_visible, search_symbols_fts,
    search_symbols_fts_visible, search_text, search_text_visible,
};

#[cfg(test)]
use common::{append_unique_symbols, glob_to_like_prefix, path_like_prefixes};
#[cfg(test)]
use content::make_snippet;
