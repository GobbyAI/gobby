use super::*;
use clap::{CommandFactory, Parser};

#[test]
fn parse_projection_lifecycle_commands() {
    let cli = Cli::try_parse_from([
        "gcode",
        "--format",
        "text",
        "graph",
        "sync-file",
        "--file",
        "src/lib.rs",
    ])
    .expect("graph sync-file parses");
    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::Graph {
            command:
                GraphCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => {
            assert_eq!(file, "src/lib.rs");
            assert!(!allow_missing_indexed_file);
        }
        _ => panic!("expected graph sync-file command"),
    }

    let cli = Cli::try_parse_from([
        "gcode",
        "--format",
        "text",
        "vector",
        "sync-file",
        "--file",
        "src/lib.rs",
    ])
    .expect("vector sync-file parses");
    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::Vector {
            command: VectorCommand::SyncFile { file },
        } => assert_eq!(file, "src/lib.rs"),
        _ => panic!("expected vector sync-file command"),
    }

    let cli = Cli::try_parse_from(["gcode", "graph", "clear"]).expect("graph clear parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Clear { project_id: None }
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "clear", "--project-id", "project-1"])
        .expect("graph clear --project-id parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Clear {
                project_id: Some(project_id)
            }
        } if project_id == "project-1"
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "rebuild"]).expect("graph rebuild parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Rebuild
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "report"]).expect("graph report parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Report { top_n: 10 }
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "overview"]).expect("graph overview parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Overview { limit: 100 }
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "overview", "--limit", "25"])
        .expect("graph overview limit parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::Overview { limit: 25 }
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "graph", "file", "--file", "src/main.rs"])
        .expect("graph file parses");
    match cli.command {
        Command::Graph {
            command: GraphCommand::File { file },
        } => assert_eq!(file, "src/main.rs"),
        _ => panic!("expected graph file command"),
    }

    let cli = Cli::try_parse_from([
        "gcode",
        "graph",
        "neighbors",
        "--symbol-id",
        "sym-1",
        "--limit",
        "7",
    ])
    .expect("graph neighbors parses");
    match cli.command {
        Command::Graph {
            command: GraphCommand::Neighbors { symbol_id, limit },
        } => {
            assert_eq!(symbol_id, "sym-1");
            assert_eq!(limit, 7);
        }
        _ => panic!("expected graph neighbors command"),
    }

    let cli = Cli::try_parse_from([
        "gcode",
        "graph",
        "blast-radius",
        "--symbol-id",
        "sym-1",
        "--depth",
        "2",
        "--limit",
        "9",
    ])
    .expect("graph blast-radius symbol parses");
    match cli.command {
        Command::Graph {
            command:
                GraphCommand::BlastRadius {
                    symbol_id,
                    file,
                    depth,
                    limit,
                },
        } => {
            assert_eq!(symbol_id.as_deref(), Some("sym-1"));
            assert_eq!(file, None);
            assert_eq!(depth, 2);
            assert_eq!(limit, 9);
        }
        _ => panic!("expected graph blast-radius command"),
    }

    let cli = Cli::try_parse_from([
        "gcode",
        "graph",
        "blast-radius",
        "--file",
        "src/lib.rs",
        "--depth",
        "2",
        "--limit",
        "9",
    ])
    .expect("graph blast-radius file parses");
    match cli.command {
        Command::Graph {
            command:
                GraphCommand::BlastRadius {
                    symbol_id,
                    file,
                    depth,
                    limit,
                },
        } => {
            assert_eq!(symbol_id, None);
            assert_eq!(file.as_deref(), Some("src/lib.rs"));
            assert_eq!(depth, 2);
            assert_eq!(limit, 9);
        }
        _ => panic!("expected graph blast-radius command"),
    }

    let cli = Cli::try_parse_from(["gcode", "vector", "clear"]).expect("vector clear parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::Clear
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "vector", "rebuild"]).expect("vector rebuild parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::Rebuild
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "index", "--sync-projections"]).expect("index parses");
    match cli.command {
        Command::Index {
            sync_projections, ..
        } => assert!(sync_projections),
        _ => panic!("expected index command"),
    }
}

