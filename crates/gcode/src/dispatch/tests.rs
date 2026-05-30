use super::*;
use crate::cli::{Cli, effective_format};
use clap::Parser;

#[test]
fn setup_runs_before_context_resolve() {
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
