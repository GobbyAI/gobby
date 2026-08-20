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
            command: VectorCommand::Clear { project_id: None }
        }
    ));
    let cli = Cli::try_parse_from([
        "gcode",
        "vector",
        "clear",
        "--project-id",
        "11111111-1111-1111-1111-111111111111",
    ])
    .expect("vector clear --project-id parses");
    assert!(matches!(
        cli.command,
        Command::Vector {
            command: VectorCommand::Clear {
                project_id: Some(_)
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
    let error = match Cli::try_parse_from(["gcode", "graph", "view", "--view", "pdg", "Derived"]) {
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
fn graph_view_depth_domain() {
    let error = match Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "seed", "--depth", "0",
    ]) {
        Ok(_) => panic!("--depth 0 must fail clap parse"),
        Err(error) => error,
    };
    assert_eq!(error.kind(), clap::error::ErrorKind::ValueValidation);

    let error = match Cli::try_parse_from([
        "gcode", "graph", "view", "--view", "fcg", "seed", "--depth", "17",
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

    let fcg = Cli::try_parse_from(["gcode", "graph", "view", "--view", "fcg", "seed"])
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

    let mcg = Cli::try_parse_from(["gcode", "graph", "view", "--view", "mcg", "src/lib.rs"])
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
    let fcg = Cli::try_parse_from(["gcode", "graph", "view", "--view", "fcg", "seed"])
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

    let mcg = Cli::try_parse_from(["gcode", "graph", "view", "--view", "mcg", "src/a.py"])
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
