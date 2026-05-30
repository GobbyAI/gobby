use gobby_core::config::ConfigSource;
use gobby_core::degradation::{DegradationKind, ServiceState};

use crate::{search, store};

use super::text::{
    display_path, document_kind_name, keyword_score, query_tokens, snippet_from_text,
};

pub(crate) struct StoreBm25Backend {
    pub(crate) hits: Vec<search::WikiSearchResult>,
}

impl search::bm25::Bm25SearchBackend for StoreBm25Backend {
    fn search_bm25(
        &mut self,
        _request: &search::bm25::Bm25SearchRequest,
    ) -> Result<Vec<search::WikiSearchResult>, search::SearchError> {
        Ok(self.hits.clone())
    }
}

pub(crate) struct UnavailableSemanticBackend;

impl search::semantic::SemanticSearchBackend for UnavailableSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: search::semantic::SemanticSearchRequest,
    ) -> Result<search::semantic::SemanticSearchOutcome, search::SearchError> {
        Ok(search::semantic::SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: Some(DegradationKind::ServiceUnavailable {
                service: "qdrant".to_string(),
                state: ServiceState::NotConfigured,
            }),
        })
    }
}

pub(crate) struct PostgresConfigSource<'a> {
    pub(crate) conn: &'a mut postgres::Client,
}

impl ConfigSource for PostgresConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        gobby_core::postgres::read_config_value(self.conn, key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.contains("$secret:") {
            anyhow::bail!("secret resolution is not available in gwiki search config");
        }

        gobby_core::config::resolve_env_pattern(value)?
            .ok_or_else(|| anyhow::anyhow!("unresolved environment pattern in `{value}`"))
    }
}

pub(crate) fn store_search_hits(
    store: &store::MemoryWikiStore,
    scope: &search::SearchScope,
    query: &str,
) -> Vec<search::WikiSearchResult> {
    let tokens = query_tokens(query);
    if tokens.is_empty() {
        return Vec::new();
    }

    let mut ranked = Vec::new();
    for document in store.documents.values() {
        if !search::bm25::is_keyword_searchable_path(&document.path.to_string_lossy()) {
            continue;
        }

        let document_score = keyword_score(
            &format!(
                "{}
{}",
                document.title.as_deref().unwrap_or_default(),
                document.body
            ),
            &tokens,
        );
        if document_score > 0 {
            ranked.push((
                document_score,
                search::WikiSearchResult {
                    id: format!("document:{}", display_path(&document.path)),
                    title: document.title.clone(),
                    scope: scope.clone(),
                    path: document.path.clone(),
                    source_path: document.path.clone(),
                    hit_kind: search::SearchHitKind::Document,
                    snippet: snippet_from_text(&document.body),
                    score: document_score as f64,
                    sources: vec![search::SearchSource::Bm25],
                    explanations: Vec::new(),
                    chunk: None,
                    provenance: search::SearchProvenance {
                        document_path: document.path.clone(),
                        source_path: document.path.clone(),
                        source_kind: document_kind_name(document.kind).to_string(),
                        content_hash: Some(document.content_hash.clone()),
                    },
                },
            ));
        }

        if let Some(chunks) = store.chunks.get(&document.path) {
            for chunk in chunks {
                let chunk_score = keyword_score(
                    &format!(
                        "{}
{}",
                        chunk.heading.as_deref().unwrap_or_default(),
                        chunk.content
                    ),
                    &tokens,
                );
                if chunk_score == 0 {
                    continue;
                }
                ranked.push((
                    chunk_score,
                    search::WikiSearchResult {
                        id: format!("chunk:{}:{}", display_path(&chunk.path), chunk.chunk_index),
                        title: document.title.clone(),
                        scope: scope.clone(),
                        path: chunk.path.clone(),
                        source_path: document.path.clone(),
                        hit_kind: search::SearchHitKind::Chunk,
                        snippet: snippet_from_text(&chunk.content),
                        score: chunk_score as f64,
                        sources: vec![search::SearchSource::Bm25],
                        explanations: Vec::new(),
                        chunk: Some(search::ChunkProvenance {
                            chunk_index: chunk.chunk_index,
                            byte_start: chunk.byte_start,
                            byte_end: chunk.byte_end,
                            heading: chunk.heading.clone(),
                        }),
                        provenance: search::SearchProvenance {
                            document_path: document.path.clone(),
                            source_path: document.path.clone(),
                            source_kind: document_kind_name(document.kind).to_string(),
                            content_hash: Some(document.content_hash.clone()),
                        },
                    },
                ));
            }
        }
    }

    ranked.sort_by(|(left_score, left), (right_score, right)| {
        right_score
            .cmp(left_score)
            .then_with(|| left.path.cmp(&right.path))
            .then_with(|| left.id.cmp(&right.id))
    });
    ranked
        .into_iter()
        .map(|(rank, mut result)| {
            result.score = rank as f64;
            result
        })
        .collect()
}
