use clap::CommandFactory;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiRouting, EnvOnlySource};
use gobby_wiki::{PurgeTarget, ScopeSelection};

use crate::cli_runtime::log_level;

use super::mapping::command_from_cli;
use super::*;

mod code;

#[test]
fn cli_subcommands_match_clap_variants() {
    let mut listed = CLI_SUBCOMMANDS
        .iter()
        .map(|command| command.to_string())
        .collect::<Vec<_>>();
    listed.sort_unstable();
    let mut actual = Cli::command()
        .get_subcommands()
        .map(|command| command.get_name().to_string())
        .collect::<Vec<_>>();
    actual.sort_unstable();

    assert_eq!(actual, listed);
}

#[test]
fn research_subcommand_is_removed() {
    use clap::Parser;

    assert!(!CLI_SUBCOMMANDS.contains(&"research"));
    let error = Cli::try_parse_from(["gwiki", "research", "How does indexing work?"])
        .expect_err("research must no longer parse");
    assert_eq!(error.kind(), clap::error::ErrorKind::InvalidSubcommand);
}

#[test]
fn ask_flag_surface_supports_deep_investigation() {
    use clap::Parser;

    let cli = Cli::try_parse_from([
        "gwiki",
        "ask",
        "--deep",
        "--ai",
        "daemon",
        "--require-ai",
        "How does indexing work?",
    ])
    .expect("ask flags parse");
    let CliCommand::Ask(args) = cli.command else {
        panic!("expected ask command");
    };
    assert!(!args.llm);
    assert!(args.deep);
    assert_eq!(args.ai, AiRouting::Daemon);
    assert!(args.require_ai);
}

#[test]
fn search_flag_surface_supports_limit_and_semantic_toggle() {
    use clap::Parser;

    let cli = Cli::try_parse_from([
        "gwiki",
        "search",
        "--limit",
        "5",
        "--no-semantic",
        "--token-budget",
        "1500",
        "ownership",
    ])
    .expect("search flags parse");
    let CliCommand::Search(args) = cli.command else {
        panic!("expected search command");
    };
    assert_eq!(args.limit, 5);
    assert!(args.no_semantic);
    assert_eq!(args.token_budget, Some(1500));
    assert_eq!(args.query, "ownership");
}

#[test]
fn purge_flag_surface_requires_explicit_confirmation() {
    use clap::Parser;

    let cli =
        Cli::try_parse_from(["gwiki", "purge", "--project", "--yes"]).expect("purge flags parse");
    assert_eq!(
        cli.scope.project.as_deref(),
        Some(std::path::Path::new("."))
    );
    let CliCommand::Purge(args) = cli.command else {
        panic!("expected purge command");
    };
    assert!(args.yes);

    let command = command_from_cli(
        CliCommand::Purge(PurgeArgs {
            project_id: None,
            yes: true,
        }),
        cli.scope.into(),
    )
    .expect("map purge command");
    let Command::Purge { target, yes } = command else {
        panic!("expected purge command");
    };
    assert_eq!(
        target,
        PurgeTarget::selection(ScopeSelection::project(PathBuf::from(".")))
    );
    assert!(yes);
}

#[test]
fn purge_project_id_maps_without_a_project_root() {
    use clap::Parser;

    let project_id = "7c2f6952-2c51-4c57-a5f9-b5ac194b6599";
    let cli = Cli::try_parse_from(["gwiki", "purge", "--project-id", project_id, "--yes"])
        .expect("ID-native purge flags parse");
    let command = command_from_cli(cli.command, cli.scope.into()).expect("map ID-native purge");
    let Command::Purge { target, yes } = command else {
        panic!("expected purge command");
    };

    assert_eq!(target, PurgeTarget::project_id(project_id));
    assert!(yes);
}

#[test]
fn prune_force_maps_to_reachable_command() {
    use clap::Parser;

    let cli = Cli::try_parse_from(["gwiki", "prune", "--force"]).expect("prune flags parse");
    let command = command_from_cli(cli.command, cli.scope.into()).expect("map prune command");

    assert_eq!(command, Command::Prune { force: true });
}

#[test]
fn project_flag_normalization_handles_every_subcommand() {
    for subcommand in CLI_SUBCOMMANDS {
        let normalized = normalize_project_flag_args(["gwiki", "--project", subcommand]);
        assert_eq!(
            normalized,
            vec![
                OsString::from("gwiki"),
                OsString::from("--project"),
                OsString::from("."),
                OsString::from(subcommand),
            ],
            "bare --project should receive cwd before {subcommand}"
        );
    }
}

