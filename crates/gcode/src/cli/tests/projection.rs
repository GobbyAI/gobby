use super::*;

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
            command:
                VectorCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => {
            assert_eq!(file, "src/lib.rs");
            assert!(!allow_missing_indexed_file);
        }
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

    let cli = Cli::try_parse_from(["gcode", "graph", "cleanup-orphans"])
        .expect("graph cleanup-orphans parses");
    assert!(matches!(
        cli.command,
        Command::Graph {
            command: GraphCommand::CleanupOrphans
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
            command: VectorCommand::Clear {
                project_id: None,
                drop_collection: false,
            }
        }
    ));
    let cli = Cli::try_parse_from([
        "gcode",
        "vector",
        "clear",
        "--project-id",
        "11111111-1111-1111-1111-111111111111",
        "--drop-collection",
    ])
    .expect("vector clear --project-id --drop-collection parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::Clear {
                project_id: Some(_),
                drop_collection: true,
            }
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "vector", "rebuild"]).expect("vector rebuild parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::Rebuild
        }
    ));

    let cli = Cli::try_parse_from(["gcode", "vector", "cleanup-orphans"])
        .expect("vector cleanup-orphans parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::CleanupOrphans
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
fn parse_vector_sync_file_with_flag() {
    let cli = Cli::try_parse_from([
        "gcode",
        "vector",
        "sync-file",
        "--file",
        "src/lib.rs",
        "--allow-missing-indexed-file",
    ])
    .expect("vector sync-file with flag parses");
    match cli.command {
        Command::Vector {
            command:
                VectorCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => {
            assert_eq!(file, "src/lib.rs");
            assert!(allow_missing_indexed_file);
        }
        _ => panic!("expected vector sync-file command"),
    }
}

#[test]
fn graph_view_rejects_unknown_kind() {
    let error = match Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "pdg", "--symbol", "Derived",
    ]) {
        Ok(_) => panic!("unknown --view must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), clap::error::ErrorKind::InvalidValue);
    let message = error.to_string();
    assert!(
        message.contains("invalid value 'pdg'") || message.contains("invalid value"),
        "unexpected error: {message}"
    );
}

#[test]
fn graph_view_requires_typed_compatible_selector() {
    for args in [
        ["--view", "mcg", "--file", "src/lib.rs"],
        ["--view", "mcg", "--module", "crate.module"],
        ["--view", "fcg", "--symbol", "run"],
        ["--view", "class-hierarchy", "--symbol", "Derived"],
    ] {
        Cli::try_parse_from(["gcode", "graph", "view"].into_iter().chain(args))
            .expect("valid typed graph selector parses");
    }

    for args in [
        ["--view", "mcg", "--symbol", "run"],
        ["--view", "fcg", "--file", "src/lib.rs"],
        ["--view", "fcg", "--module", "crate.module"],
        ["--view", "class-hierarchy", "--file", "src/lib.rs"],
    ] {
        let error = match Cli::try_parse_from(["gcode", "graph", "view"].into_iter().chain(args)) {
            Ok(_) => panic!("incompatible graph selector must fail"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), clap::error::ErrorKind::ArgumentConflict);
    }

    let positional =
        match Cli::try_parse_from(["gcode", "graph", "view", "--view", "fcg", "Derived"]) {
            Ok(_) => panic!("removed positional seed syntax must fail"),
            Err(error) => error,
        };
    assert_eq!(positional.kind(), clap::error::ErrorKind::UnknownArgument);
}

#[test]
fn graph_view_depth_domain() {
    let error = match Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "--symbol", "seed", "--depth", "0",
    ]) {
        Ok(_) => panic!("--depth 0 must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), clap::error::ErrorKind::ValueValidation);

    let error = match Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "--symbol", "seed", "--depth", "17",
    ]) {
        Ok(_) => panic!("--depth 17 must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), clap::error::ErrorKind::ValueValidation);

    let cli = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "class-hierarchy",
        "--symbol",
        "Derived",
        "--depth",
        "16",
    ])
    .expect("--depth 16 is accepted");
    match cli.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.view, GraphViewKind::ClassHierarchy);
            assert_eq!(args.depth, Some(16));
            assert_eq!(args.effective_depth(), 16);
        }
        _ => panic!("expected graph view command"),
    }

    let fcg = Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "--symbol", "seed",
    ])
    .expect("omitted --depth parses for fcg");
    match fcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 1);
        }
        _ => panic!("expected graph view command"),
    }

    let mcg = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "mcg",
        "--file",
        "src/lib.rs",
    ])
    .expect("omitted --depth parses for mcg");
    match mcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 1);
        }
        _ => panic!("expected graph view command"),
    }

    let chg = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "class-hierarchy",
        "--symbol",
        "Derived",
    ])
    .expect("omitted --depth parses for class-hierarchy");
    match chg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 8);
        }
        _ => panic!("expected graph view command"),
    }
}

