use crate::{WikiError, search, store};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
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
        documents: postgres_count(conn, GwikiTable::Documents, scope)?,
        chunks: postgres_count(conn, GwikiTable::Chunks, scope)?,
        links: postgres_count(conn, GwikiTable::Links, scope)?,
        sources: postgres_count(conn, GwikiTable::Sources, scope)?,
        ingestions: postgres_count(conn, GwikiTable::Ingestions, scope)?,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum GwikiTable {
    Documents,
    Chunks,
    Links,
    Sources,
    Ingestions,
}

impl GwikiTable {
    fn as_identifier(self) -> &'static str {
        match self {
            Self::Documents => "gwiki_documents",
            Self::Chunks => "gwiki_chunks",
            Self::Links => "gwiki_links",
            Self::Sources => "gwiki_sources",
            Self::Ingestions => "gwiki_ingestions",
        }
    }
}

fn postgres_count(
    conn: &mut postgres::Client,
    table: GwikiTable,
    scope: &search::SearchScope,
) -> Result<usize, WikiError> {
    let table = table.as_identifier();
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gwiki_table_identifiers_are_fixed() {
        assert_eq!(GwikiTable::Documents.as_identifier(), "gwiki_documents");
        assert_eq!(GwikiTable::Chunks.as_identifier(), "gwiki_chunks");
        assert_eq!(GwikiTable::Links.as_identifier(), "gwiki_links");
        assert_eq!(GwikiTable::Sources.as_identifier(), "gwiki_sources");
        assert_eq!(GwikiTable::Ingestions.as_identifier(), "gwiki_ingestions");
    }
}
