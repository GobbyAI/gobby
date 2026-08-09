use super::*;

#[test]
fn parse_setup_standalone() {
    let cli = Cli::try_parse_from([
        "gcode",
        "setup",
        "--standalone",
        "--database-url",
        "postgresql://localhost/gcode",
        "--no-services",
        "--overwrite-code-index",
        "--embedding-provider",
        "ollama",
        "--embedding-query-prefix",
        "query: ",
        "--embedding-vector-dim",
        "768",
        "--embedding-api-key",
        "local-key",
        "--falkordb-password",
        "secret-pass",
    ])
    .expect("setup parses");

    match cli.command {
        Command::Setup {
            standalone,
            database_url,
            no_services,
            overwrite_code_index,
            schema,
            embedding_provider,
            embedding_query_prefix,
            embedding_vector_dim,
            embedding_api_key,
            falkordb_password,
            ..
        } => {
            assert!(standalone);
            assert_eq!(
                database_url.as_deref(),
                Some("postgresql://localhost/gcode")
            );
            assert!(no_services);
            assert!(overwrite_code_index);
            assert_eq!(schema, "public");
            assert_eq!(embedding_provider.as_deref(), Some("ollama"));
            assert_eq!(embedding_query_prefix.as_deref(), Some("query: "));
            assert_eq!(embedding_vector_dim, Some(768));
            assert_eq!(embedding_api_key.as_deref(), Some("local-key"));
            assert_eq!(falkordb_password.as_deref(), Some("secret-pass"));
        }
        _ => panic!("expected setup command"),
    }
}