#[test]
fn parse_graph_report_global_format() {
    let cli = Cli::try_parse_from([
        "gcode", "graph", "report", "--top-n", "5", "--format", "text",
    ])
    .expect("graph report parses");
    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::Graph {
            command: GraphCommand::Report { top_n },
        } => assert_eq!(top_n, 5),
        _ => panic!("expected graph report command"),
    }

    let err = match Cli::try_parse_from(["gcode", "graph", "report", "--limit", "5"]) {
        Ok(_) => panic!("report keeps minimal args"),
        Err(err) => err,
    };
    assert_eq!(err.kind(), clap::error::ErrorKind::UnknownArgument);
}

#[test]
fn test_parse_index_require_cpp_semantics() {
    let cli =
        Cli::try_parse_from(["gcode", "index", "--require-cpp-semantics"]).expect("index parses");

    match cli.command {
        Command::Index {
            require_cpp_semantics,
            sync_projections,
            ..
        } => {
            assert!(require_cpp_semantics);
            assert!(!sync_projections);
        }
        _ => panic!("expected index command"),
    }
}

#[test]
fn test_parse_callers_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "callers", "handleAuth"]).expect("callers parses");

    match cli.command {
        Command::Callers {
            symbol_name,
            limit,
            offset,
        } => {
            assert_eq!(symbol_name, "handleAuth");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
        }
        _ => panic!("expected top-level callers command"),
    }
}

#[test]
fn test_parse_usages_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "usages", "DatabasePool"]).expect("usages parses");

    match cli.command {
        Command::Usages {
            symbol_name,
            limit,
            offset,
        } => {
            assert_eq!(symbol_name, "DatabasePool");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
        }
        _ => panic!("expected top-level usages command"),
    }
}

#[test]
fn test_parse_imports_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "imports", "src/auth.ts"]).expect("imports parses");

    match cli.command {
        Command::Imports { file } => assert_eq!(file, "src/auth.ts"),
        _ => panic!("expected top-level imports command"),
    }
}

#[test]
fn test_parse_blast_radius_remains_top_level() {
    let cli =
        Cli::try_parse_from(["gcode", "blast-radius", "handleAuth"]).expect("blast-radius parses");

    match cli.command {
        Command::BlastRadius { target, depth } => {
            assert_eq!(target, "handleAuth");
            assert_eq!(depth, 3);
        }
        _ => panic!("expected top-level blast-radius command"),
    }
}

#[test]
fn test_parse_search_symbol_filters() {
    let cli = Cli::try_parse_from([
        "gcode",
        "search-symbol",
        "outline",
        "crates/gcode/src",
        "--kind",
        "function",
        "--language",
        "rust",
    ])
    .expect("search-symbol parses");

    match cli.command {
        Command::SearchSymbol {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            with_graph,
        } => {
            assert_eq!(query, "outline");
            assert_eq!(paths, vec!["crates/gcode/src"]);
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
            assert_eq!(kind.as_deref(), Some("function"));
            assert_eq!(language.as_deref(), Some("rust"));
            assert!(!with_graph);
        }
        _ => panic!("expected search-symbol command"),
    }
}

#[test]
fn test_parse_search_symbol_with_graph() {
    let cli = Cli::try_parse_from(["gcode", "search-symbol", "outline", "--with-graph"])
        .expect("search-symbol --with-graph parses");

    match cli.command {
        Command::SearchSymbol { with_graph, .. } => assert!(with_graph),
        _ => panic!("expected search-symbol command"),
    }
}

#[test]
fn test_parse_search_language_filters() {
    let cli = Cli::try_parse_from(["gcode", "search", "outline", "--language", "rust"])
        .expect("search parses");

    match cli.command {
        Command::Search { language, .. } => {
            assert_eq!(language.as_deref(), Some("rust"));
        }
        _ => panic!("expected search command"),
    }
}

#[test]
fn test_parse_search_positional_paths() {
    let cli = Cli::try_parse_from([
        "gcode",
        "search",
        "outline",
        "src/gobby",
        "tests",
        "--limit",
        "20",
    ])
    .expect("search parses");

    match cli.command {
        Command::Search { paths, limit, .. } => {
            assert_eq!(paths, vec!["src/gobby", "tests"]);
            assert_eq!(limit, 20);
        }
        _ => panic!("expected search command"),
    }
}

#[test]
fn test_parse_search_text_positional_path_after_option() {
    let cli = Cli::try_parse_from([
        "gcode",
        "search-text",
        "outline",
        "--limit",
        "5",
        "src/gobby",
    ])
    .expect("search-text parses");

    match cli.command {
        Command::SearchText { paths, limit, .. } => {
            assert_eq!(paths, vec!["src/gobby"]);
            assert_eq!(limit, 5);
        }
        _ => panic!("expected search-text command"),
    }
}

