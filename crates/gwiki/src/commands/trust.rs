use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;

use crate::support::config;
use crate::support::scope::resolve_selection_context;
use crate::support::{counts, env};
use crate::{
    CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, audit, health, indexer, store,
};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let output_scope = resolved.output_scope.clone();
    let root = resolved.scope.root().to_path_buf();
    let runtime = super::status::runtime_status_for("gwiki trust")?;
    let index = load_index_counts(resolved.scope.root(), &resolved.search_scope)?;
    let health = health::inspect(resolved.scope.root(), output_scope.clone())?;
    let audit = audit::run_with_options(
        resolved.scope.root(),
        output_scope.clone(),
        audit::AuditOptions::from_env(),
    )?;
    let report = TrustReport::from_parts(
        output_scope,
        root,
        runtime.mode,
        runtime.services,
        index,
        &health,
        &audit,
    );
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize trust report",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome(
        "trust",
        &report.scope,
        payload,
        render_text(&report),
    ))
}

struct IndexCountsOutcome {
    counts: counts::IndexCounts,
    backend: &'static str,
    degradations: Vec<String>,
}

fn load_index_counts(
    root: &Path,
    scope: &crate::search::SearchScope,
) -> Result<IndexCountsOutcome, WikiError> {
    let mut degradations = Vec::new();
    let mut index_options = config::local_index_options()?;
    if let Some(database_url) = env::database_url_for("gwiki trust")? {
        match gobby_core::postgres::connect_readonly(&database_url) {
            Ok(mut conn) => {
                index_options = config::index_options_from_conn(&mut conn)?;
                match counts::postgres_index_counts(&mut conn, scope) {
                    Ok(counts) => {
                        return Ok(IndexCountsOutcome {
                            counts,
                            backend: "postgres",
                            degradations,
                        });
                    }
                    Err(error) => {
                        log::warn!(
                            "failed to load PostgreSQL index counts for gwiki trust: {error}"
                        );
                        degradations.push("postgres_index_counts_unavailable".to_string());
                    }
                }
            }
            Err(error) => {
                log::warn!("failed to connect to PostgreSQL for gwiki trust: {error}");
                degradations.push("postgres_unavailable".to_string());
            }
        }
    } else {
        degradations.push("postgres_unconfigured".to_string());
    }

    Ok(IndexCountsOutcome {
        counts: memory_index_counts(root, index_options)?,
        backend: "memory",
        degradations,
    })
}

fn memory_index_counts(
    root: &Path,
    index_options: indexer::IndexOptions,
) -> Result<counts::IndexCounts, WikiError> {
    let mut store = store::MemoryWikiStore::default();
    if root.is_dir() {
        indexer::index_vault_with_options(root, &mut store, index_options)?;
    }
    Ok(counts::index_counts(&store))
}

#[derive(Debug, Serialize)]
struct TrustReport {
    command: &'static str,
    scope: ScopeIdentity,
    root: PathBuf,
    trust_status: &'static str,
    runtime: &'static str,
    services: Value,
    index_counts: TrustIndexCounts,
    degradations: Vec<String>,
    freshness: FreshnessSummary,
    audit_state: &'static str,
    audit_summary: AuditSummary,
    link_summary: LinkSummary,
    graph_metrics: GraphMetrics,
    health_summary: HealthSummary,
}

