use super::*;

#[test]
fn parse_codewiki_ai_flag() {
    for (raw, expected) in [
        ("auto", AiRouteArg::Auto),
        ("daemon", AiRouteArg::Daemon),
        ("direct", AiRouteArg::Direct),
        ("off", AiRouteArg::Off),
    ] {
        let cli =
            Cli::try_parse_from(["gcode", "codewiki", "--ai", raw]).expect("codewiki --ai parses");
        match cli.command {
            Command::Codewiki { ai, .. } => assert_eq!(ai, Some(expected)),
            _ => panic!("expected codewiki command"),
        }
    }
}

#[test]
fn parse_codewiki_ai_verify_profile_flag() {
    let cli = Cli::try_parse_from(["gcode", "codewiki", "--ai-verify-profile", "feature_mid"])
        .expect("codewiki --ai-verify-profile parses");
    match cli.command {
        Command::Codewiki {
            ai_verify_profile, ..
        } => assert_eq!(ai_verify_profile.as_deref(), Some("feature_mid")),
        _ => panic!("expected codewiki command"),
    }
}

#[test]
fn parse_codewiki_edge_limit_flag() {
    let cli = Cli::try_parse_from(["gcode", "codewiki"]).expect("codewiki parses");
    match cli.command {
        Command::Codewiki { edge_limit, .. } => {
            assert_eq!(edge_limit, DEFAULT_CODEWIKI_GRAPH_EDGE_LIMIT)
        }
        _ => panic!("expected codewiki command"),
    }

    let cli = Cli::try_parse_from(["gcode", "codewiki", "--edge-limit", "42"])
        .expect("codewiki edge limit parses");
    match cli.command {
        Command::Codewiki { edge_limit, .. } => assert_eq!(edge_limit, 42),
        _ => panic!("expected codewiki command"),
    }

    let error = match Cli::try_parse_from(["gcode", "codewiki", "--edge-limit", "0"]) {
        Ok(_) => panic!("zero edge limit must fail"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("positive integer"));

    let too_large = (MAX_POSITIVE_USIZE_ARG + 1).to_string();
    let error = match Cli::try_parse_from(["gcode", "codewiki", "--edge-limit", &too_large]) {
        Ok(_) => panic!("oversized edge limit must fail"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("no more than 1000000000"));
}

#[test]
fn parse_codewiki_repair_citations_flag() {
    let cli = Cli::try_parse_from(["gcode", "codewiki"]).expect("codewiki parses");
    match cli.command {
        Command::Codewiki {
            repair_citations, ..
        } => assert!(!repair_citations, "repair defaults off"),
        _ => panic!("expected codewiki command"),
    }

    let cli = Cli::try_parse_from(["gcode", "codewiki", "--repair-citations"])
        .expect("codewiki --repair-citations parses");
    match cli.command {
        Command::Codewiki {
            repair_citations, ..
        } => assert!(repair_citations),
        _ => panic!("expected codewiki command"),
    }
}

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