#[test]
fn attached_project_flag_preserves_every_subcommand() {
    for subcommand in CLI_SUBCOMMANDS {
        let normalized =
            normalize_project_flag_args(["gwiki", "--project=/tmp/wiki-project", subcommand]);
        assert_eq!(
            normalized,
            vec![
                OsString::from("gwiki"),
                OsString::from("--project=/tmp/wiki-project"),
                OsString::from(subcommand),
            ],
            "attached --project value should stay attached before {subcommand}"
        );
    }
}

#[test]
fn ingest_file_cli_flags_map_to_command_options() {
    let command = command_from_cli(
        CliCommand::IngestFile {
            path: PathBuf::from("media/interview.mp3"),
            no_ai: false,
            translate: true,
            target_lang: Some("es".to_string()),
            video_frame_interval_seconds: Some(0),
            transcription_routing: Some(AiRouting::Daemon),
            vision_routing: Some(AiRouting::Off),
            text_routing: Some(AiRouting::Daemon),
        },
        ScopeSelection::detect(),
    )
    .expect("map ingest-file command");

    let Command::IngestFile { options, .. } = command else {
        panic!("expected ingest-file command");
    };
    assert!(options.translate);
    assert_eq!(options.target_lang.as_deref(), Some("es"));
    assert_eq!(options.video_frame_interval_seconds, Some(0));

    let mut source = EnvOnlySource;
    let mut context = AiContext::resolve(None, &mut source);
    let original_transcribe_route = context.bindings.audio_transcribe.routing;
    options.apply_to_ai_context(&mut context);
    assert_eq!(
        context.bindings.audio_transcribe.routing,
        original_transcribe_route
    );
    assert_eq!(context.bindings.audio_translate.routing, AiRouting::Daemon);
    assert_eq!(context.bindings.vision_extract.routing, AiRouting::Off);
    assert_eq!(context.bindings.text_generate.routing, AiRouting::Daemon);
    assert_eq!(
        context.bindings.audio_translate.target_lang.as_deref(),
        Some("es")
    );
}

#[test]
fn ask_cli_flags_map_to_command_options() {
    let command = command_from_cli(
        CliCommand::Ask(AskArgs {
            question: "How do hooks work?".to_string(),
            llm: true,
            deep: true,
            ai: AiRouting::Daemon,
            require_ai: true,
            token_budget: Some(2000),
            include_candidates: true,
        }),
        ScopeSelection::topic("docs"),
    )
    .expect("map ask command");

    let Command::Ask {
        query,
        scope,
        llm,
        deep,
        ai,
        require_ai,
        token_budget,
        include_candidates,
    } = command
    else {
        panic!("expected ask command");
    };
    assert_eq!(query, "How do hooks work?");
    assert_eq!(scope, ScopeSelection::topic("docs"));
    assert!(llm);
    assert!(deep);
    assert_eq!(ai, AiRouting::Daemon);
    assert!(require_ai);
    assert_eq!(token_budget, Some(2000));
    assert!(include_candidates);
}

#[test]
fn compile_positional_topic_never_populates_scope_selection() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--project",
        "/tmp/example-project",
        "compile",
        "Borrow Checker",
        "--kind",
        "concept",
    ])
    .expect("parse compile with project scope and positional topic");
    assert_eq!(cli.scope.topic, None);
    assert_eq!(
        cli.scope.project.as_deref(),
        Some(std::path::Path::new("/tmp/example-project"))
    );
    let CliCommand::Compile(args) = cli.command else {
        panic!("expected parsed compile command");
    };
    assert_eq!(args.topic.as_deref(), Some("Borrow Checker"));

    let cli = Cli::try_parse_from([
        "gwiki",
        "--topic",
        "rust-async",
        "compile",
        "Borrow Checker",
    ])
    .expect("parse compile with topic scope and positional topic");
    assert_eq!(cli.scope.topic.as_deref(), Some("rust-async"));
    let CliCommand::Compile(args) = cli.command else {
        panic!("expected parsed compile command");
    };
    assert_eq!(args.topic.as_deref(), Some("Borrow Checker"));
}