impl TrustReport {
    fn from_parts(
        scope: ScopeIdentity,
        root: PathBuf,
        runtime: &'static str,
        services: Value,
        index: IndexCountsOutcome,
        health: &health::HealthReport,
        audit: &audit::AuditReport,
    ) -> Self {
        let index_counts = TrustIndexCounts::from_counts(index.backend, &index.counts);
        let freshness = FreshnessSummary {
            stale_pages: health.stale_pages.len(),
            stale_citations: health.stale_citations.len(),
            fresh: health.stale_pages.is_empty() && health.stale_citations.is_empty(),
        };
        let audit_summary = AuditSummary {
            state: audit_state(audit.unsupported_claims.len()),
            unsupported_claim_count: audit.unsupported_claims.len(),
            source_context_count: audit.source_context.len(),
        };
        let link_summary = LinkSummary {
            broken_link_count: health.broken_links.len(),
            duplicate_concept_count: health.duplicate_concepts.len(),
        };
        let health_summary = HealthSummary {
            stale_page_count: health.stale_pages.len(),
            stale_citation_count: health.stale_citations.len(),
            uncited_source_count: health.uncited_sources.len(),
            uncompiled_source_count: health.uncompiled_sources.len(),
            broken_link_count: health.broken_links.len(),
            duplicate_concept_count: health.duplicate_concepts.len(),
        };
        let graph_metrics = GraphMetrics {
            wiki_link_count: index_counts.links,
            falkordb_configured: service_configured(&services, "falkordb"),
        };
        let degradations = degradation_labels(
            &services,
            &index_counts,
            &freshness,
            &audit_summary,
            &link_summary,
            &health_summary,
            index.degradations,
        );
        let trust_status = trust_status(
            &index_counts,
            &freshness,
            &audit_summary,
            &link_summary,
            &health_summary,
            &degradations,
        );

        Self {
            command: "trust",
            scope,
            root,
            trust_status,
            runtime,
            services,
            index_counts,
            degradations,
            freshness,
            audit_state: audit_summary.state,
            audit_summary,
            link_summary,
            graph_metrics,
            health_summary,
        }
    }
}

#[derive(Debug, Serialize)]
struct TrustIndexCounts {
    backend: &'static str,
    documents: usize,
    chunks: usize,
    links: usize,
    sources: usize,
    ingestions: usize,
}

impl TrustIndexCounts {
    fn from_counts(backend: &'static str, counts: &counts::IndexCounts) -> Self {
        Self {
            backend,
            documents: counts.documents,
            chunks: counts.chunks,
            links: counts.links,
            sources: counts.sources,
            ingestions: counts.ingestions,
        }
    }
}

#[derive(Debug, Serialize)]
struct FreshnessSummary {
    stale_pages: usize,
    stale_citations: usize,
    fresh: bool,
}

#[derive(Debug, Serialize)]
struct AuditSummary {
    state: &'static str,
    unsupported_claim_count: usize,
    source_context_count: usize,
}

#[derive(Debug, Serialize)]
struct LinkSummary {
    broken_link_count: usize,
    duplicate_concept_count: usize,
}

#[derive(Debug, Serialize)]
struct GraphMetrics {
    wiki_link_count: usize,
    falkordb_configured: bool,
}

#[derive(Debug, Serialize)]
struct HealthSummary {
    stale_page_count: usize,
    stale_citation_count: usize,
    uncited_source_count: usize,
    uncompiled_source_count: usize,
    broken_link_count: usize,
    duplicate_concept_count: usize,
}

fn degradation_labels(
    services: &Value,
    index_counts: &TrustIndexCounts,
    freshness: &FreshnessSummary,
    audit: &AuditSummary,
    links: &LinkSummary,
    health: &HealthSummary,
    index_degradations: Vec<String>,
) -> Vec<String> {
    let mut labels = index_degradations.into_iter().collect::<BTreeSet<_>>();
    for service in ["postgres", "falkordb", "qdrant", "embeddings"] {
        if !service_configured(services, service) {
            labels.insert(format!("{service}_unconfigured"));
        }
    }
    if index_counts.documents == 0 {
        labels.insert("index_empty".to_string());
    }
    if !freshness.fresh {
        labels.insert("stale_content".to_string());
    }
    if audit.unsupported_claim_count > 0 {
        labels.insert("unsupported_claims".to_string());
    }
    if links.broken_link_count > 0 {
        labels.insert("broken_links".to_string());
    }
    if links.duplicate_concept_count > 0 {
        labels.insert("duplicate_concepts".to_string());
    }
    if health.uncited_source_count > 0 {
        labels.insert("uncited_sources".to_string());
    }
    if health.uncompiled_source_count > 0 {
        labels.insert("uncompiled_sources".to_string());
    }
    labels.into_iter().collect()
}

fn trust_status(
    index_counts: &TrustIndexCounts,
    freshness: &FreshnessSummary,
    audit: &AuditSummary,
    links: &LinkSummary,
    health: &HealthSummary,
    degradations: &[String],
) -> &'static str {
    if index_counts.documents == 0 {
        "unindexed"
    } else if audit.unsupported_claim_count > 0
        || links.broken_link_count > 0
        || health.uncited_source_count > 0
        || health.uncompiled_source_count > 0
        || !freshness.fresh
    {
        "attention_required"
    } else if degradations.is_empty() {
        "clean"
    } else {
        "degraded"
    }
}

