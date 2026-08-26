use super::*;

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
fn test_parse_repair() {
    let cli = Cli::try_parse_from(["gcode", "repair", "--format", "json"]).expect("repair parses");
    assert!(matches!(cli.command, Command::Repair));
    assert!(matches!(cli.format, Some(output::Format::Json)));
}

#[test]
fn test_parse_invalidate_project_id() {
    let cli = Cli::try_parse_from([
        "gcode",
        "invalidate",
        "--project-id",
        "019bfef8-89bb-7bd1-a5c3-80baabdff01b",
        "--force",
    ])
    .expect("invalidate --project-id parses");

    match cli.command {
        Command::Invalidate { project_id, force } => {
            assert_eq!(
                project_id.as_deref(),
                Some("019bfef8-89bb-7bd1-a5c3-80baabdff01b")
            );
            assert!(force);
        }
        _ => panic!("expected invalidate command"),
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
            token_budget: _,
        } => {
            assert_eq!(symbol_name, "handleAuth");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
        }
        _ => panic!("expected top-level callers command"),
    }
}

#[test]
fn test_parse_callees_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "callees", "handleAuth"]).expect("callees parses");

    match cli.command {
        Command::Callees {
            symbol_name,
            limit,
            offset,
            token_budget: _,
        } => {
            assert_eq!(symbol_name, "handleAuth");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
        }
        _ => panic!("expected top-level callees command"),
    }

    let paged = Cli::try_parse_from([
        "gcode",
        "callees",
        "handleAuth",
        "--limit",
        "3",
        "--offset",
        "2",
    ])
    .expect("callees pagination flags parse");
    match paged.command {
        Command::Callees {
            symbol_name,
            limit,
            offset,
            token_budget: _,
        } => {
            assert_eq!(symbol_name, "handleAuth");
            assert_eq!(limit, 3);
            assert_eq!(offset, 2);
        }
        _ => panic!("expected top-level callees command"),
    }

    let token_budget =
        Cli::try_parse_from(["gcode", "callees", "handleAuth", "--token-budget", "80"])
            .expect("callees token paging parses");
    match token_budget.command {
        Command::Callees { token_budget, .. } => assert_eq!(token_budget, Some(80)),
        _ => panic!("expected top-level callees command"),
    }
}

#[test]
fn outline_has_no_summarize_surface() {
    let error = match Cli::try_parse_from(["gcode", "outline", "--summarize", "src/lib.rs"]) {
        Ok(_) => panic!("outline --summarize must be gone"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), clap::error::ErrorKind::UnknownArgument);

    let parsed = Cli::try_parse_from(["gcode", "outline", "src/lib.rs"]).expect("outline parses");
    match parsed.command {
        Command::Outline { file, .. } => assert_eq!(file, "src/lib.rs"),
        _ => panic!("expected outline"),
    }

    let outline = gobby_code::contract::contract()
        .commands
        .into_iter()
        .find(|command| command.name == "outline")
        .expect("outline contract");
    assert!(
        outline.flags.iter().all(|flag| flag.name != "--summarize"),
        "in-memory outline contract still lists --summarize"
    );
}

#[test]
fn test_parse_usages_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "usages", "DatabasePool"]).expect("usages parses");

    match cli.command {
        Command::Usages {
            symbol_name,
            limit,
            offset,
            token_budget,
        } => {
            assert_eq!(symbol_name, "DatabasePool");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
            assert_eq!(token_budget, None);
        }
        _ => panic!("expected top-level usages command"),
    }
}

#[test]
fn test_parse_usages_token_budget() {
    let cli = Cli::try_parse_from(["gcode", "usages", "DatabasePool", "--token-budget", "80"])
        .expect("usages --token-budget parses");

    match cli.command {
        Command::Usages { token_budget, .. } => assert_eq!(token_budget, Some(80)),
        _ => panic!("expected top-level usages command"),
    }
}

#[test]
fn test_parse_imports_remains_top_level() {
    let cli = Cli::try_parse_from(["gcode", "imports", "src/auth.ts"]).expect("imports parses");

    match cli.command {
        Command::Imports { file, .. } => assert_eq!(file, "src/auth.ts"),
        _ => panic!("expected top-level imports command"),
    }
}

#[test]
fn test_parse_path_remains_top_level() {
    let cli =
        Cli::try_parse_from(["gcode", "path", "handleAuth", "DatabasePool"]).expect("path parses");

    match cli.command {
        Command::Path {
            symbol_a,
            symbol_b,
            max_depth,
        } => {
            assert_eq!(symbol_a, "handleAuth");
            assert_eq!(symbol_b, "DatabasePool");
            assert_eq!(max_depth, DEFAULT_SYMBOL_PATH_MAX_DEPTH);
        }
        _ => panic!("expected top-level path command"),
    }
}

#[test]
fn test_parse_blast_radius_remains_top_level() {
    let cli =
        Cli::try_parse_from(["gcode", "blast-radius", "handleAuth"]).expect("blast-radius parses");

    match cli.command {
        Command::BlastRadius {
            target,
            depth,
            token_budget,
            limit: _,
            offset: _,
        } => {
            assert_eq!(target, "handleAuth");
            assert_eq!(depth, 3);
            assert_eq!(token_budget, None);
        }
        _ => panic!("expected top-level blast-radius command"),
    }
}

