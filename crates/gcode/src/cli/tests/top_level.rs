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
fn test_parse_no_freshness_global_flag() {
    let cli = Cli::try_parse_from(["gcode", "--no-freshness", "tree"]).expect("tree parses");

    assert!(cli.no_freshness);
    assert!(matches!(cli.command, Command::Tree));
}