#[test]
fn graph_view_omitted_depth_defaults_by_kind() {
    let fcg = Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "--symbol", "seed",
    ])
    .expect("omitted --depth parses for fcg");
    match fcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 1);
        }
        _ => panic!("expected graph view command"),
    }

    let mcg = Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "mcg", "--file", "src/a.py",
    ])
    .expect("omitted --depth parses for mcg");
    match mcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 1);
        }
        _ => panic!("expected graph view command"),
    }

    let chg = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "class-hierarchy",
        "--symbol",
        "Derived",
    ])
    .expect("omitted --depth parses for class-hierarchy");
    match chg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.depth, None);
            assert_eq!(args.effective_depth(), 8);
        }
        _ => panic!("expected graph view command"),
    }
}

#[test]
fn graph_view_rejects_hierarchy_row_limits() {
    let incoming = match Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "class-hierarchy",
        "--symbol",
        "Derived",
        "--incoming-limit",
        "4",
    ]) {
        Ok(_) => panic!("class-hierarchy plus --incoming-limit must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(incoming.kind(), clap::error::ErrorKind::ArgumentConflict);

    let outgoing = match Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "class-hierarchy",
        "--symbol",
        "Derived",
        "--outgoing-limit",
        "4",
    ]) {
        Ok(_) => panic!("class-hierarchy plus --outgoing-limit must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(outgoing.kind(), clap::error::ErrorKind::ArgumentConflict);

    let fcg = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "fcg",
        "--symbol",
        "seed",
        "--incoming-limit",
        "3",
        "--outgoing-limit",
        "5",
    ])
    .expect("fcg accepts row limits");
    match fcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.incoming_limit, Some(3));
            assert_eq!(args.outgoing_limit, Some(5));
        }
        _ => panic!("expected graph view command"),
    }

    let mcg = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "mcg",
        "--file",
        "src/lib.rs",
        "--incoming-limit",
        "2",
        "--outgoing-limit",
        "9",
    ])
    .expect("mcg accepts row limits");
    match mcg.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.incoming_limit, Some(2));
            assert_eq!(args.outgoing_limit, Some(9));
        }
        _ => panic!("expected graph view command"),
    }
}

#[test]
fn communities_parses_without_seed() {
    let cli = Cli::try_parse_from(["gcode", "graph", "view", "--view", "communities"])
        .expect("seedless communities view parses");
    match cli.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => {
            assert_eq!(args.view, GraphViewKind::Communities);
            assert_eq!(args.seed, None);
            assert_eq!(args.effective_min_size(), 2);
        }
        _ => panic!("expected graph view command"),
    }
}

#[test]
fn communities_parses_min_size_and_community_selectors() {
    let sized = Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "communities",
        "--min-size",
        "5",
    ])
    .expect("communities --min-size parses");
    match sized.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => assert_eq!(args.min_size, Some(5)),
        _ => panic!("expected graph view command"),
    }

    for selector in ["memory", "crates/gcode/src/lib.rs"] {
        let cli = Cli::try_parse_from([
            "gcode",
            "graph",
            "view",
            "--view",
            "communities",
            "--community",
            selector,
        ])
        .expect("communities --community parses");
        match cli.command {
            Command::Graph {
                command: GraphCommand::View(args),
            } => assert_eq!(
                args.seed,
                Some(GraphViewSeed::Community(selector.to_string()))
            ),
            _ => panic!("expected graph view command"),
        }
    }
}

#[test]
fn communities_rejects_other_seeds_depth_and_row_limits() {
    for extra in [
        ["--file", "src/lib.rs"],
        ["--symbol", "run"],
        ["--depth", "1"],
        ["--incoming-limit", "1"],
    ] {
        let error = match Cli::try_parse_from(
            ["gcode", "graph", "view", "--view", "communities"]
                .into_iter()
                .chain(extra),
        ) {
            Ok(_) => panic!("communities must reject incompatible selectors and limits"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), clap::error::ErrorKind::ArgumentConflict);
    }
}