#[test]
fn compile_source_flags_are_repeatable_and_map_to_command() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--project",
        "/tmp/example-project",
        "compile",
        "Borrow Checker",
        "--source",
        "src-alpha",
        "--source",
        "raw/src-beta.md",
    ])
    .expect("parse compile sources");
    let CliCommand::Compile(args) = cli.command else {
        panic!("expected parsed compile command");
    };
    assert_eq!(
        args.source,
        vec!["src-alpha".to_string(), "raw/src-beta.md".to_string()]
    );

    let command = command_from_cli(CliCommand::Compile(args), cli.scope.into())
        .expect("compile command maps");
    let Command::Compile {
        source,
        topic,
        scope,
        ..
    } = command
    else {
        panic!("expected compile command");
    };
    assert_eq!(topic.as_deref(), Some("Borrow Checker"));
    assert_eq!(
        source,
        vec!["src-alpha".to_string(), "raw/src-beta.md".to_string()]
    );
    assert_eq!(
        scope.project_root(),
        Some(std::path::Path::new("/tmp/example-project"))
    );
}

#[test]
fn graph_cli_maps_to_command_options() {
    use clap::Parser;

    let cli = Cli::try_parse_from(["gwiki", "graph", "--stdout", "--include", "knowledge"])
        .expect("parse graph command");
    let CliCommand::Graph(args) = cli.command else {
        panic!("expected parsed graph command");
    };
    assert!(args.stdout);

    let command = command_from_cli(CliCommand::Graph(args), ScopeSelection::Detect)
        .expect("map graph command");
    let Command::Graph { options, .. } = command else {
        panic!("expected graph command");
    };
    assert!(options.stdout);
    assert_eq!(options.include, GraphInclude::Knowledge);

    let default_cli = Cli::try_parse_from(["gwiki", "graph"]).expect("parse default graph");
    let CliCommand::Graph(default_args) = default_cli.command else {
        panic!("expected parsed graph command");
    };
    assert!(!default_args.stdout);
    assert_eq!(default_args.include, GraphInclude::All);
}

#[test]
fn pages_cli_maps_to_command() {
    use clap::Parser;

    let cli =
        Cli::try_parse_from(["gwiki", "pages", "--prefix", "code/"]).expect("parse pages command");
    let CliCommand::Pages(args) = cli.command else {
        panic!("expected parsed pages command");
    };
    assert_eq!(args.prefix.as_deref(), Some("code/"));

    let command = command_from_cli(CliCommand::Pages(args), ScopeSelection::Detect)
        .expect("map pages command");
    let Command::Pages { prefix, .. } = command else {
        panic!("expected pages command");
    };
    assert_eq!(prefix.as_deref(), Some("code/"));

    let default_cli = Cli::try_parse_from(["gwiki", "pages"]).expect("parse default pages");
    let CliCommand::Pages(default_args) = default_cli.command else {
        panic!("expected parsed pages command");
    };
    assert!(default_args.prefix.is_none());
}

#[test]
fn graph_context_cli_maps_to_command() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--format",
        "json",
        "graph-context",
        "--topic",
        "docs",
    ])
    .expect("parse graph-context command");
    assert_eq!(cli.scope.topic.as_deref(), Some("docs"));
    let CliCommand::GraphContext = cli.command else {
        panic!("expected parsed graph-context command");
    };

    let command = command_from_cli(CliCommand::GraphContext, ScopeSelection::topic("docs"))
        .expect("map graph-context command");
    let Command::GraphContext { scope } = command else {
        panic!("expected graph-context command");
    };
    assert_eq!(scope.topic_name(), Some("docs"));
}

#[test]
fn review_report_cli_maps_to_command_options() {
    let command = command_from_cli(
        CliCommand::ReviewReport(ReviewReportArgs {
            files: vec!["src/lib.rs".to_string()],
            symbols: vec!["symbol-a".to_string()],
            diff_path: Some(PathBuf::from("pr.diff")),
            output: "reports/pr.md".to_string(),
        }),
        ScopeSelection::project("/repo"),
    )
    .expect("map review-report command");

    let Command::ReviewReport { scope, options } = command else {
        panic!("expected review-report command");
    };
    assert_eq!(scope.project_root(), Some(std::path::Path::new("/repo")));
    assert_eq!(options.files, vec!["src/lib.rs"]);
    assert_eq!(options.symbols, vec!["symbol-a"]);
    assert_eq!(options.diff_path, Some(PathBuf::from("pr.diff")));
    assert_eq!(options.output, "reports/pr.md");
}

