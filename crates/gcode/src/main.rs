use clap::{ArgGroup, Parser, Subcommand};
use gobby_code::{commands, config, freshness, output, setup};

#[derive(Parser)]
#[command(name = "gcode", version, about = "Fast code index CLI for Gobby")]
struct Cli {
    /// Override project root (default: detect from cwd)
    #[arg(long, global = true)]
    project: Option<String>,

    /// Output format
    #[arg(long, global = true, default_value = "json")]
    format: output::Format,

    /// Suppress warnings
    #[arg(long, global = true)]
    quiet: bool,

    /// Enable verbose output
    #[arg(long, global = true)]
    verbose: bool,

    /// Skip read-time freshness checks
    #[arg(long, global = true)]
    no_freshness: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    // ── Project Setup ────────────────────────────────────────────────
    /// Initialize project context (.gobby/gcode.json)
    Init,
    /// Explicitly create gcode-owned standalone database objects
    Setup {
        /// Required opt-in for setup writes in v1
        #[arg(long, required = true)]
        standalone: bool,
        /// PostgreSQL database URL to set up
        #[arg(long)]
        database_url: Option<String>,
        /// Skip Docker service provisioning
        #[arg(long)]
        no_services: bool,
        /// Drop/recreate gcode-owned code-index state and clear code-index projections
        #[arg(long)]
        overwrite_code_index: bool,
        /// PostgreSQL schema namespace for gcode-owned objects
        #[arg(long, default_value = "public")]
        schema: String,
        /// Embedding provider to store in gcore.yaml
        #[arg(long)]
        embedding_provider: Option<String>,
        /// OpenAI-compatible embedding API base URL
        #[arg(long)]
        embedding_api_base: Option<String>,
        /// Embedding model name
        #[arg(long)]
        embedding_model: Option<String>,
        /// Embedding vector dimension
        #[arg(long)]
        embedding_vector_dim: Option<usize>,
        /// Environment variable name containing the embedding API key
        #[arg(long)]
        embedding_api_key_env: Option<String>,
        /// FalkorDB host to store in gcore.yaml
        #[arg(long)]
        falkordb_host: Option<String>,
        /// FalkorDB port to store in gcore.yaml
        #[arg(long)]
        falkordb_port: Option<u16>,
        /// FalkorDB password for Docker provisioning or external config
        #[arg(long)]
        falkordb_password: Option<String>,
        /// Qdrant URL to store in gcore.yaml when services are not provisioned
        #[arg(long)]
        qdrant_url: Option<String>,
    },
    /// Index a directory (full or incremental). Writes symbols, files, and chunks to PostgreSQL hub
    Index {
        /// Path to index (default: project root)
        path: Option<String>,
        /// Index only specific files
        #[arg(long, num_args = 1..)]
        files: Option<Vec<String>>,
        /// Force full reindex (skip incremental hash check)
        #[arg(long)]
        full: bool,
        /// Fail C/C++ indexing when clangd or compile_commands.json semantics are unavailable
        #[arg(long)]
        require_cpp_semantics: bool,
        /// Synchronously update graph and vector projections after PostgreSQL indexing
        #[arg(long)]
        sync_projections: bool,
    },
    /// Show project index status
    Status,
    /// Clear index and force re-index
    Invalidate {
        /// Skip confirmation prompt
        #[arg(long)]
        force: bool,
    },
    /// Manage and inspect the code-index graph projection [requires FalkorDB]
    Graph {
        #[command(subcommand)]
        command: GraphCommand,
    },
    /// Manage the code-symbol vector projection [requires Qdrant and embeddings]
    Vector {
        #[command(subcommand)]
        command: VectorCommand,
    },

