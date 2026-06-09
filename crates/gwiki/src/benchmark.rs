use std::collections::{BTreeMap, BTreeSet, HashMap};

use gobby_core::ai_context::AiContext;
use gobby_core::config::{
    AiCapability, AiRouting, EmbeddingConfig, FalkorConfig, QdrantConfig, resolve_embedding_config,
    resolve_falkordb_config, resolve_qdrant_config,
};
use gobby_core::degradation::DegradationKind;
use gobby_core::falkor::GraphClient;
use postgres::Client;
use serde::Serialize;
use serde_json::Value;

use crate::falkor_graph::FALKORDB_GRAPH_NAME;
use crate::search::SearchScope;
use crate::search::semantic::{
    GobbyQdrantBackend, OpenAiEmbeddingBackend, QueryEmbedder, SemanticEmbedding,
    SemanticSearchRequest, VectorSearchBackend, search_semantic,
};
use crate::{ScopeIdentity, WikiError};

const DEGRADE_FALKORDB: &str = "falkordb";
const DEGRADE_QDRANT: &str = "qdrant";
const DEGRADE_EMBEDDINGS: &str = "embeddings";
const DEGRADE_MODEL_PROVIDER: &str = "model_provider";

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BenchmarkReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub token_compression: TokenCompressionReport,
    pub graph_coverage: GraphCoverageReport,
    pub retrieval_precision: RetrievalPrecisionReport,
    pub source_mix: SourceMixReport,
    pub model_provider: AvailabilityReport,
    pub degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TokenCompressionReport {
    pub available: bool,
    pub document_tokens: u64,
    pub chunk_tokens: u64,
    pub ratio: Option<f64>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct GraphCoverageReport {
    pub available: bool,
    pub postgres_documents: u64,
    pub postgres_links: u64,
    pub graph_documents: Option<u64>,
    pub graph_links: Option<u64>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RetrievalPrecisionReport {
    pub available: bool,
    pub examples: Vec<RetrievalPrecisionExample>,
    pub reason: Option<String>,
    #[serde(skip)]
    pub backend_degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RetrievalPrecisionExample {
    pub query: String,
    pub expected_path: String,
    pub returned_path: Option<String>,
    pub precision_at_1: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SourceMixReport {
    pub available: bool,
    pub total_documents: u64,
    pub total_sources: u64,
    pub document_kinds: BTreeMap<String, u64>,
    pub source_kinds: BTreeMap<String, u64>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AvailabilityReport {
    pub available: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionalBenchmarkSources {
    pub falkor: Option<FalkorConfig>,
    pub qdrant: Option<QdrantConfig>,
    pub embedding: Option<EmbeddingConfig>,
    pub model_provider_available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct BenchmarkRows {
    document_paths: Vec<String>,
    document_bodies: Vec<String>,
    chunk_paths: Vec<String>,
    chunk_contents: Vec<String>,
    documents: u64,
    links: u64,
    sources: u64,
    document_kinds: BTreeMap<String, u64>,
    source_kinds: BTreeMap<String, u64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RetrievalPrecisionCandidate {
    query: String,
    expected_path: String,
}

pub fn report_from_postgres(
    conn: &mut Client,
    scope: ScopeIdentity,
    search_scope: SearchScope,
    optional: OptionalBenchmarkSources,
) -> Result<BenchmarkReport, WikiError> {
    let rows = load_benchmark_rows(conn, &search_scope)?;
    let token_compression = token_compression(&rows);
    let source_mix = source_mix(&rows);
    let graph_coverage = graph_coverage(&rows, &search_scope, optional.falkor.as_ref());
    let retrieval_precision = retrieval_precision(&rows, &search_scope, &optional);
    let model_provider = model_provider(&optional);
    Ok(build_report(
        scope,
        token_compression,
        graph_coverage,
        retrieval_precision,
        source_mix,
        &optional,
        model_provider,
    ))
}

pub fn resolve_optional_sources(
    ai_context: &AiContext,
    source: &mut impl gobby_core::config::ConfigSource,
) -> OptionalBenchmarkSources {
    OptionalBenchmarkSources {
        falkor: resolve_falkordb_config(source),
        qdrant: resolve_qdrant_config(source),
        embedding: resolve_embedding_config(source),
        model_provider_available: model_provider_available(ai_context),
    }
}

fn build_report(
    scope: ScopeIdentity,
    token_compression: TokenCompressionReport,
    graph_coverage: GraphCoverageReport,
    retrieval_precision: RetrievalPrecisionReport,
    source_mix: SourceMixReport,
    optional: &OptionalBenchmarkSources,
    model_provider: AvailabilityReport,
) -> BenchmarkReport {
    let mut degraded = BTreeSet::new();
    if !graph_coverage.available {
        degraded.insert(DEGRADE_FALKORDB.to_string());
    }
    if !qdrant_source_configured(optional) {
        degraded.insert(DEGRADE_QDRANT.to_string());
    }
    if optional.embedding.is_none() {
        degraded.insert(DEGRADE_EMBEDDINGS.to_string());
    }
    degraded.extend(retrieval_precision.backend_degraded_sources.iter().cloned());
    if !model_provider.available {
        degraded.insert(DEGRADE_MODEL_PROVIDER.to_string());
    }

    BenchmarkReport {
        command: "benchmark",
        scope,
        token_compression,
        graph_coverage,
        retrieval_precision,
        source_mix,
        model_provider,
        degraded_sources: degraded.into_iter().collect(),
    }
}

fn load_benchmark_rows(conn: &mut Client, scope: &SearchScope) -> Result<BenchmarkRows, WikiError> {
    let scope_kind = scope.scope_kind();
    let scope_id = scope.scope_value();
    let document_rows = conn
        .query(
            "SELECT path, body FROM gwiki_documents WHERE scope_kind = $1 AND scope_id = $2 ORDER BY path",
            &[&scope_kind, &scope_id],
        )
        .map_err(postgres_error("query gwiki benchmark documents"))?
        .into_iter()
        .map(|row| (row.get::<_, String>(0), row.get::<_, String>(1)))
        .collect::<Vec<_>>();
    let chunk_rows = conn
        .query(
            "SELECT path, content FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 ORDER BY path, chunk_index",
            &[&scope_kind, &scope_id],
        )
        .map_err(postgres_error("query gwiki benchmark chunks"))?
        .into_iter()
        .map(|row| (row.get::<_, String>(0), row.get::<_, String>(1)))
        .collect::<Vec<_>>();
    let document_kinds = grouped_counts(
        conn,
        "SELECT source_kind, COUNT(*) FROM gwiki_documents WHERE scope_kind = $1 AND scope_id = $2 GROUP BY source_kind ORDER BY source_kind",
        scope_kind,
        scope_id,
        "query gwiki benchmark document mix",
    )?;
    let source_kinds = grouped_counts(
        conn,
        "SELECT source_kind, COUNT(*) FROM gwiki_sources WHERE scope_kind = $1 AND scope_id = $2 GROUP BY source_kind ORDER BY source_kind",
        scope_kind,
        scope_id,
        "query gwiki benchmark source mix",
    )?;
    let links = scalar_count(
        conn,
        "SELECT COUNT(*) FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND link_kind = 'wiki'",
        scope_kind,
        scope_id,
        "query gwiki benchmark link count",
    )?;

    Ok(BenchmarkRows {
        documents: document_rows.len() as u64,
        sources: source_kinds.values().sum(),
        links,
        document_paths: document_rows.iter().map(|(path, _)| path.clone()).collect(),
        document_bodies: document_rows.into_iter().map(|(_, body)| body).collect(),
        chunk_paths: chunk_rows.iter().map(|(path, _)| path.clone()).collect(),
        chunk_contents: chunk_rows.into_iter().map(|(_, content)| content).collect(),
        document_kinds,
        source_kinds,
    })
}

fn grouped_counts(
    conn: &mut Client,
    sql: &str,
    scope_kind: &str,
    scope_id: &str,
    action: &'static str,
) -> Result<BTreeMap<String, u64>, WikiError> {
    conn.query(sql, &[&scope_kind, &scope_id])
        .map_err(postgres_error(action))?
        .into_iter()
        .map(|row| {
            let kind = row.get::<_, String>(0);
            let count = row.get::<_, i64>(1);
            Ok((kind, count as u64))
        })
        .collect()
}

fn scalar_count(
    conn: &mut Client,
    sql: &str,
    scope_kind: &str,
    scope_id: &str,
    action: &'static str,
) -> Result<u64, WikiError> {
    let row = conn
        .query_one(sql, &[&scope_kind, &scope_id])
        .map_err(postgres_error(action))?;
    let count = row.get::<_, i64>(0);
    Ok(count as u64)
}

fn token_compression(rows: &BenchmarkRows) -> TokenCompressionReport {
    let document_tokens = rows
        .document_bodies
        .iter()
        .map(|body| token_count(body))
        .sum::<u64>();
    let chunk_tokens = rows
        .chunk_contents
        .iter()
        .map(|content| token_count(content))
        .sum::<u64>();
    let ratio = if document_tokens == 0 {
        None
    } else {
        Some(round_ratio(chunk_tokens as f64 / document_tokens as f64))
    };
    TokenCompressionReport {
        available: ratio.is_some(),
        document_tokens,
        chunk_tokens,
        ratio,
        reason: (document_tokens == 0)
            .then(|| "seeded project has no indexed document tokens".to_string()),
    }
}

fn source_mix(rows: &BenchmarkRows) -> SourceMixReport {
    SourceMixReport {
        available: true,
        total_documents: rows.documents,
        total_sources: rows.sources,
        document_kinds: rows.document_kinds.clone(),
        source_kinds: rows.source_kinds.clone(),
        reason: None,
    }
}

fn graph_coverage(
    rows: &BenchmarkRows,
    scope: &SearchScope,
    falkor: Option<&FalkorConfig>,
) -> GraphCoverageReport {
    let Some(falkor) = falkor else {
        return GraphCoverageReport {
            available: false,
            postgres_documents: rows.documents,
            postgres_links: rows.links,
            graph_documents: None,
            graph_links: None,
            reason: Some("FalkorDB is not configured".to_string()),
        };
    };
    match query_graph_counts(falkor, scope) {
        Ok((graph_documents, graph_links)) => GraphCoverageReport {
            available: true,
            postgres_documents: rows.documents,
            postgres_links: rows.links,
            graph_documents: Some(graph_documents),
            graph_links: Some(graph_links),
            reason: None,
        },
        Err(error) => GraphCoverageReport {
            available: false,
            postgres_documents: rows.documents,
            postgres_links: rows.links,
            graph_documents: None,
            graph_links: None,
            reason: Some(error.to_string()),
        },
    }
}

fn retrieval_precision(
    rows: &BenchmarkRows,
    scope: &SearchScope,
    optional: &OptionalBenchmarkSources,
) -> RetrievalPrecisionReport {
    let mut embedder = OpenAiEmbeddingBackend::new();
    let mut vector_backend = GobbyQdrantBackend;
    retrieval_precision_with_backends(rows, scope, optional, &mut embedder, &mut vector_backend)
}

fn retrieval_precision_with_backends<E, V>(
    rows: &BenchmarkRows,
    scope: &SearchScope,
    optional: &OptionalBenchmarkSources,
    embedder: &mut E,
    vector_backend: &mut V,
) -> RetrievalPrecisionReport
where
    E: QueryEmbedder,
    V: VectorSearchBackend,
{
    let Some(qdrant) = optional
        .qdrant
        .as_ref()
        .filter(|_| qdrant_source_configured(optional))
    else {
        return unavailable_retrieval_precision(
            "Qdrant and embedding provider are not both configured",
            vec![DEGRADE_QDRANT.to_string()],
        );
    };
    let Some(embedding) = optional.embedding.as_ref() else {
        return unavailable_retrieval_precision(
            "Qdrant and embedding provider are not both configured",
            vec![DEGRADE_EMBEDDINGS.to_string()],
        );
    };
    let candidates = seeded_retrieval_candidates(rows);
    if candidates.is_empty() {
        return unavailable_retrieval_precision(
            "seeded project has no path-backed chunks for retrieval precision",
            Vec::new(),
        );
    }

    let embedding = SemanticEmbedding::Direct(embedding.clone());
    let mut examples = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let outcome = match search_semantic(
            SemanticSearchRequest {
                query: candidate.query.clone(),
                scope: scope.clone(),
                limit: 1,
            },
            Some(&embedding),
            Some(qdrant),
            embedder,
            vector_backend,
        ) {
            Ok(outcome) => outcome,
            Err(error) => {
                return unavailable_retrieval_precision(error.to_string(), Vec::new());
            }
        };
        if let Some(degradation) = outcome.degradation {
            return unavailable_retrieval_precision(
                format!("{degradation:?}"),
                semantic_degraded_sources(&degradation),
            );
        }
        let returned_path = outcome
            .hits
            .first()
            .map(|hit| hit.path.to_string_lossy().to_string());
        let precision_at_1 = returned_path.as_deref().map(|path| {
            if path == candidate.expected_path {
                1.0
            } else {
                0.0
            }
        });
        examples.push(RetrievalPrecisionExample {
            query: candidate.query,
            expected_path: candidate.expected_path,
            returned_path,
            precision_at_1,
        });
    }

    if examples.is_empty() {
        return unavailable_retrieval_precision(
            "semantic benchmark returned no retrieval examples",
            Vec::new(),
        );
    }

    RetrievalPrecisionReport {
        available: true,
        examples,
        reason: None,
        backend_degraded_sources: Vec::new(),
    }
}

fn unavailable_retrieval_precision(
    reason: impl Into<String>,
    backend_degraded_sources: Vec<String>,
) -> RetrievalPrecisionReport {
    RetrievalPrecisionReport {
        available: false,
        examples: Vec::new(),
        reason: Some(reason.into()),
        backend_degraded_sources,
    }
}

fn semantic_degraded_sources(degradation: &DegradationKind) -> Vec<String> {
    match degradation {
        DegradationKind::ServiceUnavailable { service, .. } => vec![service.clone()],
        DegradationKind::PartialSearch { unavailable, .. } => unavailable.clone(),
        _ => Vec::new(),
    }
}

fn qdrant_source_configured(optional: &OptionalBenchmarkSources) -> bool {
    optional
        .qdrant
        .as_ref()
        .and_then(|config| config.url.as_deref())
        .is_some_and(|url| !url.trim().is_empty())
}

fn seeded_retrieval_candidates(rows: &BenchmarkRows) -> Vec<RetrievalPrecisionCandidate> {
    rows.document_paths
        .iter()
        .filter_map(|expected_path| {
            rows.chunk_paths
                .iter()
                .zip(rows.chunk_contents.iter())
                .find(|(chunk_path, content)| *chunk_path == expected_path && non_empty(content))
                .map(|(_, content)| RetrievalPrecisionCandidate {
                    query: benchmark_query(content),
                    expected_path: expected_path.clone(),
                })
        })
        .take(3)
        .collect()
}

fn benchmark_query(content: &str) -> String {
    content
        .split_whitespace()
        .take(8)
        .collect::<Vec<_>>()
        .join(" ")
}

fn model_provider(optional: &OptionalBenchmarkSources) -> AvailabilityReport {
    AvailabilityReport {
        available: optional.model_provider_available,
        reason: (!optional.model_provider_available)
            .then(|| "text model provider is not configured".to_string()),
    }
}

fn model_provider_available(context: &AiContext) -> bool {
    let binding = context.binding(AiCapability::TextGenerate);
    match binding.routing {
        AiRouting::Off => false,
        AiRouting::Daemon => true,
        AiRouting::Direct => {
            binding.api_base.as_deref().is_some_and(non_empty)
                && binding.model.as_deref().is_some_and(non_empty)
        }
        AiRouting::Auto => {
            binding.api_base.as_deref().is_some_and(non_empty)
                && binding.model.as_deref().is_some_and(non_empty)
        }
    }
}

fn query_graph_counts(config: &FalkorConfig, scope: &SearchScope) -> anyhow::Result<(u64, u64)> {
    let mut client = GraphClient::from_config(config, FALKORDB_GRAPH_NAME)?;
    let params = graph_scope_params(scope);
    let doc_rows = client.query(
        "MATCH (doc:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id}) RETURN count(doc) AS count",
        Some(params.clone()),
    )?;
    let link_rows = client.query(
        "MATCH (source:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})-[rel:WIKI_LINKS_TO|MENTIONS_TARGET]->(target) WHERE (target:WikiDoc AND target.scope_kind = $scope_kind AND target.scope_id = $scope_id) OR (target:WikiTarget AND target.scope_kind = $scope_kind AND target.scope_id = $scope_id) RETURN count(rel) AS count",
        Some(params),
    )?;
    Ok((falkor_count(&doc_rows), falkor_count(&link_rows)))
}

fn graph_scope_params(scope: &SearchScope) -> HashMap<String, String> {
    HashMap::from([
        (
            "scope_kind".to_string(),
            gobby_core::falkor::escape_string(scope.scope_kind()),
        ),
        (
            "scope_id".to_string(),
            gobby_core::falkor::escape_string(scope.scope_value()),
        ),
    ])
}

fn falkor_count(rows: &[HashMap<String, Value>]) -> u64 {
    rows.first()
        .and_then(|row| row.get("count"))
        .and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_i64().map(|count| count as u64))
        })
        .unwrap_or(0)
}

fn token_count(text: &str) -> u64 {
    text.split_whitespace().count() as u64
}

fn round_ratio(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

fn non_empty(value: &str) -> bool {
    !value.trim().is_empty()
}

fn postgres_error(action: &'static str) -> impl FnOnce(postgres::Error) -> WikiError {
    move |error| WikiError::Config {
        detail: format!("{action}: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::qdrant::{SearchHit, SearchRequest};

    struct FixedEmbedder;

    impl QueryEmbedder for FixedEmbedder {
        fn embed_query(
            &mut self,
            _embedding: &SemanticEmbedding,
            _query: &str,
        ) -> Result<Vec<f32>, crate::search::SearchError> {
            Ok(vec![1.0])
        }
    }

    struct SequenceVectorBackend {
        paths: Vec<String>,
    }

    impl SequenceVectorBackend {
        fn new(paths: Vec<&str>) -> Self {
            Self {
                paths: paths.into_iter().map(str::to_string).collect(),
            }
        }
    }

    impl VectorSearchBackend for SequenceVectorBackend {
        fn search(
            &mut self,
            _config: &QdrantConfig,
            _collection: &str,
            _request: SearchRequest,
        ) -> anyhow::Result<Vec<SearchHit>> {
            let path = self.paths.remove(0);
            let mut payload = serde_json::Map::new();
            payload.insert("namespace".to_string(), serde_json::json!("gwiki"));
            payload.insert("scope_kind".to_string(), serde_json::json!("topic"));
            payload.insert("topic".to_string(), serde_json::json!("rust"));
            payload.insert("project_id".to_string(), serde_json::json!("rust"));
            payload.insert("path".to_string(), serde_json::json!(path));
            payload.insert("source_kind".to_string(), serde_json::json!("wiki"));
            Ok(vec![SearchHit {
                id: "hit".to_string(),
                score: 1.0,
                payload,
            }])
        }
    }

    fn rows() -> BenchmarkRows {
        BenchmarkRows {
            document_paths: vec!["ownership.md".to_string(), "rust.md".to_string()],
            document_bodies: vec![
                "one two three four".to_string(),
                "five six seven eight".to_string(),
            ],
            chunk_paths: vec!["ownership.md".to_string(), "rust.md".to_string()],
            chunk_contents: vec!["one two".to_string(), "five six".to_string()],
            documents: 2,
            links: 1,
            sources: 1,
            document_kinds: BTreeMap::from([("wiki".to_string(), 2)]),
            source_kinds: BTreeMap::from([("file".to_string(), 1)]),
        }
    }

    fn configured_optional_sources() -> OptionalBenchmarkSources {
        OptionalBenchmarkSources {
            falkor: None,
            qdrant: Some(QdrantConfig {
                url: Some("http://qdrant.local".to_string()),
                api_key: None,
            }),
            embedding: Some(EmbeddingConfig {
                api_base: "http://embeddings.local/v1".to_string(),
                model: "embed-model".to_string(),
                api_key: None,
                query_prefix: Some("query: ".to_string()),
                timeout_seconds: 10,
            }),
            model_provider_available: false,
        }
    }

    #[test]
    fn retrieval_precision_available_path_includes_examples() {
        let mut embedder = FixedEmbedder;
        let mut vector_backend = SequenceVectorBackend::new(vec!["ownership.md", "rust.md"]);
        let report = retrieval_precision_with_backends(
            &rows(),
            &SearchScope::topic("rust"),
            &configured_optional_sources(),
            &mut embedder,
            &mut vector_backend,
        );

        assert!(report.available);
        assert_eq!(
            report.examples,
            vec![
                RetrievalPrecisionExample {
                    query: "one two".to_string(),
                    expected_path: "ownership.md".to_string(),
                    returned_path: Some("ownership.md".to_string()),
                    precision_at_1: Some(1.0),
                },
                RetrievalPrecisionExample {
                    query: "five six".to_string(),
                    expected_path: "rust.md".to_string(),
                    returned_path: Some("rust.md".to_string()),
                    precision_at_1: Some(1.0),
                },
            ]
        );
        assert_eq!(report.reason, None);
    }

    #[test]
    fn benchmark_report_marks_optional_sources_degraded_without_zero_fill() {
        let optional = OptionalBenchmarkSources {
            falkor: None,
            qdrant: None,
            embedding: None,
            model_provider_available: false,
        };
        let report = build_report(
            ScopeIdentity::topic("rust"),
            token_compression(&rows()),
            graph_coverage(&rows(), &SearchScope::topic("rust"), None),
            retrieval_precision(&rows(), &SearchScope::topic("rust"), &optional),
            source_mix(&rows()),
            &optional,
            AvailabilityReport {
                available: false,
                reason: Some("text model provider is not configured".to_string()),
            },
        );

        assert!(report.token_compression.available);
        assert_eq!(report.token_compression.document_tokens, 8);
        assert_eq!(report.token_compression.chunk_tokens, 4);
        assert_eq!(report.token_compression.ratio, Some(0.5));
        assert!(!report.graph_coverage.available);
        assert_eq!(report.graph_coverage.graph_documents, None);
        assert!(!report.retrieval_precision.available);
        assert!(report.retrieval_precision.examples.is_empty());
        assert!(report.source_mix.available);
        assert_eq!(report.source_mix.total_documents, 2);
        assert_eq!(
            report.degraded_sources,
            vec![
                "embeddings".to_string(),
                "falkordb".to_string(),
                "model_provider".to_string(),
                "qdrant".to_string(),
            ]
        );
    }

    #[test]
    fn benchmark_report_does_not_degrade_configured_backends_without_examples() {
        let rows = BenchmarkRows {
            document_paths: Vec::new(),
            document_bodies: Vec::new(),
            chunk_paths: Vec::new(),
            chunk_contents: Vec::new(),
            documents: 0,
            links: 0,
            sources: 0,
            document_kinds: BTreeMap::new(),
            source_kinds: BTreeMap::new(),
        };
        let optional = configured_optional_sources();
        let mut embedder = FixedEmbedder;
        let mut vector_backend = SequenceVectorBackend::new(Vec::new());
        let retrieval_precision = retrieval_precision_with_backends(
            &rows,
            &SearchScope::topic("rust"),
            &optional,
            &mut embedder,
            &mut vector_backend,
        );
        let report = build_report(
            ScopeIdentity::topic("rust"),
            token_compression(&rows),
            graph_coverage(&rows, &SearchScope::topic("rust"), None),
            retrieval_precision,
            source_mix(&rows),
            &optional,
            model_provider(&optional),
        );

        assert!(!report.degraded_sources.contains(&"qdrant".to_string()));
        assert!(!report.degraded_sources.contains(&"embeddings".to_string()));
        assert_eq!(
            report.retrieval_precision.reason.as_deref(),
            Some("seeded project has no path-backed chunks for retrieval precision")
        );
    }
}
