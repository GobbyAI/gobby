use super::*;
use clap::{CommandFactory, Parser};

#[test]
fn parse_symbol_at_path_line_location() {
    let cli =
        Cli::try_parse_from(["gcode", "symbol-at", "src/auth.ts:42"]).expect("symbol-at parses");

    match cli.command {
        Command::SymbolAt { location, line } => {
            assert_eq!(location, "src/auth.ts:42");
            assert_eq!(line, None);
        }
        _ => panic!("expected symbol-at command"),
    }
}

#[test]
fn parse_symbol_at_path_line_column_location() {
    let cli =
        Cli::try_parse_from(["gcode", "symbol-at", "src/auth.ts:42:7"]).expect("symbol-at parses");

    match cli.command {
        Command::SymbolAt { location, line } => {
            assert_eq!(location, "src/auth.ts:42:7");
            assert_eq!(line, None);
        }
        _ => panic!("expected symbol-at command"),
    }
}

#[test]
fn parse_symbol_at_path_and_separate_line() {
    let cli =
        Cli::try_parse_from(["gcode", "symbol-at", "src:auth.ts", "42"]).expect("symbol-at parses");

    match cli.command {
        Command::SymbolAt { location, line } => {
            assert_eq!(location, "src:auth.ts");
            assert_eq!(line, Some(42));
        }
        _ => panic!("expected symbol-at command"),
    }
}

#[test]
fn parse_symbol_at_rejects_zero_separate_line() {
    let error = match Cli::try_parse_from(["gcode", "symbol-at", "src/auth.ts", "0"]) {
        Ok(_) => panic!("line 0 should be rejected"),
        Err(error) => error,
    };

    assert!(error.to_string().contains("must be a positive integer"));
}

#[test]
fn top_level_help_mentions_symbol_at_location_lookup() {
    let help = Cli::command().render_help().to_string();

    assert!(help.contains("gcode symbol-at src/auth.ts:42"));
}