#[test]
fn test_parse_search_content_positional_paths_and_format() {
    let cli = Cli::try_parse_from([
        "gcode",
        "search-content",
        "QUERY",
        "src/gobby",
        "tests",
        "--limit",
        "20",
        "--format",
        "text",
    ])
    .expect("search-content parses");

    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::SearchContent { paths, limit, .. } => {
            assert_eq!(paths, vec!["src/gobby", "tests"]);
            assert_eq!(limit, 20);
        }
        _ => panic!("expected search-content command"),
    }
}

#[test]
fn test_parse_search_content_positional_path_after_option() {
    let cli = Cli::try_parse_from([
        "gcode",
        "search-content",
        "QUERY",
        "--limit",
        "5",
        "src/gobby",
    ])
    .expect("search-content parses");

    match cli.command {
        Command::SearchContent { paths, limit, .. } => {
            assert_eq!(paths, vec!["src/gobby"]);
            assert_eq!(limit, 5);
        }
        _ => panic!("expected search-content command"),
    }
}

#[test]
fn test_parse_search_path_flag_rejected() {
    for command in ["search", "search-symbol", "search-text", "search-content"] {
        let err =
            match Cli::try_parse_from(["gcode", command, "QUERY", "--path", "crates/gcode/src"]) {
                Ok(_) => panic!("--path should be rejected for {command}"),
                Err(err) => err,
            };

        assert_eq!(err.kind(), clap::error::ErrorKind::UnknownArgument);
        assert!(
            err.to_string().contains("--path"),
            "unexpected error for {command}: {err}"
        );
    }
}

#[test]
fn top_level_help_includes_agent_task_examples() {
    let help = Cli::command().render_help().to_string();

    assert!(help.contains("gcode grep \"spawn_ui_server(\" [PATH...] -m 50"));
    assert!(help.contains("gcode search-symbol \"spawn_ui_server\" --kind function"));
    assert!(help.contains("gcode symbol <id>"));
    assert!(help.contains("gcode grep \"config.ui.mode\" -F [PATH...] -m 50"));
}

#[test]
fn search_help_routes_literal_and_ranked_content_queries() {
    let mut command = Cli::command();
    let help = command
        .find_subcommand_mut("search")
        .expect("search command")
        .render_help()
        .to_string();

    assert!(help.contains("hybrid/fuzzy concept search"));
    assert!(help.contains("gcode grep \"pattern\" [PATH...] -m 50"));
    assert!(help.contains("exact literals, call sites, dotted config keys"));
    assert!(help.contains("gcode search-content \"query\" [PATH...]"));
}

#[test]
fn test_parse_no_freshness_global_flag() {
    let cli = Cli::try_parse_from(["gcode", "--no-freshness", "tree"]).expect("tree parses");

    assert!(cli.no_freshness);
    assert!(matches!(cli.command, Command::Tree));
}

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