    // ── Search (works in all modes) ──────────────────────────────────
    /// Hybrid search: pg_search BM25 + optional semantic (Qdrant) + optional graph boost (FalkorDB)
    Search {
        query: String,
        /// Optional file paths or globs to filter results
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by symbol kind
        #[arg(long)]
        kind: Option<String>,
        /// Filter by source language (e.g. rust, python, css)
        #[arg(long)]
        language: Option<String>,
    },
    /// Exact-first symbol/name search with deterministic ranking
    SearchSymbol {
        query: String,
        /// Optional file paths or globs to filter results
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by symbol kind
        #[arg(long)]
        kind: Option<String>,
        /// Filter by source language (e.g. rust, python, css)
        #[arg(long)]
        language: Option<String>,
        /// Include FalkorDB graph neighbors in the exact-first ranking [requires Gobby]
        #[arg(long)]
        with_graph: bool,
    },
    /// pg_search BM25 search on symbol metadata (names, signatures, docstrings)
    SearchText {
        query: String,
        /// Optional file paths or globs to filter results
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by source language (e.g. rust, python, css)
        #[arg(long)]
        language: Option<String>,
    },
    /// pg_search BM25 search on file content chunks
    SearchContent {
        query: String,
        /// Optional file paths or globs to filter results
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by source language (e.g. rust, python, css)
        #[arg(long)]
        language: Option<String>,
    },

    // ── Symbol Retrieval (works in all modes) ────────────────────────
    /// Hierarchical symbol tree for a file
    Outline { file: String },
    /// Fetch symbol source code by ID (byte-offset read)
    Symbol { id: String },
    /// Batch retrieve symbols by ID
    Symbols { ids: Vec<String> },
    /// List distinct symbol kinds in the index
    Kinds,
    /// File tree with symbol counts
    Tree,

    // ── Dependency Graph (requires Gobby) ──────────────────────────────
    /// Find callers of a symbol query, resolved to a canonical symbol ID [requires Gobby]
    Callers {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Find incoming call usages of a symbol query, resolved to a canonical symbol ID [requires Gobby]
    Usages {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Show import graph for a file [requires Gobby]
    Imports { file: String },
    /// Transitive impact analysis for a symbol query, resolved to a canonical symbol ID [requires Gobby]
    BlastRadius {
        /// Symbol query
        target: String,
        #[arg(long, default_value = "3")]
        depth: usize,
    },

    // ── Project Management ───────────────────────────────────────────
    /// Directory-grouped project stats
    RepoOutline,
    /// List indexed projects
    Projects,
    /// Remove stale projects (dead paths, invalid entries)
    Prune {
        /// Skip confirmation prompt
        #[arg(long)]
        force: bool,
    },
}

#[derive(Subcommand)]
enum GraphCommand {
    /// Sync one indexed file into the code-index graph projection
    SyncFile {
        /// Indexed file path to sync
        #[arg(long)]
        file: String,
    },
    /// Clear the current project's code-index graph projection
    Clear,
    /// Rebuild the current project's code-index graph projection from PostgreSQL facts
    Rebuild,
    /// Generate a project graph report
    Report {
        /// Number of top hotspot and target rows to include
        #[arg(long, default_value = "10")]
        top_n: usize,
    },
    /// Show an overview graph for the current project
    Overview {
        /// Maximum files to include
        #[arg(long, default_value = "100")]
        limit: usize,
    },
    /// Show graph nodes and links for one indexed file
    File {
        /// Indexed file path to inspect
        #[arg(long)]
        file: String,
    },
    /// Show graph neighbors for one symbol ID
    Neighbors {
        /// Symbol ID to inspect
        #[arg(long)]
        symbol_id: String,
        #[arg(long, default_value = "100")]
        limit: usize,
    },
    /// Show transitive graph impact for a symbol ID or file path
    #[command(group(
        ArgGroup::new("target")
            .required(true)
            .args(["symbol_id", "file"])
    ))]
    BlastRadius {
        /// Symbol ID to inspect
        #[arg(long)]
        symbol_id: Option<String>,
        /// Indexed file path to inspect
        #[arg(long)]
        file: Option<String>,
        #[arg(long, default_value = "3")]
        depth: usize,
        #[arg(long, default_value = "100")]
        limit: usize,
    },
}

#[derive(Subcommand)]
enum VectorCommand {
    /// Sync one indexed file into the code-symbol vector projection
    SyncFile {
        /// Indexed file path to sync
        #[arg(long)]
        file: String,
    },
    /// Clear the current project's code-symbol vector projection
    Clear,
    /// Rebuild the current project's code-symbol vector projection from PostgreSQL facts
    Rebuild,
}