#[test]
fn ingest_url_cli_accepts_multiple_urls() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "ingest-url",
        "--topic",
        "rust",
        "https://example.test/one",
        "https://example.test/two",
    ])
    .expect("parse ingest-url command");
    assert_eq!(cli.scope.topic.as_deref(), Some("rust"));
    let CliCommand::IngestUrl {
        urls,
        max_age_hours,
    } = &cli.command
    else {
        panic!("expected parsed ingest-url command");
    };
    assert_eq!(*max_age_hours, 24);
    let expected_urls = [
        "https://example.test/one".to_string(),
        "https://example.test/two".to_string(),
    ];
    assert_eq!(urls.as_slice(), expected_urls.as_slice(),);

    let command = command_from_cli(cli.command, ScopeSelection::topic("rust"))
        .expect("map ingest-url command");

    let Command::IngestUrl {
        urls,
        scope,
        max_age_hours,
    } = command
    else {
        panic!("expected ingest-url command");
    };
    assert_eq!(max_age_hours, 24);
    assert_eq!(
        urls,
        vec![
            "https://example.test/one".to_string(),
            "https://example.test/two".to_string()
        ]
    );
    assert_eq!(scope.topic_name(), Some("rust"));
}

#[test]
fn ingest_url_cli_forwards_explicit_max_age_hours() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "ingest-url",
        "--max-age-hours",
        "0",
        "https://example.test/source",
    ])
    .expect("parse ingest-url command");

    let command = command_from_cli(cli.command, ScopeSelection::project("/repo"))
        .expect("map ingest-url command");
    let Command::IngestUrl {
        urls,
        max_age_hours,
        ..
    } = command
    else {
        panic!("expected ingest-url command");
    };

    assert_eq!(urls, vec!["https://example.test/source".to_string()]);
    assert_eq!(max_age_hours, 0);
}

#[test]
fn ingest_url_cli_rejects_unbounded_max_age_hours() {
    let error = Cli::try_parse_from([
        "gwiki",
        "ingest-url",
        "--max-age-hours",
        "8761",
        "https://example.test/source",
    ])
    .expect_err("max age above one year must fail");

    assert!(error.to_string().contains("8760"));
}

#[test]
fn ingest_url_cli_command_literal_carries_max_age_hours() {
    let command = command_from_cli(
        CliCommand::IngestUrl {
            urls: vec!["https://example.test/source".to_string()],
            max_age_hours: 12,
        },
        ScopeSelection::detect(),
    )
    .expect("map ingest-url command");

    let Command::IngestUrl { max_age_hours, .. } = command else {
        panic!("expected ingest-url command");
    };
    assert_eq!(max_age_hours, 12);
}

#[test]
fn sync_sessions_cli_flags_map_to_command_options() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--project",
        "/tmp/example-project",
        "sync-sessions",
        "--archive-dir",
        "/tmp/session_transcripts",
        "--wiki-dir",
        "/tmp/session_wiki",
        "--limit",
        "3",
        "--raw",
        "--no-enrich",
    ])
    .expect("parse sync-sessions command");
    let CliCommand::SyncSessions(args) = cli.command else {
        panic!("expected parsed sync-sessions command");
    };
    assert_eq!(
        args.archive_dir.as_deref(),
        Some(std::path::Path::new("/tmp/session_transcripts"))
    );
    assert_eq!(
        args.wiki_dir.as_deref(),
        Some(std::path::Path::new("/tmp/session_wiki"))
    );
    assert_eq!(args.limit, Some(3));
    assert!(args.raw);
    assert!(args.no_enrich);

    let command = command_from_cli(CliCommand::SyncSessions(args), cli.scope.into())
        .expect("map sync-sessions command");
    let Command::SyncSessions { scope, options } = command else {
        panic!("expected sync-sessions command");
    };
    assert_eq!(
        scope.project_root(),
        Some(std::path::Path::new("/tmp/example-project"))
    );
    assert_eq!(
        options.archive_dir.as_deref(),
        Some(std::path::Path::new("/tmp/session_transcripts"))
    );
    assert_eq!(
        options.wiki_dir.as_deref(),
        Some(std::path::Path::new("/tmp/session_wiki"))
    );
    assert_eq!(options.limit, Some(3));
    assert!(options.raw);
    assert!(!options.enrich);

    let default_cli = Cli::try_parse_from(["gwiki", "sync-sessions"])
        .expect("parse default sync-sessions command");
    let CliCommand::SyncSessions(default_args) = default_cli.command else {
        panic!("expected parsed sync-sessions command");
    };
    assert!(!default_args.raw);
    assert!(!default_args.no_enrich);
    let default_command = command_from_cli(
        CliCommand::SyncSessions(default_args),
        default_cli.scope.into(),
    )
    .expect("map default sync-sessions command");
    let Command::SyncSessions {
        options: default_options,
        ..
    } = default_command
    else {
        panic!("expected sync-sessions command");
    };
    assert!(!default_options.raw);
    assert!(default_options.enrich);
}

