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
            command: VectorCommand::Clear
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
