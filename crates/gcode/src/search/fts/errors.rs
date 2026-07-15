use crate::bm25_health::REPAIR_COMMAND;
use crate::postgres_errors;

pub(super) const SYMBOL_INDEX: &str = "public.code_symbols_search_bm25";
pub(super) const CONTENT_INDEX: &str = "public.code_content_search_bm25";

pub(super) fn database_query_error(operation: &str, error: &postgres::Error) -> anyhow::Error {
    anyhow::anyhow!("{operation} failed: {}", postgres_errors::message(error))
}

pub(super) fn bm25_query_error(index: &str, error: &postgres::Error) -> anyhow::Error {
    anyhow::anyhow!(
        "BM25 query using {index} failed: {}. Run `gcode status`; if the index is damaged, run `{REPAIR_COMMAND}`",
        postgres_errors::message(error)
    )
}