fn ensure_project_fresh(ctx: &config::Context, disabled: bool) -> anyhow::Result<()> {
    if !disabled {
        freshness::ensure_fresh(ctx, freshness::FreshnessScope::Project)?;
    }
    Ok(())
}

fn ensure_files_fresh(
    ctx: &config::Context,
    disabled: bool,
    files: Vec<std::path::PathBuf>,
) -> anyhow::Result<()> {
    if !disabled {
        freshness::ensure_fresh(ctx, freshness::FreshnessScope::Files(files))?;
    }
    Ok(())
}

fn ensure_symbol_fresh(ctx: &config::Context, disabled: bool, id: &str) -> anyhow::Result<()> {
    if !disabled {
        freshness::ensure_symbol_fresh(ctx, id)?;
    }
    Ok(())
}

fn dispatch_early_command<F>(cli: &Cli, setup_runner: F) -> anyhow::Result<bool>
where
    F: FnOnce(setup::StandaloneSetupRequest, output::Format, bool) -> anyhow::Result<()>,
{
    match &cli.command {
        Command::Init => {
            let root = match &cli.project {
                Some(p) => std::path::PathBuf::from(p).canonicalize()?,
                None => config::detect_project_root()?,
            };
            commands::init::run(&root, cli.format, cli.quiet)?;
            Ok(true)
        }
        Command::Setup {
            standalone,
            database_url,
            no_services,
            overwrite_code_index,
            schema,
            embedding_provider,
            embedding_api_base,
            embedding_model,
            embedding_vector_dim,
            embedding_api_key_env,
            falkordb_host,
            falkordb_port,
            falkordb_password,
            qdrant_url,
        } => {
            let mut request = setup::StandaloneSetupRequest::new(
                *standalone,
                database_url.clone(),
                Some(schema.clone()),
            );
            request.no_services = *no_services;
            request.overwrite_code_index = *overwrite_code_index;
            request.embedding_provider = embedding_provider.clone();
            request.embedding_api_base = embedding_api_base.clone();
            request.embedding_model = embedding_model.clone();
            request.embedding_vector_dim = *embedding_vector_dim;
            request.embedding_api_key_env = embedding_api_key_env.clone();
            request.falkordb_host = falkordb_host.clone();
            request.falkordb_port = *falkordb_port;
            request.falkordb_password = falkordb_password.clone();
            request.qdrant_url = qdrant_url.clone();
            setup_runner(request, cli.format, cli.quiet)?;
            Ok(true)
        }
        Command::Projects => {
            commands::status::projects(cli.format)?;
            Ok(true)
        }
        Command::Prune { force } => {
            commands::status::prune(*force)?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Commands that must run before Context::resolve() (work on uninitialized projects)
    if dispatch_early_command(&cli, commands::setup::run)? {
        return Ok(());
    }

    let ctx = config::Context::resolve(cli.project.as_deref(), cli.quiet)?;

    match cli.command {
        Command::Init | Command::Setup { .. } | Command::Projects | Command::Prune { .. } => {
            unreachable!()
        }
        Command::Index {
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
        } => commands::index::run(
            &ctx,
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
            cli.format,
        ),
        Command::Status => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::status::run(&ctx, cli.format)
        }
        Command::Invalidate { force } => commands::status::invalidate(&ctx, force),
        Command::Graph {
            command: GraphCommand::SyncFile { file },
        } => {
            ensure_files_fresh(
                &ctx,
                cli.no_freshness,
                vec![std::path::PathBuf::from(&file)],
            )?;
            commands::graph::sync_file(&ctx, &file, cli.format)
        }
        Command::Graph {
            command: GraphCommand::Clear,
        } => commands::graph::clear(&ctx, cli.format),
        Command::Graph {
            command: GraphCommand::Rebuild,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::rebuild(&ctx, cli.format)
        }
        Command::Graph {
            command: GraphCommand::Report { top_n },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::report(&ctx, top_n, cli.format)
        }
        Command::Vector {
            command: VectorCommand::SyncFile { file },
        } => {
            ensure_files_fresh(
                &ctx,
                cli.no_freshness,
                vec![std::path::PathBuf::from(&file)],
            )?;
            commands::vector::sync_file(&ctx, &file, cli.format)
        }
        Command::Vector {
            command: VectorCommand::Clear,
        } => commands::vector::clear(&ctx, cli.format),
        Command::Vector {
            command: VectorCommand::Rebuild,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::vector::rebuild(&ctx, cli.format)
        }
        Command::Graph {
            command: GraphCommand::Overview { limit },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::overview(&ctx, limit, cli.format)
        }
        Command::Graph {
            command: GraphCommand::File { file },
        } => {
            ensure_files_fresh(
                &ctx,
                cli.no_freshness,
                vec![std::path::PathBuf::from(&file)],
            )?;
            commands::graph::file(&ctx, &file, cli.format)
        }
        Command::Graph {
            command: GraphCommand::Neighbors { symbol_id, limit },
        } => {
            ensure_symbol_fresh(&ctx, cli.no_freshness, &symbol_id)?;
            commands::graph::neighbors(&ctx, &symbol_id, limit, cli.format)
        }
        Command::Graph {
            command:
                GraphCommand::BlastRadius {
                    symbol_id,
                    file,
                    depth,
                    limit,
                },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::graph_blast_radius(
                &ctx,
                symbol_id.as_deref(),
                file.as_deref(),
                depth,
                limit,
                cli.format,
            )
        }

        Command::Search {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search(
                &ctx,
                &query,
                commands::search::SearchOptions {
                    limit,
                    offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format: cli.format,
                    with_graph: true,
                },
            )
        }
        Command::SearchSymbol {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            with_graph,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_symbol(
                &ctx,
                &query,
                commands::search::SearchOptions {
                    limit,
                    offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format: cli.format,
                    with_graph,
                },
            )
        }
        Command::SearchText {
            query,
            paths,
            limit,
            offset,
            language,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_text(
                &ctx,
                &query,
                limit,
                offset,
                language.as_deref(),
                &paths,
                cli.format,
            )
        }
        Command::SearchContent {
            query,
            paths,
            limit,
            offset,
            language,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_content(
                &ctx,
                &query,
                limit,
                offset,
                language.as_deref(),
                &paths,
                cli.format,
            )
        }

        Command::Outline { file } => {
            ensure_files_fresh(
                &ctx,
                cli.no_freshness,
                vec![std::path::PathBuf::from(&file)],
            )?;
            commands::symbols::outline(&ctx, &file, cli.format, cli.verbose)
        }
        Command::Symbol { id } => {
            ensure_symbol_fresh(&ctx, cli.no_freshness, &id)?;
            commands::symbols::symbol(&ctx, &id, cli.format)
        }
        Command::Symbols { ids } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::symbols(&ctx, &ids, cli.format)
        }
        Command::Kinds => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::kinds(&ctx, cli.format)
        }
        Command::Tree => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::tree(&ctx, cli.format)
        }

        Command::Callers {
            symbol_name,
            limit,
            offset,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::callers(&ctx, &symbol_name, limit, offset, cli.format)
        }
        Command::Usages {
            symbol_name,
            limit,
            offset,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::usages(&ctx, &symbol_name, limit, offset, cli.format)
        }
        Command::Imports { file } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::imports(&ctx, &file, cli.format)
        }
        Command::BlastRadius { target, depth } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::blast_radius(&ctx, &target, depth, cli.format)
        }

        Command::RepoOutline => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::status::repo_outline(&ctx, cli.format)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

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
        assert!(matches!(cli.format, output::Format::Text));
        match cli.command {
            Command::Graph {
                command: GraphCommand::SyncFile { file },
            } => assert_eq!(file, "src/lib.rs"),
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
        assert!(matches!(cli.format, output::Format::Text));
        match cli.command {
            Command::Vector {
                command: VectorCommand::SyncFile { file },
            } => assert_eq!(file, "src/lib.rs"),
            _ => panic!("expected vector sync-file command"),
        }

        let cli = Cli::try_parse_from(["gcode", "graph", "clear"]).expect("graph clear parses");
        assert!(matches!(
            cli.command,
            Command::Graph {
                command: GraphCommand::Clear
            }
        ));

        let cli = Cli::try_parse_from(["gcode", "graph", "rebuild"]).expect("graph rebuild parses");
        assert!(matches!(
            cli.command,
            Command::Graph {
                command: GraphCommand::Rebuild
            }
        ));

        let cli = Cli::try_parse_from(["gcode", "graph", "report"]).expect("graph report parses");
        assert!(matches!(
            cli.command,
            Command::Graph {
                command: GraphCommand::Report { top_n: 10 }
            }
        ));

        let cli =
            Cli::try_parse_from(["gcode", "graph", "overview"]).expect("graph overview parses");
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

        let cli =
            Cli::try_parse_from(["gcode", "vector", "rebuild"]).expect("vector rebuild parses");
        assert!(matches!(
            cli.command,
            Command::Vector {
                command: VectorCommand::Rebuild
            }
        ));

        let cli =
            Cli::try_parse_from(["gcode", "index", "--sync-projections"]).expect("index parses");
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
        assert!(matches!(cli.format, output::Format::Text));
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
    fn test_parse_index_require_cpp_semantics() {
        let cli = Cli::try_parse_from(["gcode", "index", "--require-cpp-semantics"])
            .expect("index parses");

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
            } => {
                assert_eq!(symbol_name, "DatabasePool");
                assert_eq!(limit, 10);
                assert_eq!(offset, 0);
            }
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
    fn test_parse_blast_radius_remains_top_level() {
        let cli = Cli::try_parse_from(["gcode", "blast-radius", "handleAuth"])
            .expect("blast-radius parses");

        match cli.command {
            Command::BlastRadius { target, depth } => {
                assert_eq!(target, "handleAuth");
                assert_eq!(depth, 3);
            }
            _ => panic!("expected top-level blast-radius command"),
        }
    }

    #[test]
    fn test_parse_search_symbol_filters() {
        let cli = Cli::try_parse_from([
            "gcode",
            "search-symbol",
            "outline",
            "crates/gcode/src",
            "--kind",
            "function",
            "--language",
            "rust",
        ])
        .expect("search-symbol parses");

        match cli.command {
            Command::SearchSymbol {
                query,
                paths,
                limit,
                offset,
                kind,
                language,
                with_graph,
            } => {
                assert_eq!(query, "outline");
                assert_eq!(paths, vec!["crates/gcode/src"]);
                assert_eq!(limit, 10);
                assert_eq!(offset, 0);
                assert_eq!(kind.as_deref(), Some("function"));
                assert_eq!(language.as_deref(), Some("rust"));
                assert!(!with_graph);
            }
            _ => panic!("expected search-symbol command"),
        }
    }

    #[test]
    fn test_parse_search_symbol_with_graph() {
        let cli = Cli::try_parse_from(["gcode", "search-symbol", "outline", "--with-graph"])
            .expect("search-symbol --with-graph parses");

        match cli.command {
            Command::SearchSymbol { with_graph, .. } => assert!(with_graph),
            _ => panic!("expected search-symbol command"),
        }
    }

    #[test]
    fn test_parse_search_language_filters() {
        let cli = Cli::try_parse_from(["gcode", "search", "outline", "--language", "rust"])
            .expect("search parses");

        match cli.command {
            Command::Search { language, .. } => {
                assert_eq!(language.as_deref(), Some("rust"));
            }
            _ => panic!("expected search command"),
        }
    }

    #[test]
    fn test_parse_search_positional_paths() {
        let cli = Cli::try_parse_from([
            "gcode",
            "search",
            "outline",
            "src/gobby",
            "tests",
            "--limit",
            "20",
        ])
        .expect("search parses");

        match cli.command {
            Command::Search { paths, limit, .. } => {
                assert_eq!(paths, vec!["src/gobby", "tests"]);
                assert_eq!(limit, 20);
            }
            _ => panic!("expected search command"),
        }
    }

    #[test]
    fn test_parse_search_text_positional_path_after_option() {
        let cli = Cli::try_parse_from([
            "gcode",
            "search-text",
            "outline",
            "--limit",
            "5",
            "src/gobby",
        ])
        .expect("search-text parses");

        match cli.command {
            Command::SearchText { paths, limit, .. } => {
                assert_eq!(paths, vec!["src/gobby"]);
                assert_eq!(limit, 5);
            }
            _ => panic!("expected search-text command"),
        }
    }

    #[test]
    fn test_parse_search_content_positional_paths_and_format() {
        let cli = Cli::try_parse_from([
            "gcode",
            "search-content",
            "QUERY",
            "src/gobby",
            "tests",
            "--limit",
            "20",
            "--format",
            "text",
        ])
        .expect("search-content parses");

        assert!(matches!(cli.format, output::Format::Text));
        match cli.command {
            Command::SearchContent { paths, limit, .. } => {
                assert_eq!(paths, vec!["src/gobby", "tests"]);
                assert_eq!(limit, 20);
            }
            _ => panic!("expected search-content command"),
        }
    }

    #[test]
    fn test_parse_search_content_positional_path_after_option() {
        let cli = Cli::try_parse_from([
            "gcode",
            "search-content",
            "QUERY",
            "--limit",
            "5",
            "src/gobby",
        ])
        .expect("search-content parses");

        match cli.command {
            Command::SearchContent { paths, limit, .. } => {
                assert_eq!(paths, vec!["src/gobby"]);
                assert_eq!(limit, 5);
            }
            _ => panic!("expected search-content command"),
        }
    }

    #[test]
    fn test_parse_search_path_flag_rejected() {
        for command in ["search", "search-symbol", "search-text", "search-content"] {
            let err = match Cli::try_parse_from([
                "gcode",
                command,
                "QUERY",
                "--path",
                "crates/gcode/src",
            ]) {
                Ok(_) => panic!("--path should be rejected for {command}"),
                Err(err) => err,
            };

            assert_eq!(err.kind(), clap::error::ErrorKind::UnknownArgument);
            assert!(
                err.to_string().contains("--path"),
                "unexpected error for {command}: {err}"
            );
        }
    }

    #[test]
    fn test_parse_no_freshness_global_flag() {
        let cli = Cli::try_parse_from(["gcode", "--no-freshness", "tree"]).expect("tree parses");

        assert!(cli.no_freshness);
        assert!(matches!(cli.command, Command::Tree));
    }

    #[test]
    fn parse_setup_standalone() {
        let cli = Cli::try_parse_from([
            "gcode",
            "setup",
            "--standalone",
            "--database-url",
            "postgresql://localhost/gcode",
            "--no-services",
            "--overwrite-code-index",
            "--embedding-provider",
            "ollama",
            "--embedding-vector-dim",
            "768",
            "--falkordb-password",
            "secret-pass",
        ])
        .expect("setup parses");

        match cli.command {
            Command::Setup {
                standalone,
                database_url,
                no_services,
                overwrite_code_index,
                schema,
                embedding_provider,
                embedding_vector_dim,
                falkordb_password,
                ..
            } => {
                assert!(standalone);
                assert_eq!(
                    database_url.as_deref(),
                    Some("postgresql://localhost/gcode")
                );
                assert!(no_services);
                assert!(overwrite_code_index);
                assert_eq!(schema, "public");
                assert_eq!(embedding_provider.as_deref(), Some("ollama"));
                assert_eq!(embedding_vector_dim, Some(768));
                assert_eq!(falkordb_password.as_deref(), Some("secret-pass"));
            }
            _ => panic!("expected setup command"),
        }
    }

    #[test]
    fn setup_runs_before_context_resolve() {
        let project = tempfile::tempdir().expect("temp project");
        let cli = Cli::try_parse_from([
            "gcode",
            "--project",
            project.path().to_str().expect("utf8 temp path"),
            "setup",
            "--standalone",
            "--database-url",
            "postgresql://localhost/gcode",
            "--overwrite-code-index",
            "--embedding-api-base",
            "https://embeddings.example/v1",
        ])
        .expect("setup parses");

        let mut called = false;
        let dispatched = dispatch_early_command(&cli, |request, _format, _quiet| {
            called = true;
            assert!(request.standalone);
            assert_eq!(
                request.database_url.as_deref(),
                Some("postgresql://localhost/gcode")
            );
            assert_eq!(request.schema, "public");
            assert!(request.overwrite_code_index);
            assert_eq!(
                request.embedding_api_base.as_deref(),
                Some("https://embeddings.example/v1")
            );
            Ok(())
        })
        .expect("early dispatch succeeds without resolving project context");

        assert!(dispatched);
        assert!(called);
    }
}
