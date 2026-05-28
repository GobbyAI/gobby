pub mod commands;
pub mod config;
pub mod db;
pub mod falkor;
pub mod freshness;
pub mod git;
pub mod graph;
pub mod index;
pub mod models;
pub mod output;
pub mod progress;
pub mod project;
pub mod projection;
pub mod savings;
pub mod schema;
pub mod search;
pub mod secrets;
pub mod setup;
pub mod skill;
pub mod utils;
pub mod vector;

#[cfg(test)]
mod tests {
    use serde::Serialize;
    use serde::de::DeserializeOwned;

    fn assert_cli_independent_contract<T>()
    where
        T: Serialize + DeserializeOwned,
    {
        let type_name = std::any::type_name::<T>();
        assert!(!type_name.contains("commands::"), "{type_name}");
        assert!(!type_name.contains("output::"), "{type_name}");
        assert!(!type_name.contains("clap"), "{type_name}");
    }

    #[test]
    fn public_projection_api_is_cli_independent() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for rel_path in [
            "src/index/api.rs",
            "src/graph/typed_query.rs",
            "src/graph/code_graph.rs",
            "src/vector/code_symbols.rs",
            "src/projection/sync.rs",
        ] {
            assert!(
                manifest_dir.join(rel_path).exists(),
                "missing projection boundary module {rel_path}"
            );
        }

        assert_cli_independent_contract::<crate::index::api::CodeFactWriteRequest>();
        assert_cli_independent_contract::<crate::index::api::CodeFactWriteSummary>();
        assert_cli_independent_contract::<crate::graph::code_graph::GraphLifecycleRequest>();
        assert_cli_independent_contract::<crate::graph::code_graph::GraphLifecycleOutput>();
        assert_cli_independent_contract::<crate::graph::code_graph::GraphReadRequest>();
        assert_cli_independent_contract::<crate::vector::code_symbols::CodeSymbolVectorSearchRequest>(
        );
        assert_cli_independent_contract::<crate::vector::code_symbols::CodeSymbolVectorSearchHit>();
        assert_cli_independent_contract::<crate::projection::sync::ProjectionSyncRequest>();
        assert_cli_independent_contract::<crate::projection::sync::ProjectionSyncStatus>();
    }

    #[test]
    fn falkor_facade_is_available() {
        let _ = std::any::type_name::<crate::falkor::FalkorClient>();

        let ctx = crate::config::Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: std::path::PathBuf::from("/nonexistent"),
            project_id: "project-1".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            daemon_url: None,
        };

        let value = crate::falkor::with_falkor(&ctx, 7usize, |_| Ok(9usize))
            .expect("missing FalkorDB config degrades to default");
        assert_eq!(value, 7);
    }
}
