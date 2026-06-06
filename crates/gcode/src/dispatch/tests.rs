use super::*;
use crate::cli::{Cli, effective_format};
use clap::Parser;

fn services_for(args: &[&str]) -> config::ServiceConfigSelection {
    let cli = Cli::try_parse_from(std::iter::once("gcode").chain(args.iter().copied()))
        .expect("command parses");
    service_config_selection(&cli.command)
}

#[test]
fn setup_early_dispatch_uses_parsed_request_without_context() {
    let project = tempfile::tempdir().expect("temp project");
    let cli = Cli::try_parse_from([
        "gcode",
        "--project",
        project.path().to_str().expect("utf8 temp path"),
        "setup",
        "--standalone",
        "--database-url",
        "postgresql://localhost/gcode",
        "--overwrite-code-index",
        "--embedding-api-base",
        "https://embeddings.example/v1",
    ])
    .expect("setup parses");

    let mut called = false;
    let dispatched = dispatch_early_command(
        &cli,
        effective_format(cli.format, &cli.command),
        |request, _format, _quiet| {
            called = true;
            assert!(request.standalone);
            assert_eq!(
                request.database_url.as_deref(),
                Some("postgresql://localhost/gcode")
            );
            assert_eq!(request.schema, "public");
            assert!(request.overwrite_code_index);
            assert_eq!(
                request.embedding_api_base.as_deref(),
                Some("https://embeddings.example/v1")
            );
            Ok(())
        },
    )
    .expect("early dispatch succeeds without resolving project context");

    assert!(dispatched);
    assert!(called);
}

#[test]
fn lookup_commands_skip_service_config_resolution() {
    for args in [
        &["grep", "-F", "needle"][..],
        &["tree"][..],
        &["symbol-at", "src/lib.rs:10"][..],
        &["search-content", "needle"][..],
        &["search-text", "needle"][..],
        &["search-symbol", "needle"][..],
    ] {
        assert_eq!(
            services_for(args),
            config::ServiceConfigSelection::database_only()
        );
    }
}

#[test]
fn graph_and_ai_commands_request_only_needed_services() {
    assert_eq!(
        services_for(&["search", "concept"]),
        config::ServiceConfigSelection::hybrid_search()
    );
    assert_eq!(
        services_for(&["search-symbol", "needle", "--with-graph"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["callers", "needle"]),
        config::ServiceConfigSelection::falkordb_only()
    );
    assert_eq!(
        services_for(&["embeddings", "doctor"]),
        config::ServiceConfigSelection::vectors()
    );
}