#[test]
fn parse_grep_basic() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src"]).expect("grep basic parses");
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            ignore_case,
            word,
            ..
        } => {
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src"]);
            assert!(!fixed_strings);
            assert!(!ignore_case);
            assert!(!word);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_ignore_case() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "--ignore-case"])
        .expect("grep ignore-case parses");
    match cli.command {
        Command::Grep { ignore_case, .. } => assert!(ignore_case),
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_word() {
    let cli = Cli::try_parse_from(["gcode", "grep", "-w", "note_path"]).expect("grep -w parses");
    match cli.command {
        Command::Grep { pattern, word, .. } => {
            assert_eq!(pattern, "note_path");
            assert!(word);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_word_long_with_fixed_json() {
    let cli = Cli::try_parse_from([
        "gcode",
        "grep",
        "--word",
        "-F",
        "note_path",
        "src",
        "-m",
        "50",
        "--format",
        "json",
    ])
    .expect("grep --word with fixed-string json parses");
    assert!(matches!(cli.format, Some(output::Format::Json)));
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            word,
            max_count,
            ..
        } => {
            assert_eq!(pattern, "note_path");
            assert_eq!(paths, vec!["src"]);
            assert!(fixed_strings);
            assert!(word);
            assert_eq!(max_count, Some(50));
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_with_flags() {
    let cli = Cli::try_parse_from([
        "gcode",
        "grep",
        "needle",
        "-F",
        "-C",
        "2",
        "-g",
        "*.py",
        "src/gobby",
    ])
    .expect("grep with flags parses");
    match cli.command {
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            context,
            glob,
            ..
        } => {
            assert_eq!(pattern, "needle");
            assert_eq!(paths, vec!["src/gobby"]);
            assert!(fixed_strings);
            assert_eq!(context, Some(2));
            assert_eq!(glob, vec!["*.py"]);
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_max_count() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "-m", "5", "src"])
        .expect("grep with -m parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "--max-count", "5", "src"])
        .expect("grep with --max-count parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src", "-m", "5"])
        .expect("grep with -m after path parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }

    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src", "--max-count", "5"])
        .expect("grep with --max-count after path parses");
    match cli.command {
        Command::Grep {
            paths, max_count, ..
        } => {
            assert_eq!(paths, vec!["src"]);
            assert_eq!(max_count, Some(5));
        }
        _ => panic!("expected grep command"),
    }
}

#[test]
fn parse_grep_rejects_limit() {
    let cli = match Cli::try_parse_from(["gcode", "grep", "needle", "src", "--limit", "5"]) {
        Ok(cli) => cli,
        Err(err) => panic!("--limit should parse for dispatch-time rejection: {err}"),
    };
    let err = reject_unsupported_grep_flags(&cli.command).expect_err("--limit should be rejected");
    assert!(
        err.to_string().contains("gcode grep is indexed search"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("-m/--max-count"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_rejects_empty_pattern() {
    let err = match Cli::try_parse_from(["gcode", "grep", ""]) {
        Ok(_) => panic!("empty pattern should be rejected"),
        Err(err) => err,
    };
    assert!(
        err.to_string()
            .contains("gcode grep pattern cannot be empty"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_unsupported_flag_fails_before_context_resolution() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "--files-with-matches"])
        .expect("unsupported grep flag parses for contract rejection");
    let err = reject_unsupported_grep_flags(&cli.command)
        .expect_err("unsupported grep flag should fail before context resolution");

    assert!(
        err.to_string().contains("gcode grep is indexed search"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("--files-with-matches"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("raw `rg`"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_grep_unsupported_flag_after_path_fails_with_indexed_search_message() {
    let cli = Cli::try_parse_from(["gcode", "grep", "needle", "src", "--files-with-matches"])
        .expect("unsupported grep flag after path parses for contract rejection");
    let err = reject_unsupported_grep_flags(&cli.command)
        .expect_err("unsupported grep flag after path should fail");

    assert!(
        err.to_string().contains("gcode grep is indexed search"),
        "unexpected error: {err}"
    );
}

#[test]
fn parse_graph_sync_file_with_flag() {
    let cli = Cli::try_parse_from([
        "gcode",
        "graph",
        "sync-file",
        "--file",
        "src/lib.rs",
        "--allow-missing-indexed-file",
    ])
    .expect("graph sync-file with flag parses");
    match cli.command {
        Command::Graph {
            command:
                GraphCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => {
            assert_eq!(file, "src/lib.rs");
            assert!(allow_missing_indexed_file);
        }
        _ => panic!("expected graph sync-file command"),
    }
}

#[test]
fn parse_grep_with_global_format() {
    let cli = Cli::try_parse_from(["gcode", "--format", "text", "grep", "needle", "src"])
        .expect("grep with global format parses");
    assert!(matches!(cli.format, Some(output::Format::Text)));
    match cli.command {
        Command::Grep { pattern, .. } => assert_eq!(pattern, "needle"),
        _ => panic!("expected grep command"),
    }
}

#[test]
fn effective_format_defaults_grep_to_text() {
    let cli =
        Cli::try_parse_from(["gcode", "grep", "needle", "src", "-m", "50"]).expect("grep parses");

    assert!(cli.format.is_none());
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Text
    ));
}

#[test]
fn effective_format_honors_explicit_grep_json() {
    let cli = Cli::try_parse_from([
        "gcode", "grep", "needle", "src", "-m", "50", "--format", "json",
    ])
    .expect("grep parses with explicit json format");

    assert!(matches!(cli.format, Some(output::Format::Json)));
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Json
    ));
}

#[test]
fn effective_format_keeps_other_commands_json_by_default() {
    let cli = Cli::try_parse_from(["gcode", "search-content", "needle"]).expect("search parses");

    assert!(cli.format.is_none());
    assert!(matches!(
        effective_format(cli.format, &cli.command),
        output::Format::Json
    ));
}
