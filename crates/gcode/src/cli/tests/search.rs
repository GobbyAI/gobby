use super::*;

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