#[test]
fn min_size_rejected_off_communities_and_at_zero() {
    let off_communities = match Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "mcg",
        "--file",
        "src/lib.rs",
        "--min-size",
        "2",
    ]) {
        Ok(_) => panic!("--min-size only belongs to communities"),
        Err(error) => error,
    };
    assert_eq!(
        off_communities.kind(),
        clap::error::ErrorKind::ArgumentConflict
    );

    let zero = match Cli::try_parse_from([
        "gcode",
        "graph",
        "view",
        "--view",
        "communities",
        "--min-size",
        "0",
    ]) {
        Ok(_) => panic!("zero is not a positive minimum size"),
        Err(error) => error,
    };
    assert_eq!(zero.kind(), clap::error::ErrorKind::ValueValidation);
}

#[test]
fn effective_min_size_defaults_to_two() {
    let cli = Cli::try_parse_from(["gcode", "graph", "view", "--view", "communities"])
        .expect("seedless communities view parses");
    match cli.command {
        Command::Graph {
            command: GraphCommand::View(args),
        } => assert_eq!(args.effective_min_size(), 2),
        _ => panic!("expected graph view command"),
    }
}

#[test]
fn seeded_views_still_require_a_seed() {
    for view in ["mcg", "fcg", "class-hierarchy"] {
        let error = match Cli::try_parse_from(["gcode", "graph", "view", "--view", view]) {
            Ok(_) => panic!("seeded graph views still require a seed"),
            Err(error) => error,
        };
        assert_eq!(
            error.kind(),
            clap::error::ErrorKind::MissingRequiredArgument
        );
    }
}