#[test]
fn refresh_cli_flags_map_to_command_options() {
    let cli = Cli::try_parse_from([
        "gwiki",
        "--format",
        "json",
        "refresh",
        "--id",
        "src1",
        "--id",
        "src2",
        "--dry-run",
        "--topic",
        "docs",
    ])
    .expect("parse refresh command");
    assert_eq!(cli.scope.topic.as_deref(), Some("docs"));
    let CliCommand::Refresh(args) = cli.command else {
        panic!("expected parsed refresh command");
    };
    assert_eq!(args.id, vec!["src1".to_string(), "src2".to_string()]);
    assert!(args.dry_run);

    let command = command_from_cli(
        CliCommand::Refresh(RefreshArgs {
            id: vec!["src1".to_string(), "src2".to_string()],
            dry_run: true,
        }),
        ScopeSelection::topic("docs"),
    )
    .expect("map refresh command");

    let Command::Refresh {
        scope,
        source_ids,
        dry_run,
    } = command
    else {
        panic!("expected refresh command");
    };
    assert_eq!(scope.topic_name(), Some("docs"));
    assert_eq!(source_ids, vec!["src1".to_string(), "src2".to_string()]);
    assert!(dry_run);

    assert!(
        Cli::try_parse_from(["gwiki", "refresh", "--scope", "project"]).is_err(),
        "refresh must use existing --project/--topic globals, not --scope"
    );

    let bare_project =
        Cli::try_parse_from(["gwiki", "refresh", "--project"]).expect("parse bare project");
    assert_eq!(bare_project.scope.project, Some(PathBuf::from(".")));

    let rooted_project = Cli::try_parse_from(["gwiki", "refresh", "--project", "/repo"])
        .expect("parse explicit project root");
    assert_eq!(rooted_project.scope.project, Some(PathBuf::from("/repo")));
}

#[test]
fn setup_cli_flags_map_to_command_options() {
    let command = command_from_cli(
        CliCommand::Setup(SetupArgs {
            standalone: true,
            database_url: Some("postgresql://localhost/gwiki".to_string()),
            no_services: true,
            falkordb_host: Some("127.0.0.2".to_string()),
            falkordb_port: Some(26379),
            falkordb_password: Some("secret".to_string()),
            qdrant_url: Some("http://localhost:7333".to_string()),
            embedding_provider: Some("openai-compatible".to_string()),
            embedding_api_base: Some("http://localhost:1234/v1".to_string()),
            embedding_model: Some("embed-small".to_string()),
            embedding_query_prefix: Some("query: ".to_string()),
            embedding_vector_dim: Some(1024),
            embedding_api_key: Some("api-key".to_string()),
        }),
        ScopeSelection::detect(),
    )
    .expect("map setup command");

    let Command::Setup { options, .. } = command else {
        panic!("expected setup command");
    };
    assert!(options.standalone);
    assert_eq!(
        options.database_url.as_deref(),
        Some("postgresql://localhost/gwiki")
    );
    assert!(options.no_services);
    assert_eq!(options.falkordb_host.as_deref(), Some("127.0.0.2"));
    assert_eq!(options.falkordb_port, Some(26379));
    assert_eq!(options.qdrant_url.as_deref(), Some("http://localhost:7333"));
    assert_eq!(options.embedding_vector_dim, Some(1024));
}

#[test]
fn benchmark_cli_maps_to_command_options() {
    let command = command_from_cli(
        CliCommand::Benchmark(BenchmarkArgs {
            retrieval_candidates: 5,
        }),
        ScopeSelection::topic("rust"),
    )
    .expect("benchmark command maps");

    assert_eq!(
        command,
        Command::Benchmark {
            scope: ScopeSelection::topic("rust"),
            options: BenchmarkOptions {
                retrieval_candidates: 5,
            }
        }
    );
}

