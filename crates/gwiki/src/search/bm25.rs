use std::path::PathBuf;

use crate::search::{
    ChunkProvenance, SearchError, SearchHitKind, SearchProvenance, SearchScope, SearchSource,
    WikiSearchResult,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bm25SearchRequest {
    pub query: String,
    pub scope: SearchScope,
    pub limit: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bm25Sql {
    pub sql: String,
    pub params: Bm25SqlParams,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bm25SqlParams {
    pub query: String,
    pub scope_kind: String,
    pub scope_value: String,
    pub limit: i64,
}

pub trait Bm25SearchBackend {
    fn search_bm25(
        &mut self,
        request: &Bm25SearchRequest,
    ) -> Result<Vec<WikiSearchResult>, SearchError>;
}

pub fn search_bm25<B>(
    backend: &mut B,
    request: Bm25SearchRequest,
) -> Result<Vec<WikiSearchResult>, SearchError>
where
    B: Bm25SearchBackend,
{
    let Some(_) = build_bm25_sql(&request.query, &request.scope, request.limit) else {
        return Ok(Vec::new());
    };

    let mut results = backend.search_bm25(&request)?;
    results.retain(|result| {
        result.scope == request.scope
            && is_keyword_searchable_path(&result.path.to_string_lossy())
            && result.sources.contains(&SearchSource::Bm25)
    });
    results.truncate(request.limit);
    Ok(results)
}

pub fn build_bm25_sql(query: &str, scope: &SearchScope, limit: usize) -> Option<Bm25Sql> {
    let query = sanitize_pg_search_query(query);
    if query.is_empty() || limit == 0 {
        return None;
    }

    let chunk_searchable_path_predicate = searchable_path_predicate("c.path");
    let document_searchable_path_predicate = searchable_path_predicate("d.path");
    let sql = format!(
        r#"
WITH hits AS (
    SELECT
        'chunk:' || c.path || ':' || c.chunk_index::text AS id,
        'chunk' AS hit_kind,
        d.title,
        c.path,
        COALESCE(NULLIF(c.provenance->>'source_path', ''), NULLIF(d.provenance->>'source_path', ''), c.path) AS source_path,
        c.chunk_index::BIGINT AS chunk_index,
        CASE
            WHEN c.provenance->>'byte_start' ~ '^[0-9]+$'
            THEN (c.provenance->>'byte_start')::BIGINT
        END AS byte_start,
        CASE
            WHEN c.provenance->>'byte_end' ~ '^[0-9]+$'
            THEN (c.provenance->>'byte_end')::BIGINT
        END AS byte_end,
        COALESCE(NULLIF(c.provenance->>'heading', ''), c.heading_path[array_length(c.heading_path, 1)]) AS heading,
        c.content AS snippet,
        c.source_kind,
        c.content_hash,
        pdb.score(c.id) AS score
    FROM gwiki_chunks c
    JOIN gwiki_documents d
        ON d.id = c.document_id
       AND d.scope_kind = c.scope_kind
       AND d.scope_id = c.scope_id
       AND d.path = c.path
    WHERE c.scope_kind = $2
      AND c.scope_id = $3
      AND ({chunk_searchable_path_predicate})
      AND (c.path @@@ $1 OR c.content @@@ $1)

    UNION ALL

    SELECT
        'document:' || d.path AS id,
        'document' AS hit_kind,
        d.title,
        d.path,
        COALESCE(NULLIF(d.provenance->>'source_path', ''), d.path) AS source_path,
        NULL::BIGINT AS chunk_index,
        NULL::BIGINT AS byte_start,
        NULL::BIGINT AS byte_end,
        NULL::TEXT AS heading,
        d.body AS snippet,
        d.source_kind,
        d.content_hash,
        pdb.score(d.id) AS score
    FROM gwiki_documents d
    WHERE d.scope_kind = $2
      AND d.scope_id = $3
      AND ({document_searchable_path_predicate})
      AND (d.path @@@ $1 OR d.title @@@ $1 OR d.body @@@ $1)
)
SELECT *
FROM hits
ORDER BY score DESC, path ASC, id ASC
LIMIT $4
"#
    );

    Some(Bm25Sql {
        sql,
        params: Bm25SqlParams {
            query,
            scope_kind: scope.scope_kind().to_string(),
            scope_value: scope.scope_value().to_string(),
            limit: limit as i64,
        },
    })
}

pub fn sanitize_pg_search_query(query: &str) -> String {
    let cleaned = query
        .chars()
        .map(|ch| {
            if ch.is_alphanumeric() || matches!(ch, ' ' | '_' | '-') {
                ch
            } else {
                ' '
            }
        })
        .collect::<String>();

    cleaned
        .split_whitespace()
        .filter(|token| !token.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn is_keyword_searchable_path(path: &str) -> bool {
    let normalized = path.trim_start_matches("./").replace('\\', "/");
    if normalized == "raw/INDEX.md" {
        return true;
    }
    if !normalized.ends_with(".md") {
        return false;
    }
    normalized.starts_with("wiki/sources/")
        || normalized.starts_with("wiki/concepts/")
        || normalized.starts_with("wiki/topics/")
}

fn searchable_path_predicate(column: &str) -> String {
    format!(
        "{column} = 'raw/INDEX.md' OR {column} LIKE 'wiki/sources/%.md' OR {column} LIKE 'wiki/concepts/%.md' OR {column} LIKE 'wiki/topics/%.md'"
    )
}

pub struct PostgresBm25Backend<'a> {
    conn: &'a mut postgres::Client,
}

impl<'a> PostgresBm25Backend<'a> {
    pub fn new(conn: &'a mut postgres::Client) -> Self {
        Self { conn }
    }
}

impl Bm25SearchBackend for PostgresBm25Backend<'_> {
    fn search_bm25(
        &mut self,
        request: &Bm25SearchRequest,
    ) -> Result<Vec<WikiSearchResult>, SearchError> {
        let Some(query) = build_bm25_sql(&request.query, &request.scope, request.limit) else {
            return Ok(Vec::new());
        };

        let rows = self
            .conn
            .query(
                &query.sql,
                &[
                    &query.params.query,
                    &query.params.scope_kind,
                    &query.params.scope_value,
                    &query.params.limit,
                ],
            )
            .map_err(|error| SearchError::Backend(error.to_string()))?;

        rows.into_iter()
            .map(|row| row_to_result(row, &request.scope))
            .collect()
    }
}

fn row_to_result(row: postgres::Row, scope: &SearchScope) -> Result<WikiSearchResult, SearchError> {
    let id = read_string(&row, "id")?;
    let path = PathBuf::from(read_string(&row, "path")?);
    let source_path = PathBuf::from(read_string(&row, "source_path")?);
    let source_kind = read_string(&row, "source_kind")?;
    let hit_kind = match read_string(&row, "hit_kind")?.as_str() {
        "document" => SearchHitKind::Document,
        _ => SearchHitKind::Chunk,
    };
    let chunk = match (
        read_optional_i64(&row, "chunk_index"),
        read_optional_i64(&row, "byte_start"),
        read_optional_i64(&row, "byte_end"),
    ) {
        (Some(chunk_index), Some(byte_start), Some(byte_end)) => Some(ChunkProvenance {
            chunk_index: chunk_index as usize,
            byte_start: byte_start as usize,
            byte_end: byte_end as usize,
            heading: row
                .try_get::<_, Option<String>>("heading")
                .map_err(|error| SearchError::Backend(error.to_string()))?,
        }),
        _ => None,
    };

    Ok(WikiSearchResult {
        id,
        title: row
            .try_get::<_, Option<String>>("title")
            .map_err(|error| SearchError::Backend(error.to_string()))?,
        scope: scope.clone(),
        path: path.clone(),
        source_path: source_path.clone(),
        hit_kind,
        snippet: read_string(&row, "snippet")?,
        score: row
            .try_get::<_, f64>("score")
            .map_err(|error| SearchError::Backend(error.to_string()))?,
        sources: vec![SearchSource::Bm25],
        explanations: Vec::new(),
        chunk,
        provenance: SearchProvenance {
            document_path: path,
            source_path,
            source_kind,
            content_hash: row
                .try_get::<_, Option<String>>("content_hash")
                .map_err(|error| SearchError::Backend(error.to_string()))?,
        },
    })
}

fn read_string(row: &postgres::Row, column: &str) -> Result<String, SearchError> {
    row.try_get::<_, String>(column)
        .map_err(|error| SearchError::Backend(error.to_string()))
}

fn read_optional_i64(row: &postgres::Row, column: &str) -> Option<i64> {
    row.try_get::<_, Option<i64>>(column).ok().flatten()
}

#[cfg(test)]
#[derive(Debug)]
pub struct MemoryBm25Backend {
    hits: Vec<WikiSearchResult>,
}

#[cfg(test)]
impl MemoryBm25Backend {
    pub fn new(hits: Vec<WikiSearchResult>) -> Self {
        Self { hits }
    }
}

#[cfg(test)]
impl Bm25SearchBackend for MemoryBm25Backend {
    fn search_bm25(
        &mut self,
        _request: &Bm25SearchRequest,
    ) -> Result<Vec<WikiSearchResult>, SearchError> {
        Ok(self.hits.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search::{SearchHitKind, SearchScope};
    use crate::setup::{GwikiPostgresObject, GwikiStandaloneSetup};
    use std::collections::BTreeSet;

    #[test]
    fn bm25_is_scope_filtered() {
        let mut backend = MemoryBm25Backend::new(vec![
            memory_hit("hit-project", SearchScope::project("project-1")),
            memory_hit("hit-topic", SearchScope::topic("rust")),
        ]);

        let results = search_bm25(
            &mut backend,
            Bm25SearchRequest {
                query: "ownership".to_string(),
                scope: SearchScope::project("project-1"),
                limit: 10,
            },
        )
        .expect("bm25 search succeeds");

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "hit-project");
        assert_eq!(results[0].scope, SearchScope::project("project-1"));
        assert_eq!(results[0].hit_kind, SearchHitKind::Chunk);

        let sql = build_bm25_sql("ownership", &SearchScope::project("project-1"), 10)
            .expect("query is searchable");
        assert!(sql.sql.contains("gwiki_documents"));
        assert!(sql.sql.contains("gwiki_chunks"));
        assert!(sql.sql.contains("@@@"));
        assert_eq!(sql.params.scope_kind, "project");
        assert_eq!(sql.params.scope_value, "project-1");
    }

    #[test]
    fn keyword_search_covers_generated_wiki_pages() {
        for path in [
            "raw/INDEX.md",
            "wiki/sources/citation.md",
            "wiki/concepts/ownership.md",
            "wiki/topics/rust.md",
        ] {
            assert!(
                is_keyword_searchable_path(path),
                "{path} should be searched"
            );
        }

        assert!(!is_keyword_searchable_path("raw/private-note.md"));
        assert!(!is_keyword_searchable_path("outputs/export.md"));
    }

    #[test]
    fn bm25_sql_uses_setup_created_columns() {
        let setup_objects = GwikiStandaloneSetup::new("public")
            .postgres_objects()
            .expect("setup schema objects");
        let document_columns = table_columns(&setup_objects, "gwiki_documents");
        let chunk_columns = table_columns(&setup_objects, "gwiki_chunks");
        let sql = build_bm25_sql("ownership", &SearchScope::project("project-1"), 10)
            .expect("query is searchable")
            .sql;

        let unknown_document_columns = unknown_alias_columns("d", &sql, &document_columns);
        let unknown_chunk_columns = unknown_alias_columns("c", &sql, &chunk_columns);

        assert!(
            unknown_document_columns.is_empty() && unknown_chunk_columns.is_empty(),
            "BM25 SQL references columns absent from setup schema: documents={unknown_document_columns:?}, chunks={unknown_chunk_columns:?}"
        );
    }

    fn memory_hit(id: &str, scope: SearchScope) -> crate::search::WikiSearchResult {
        crate::search::WikiSearchResult {
            id: id.to_string(),
            title: Some("Ownership".to_string()),
            scope,
            path: "wiki/topics/rust.md".into(),
            source_path: "raw/INDEX.md".into(),
            hit_kind: SearchHitKind::Chunk,
            snippet: "ownership borrowing lifetimes".to_string(),
            score: 1.0,
            sources: vec![crate::search::SearchSource::Bm25],
            explanations: Vec::new(),
            chunk: Some(crate::search::ChunkProvenance {
                chunk_index: 0,
                byte_start: 0,
                byte_end: 29,
                heading: Some("Ownership".to_string()),
            }),
            provenance: crate::search::SearchProvenance {
                document_path: "wiki/topics/rust.md".into(),
                source_path: "raw/INDEX.md".into(),
                source_kind: "topic".to_string(),
                content_hash: Some("hash".to_string()),
            },
        }
    }

    fn table_columns(objects: &[GwikiPostgresObject], table_name: &str) -> BTreeSet<String> {
        let sql = objects
            .iter()
            .find(|object| object.name == table_name)
            .unwrap_or_else(|| panic!("missing setup object {table_name}"))
            .sql
            .as_str();
        let (_, definitions) = sql
            .split_once('(')
            .unwrap_or_else(|| panic!("missing table body for {table_name}: {sql}"));

        definitions
            .lines()
            .filter_map(|line| {
                let column = line
                    .trim()
                    .trim_end_matches(',')
                    .split_whitespace()
                    .next()?;
                if column.starts_with(')') || matches!(column, "UNIQUE" | "PRIMARY" | "FOREIGN") {
                    None
                } else {
                    Some(column.to_string())
                }
            })
            .collect()
    }

    fn unknown_alias_columns(
        alias: &str,
        sql: &str,
        setup_columns: &BTreeSet<String>,
    ) -> Vec<String> {
        alias_columns(sql, alias)
            .difference(setup_columns)
            .cloned()
            .collect::<Vec<_>>()
    }

    fn alias_columns(sql: &str, alias: &str) -> BTreeSet<String> {
        let marker = format!("{alias}.");
        sql.match_indices(&marker)
            .filter_map(|(start, _)| {
                let column = sql[start + marker.len()..]
                    .chars()
                    .take_while(|ch| ch.is_ascii_alphanumeric() || *ch == '_')
                    .collect::<String>();
                (!column.is_empty()).then_some(column)
            })
            .collect()
    }
}
