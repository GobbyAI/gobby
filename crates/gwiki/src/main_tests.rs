use clap::CommandFactory;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiRouting, EnvOnlySource};

use super::*;

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
            transcription_routing: Some(AiRouting::Direct),
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
    assert_eq!(context.bindings.audio_translate.routing, AiRouting::Direct);
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
            ai: AiRouting::Direct,
            require_ai: true,
        }),
        ScopeSelection::topic("docs"),
    )
    .expect("map ask command");

    let Command::Ask {
        query,
        scope,
        llm,
        ai,
        require_ai,
    } = command
    else {
        panic!("expected ask command");
    };
    assert_eq!(query, "How do hooks work?");
    assert_eq!(scope, ScopeSelection::topic("docs"));
    assert!(llm);
    assert_eq!(ai, AiRouting::Direct);
    assert!(require_ai);
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
    let CliCommand::IngestUrl { urls } = cli.command else {
        panic!("expected parsed ingest-url command");
    };
    assert_eq!(
        urls,
        vec![
            "https://example.test/one".to_string(),
            "https://example.test/two".to_string()
        ]
    );

    let command = command_from_cli(
        CliCommand::IngestUrl {
            urls: vec![
                "https://example.test/one".to_string(),
                "https://example.test/two".to_string(),
            ],
        },
        ScopeSelection::topic("rust"),
    )
    .expect("map ingest-url command");

    let Command::IngestUrl { urls, scope } = command else {
        panic!("expected ingest-url command");
    };
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
fn log_level_honors_rust_log_and_quiet() {
    assert_eq!(log_level(false, None), log::LevelFilter::Off);
    assert_eq!(log_level(false, Some("warn")), log::LevelFilter::Warn);
    assert_eq!(log_level(false, Some(" DEBUG ")), log::LevelFilter::Debug);
    assert_eq!(log_level(false, Some("not-a-level")), log::LevelFilter::Off);
    assert_eq!(log_level(true, Some("trace")), log::LevelFilter::Off);
}
