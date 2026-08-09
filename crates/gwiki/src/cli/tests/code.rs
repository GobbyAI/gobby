use std::path::PathBuf;

use gobby_wiki::commands::code::{AiDepth, DEFAULT_CODE_GRAPH_EDGE_LIMIT, ProseDepth, VerifyScope};

use super::*;

fn map_code(args: &[&str]) -> gobby_wiki::commands::code::CodeCommandOptions {
    let argv = std::iter::once("gwiki")
        .chain(std::iter::once("code"))
        .chain(args.iter().copied())
        .collect::<Vec<_>>();
    let cli = Cli::try_parse_from(argv).expect("code command parses");
    let command = mapping::command_from_cli(cli.command, cli.scope.into())
        .expect("code command maps to the public API");
    let Command::Code(options) = command else {
        panic!("expected public code command");
    };
    options
}

#[test]
fn code_cli_maps_to_public_command_with_freshness_control() {
    let cli = Cli::try_parse_from(["gwiki", "code", "--project", "/repo", "--no-freshness"])
        .expect("code command parses");

    let command = mapping::command_from_cli(cli.command, cli.scope.into())
        .expect("code command maps to the public API");
    let Command::Code(options) = command else {
        panic!("expected public code command");
    };
    assert_eq!(options.project_root, PathBuf::from("/repo"));
    assert!(options.no_freshness);
}

#[test]
fn code_defaults_match_the_legacy_flat_command() {
    let options = map_code(&["--project", "/repo"]);

    assert_eq!(options.out, None);
    assert!(!options.purge);
    assert!(!options.force);
    assert!(options.scope.is_empty());
    assert!(!options.complete_scope);
    assert_eq!(options.ai.routing, None);
    assert_eq!(options.ai.depth, AiDepth::Files);
    assert_eq!(options.ai.prose_depth, ProseDepth::Standard);
    assert_eq!(options.ai.register, None);
    assert_eq!(options.ai.verify_scope, VerifyScope::Aggregates);
    assert_eq!(options.edge_limit, DEFAULT_CODE_GRAPH_EDGE_LIMIT);
    assert!(!options.include_docs);
    assert_eq!(options.since, None);
    assert_eq!(options.compare_to, None);
    assert_eq!(options.max_workers, 1);
    assert!(!options.repair_citations);
    assert!(!options.no_freshness);
}

#[test]
fn code_generation_flags_map_through_the_public_command() {
    let options = map_code(&[
        "--project",
        "/repo",
        "--out",
        "wiki",
        "--scope",
        "crates",
        "src",
        "--complete-scope",
        "--ai",
        "daemon",
        "--ai-depth",
        "symbols",
        "--ai-aggregate-profile",
        "feature_high",
        "--ai-verify-profile",
        "feature_mid",
        "--ai-verify-scope",
        "all",
        "--ai-prose-depth",
        "deep",
        "--ai-register",
        "maintainer",
        "--edge-limit",
        "42",
        "--include-docs",
        "--since",
        "HEAD~1",
        "--max-workers",
        "4",
    ]);

    assert_eq!(options.project_root, PathBuf::from("/repo"));
    assert_eq!(options.out.as_deref(), Some("wiki"));
    assert_eq!(options.scope, ["crates", "src"]);
    assert!(options.complete_scope);
    assert_eq!(options.edge_limit, 42);
    assert!(options.include_docs);
    assert_eq!(options.since.as_deref(), Some("HEAD~1"));
    assert_eq!(options.max_workers, 4);
}

#[test]
fn code_value_enums_accept_the_full_legacy_surface() {
    for value in ["auto", "daemon", "direct", "off"] {
        assert!(Cli::try_parse_from(["gwiki", "code", "--ai", value]).is_ok());
    }
    for value in ["sections", "files", "symbols"] {
        assert!(Cli::try_parse_from(["gwiki", "code", "--ai-depth", value]).is_ok());
    }
    for value in ["brief", "standard", "deep"] {
        assert!(Cli::try_parse_from(["gwiki", "code", "--ai-prose-depth", value]).is_ok());
    }
    for value in ["newcomer", "maintainer", "agent"] {
        assert!(Cli::try_parse_from(["gwiki", "code", "--ai-register", value]).is_ok());
    }
    for value in ["aggregates", "all"] {
        assert!(Cli::try_parse_from(["gwiki", "code", "--ai-verify-scope", value]).is_ok());
    }
}

#[test]
fn code_mode_conflicts_match_the_legacy_matrix() {
    let generation_flags: &[&[&str]] = &[
        &["--scope", "src"],
        &["--scope", "src", "--complete-scope"],
        &["--ai", "off"],
        &["--ai-depth", "symbols"],
        &["--ai-aggregate-profile", "feature_high"],
        &["--ai-aggregate-candidate", "claude/sonnet@high"],
        &["--ai-verify-profile", "feature_mid"],
        &["--ai-verify-scope", "all"],
        &["--ai-prose-depth", "deep"],
        &["--ai-register", "agent"],
        &["--edge-limit", "42"],
        &["--include-docs"],
        &["--since", "HEAD~1"],
        &["--max-workers", "4"],
        &["--repair-citations"],
    ];
    for flags in generation_flags {
        let mut purge = vec!["gwiki", "code", "--purge", "--force"];
        purge.extend_from_slice(flags);
        assert!(
            Cli::try_parse_from(purge).is_err(),
            "purge accepted {flags:?}"
        );

        let mut compare = vec!["gwiki", "code", "--compare-to", "HEAD"];
        compare.extend_from_slice(flags);
        assert!(
            Cli::try_parse_from(compare).is_err(),
            "compare accepted {flags:?}"
        );
    }
    assert!(Cli::try_parse_from(["gwiki", "code", "--force"]).is_err());
    assert!(Cli::try_parse_from(["gwiki", "code", "--complete-scope"]).is_err());
    assert!(
        Cli::try_parse_from([
            "gwiki",
            "code",
            "--ai-aggregate-profile",
            "feature_high",
            "--ai-aggregate-candidate",
            "claude/sonnet@high",
        ])
        .is_err()
    );
}

#[test]
fn code_positive_limits_keep_the_legacy_bound() {
    assert!(Cli::try_parse_from(["gwiki", "code", "--edge-limit", "0"]).is_err());
    assert!(Cli::try_parse_from(["gwiki", "code", "--max-workers", "0"]).is_err());
    assert!(Cli::try_parse_from(["gwiki", "code", "--edge-limit", "1000000001"]).is_err());
}

#[test]
fn code_modes_require_freshness_only_for_generation() {
    assert!(map_code(&["--project", "/repo"]).requires_freshness());
    assert!(!map_code(&["--project", "/repo", "--compare-to", "HEAD"]).requires_freshness());
    assert!(!map_code(&["--project", "/repo", "--purge", "--force"]).requires_freshness());
    assert!(!map_code(&["--project", "/repo", "--repair-citations"]).requires_freshness());
}

#[test]
fn code_mapping_preserves_cli_output_controls() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--format",
        "text",
        "--quiet",
        "code",
        "--project",
        "/repo",
    ])
    .expect("code command parses");
    let command = mapping::command_from_cli_with_runtime(
        cli.command,
        cli.scope.into(),
        cli.format,
        cli.quiet,
        cli.verbose,
    )
    .expect("code command maps");
    let Command::Code(options) = command else {
        panic!("expected public code command");
    };
    assert_eq!(options.format, gobby_wiki::output::Format::Text);
    assert!(options.quiet);
    assert!(!options.verbose);
}