#[test]
fn test_parse_blast_radius_token_budget() {
    let cli = Cli::try_parse_from([
        "gcode",
        "blast-radius",
        "handleAuth",
        "--token-budget",
        "100",
    ])
    .expect("blast-radius --token-budget parses");

    match cli.command {
        Command::BlastRadius { token_budget, .. } => assert_eq!(token_budget, Some(100)),
        _ => panic!("expected top-level blast-radius command"),
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
fn test_parse_allow_stale_global_flag() {
    for argv in [
        ["gcode", "--allow-stale", "tree"],
        ["gcode", "tree", "--allow-stale"],
    ] {
        let cli = Cli::try_parse_from(argv).expect("tree parses");

        assert!(cli.allow_stale);
        assert!(matches!(cli.command, Command::Tree { .. }));
    }
}

fn parse_failure(args: &[&str]) -> clap::Error {
    match Cli::try_parse_from(args) {
        Err(error) => error,
        Ok(_) => panic!("expected clap parse failure for {args:?}"),
    }
}

#[test]
fn test_rejects_removed_no_freshness_global_flag() {
    let error = parse_failure(&["gcode", "--no-freshness", "tree"]);
    assert_eq!(error.kind(), clap::error::ErrorKind::UnknownArgument);
}

#[test]
fn help_request_keeps_full_clap_output() {
    let error = parse_failure(&["gcode", "--help"]);
    assert_eq!(error.kind(), clap::error::ErrorKind::DisplayHelp);
    let rendered = error.to_string();
    assert!(rendered.contains("Usage:"));
    assert!(rendered.contains("Commands:"));
}

#[test]
fn version_request_keeps_full_clap_output() {
    let error = parse_failure(&["gcode", "--version"]);
    assert_eq!(error.kind(), clap::error::ErrorKind::DisplayVersion);
    assert!(error.to_string().contains("gcode"));
}

#[test]
fn top_level_help_only_advertises_allow_stale() {
    let help = Cli::command().render_help().to_string();

    assert!(help.contains("--allow-stale"));
    assert!(!help.contains("--no-freshness"));
}

#[test]
fn navigation_commands_default_to_compact_text() {
    let commands = [
        vec!["gcode", "search", "query"],
        vec!["gcode", "search-symbol", "name"],
        vec!["gcode", "search-text", "query"],
        vec!["gcode", "search-content", "query"],
        vec!["gcode", "grep", "pattern"],
        vec!["gcode", "outline", "src/lib.rs"],
        vec!["gcode", "symbol", "00000000-0000-0000-0000-000000000000"],
        vec!["gcode", "symbol-at", "src/lib.rs:1"],
        vec!["gcode", "symbols", "00000000-0000-0000-0000-000000000000"],
        vec!["gcode", "kinds"],
        vec!["gcode", "tree"],
        vec!["gcode", "callers", "name"],
        vec!["gcode", "callees", "name"],
        vec!["gcode", "usages", "name"],
        vec!["gcode", "imports", "src/lib.rs"],
        vec!["gcode", "path", "from", "to"],
        vec!["gcode", "blast-radius", "name"],
        vec!["gcode", "repo-outline"],
    ];

    for argv in commands {
        let cli = Cli::try_parse_from(argv.clone()).expect("navigation command parses");
        assert!(
            matches!(
                effective_format(cli.format, &cli.command),
                output::Format::Text
            ),
            "{argv:?}"
        );
    }
}

#[test]
fn structural_commands_keep_json_defaults() {
    for argv in [
        vec!["gcode", "status"],
        vec!["gcode", "graph", "overview"],
        vec!["gcode", "graph", "rebuild"],
        vec!["gcode", "vector", "cleanup-orphans"],
    ] {
        let cli = Cli::try_parse_from(argv.clone()).expect("structural command parses");
        assert!(
            matches!(
                effective_format(cli.format, &cli.command),
                output::Format::Json
            ),
            "{argv:?}"
        );
    }
}

#[test]
fn explicit_format_and_automatic_budget_are_classified_centrally() {
    let text = Cli::try_parse_from(["gcode", "tree"]).expect("tree parses");
    assert_eq!(
        effective_token_budget(effective_format(text.format, &text.command), &text.command),
        Some(crate::commands::token_budget::AUTOMATIC_TEXT_TOKEN_BUDGET)
    );

    let json =
        Cli::try_parse_from(["gcode", "tree", "--format", "json"]).expect("JSON tree parses");
    assert!(matches!(
        effective_format(json.format, &json.command),
        output::Format::Json
    ));
    assert_eq!(
        effective_token_budget(output::Format::Json, &json.command),
        None
    );

    let explicit = Cli::try_parse_from(["gcode", "tree", "--token-budget", "137"])
        .expect("explicit tree budget parses");
    assert_eq!(
        effective_token_budget(output::Format::Text, &explicit.command),
        Some(137)
    );

    for argv in [
        vec!["gcode", "symbol", "00000000-0000-0000-0000-000000000000"],
        vec!["gcode", "symbol-at", "src/lib.rs:1"],
        vec!["gcode", "path", "from", "to"],
    ] {
        let cli = Cli::try_parse_from(argv).expect("complete command parses");
        assert_eq!(
            effective_token_budget(output::Format::Text, &cli.command),
            None
        );
    }
}