#[test]
fn upkeep_cli_flags_map_to_command_options() {
    use clap::Parser;

    let cli = Cli::try_parse_from([
        "gwiki",
        "upkeep",
        "--max-pages",
        "4",
        "--min-mentions",
        "3",
        "--max-sources-per-page",
        "6",
        "--time-budget-seconds",
        "1320",
        "--dry-run",
        "--ai",
        "off",
    ])
    .expect("parse upkeep command");
    let CliCommand::Upkeep(args) = cli.command else {
        panic!("expected parsed upkeep command");
    };
    assert_eq!(args.max_pages, 4);
    assert_eq!(args.min_mentions, 3);
    assert_eq!(args.max_sources_per_page, 6);
    assert_eq!(args.time_budget_seconds, Some(1320));
    assert!(args.dry_run);
    assert_eq!(args.ai, AiRouting::Off);

    let command =
        command_from_cli(CliCommand::Upkeep(args), cli.scope.into()).expect("map upkeep command");
    let Command::Upkeep { options, ai, .. } = command else {
        panic!("expected upkeep command");
    };
    assert_eq!(options.max_pages, 4);
    assert_eq!(options.min_mentions, 3);
    assert_eq!(options.max_sources_per_page, 6);
    assert_eq!(options.time_budget_seconds, Some(1320));
    assert!(options.dry_run);
    assert_eq!(ai, AiRouting::Off);

    let default_cli = Cli::try_parse_from(["gwiki", "upkeep"]).expect("parse default upkeep");
    let CliCommand::Upkeep(default_args) = default_cli.command else {
        panic!("expected parsed upkeep command");
    };
    assert_eq!(default_args.max_pages, 10);
    assert_eq!(default_args.min_mentions, 2);
    assert_eq!(default_args.max_sources_per_page, 12);
    assert_eq!(default_args.time_budget_seconds, None);
    assert!(!default_args.dry_run);
    assert_eq!(default_args.ai, AiRouting::Daemon);
}

#[test]
fn recap_cli_flags_map_to_command_options() {
    use clap::Parser;

    let cli = Cli::try_parse_from(["gwiki", "recap", "--date", "2026-07-04", "--ai", "off"])
        .expect("parse recap command");
    let CliCommand::Recap(args) = cli.command else {
        panic!("expected parsed recap command");
    };
    assert_eq!(args.date.as_deref(), Some("2026-07-04"));
    assert_eq!(args.ai, AiRouting::Off);

    let command =
        command_from_cli(CliCommand::Recap(args), cli.scope.into()).expect("map recap command");
    let Command::Recap { options, ai, .. } = command else {
        panic!("expected recap command");
    };
    assert_eq!(options.date.as_deref(), Some("2026-07-04"));
    assert_eq!(ai, AiRouting::Off);

    let default_cli = Cli::try_parse_from(["gwiki", "recap"]).expect("parse default recap");
    let CliCommand::Recap(default_args) = default_cli.command else {
        panic!("expected parsed recap command");
    };
    assert!(default_args.date.is_none());
    assert_eq!(default_args.ai, AiRouting::Daemon);
}

#[test]
fn log_level_honors_rust_log_and_quiet() {
    assert_eq!(log_level(false, false, None), log::LevelFilter::Off);
    assert_eq!(log_level(false, true, None), log::LevelFilter::Debug);
    assert_eq!(
        log_level(false, true, Some("warn")),
        log::LevelFilter::Debug
    );
    assert_eq!(
        log_level(false, true, Some("trace")),
        log::LevelFilter::Trace
    );
    assert_eq!(
        log_level(false, false, Some(" DEBUG ")),
        log::LevelFilter::Debug
    );
    assert_eq!(
        log_level(false, false, Some("not-a-level")),
        log::LevelFilter::Off
    );
    assert_eq!(log_level(true, true, Some("trace")), log::LevelFilter::Off);
}

#[test]
fn quiet_and_verbose_flags_parse_and_conflict() {
    use clap::Parser;

    let quiet = Cli::try_parse_from(["gwiki", "-q", "status"]).expect("parse short quiet");
    assert!(quiet.quiet);
    assert!(!quiet.verbose);

    let verbose =
        Cli::try_parse_from(["gwiki", "status", "--verbose"]).expect("parse global verbose");
    assert!(!verbose.quiet);
    assert!(verbose.verbose);

    let error = Cli::try_parse_from(["gwiki", "--quiet", "--verbose", "status"])
        .expect_err("quiet and verbose must conflict");
    assert_eq!(error.kind(), clap::error::ErrorKind::ArgumentConflict);
}
