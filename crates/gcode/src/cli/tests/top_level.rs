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
        } => {
            assert_eq!(symbol_name, "handleAuth");
            assert_eq!(limit, 10);
            assert_eq!(offset, 0);
        }
        _ => panic!("expected top-level callers command"),
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
        Command::Imports { file } => assert_eq!(file, "src/auth.ts"),
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
        assert!(matches!(cli.command, Command::Tree));
    }
}

#[test]
fn test_rejects_removed_no_freshness_global_flag() {
    assert!(Cli::try_parse_from(["gcode", "--no-freshness", "tree"]).is_err());
}

#[test]
fn top_level_help_only_advertises_allow_stale() {
    let help = Cli::command().render_help().to_string();

    assert!(help.contains("--allow-stale"));
    assert!(!help.contains("--no-freshness"));
}
