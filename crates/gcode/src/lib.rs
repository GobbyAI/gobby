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
        assert_cli_independent_contract::<crate::vector::code_symbols::CodeSymbolVectorPayload>();
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

    #[test]
    fn foundation_consumer_migration() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let cargo = std::fs::read_to_string(manifest_dir.join("Cargo.toml"))
            .expect("read gobby-code Cargo.toml");
        for feature in ["postgres", "falkor", "qdrant", "search", "indexing"] {
            assert!(
                cargo.contains(feature),
                "gobby-code must enable gobby-core feature `{feature}`"
            );
        }

        let config =
            std::fs::read_to_string(manifest_dir.join("src/config.rs")).expect("read config.rs");
        assert!(config.contains("gobby_core::config::resolve_falkordb_config"));
        assert!(config.contains("gobby_core::config::resolve_qdrant_config"));
        assert!(config.contains("gobby_core::config::resolve_embedding_config"));
        assert!(config.contains("impl gobby_core::config::ConfigSource for PostgresConfigSource"));
        assert!(config.contains("gobby_core::postgres::read_config_value"));
        assert!(!config.contains("fn decode_config_value("));

        let db = std::fs::read_to_string(manifest_dir.join("src/db.rs")).expect("read db.rs");
        assert!(db.contains("gobby_core::postgres::connect_readonly"));
        assert!(db.contains("gobby_core::postgres::connect_readwrite"));
        assert!(!db.contains("Client::connect(database_url, NoTls)"));

        let graph = std::fs::read_to_string(manifest_dir.join("src/graph/code_graph.rs"))
            .expect("read graph/code_graph.rs");
        assert!(graph.contains("gobby_core::falkor::with_graph"));
        assert!(!graph.contains("falkor::with_falkor"));

        let semantic = std::fs::read_to_string(manifest_dir.join("src/search/semantic.rs"))
            .expect("read search/semantic.rs");
        assert!(semantic.contains("gobby_core::qdrant::with_qdrant"));
        assert!(semantic.contains("gobby_core::qdrant::collection_name"));
        assert!(semantic.contains("gobby_core::qdrant::search"));
    }

    #[test]
    fn falkor_facade_exception_scoped_to_falkor_rs() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let src_dir = manifest_dir.join("src");
        let mut offenders = Vec::new();

        fn visit(path: &std::path::Path, offenders: &mut Vec<std::path::PathBuf>) {
            for entry in std::fs::read_dir(path).expect("read source directory") {
                let entry = entry.expect("source entry");
                let path = entry.path();
                if path.is_dir() {
                    visit(&path, offenders);
                    continue;
                }
                if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                    continue;
                }
                let source = std::fs::read_to_string(&path).expect("read source file");
                let builder = ["Falkor", "Client", "Builder"].concat();
                if source.contains(&builder) && !path.ends_with("falkor.rs") {
                    offenders.push(path);
                }
            }
        }

        visit(&src_dir, &mut offenders);
        assert!(
            offenders.is_empty(),
            "Falkor client builder must remain scoped to falkor.rs: {offenders:?}"
        );
    }
}
