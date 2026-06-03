pub mod commands;
pub mod config;
pub mod contract;
pub mod db;
pub mod falkor;
pub mod freshness;
pub mod git;
pub mod graph;
pub mod index;
pub(crate) mod index_lock;
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
pub(crate) mod visibility;

pub use index::api::{
    IndexDegradation, IndexDurations, IndexOutcome, IndexRequest, UnsupportedFileType, index_files,
};

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
        assert_cli_independent_contract::<crate::index::api::IndexRequest>();
        assert_cli_independent_contract::<crate::index::api::IndexOutcome>();
        assert_cli_independent_contract::<crate::index::api::IndexDurations>();
        assert_cli_independent_contract::<crate::index::api::IndexDegradation>();
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
            code_vectors: crate::config::CodeVectorSettings::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
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

        let config_root =
            std::fs::read_to_string(manifest_dir.join("src/config.rs")).expect("read config.rs");
        let config_services = std::fs::read_to_string(manifest_dir.join("src/config/services.rs"))
            .expect("read config/services.rs");
        assert!(config_services.contains("gobby_core::config::{"));
        assert!(config_services.contains("ConfigSource"));
        assert!(config_services.contains("StandaloneConfig"));
        assert!(config_services.contains("impl ConfigSource for PostgresConfigSource"));
        assert!(config_services.contains("gobby_core::postgres::read_config_value"));
        assert!(config_services.contains("gobby_core::config::decode_config_value"));
        assert!(!config_root.contains("fn decode_config_value("));

        let db = [
            std::fs::read_to_string(manifest_dir.join("src/db/mod.rs")).expect("read db/mod.rs"),
            std::fs::read_to_string(manifest_dir.join("src/db/resolution.rs"))
                .expect("read db/resolution.rs"),
        ]
        .join("\n");
        assert!(db.contains("gobby_core::gobby_home"));
        assert!(db.contains("gobby_core::postgres::connect_readonly"));
        assert!(db.contains("gobby_core::postgres::connect_readwrite"));
        assert!(!db.contains("Client::connect(database_url, NoTls)"));

        let graph = [
            std::fs::read_to_string(manifest_dir.join("src/graph/code_graph.rs"))
                .expect("read graph/code_graph.rs"),
            std::fs::read_to_string(manifest_dir.join("src/graph/code_graph/connection.rs"))
                .expect("read graph/code_graph/connection.rs"),
            std::fs::read_to_string(manifest_dir.join("src/graph/code_graph/write.rs"))
                .expect("read graph/code_graph/write.rs"),
        ]
        .join("\n");
        assert!(graph.contains("gobby_core::falkor::with_graph"));
        assert!(!graph.contains("falkor::with_falkor"));

        let falkor =
            std::fs::read_to_string(manifest_dir.join("src/falkor.rs")).expect("read falkor.rs");
        assert!(falkor.contains("gobby_core::falkor::GraphClient"));
        for token in [
            ["Falkor", "Client", "Builder"].concat(),
            ["Falkor", "Connection", "Info"].concat(),
            ["Sync", "Graph"].concat(),
            ["Falkor", "Value"].concat(),
            ["Lazy", "Result", "Set"].concat(),
            ["Query", "Result"].concat(),
        ] {
            assert!(!falkor.contains(&token), "{token} leaked into falkor.rs");
        }

        let semantic = std::fs::read_to_string(manifest_dir.join("src/search/semantic.rs"))
            .expect("read search/semantic.rs");
        assert!(semantic.contains("gobby_core::qdrant::with_qdrant"));
        assert!(semantic.contains("gobby_core::qdrant::collection_name"));
        assert!(semantic.contains("gobby_core::qdrant::search"));
    }

    #[test]
    fn indexing_search_primitive_migration() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));

        let walker = std::fs::read_to_string(manifest_dir.join("src/index/walker.rs"))
            .expect("read index/walker.rs");
        assert!(walker.contains("gobby_core::indexing::WalkerSettings"));
        let local_walker_builder = ["WalkBuilder", "::new(root)"].concat();
        assert!(!walker.contains(&local_walker_builder));

        let hasher = std::fs::read_to_string(manifest_dir.join("src/index/hasher.rs"))
            .expect("read index/hasher.rs");
        assert!(hasher.contains("gobby_core::indexing::file_content_hash"));
        let local_buffer = format!("let mut buf = [0u8; {}]", 64 * 1024);
        assert!(!hasher.contains(&local_buffer));

        let rrf =
            std::fs::read_to_string(manifest_dir.join("src/search/rrf.rs")).expect("read rrf.rs");
        assert!(rrf.contains("gobby_core::search::rrf_merge"));
        let local_rrf_const = ["const ", "RRF_K"].concat();
        assert!(!rrf.contains(&local_rrf_const));

        let chunker = std::fs::read_to_string(manifest_dir.join("src/index/chunker.rs"))
            .expect("read index/chunker.rs");
        assert!(!chunker.contains("use gobby_core::indexing::Chunk"));
        assert!(!chunker.contains("use gobby_core::indexing::ChunkIdentity"));
        assert!(!chunker.contains("use gobby_core::indexing::IndexEvent"));
        assert!(!chunker.contains("use gobby_core::indexing::index_events_from_hashes"));
    }

    #[test]
    fn falkor_facade_uses_core_graph_client_only() {
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
                if source.contains(&builder) {
                    offenders.push(path);
                }
            }
        }

        visit(&src_dir, &mut offenders);
        assert!(
            offenders.is_empty(),
            "gobby-code must use gobby_core::falkor::GraphClient instead of direct FalkorDB builders: {offenders:?}"
        );
    }
}
