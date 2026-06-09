use gobby_core::ai_context::{AiConfigSource, AiContext};

use crate::benchmark;
use crate::commands::run_analysis_command;
use crate::support::env::database_url_for;
use crate::support::search as search_support;
use crate::{CommandOutcome, ScopeIdentity, ScopeKind, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let Some(database_url) = database_url_for("gwiki benchmark")? else {
        return Err(WikiError::Config {
            detail: "gwiki benchmark requires PostgreSQL and a seeded indexed project".to_string(),
        });
    };
    run_analysis_command(
        "benchmark",
        selection,
        "serialize gwiki benchmark report",
        |_root, output_scope| {
            let mut conn =
                gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
                    WikiError::Config {
                        detail: format!(
                            "failed to connect to PostgreSQL for gwiki benchmark: {error}"
                        ),
                    }
                })?;
            let search_scope = search_scope_for_identity(&output_scope);
            run_attached(&mut conn, output_scope, search_scope)
        },
        benchmark_text,
    )
}

fn run_attached(
    conn: &mut postgres::Client,
    output_scope: ScopeIdentity,
    search_scope: crate::search::SearchScope,
) -> Result<benchmark::BenchmarkReport, WikiError> {
    let optional = {
        let gobby_home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
            detail: format!("failed to resolve Gobby home for gwiki benchmark: {error}"),
        })?;
        let mut source = AiConfigSource::with_primary_from_gobby_home(
            search_support::PostgresConfigSource { conn },
            &gobby_home,
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to resolve config for gwiki benchmark: {error}"),
        })?;
        let ai_context = AiContext::resolve(None, &mut source);
        benchmark::resolve_optional_sources(&ai_context, &mut source)
    };
    benchmark::report_from_postgres(conn, output_scope, search_scope, optional)
}

fn search_scope_for_identity(scope: &ScopeIdentity) -> crate::search::SearchScope {
    match scope.kind {
        ScopeKind::Global => crate::search::SearchScope::global(),
        ScopeKind::Topic => crate::search::SearchScope::topic(scope.id.clone()),
        ScopeKind::Project => crate::search::SearchScope::project(scope.id.clone()),
    }
}

fn benchmark_text(report: &benchmark::BenchmarkReport) -> String {
    let mut lines = Vec::new();
    lines.push(format!("benchmark scope {}", report.scope));
    if let Some(ratio) = report.token_compression.ratio {
        lines.push(format!(
            "token compression: {ratio:.3} ({} chunk tokens / {} document tokens)",
            report.token_compression.chunk_tokens, report.token_compression.document_tokens
        ));
    } else {
        lines.push("token compression: unavailable".to_string());
    }
    lines.push(format!(
        "graph coverage: {}",
        if report.graph_coverage.available {
            "available"
        } else {
            "unavailable"
        }
    ));
    lines.push(format!(
        "retrieval precision: {}",
        if report.retrieval_precision.available {
            "available"
        } else {
            "unavailable"
        }
    ));
    lines.push(format!(
        "source mix: {} documents, {} sources",
        report.source_mix.total_documents, report.source_mix.total_sources
    ));
    if !report.degraded_sources.is_empty() {
        lines.push(format!(
            "degraded sources: {}",
            report.degraded_sources.join(", ")
        ));
    }
    lines.join("\n")
}