mod pin_recovery {
    use crate::config::{CodeVectorSettings, EmbeddingConfig, QdrantConfig};
    use crate::db;
    use crate::models::{SearchWarningCause, SearchWarningLane, Symbol};
    use crate::projection::sync::tests::serial_db::{
        FILE, pinned_fixture, respond, seed_version, version_hash,
    };
    use crate::vector::code_symbols::{self, tests::read_http_request};
    use serde_json::json;
    use std::net::{TcpListener, TcpStream};
    use std::thread;

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn text_search_survives_projection_recovery() {
        let database_url = crate::test_env::postgres_test_database_url("pinned text recovery");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let (mut ctx, source, _source_cleanup, _overlay_cleanup, _root) =
            pinned_fixture(&mut conn, &database_url);
        let symbol_id = Symbol::make_id(&source, FILE, &version_hash("h"), "run", "function", 0);
        let facts = crate::codewiki_facts::CodewikiFacts::from_context(ctx.clone());
        let text = facts.search("run", 10).expect("pinned PostgreSQL search");
        assert_eq!(
            text.iter()
                .map(|hit| hit.symbol.id.as_str())
                .collect::<Vec<_>>(),
            vec![symbol_id.as_str()]
        );
        let bm25 = crate::search::fts::search_symbols_fts_visible(
            &mut conn,
            "run",
            &ctx,
            None,
            None,
            &[],
            10,
        )
        .expect("pinned BM25 search");
        let bm25_ids = bm25.into_iter().map(|symbol| symbol.id).collect::<Vec<_>>();
        assert_eq!(bm25_ids, vec![symbol_id]);

        // The source selector moved and the child pin is still unprojected.
        // Embeddings are unavailable during recovery; text facts remain usable.
        ctx.embedding = None;
        let semantic = crate::commands::search::semantic_lane_for_test(&ctx, "run", 10);
        let assembled = crate::commands::search::assemble_hybrid_sources(
            vec![],
            bm25_ids.clone(),
            semantic,
            vec![],
            vec![],
        );
        assert!(
            assembled
                .sources
                .iter()
                .any(|(lane, ids)| *lane == "fts" && ids == &bm25_ids)
        );
        assert!(
            assembled.warnings.iter().any(|warning| {
                let message = warning.message.to_lowercase();
                (message.contains("projection") || message.contains("pin"))
                    && ["pending", "recover", "unavailable", "degraded"]
                        .iter()
                        .any(|state| message.contains(state))
            }),
            "search must report pending projection degradation: {:?}",
            assembled.warnings
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn projection_reads_respect_pin_versions() {
        use std::sync::{
            Arc,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        };
        let database_url = crate::test_env::postgres_test_database_url("pin projection reads");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let (mut ctx, source, _source_cleanup, _overlay_cleanup, _root) =
            pinned_fixture(&mut conn, &database_url);
        let symbol_id = Symbol::make_id(&source, FILE, &version_hash("h"), "run", "function", 0);
        // The PostgreSQL/BM25 lane remains available before projection recovery.
        let facts = crate::codewiki_facts::CodewikiFacts::from_context(ctx.clone());
        let text = facts.search("run", 10).expect("read pinned text facts");
        assert_eq!(
            text.iter()
                .map(|hit| hit.symbol.id.as_str())
                .collect::<Vec<_>>(),
            vec![symbol_id.as_str()]
        );

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind semantic service");
        let address = listener.local_addr().expect("address");
        let url = format!("http://{address}");
        ctx.qdrant = Some(QdrantConfig {
            url: Some(url.clone()),
            api_key: None,
        });
        ctx.embedding = Some(EmbeddingConfig {
            api_base: format!("{url}/v1"),
            model: "embed-small".into(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 5,
        });
        ctx.code_vectors = CodeVectorSettings::with_vector_dim(Some(3));
        let direct =
            code_symbols::EmbeddingSource::Direct(ctx.embedding.clone().expect("embedding config"));
        let stop = Arc::new(AtomicBool::new(false));
        let namespace = Arc::new(AtomicUsize::new(0));
        let server = {
            let stop = Arc::clone(&stop);
            let namespace = Arc::clone(&namespace);
            let symbol_id = symbol_id.clone();
            let source_collection = code_symbols::collection_name(
                crate::config::CODE_SYMBOL_COLLECTION_PREFIX,
                &source,
            )
            .expect("source collection");
            thread::spawn(move || {
                while let Ok((mut stream, _)) = listener.accept() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let request = read_http_request(&mut stream);
                    let line = request.lines().next().expect("request line");
                    let body = if line.contains("/collections/") {
                        let is_source = line.contains(&source_collection);
                        let admitted = is_source == (namespace.load(Ordering::SeqCst) == 0);
                        json!({"result": if admitted { vec![json!({"id":symbol_id, "score":0.9})] } else { vec![] }})
                    } else {
                        json!({"data":[{"index":0,"embedding":[0.1,0.2,0.3]}]})
                    };
                    respond(&mut stream, body);
                }
            })
        };
        let hash_h = version_hash("h");
        let hash_k = version_hash("k");
        let mut observed = Vec::new();
        for (hash, projected, backend, expected) in [
            (hash_h.as_str(), false, 0, true),  // live source namespace
            (hash_h.as_str(), false, 1, false), // stray child rows are not recovered rows
            (hash_k.as_str(), true, 1, true),   // completed child projection
            (hash_k.as_str(), true, 0, false),  // source moved; old source projection is excluded
            (hash_k.as_str(), false, 0, false),
            (hash_k.as_str(), false, 1, false), // late/discarded upsert remains unadmitted
        ] {
            seed_version(&mut conn, &source, hash, hash == hash_h.as_str());
            conn.execute(
                "UPDATE code_overlay_pins SET vector_projected = $2 WHERE overlay_project_id = $1",
                &[
                    &db::id_param(&ctx.project_id).expect("child UUID"),
                    &projected,
                ],
            )
            .expect("set recovery state");
            namespace.store(backend, Ordering::SeqCst);
            let hits = code_symbols::semantic_search_with_source(&ctx, &direct, "run", 10);
            observed.push(((hash, projected, backend), hits, expected));
        }
        // Simulate rows left by a late child upsert after each invalidating event.
        namespace.store(1, Ordering::SeqCst);
        let overlay_uuid = db::id_param(&ctx.project_id).expect("child UUID");
        for change in ["repin", "owned", "tombstone", "purge"] {
            conn.execute(
                "DELETE FROM code_indexed_file_states WHERE project_id = $1",
                &[&overlay_uuid],
            )
            .expect("clear child shadow");
            conn.execute("UPDATE code_overlay_pins SET content_hash = $2, vector_projected = false WHERE overlay_project_id = $1", &[&overlay_uuid, &hash_h]).expect("restore pin");
            match change {
                "repin" => {
                    conn.execute("UPDATE code_overlay_pins SET content_hash = $2 WHERE overlay_project_id = $1", &[&overlay_uuid, &hash_k]).expect("repin");
                }
                "purge" => {
                    conn.execute(
                        "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1",
                        &[&overlay_uuid],
                    )
                    .expect("purge");
                }
                _ => {
                    seed_version(&mut conn, &ctx.project_id, "child-version", false);
                    if change == "tombstone" {
                        conn.execute("UPDATE code_indexed_files SET language = '__gcode_deleted__' WHERE project_id = $1", &[&overlay_uuid]).expect("tombstone");
                    }
                }
            }
            observed.push((
                (change, false, 1),
                code_symbols::semantic_search_with_source(&ctx, &direct, "run", 10),
                false,
            ));
        }
        stop.store(true, Ordering::SeqCst);
        drop(TcpStream::connect(address).expect("wake semantic server"));
        server.join().expect("semantic service");
        for (state, hits, expected) in observed {
            let expected_hits = if expected {
                vec![symbol_id.clone()]
            } else {
                vec![]
            };
            assert_eq!(
                hits.into_iter().map(|(id, _)| id).collect::<Vec<_>>(),
                expected_hits,
                "selector/projected/backend={state:?}"
            );
        }
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn graph_reads_report_pending_pin_projection() {
        let database_url = crate::test_env::postgres_test_database_url("pinned graph pending");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let (ctx, _source, _source_cleanup, _overlay_cleanup) =
            pinned_fixture(&mut conn, &database_url);
        let overlay_uuid = db::id_param(&ctx.project_id).expect("child UUID");

        // The fixture pin has no recorded graph projection, so its path serves no
        // graph facts; graph reads must report that instead of returning empty.
        let warnings = crate::visibility::graph_versions::pending_projection_warnings(&ctx);
        assert_eq!(
            warnings.len(),
            1,
            "pending graph pin must warn: {warnings:?}"
        );
        assert_eq!(warnings[0].lane, SearchWarningLane::Graph);
        assert_eq!(warnings[0].cause, SearchWarningCause::ProjectionPending);
        assert!(
            warnings[0].message.contains("graph") && warnings[0].message.contains("pending"),
            "{}",
            warnings[0].message
        );

        conn.execute(
            "UPDATE code_overlay_pins SET graph_projected = true WHERE overlay_project_id = $1",
            &[&overlay_uuid],
        )
        .expect("record graph projection");
        assert!(
            crate::visibility::graph_versions::pending_projection_warnings(&ctx).is_empty(),
            "a recorded graph projection clears the pending warning"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn fresh_pin_graph_reads_admit_source_namespace() {
        let database_url = crate::test_env::postgres_test_database_url("fresh pin graph source");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let (ctx, source, _source_cleanup, _overlay_cleanup) =
            pinned_fixture(&mut conn, &database_url);
        // Point the source selector back at the pinned hash: the pin is fresh, its
        // graph projection has not landed, and the source still serves that version.
        seed_version(&mut conn, &source, "hash-h", true);
        let pinned_key = format!("{FILE}|hash-h");

        let namespaces =
            crate::visibility::graph_versions::admitted_namespaces(&ctx).expect("admit");
        let source_namespace = namespaces
            .iter()
            .find(|namespace| namespace.project_id == source)
            .unwrap_or_else(|| panic!("source namespace must serve the fresh pin: {namespaces:?}"));
        assert_eq!(
            source_namespace.versions.keys,
            vec![pinned_key.clone()],
            "a fresh pin is admitted from the source namespace at its pinned hash"
        );
        assert!(
            namespaces
                .iter()
                .filter(|namespace| namespace.project_id == ctx.project_id)
                .all(|namespace| !namespace.versions.keys.contains(&pinned_key)),
            "an unprojected pin is not admitted in the child namespace: {namespaces:?}"
        );
        assert!(
            crate::visibility::graph_versions::pending_projection_warnings(&ctx).is_empty(),
            "a source that still names the pinned hash leaves nothing pending"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires PostgreSQL test database URL"
    )]
    fn projected_pin_keeps_pinned_version_after_source_moves() {
        let database_url = crate::test_env::postgres_test_database_url("projected pin graph");
        let mut conn = db::connect_readwrite(&database_url).expect("connect");
        let (ctx, _source, _source_cleanup, _overlay_cleanup) =
            pinned_fixture(&mut conn, &database_url);
        let overlay_uuid = db::id_param(&ctx.project_id).expect("child UUID");
        conn.execute(
            "UPDATE code_overlay_pins SET graph_projected = true WHERE overlay_project_id = $1",
            &[&overlay_uuid],
        )
        .expect("record graph projection");

        // The fixture's source selector already moved to hash-k. The projected pin is
        // served from the child namespace at its pinned hash, never at the new one.
        let namespaces =
            crate::visibility::graph_versions::admitted_namespaces(&ctx).expect("admit");
        let child = namespaces
            .iter()
            .find(|namespace| namespace.project_id == ctx.project_id)
            .expect("child namespace");
        assert!(
            child.versions.keys.contains(&format!("{FILE}|hash-h")),
            "a projected pin keeps serving its pinned version: {child:?}"
        );
        assert!(
            !child.versions.keys.contains(&format!("{FILE}|hash-k")),
            "the moved source version stays out of the child namespace: {child:?}"
        );
    }
}
