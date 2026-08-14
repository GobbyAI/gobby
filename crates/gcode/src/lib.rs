pub(crate) mod bm25_health;
pub mod codewiki_facts;
mod commands;
mod config;
pub mod contract;
mod db;
mod freshness;
mod git;
#[allow(
    clippy::enum_variant_names,
    reason = "privatization preserves established graph error naming"
)]
mod graph;
pub mod index;
pub(crate) mod index_lock;
mod models;
mod output;
pub(crate) mod postgres_errors;
mod project;
mod projection;
mod savings;
mod schema;
mod search;
mod skill;
#[doc(hidden)]
#[cfg(any(test, feature = "test-support"))]
pub mod test_env;
mod utils;
mod vector;
pub(crate) mod visibility;

extern crate self as gobby_code;

mod cli;
mod cli_error;
mod daemon;
mod dispatch;

#[cfg(test)]
#[path = "../tests/common/mod.rs"]
mod integration_test_support;

pub use graph::code_graph::{GraphLifecycleOutput, GraphLifecycleRequest, GraphReadRequest};
pub use index::api::{
    CodeFactWriteRequest, CodeFactWriteSummary, IndexDegradation, IndexDurations, IndexOutcome,
    IndexRequest,
};
pub use projection::sync::{ProjectionSyncRequest, ProjectionSyncStatus};
pub use vector::code_symbols::{
    CodeSymbolVectorPayload, CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest,
};

/// Run the `gcode` command-line application.
pub fn run_cli() -> std::process::ExitCode {
    reset_sigpipe();
    dispatch::run_with_exit_code()
}

/// Restore the default `SIGPIPE` disposition so a closed stdout terminates
/// quietly instead of panicking inside `println!`.
#[cfg(unix)]
fn reset_sigpipe() {
    // SAFETY: called once at startup before any threads are spawned; resetting a
    // signal to its default disposition is async-signal-safe.
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
}

#[cfg(not(unix))]
fn reset_sigpipe() {}

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

        assert_cli_independent_contract::<crate::CodeFactWriteRequest>();
        assert_cli_independent_contract::<crate::CodeFactWriteSummary>();
        assert_cli_independent_contract::<crate::IndexRequest>();
        assert_cli_independent_contract::<crate::IndexOutcome>();
        assert_cli_independent_contract::<crate::IndexDurations>();
        assert_cli_independent_contract::<crate::IndexDegradation>();
        assert_cli_independent_contract::<crate::GraphLifecycleRequest>();
        assert_cli_independent_contract::<crate::GraphLifecycleOutput>();
        assert_cli_independent_contract::<crate::GraphReadRequest>();
        assert_cli_independent_contract::<crate::CodeSymbolVectorSearchRequest>();
        assert_cli_independent_contract::<crate::CodeSymbolVectorSearchHit>();
        assert_cli_independent_contract::<crate::CodeSymbolVectorPayload>();
        assert_cli_independent_contract::<crate::ProjectionSyncRequest>();
        assert_cli_independent_contract::<crate::ProjectionSyncStatus>();
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
        let config_layers = std::fs::read_to_string(manifest_dir.join("src/config/layers.rs"))
            .expect("read config/layers.rs");
        let runtime_contract =
            std::fs::read_to_string(manifest_dir.join("src/config/runtime_contract.rs"))
                .expect("read config/runtime_contract.rs");
        assert!(config_services.contains("gobby_core::config::{"));
        assert!(config_services.contains("ConfigSource"));
        assert!(!config_services.contains(&format!("{}{}", "Standalone", "Config")));
        assert!(config_layers.contains("impl ServiceConfigSource for ServiceSource"));
        assert!(config_services.contains("struct ErrorCapturingConfigSource"));
        assert!(config_services.contains("impl<S> ConfigSource for ErrorCapturingConfigSource"));
        assert!(!config_services.contains("impl ConfigSource for PostgresConfigSource"));
        assert!(runtime_contract.contains("gobby_core::config::reject_secret_marker"));
        assert!(runtime_contract.contains("gobby_core::config::decode_config_value"));
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

        assert!(
            !manifest_dir.join("src/falkor.rs").exists(),
            "graph callers should use graph::code_graph or gobby_core::falkor directly"
        );
        assert!(
            !manifest_dir.join("src/search/semantic.rs").exists(),
            "semantic search callers should use vector::code_symbols directly"
        );

        let code_symbols_qdrant =
            std::fs::read_to_string(manifest_dir.join("src/vector/code_symbols/qdrant.rs"))
                .expect("read vector/code_symbols/qdrant.rs");
        assert!(code_symbols_qdrant.contains("gobby_core::qdrant::with_qdrant"));
        assert!(code_symbols_qdrant.contains("gobby_core::qdrant::collection_name"));
        assert!(code_symbols_qdrant.contains("gobby_core::qdrant::search"));
    }

    #[test]
    fn indexing_search_primitive_migration() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));

        let walker = [
            std::fs::read_to_string(manifest_dir.join("src/index/walker.rs"))
                .expect("read index/walker.rs"),
            std::fs::read_to_string(manifest_dir.join("src/index/walker/classification.rs"))
                .expect("read index/walker/classification.rs"),
            std::fs::read_to_string(manifest_dir.join("src/index/walker/discovery.rs"))
                .expect("read index/walker/discovery.rs"),
        ]
        .join("\n");
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