fn audit_state(unsupported_claim_count: usize) -> &'static str {
    if unsupported_claim_count == 0 {
        "clean"
    } else {
        "unsupported_claims"
    }
}

fn service_configured(services: &Value, service: &str) -> bool {
    services
        .get(service)
        .and_then(|status| status.get("configured"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn render_text(report: &TrustReport) -> String {
    let degradations = if report.degradations.is_empty() {
        "none".to_string()
    } else {
        report.degradations.join(", ")
    };
    format!(
        "gwiki trust {}\nScope: {}\nRuntime: {}\nIndex: {} documents, {} chunks ({})\nFreshness: {} stale pages, {} stale citations\nAudit: {} unsupported claims\nLinks: {} broken\nDegradations: {}",
        report.trust_status,
        report.scope,
        report.runtime,
        report.index_counts.documents,
        report.index_counts.chunks,
        report.index_counts.backend,
        report.freshness.stale_pages,
        report.freshness.stale_citations,
        report.audit_summary.unsupported_claim_count,
        report.link_summary.broken_link_count,
        degradations,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn indexed_counts() -> TrustIndexCounts {
        TrustIndexCounts {
            backend: "memory",
            documents: 1,
            chunks: 2,
            links: 3,
            sources: 1,
            ingestions: 1,
        }
    }

    #[test]
    fn trust_status_prioritizes_audit_attention() {
        let freshness = FreshnessSummary {
            stale_pages: 0,
            stale_citations: 0,
            fresh: true,
        };
        let audit = AuditSummary {
            state: "unsupported_claims",
            unsupported_claim_count: 1,
            source_context_count: 2,
        };
        let links = LinkSummary {
            broken_link_count: 0,
            duplicate_concept_count: 0,
        };
        let health = HealthSummary {
            stale_page_count: 0,
            stale_citation_count: 0,
            uncited_source_count: 0,
            uncompiled_source_count: 0,
            broken_link_count: 0,
            duplicate_concept_count: 0,
        };

        assert_eq!(
            trust_status(&indexed_counts(), &freshness, &audit, &links, &health, &[]),
            "attention_required"
        );
    }

    #[test]
    fn trust_report_json_includes_contract_fields() {
        let index_counts = indexed_counts();
        let freshness = FreshnessSummary {
            stale_pages: 0,
            stale_citations: 0,
            fresh: true,
        };
        let audit_summary = AuditSummary {
            state: "clean",
            unsupported_claim_count: 0,
            source_context_count: 1,
        };
        let link_summary = LinkSummary {
            broken_link_count: 0,
            duplicate_concept_count: 0,
        };
        let health_summary = HealthSummary {
            stale_page_count: 0,
            stale_citation_count: 0,
            uncited_source_count: 0,
            uncompiled_source_count: 0,
            broken_link_count: 0,
            duplicate_concept_count: 0,
        };
        let services = json!({
            "postgres": {"configured": true},
            "falkordb": {"configured": true},
            "qdrant": {"configured": true},
            "embeddings": {"configured": true}
        });
        let degradations = degradation_labels(
            &services,
            &index_counts,
            &freshness,
            &audit_summary,
            &link_summary,
            &health_summary,
            Vec::new(),
        );
        let report = TrustReport {
            command: "trust",
            scope: ScopeIdentity::topic("rust"),
            root: "wiki-root".into(),
            trust_status: trust_status(
                &index_counts,
                &freshness,
                &audit_summary,
                &link_summary,
                &health_summary,
                &degradations,
            ),
            runtime: "memory",
            services,
            index_counts,
            degradations,
            freshness,
            audit_state: audit_summary.state,
            audit_summary,
            link_summary,
            graph_metrics: GraphMetrics {
                wiki_link_count: 3,
                falkordb_configured: true,
            },
            health_summary,
        };

        let value = serde_json::to_value(report).expect("trust JSON");
        assert_eq!(value["command"], "trust");
        assert_eq!(value["trust_status"], "clean");
        assert_eq!(value["index_counts"]["documents"], 1);
        assert_eq!(value["freshness"]["fresh"], true);
        assert_eq!(value["audit_summary"]["unsupported_claim_count"], 0);
        assert_eq!(value["graph_metrics"]["falkordb_configured"], true);
    }
}
