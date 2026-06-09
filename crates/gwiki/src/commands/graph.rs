use gobby_core::ai_context::{AiConfigSource, AiContext};
use gobby_core::config::{AiRouting, ConfigSource, resolve_embedding_config};
use gobby_core::gobby_home;
use serde_json::json;

use crate::graph::GraphExportOptions;
use crate::support::config::qdrant_config_has_url;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::{CommandOutcome, ScopeSelection, WikiError, exports};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let database_url = database_url_for("gwiki graph")?.ok_or_else(|| WikiError::Config {
        detail: "gwiki graph requires PostgreSQL index configuration".to_string(),
    })?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki graph: {error}"),
        }
    })?;

    let degraded_sources = degraded_optional_sources(&mut conn)?;
    let facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let options = if degraded_sources.is_empty() {
        GraphExportOptions::available()
    } else {
        GraphExportOptions::degraded(degraded_sources)
    };
    let artifacts = exports::export_graph_artifacts(resolved.scope.root(), &facts, options)?;
    let paths = artifacts
        .iter()
        .map(|artifact| artifact.path.display().to_string())
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "graph",
        "scope": resolved.output_scope,
        "artifacts": artifacts,
    });
    let text = format!(
        "Exported wiki graph artifacts\nScope: {}\nArtifacts: {}",
        resolved.output_scope,
        paths.join(", ")
    );
    Ok(super::scoped_outcome(
        "graph",
        &resolved.output_scope,
        payload,
        text,
    ))
}

fn degraded_optional_sources(conn: &mut postgres::Client) -> Result<Vec<String>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki graph: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional graph export config: {error}"),
            }
        })?;

    Ok(degraded_optional_sources_from_config(&mut source))
}

fn degraded_optional_sources_from_config(source: &mut impl ConfigSource) -> Vec<String> {
    let mut degraded = Vec::new();
    if gobby_core::config::resolve_falkordb_config(source).is_none() {
        degraded.push("falkordb_unavailable".to_string());
    }

    let ai_context = AiContext::resolve(None, source);
    let has_embedding = has_embedding_capability(
        ai_context
            .binding(gobby_core::config::AiCapability::Embed)
            .routing,
        source,
    );
    let has_qdrant = gobby_core::config::resolve_qdrant_config(source)
        .filter(qdrant_config_has_url)
        .is_some();
    if !has_embedding || !has_qdrant {
        degraded.push("semantic_relations_unavailable".to_string());
    }

    degraded
}

/// Reports whether graph commands can attempt embedding-backed features.
fn has_embedding_capability(routing: AiRouting, source: &mut impl ConfigSource) -> bool {
    match routing {
        AiRouting::Off => false,
        AiRouting::Daemon => {
            #[cfg(feature = "ai")]
            {
                true
            }
            #[cfg(not(feature = "ai"))]
            {
                false
            }
        }
        AiRouting::Direct => resolve_embedding_config(source).is_some(),
        AiRouting::Auto => {
            #[cfg(feature = "ai")]
            {
                true
            }
            #[cfg(not(feature = "ai"))]
            {
                resolve_embedding_config(source).is_some()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use crate::support::test_env::EnvGuard;

    use super::*;

    #[derive(Default)]
    struct TestConfigSource {
        values: BTreeMap<&'static str, &'static str>,
    }

    impl TestConfigSource {
        fn with(mut self, key: &'static str, value: &'static str) -> Self {
            self.values.insert(key, value);
            self
        }
    }

    impl ConfigSource for TestConfigSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).map(|value| (*value).to_string())
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            Ok(value.to_string())
        }
    }

    fn degraded_markers(mut source: TestConfigSource) -> Vec<String> {
        let _env = EnvGuard::unset("GOBBY_FALKORDB_HOST")
            .and_unset("GOBBY_FALKORDB_PORT")
            .and_unset("GOBBY_FALKORDB_PASSWORD")
            .and_unset("GOBBY_QDRANT_URL")
            .and_unset("GOBBY_QDRANT_API_KEY")
            .and_set("GWIKI_TEST_ENV_GUARD_AND_SET", "1");
        degraded_optional_sources_from_config(&mut source)
    }

    fn falkor_config() -> TestConfigSource {
        TestConfigSource::default().with("databases.falkordb.host", "127.0.0.1")
    }

    fn with_embedding_and_qdrant(source: TestConfigSource) -> TestConfigSource {
        source
            .with("ai.embeddings.api_base", "http://embeddings.local/v1")
            .with("databases.qdrant.url", "http://qdrant.local")
    }

    #[test]
    #[serial_test::serial]
    fn degraded_optional_sources_reports_all_missing_optional_services() {
        assert_eq!(
            degraded_markers(TestConfigSource::default()),
            vec![
                "falkordb_unavailable".to_string(),
                "semantic_relations_unavailable".to_string()
            ]
        );
    }

    #[test]
    #[serial_test::serial]
    fn degraded_optional_sources_accepts_present_falkor_embedding_and_qdrant() {
        assert!(degraded_markers(with_embedding_and_qdrant(falkor_config())).is_empty());
    }

    #[test]
    #[serial_test::serial]
    fn degraded_optional_sources_reports_missing_falkor_only() {
        assert_eq!(
            degraded_markers(with_embedding_and_qdrant(TestConfigSource::default())),
            vec!["falkordb_unavailable".to_string()]
        );
    }

    #[test]
    #[serial_test::serial]
    fn degraded_optional_sources_reports_missing_semantic_relations() {
        assert_eq!(
            degraded_markers(falkor_config()),
            vec!["semantic_relations_unavailable".to_string()]
        );
    }

    #[test]
    #[serial_test::serial]
    fn degraded_optional_sources_treats_blank_qdrant_url_as_missing() {
        let source = falkor_config()
            .with("ai.embeddings.api_base", "http://embeddings.local/v1")
            .with("databases.qdrant.url", " ");

        assert_eq!(
            degraded_markers(source),
            vec!["semantic_relations_unavailable".to_string()]
        );
    }
}
