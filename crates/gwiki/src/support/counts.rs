use crate::{WikiError, search, store};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct IndexCounts {
    pub(crate) documents: usize,
    pub(crate) chunks: usize,
    pub(crate) links: usize,
    pub(crate) sources: usize,
    pub(crate) ingestions: usize,
}

pub(crate) fn index_counts(store: &store::MemoryWikiStore) -> IndexCounts {
    IndexCounts {
        documents: store.documents.len(),
        chunks: store.chunks.values().map(Vec::len).sum(),
        links: store.links.values().map(Vec::len).sum(),
        sources: store.sources.len(),
        ingestions: store.ingestions.len(),
    }
}

pub(crate) fn postgres_index_counts(
    conn: &mut postgres::Client,
    scope: &search::SearchScope,
) -> Result<IndexCounts, WikiError> {
    Ok(IndexCounts {
        documents: postgres_count(conn, "gwiki_documents", scope)?,
        chunks: postgres_count(conn, "gwiki_chunks", scope)?,
        links: postgres_count(conn, "gwiki_links", scope)?,
        sources: postgres_count(conn, "gwiki_sources", scope)?,
        ingestions: postgres_count(conn, "gwiki_ingestions", scope)?,
    })
}

fn postgres_count(
    conn: &mut postgres::Client,
    table: &'static str,
    scope: &search::SearchScope,
) -> Result<usize, WikiError> {
    let sql = format!("SELECT COUNT(*) FROM {table} WHERE scope_kind = $1 AND scope_id = $2");
    let count = conn
        .query_one(&sql, &[&scope.scope_kind(), &scope.scope_value()])
        .map_err(|error| WikiError::Config {
            detail: format!("failed to count PostgreSQL gwiki rows in {table}: {error}"),
        })?
        .get::<_, i64>(0);
    usize::try_from(count).map_err(|error| WikiError::Config {
        detail: format!("invalid PostgreSQL gwiki row count in {table}: {error}"),
    })
}
