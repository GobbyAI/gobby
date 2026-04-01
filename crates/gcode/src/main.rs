mod commands;
mod config;
mod db;
mod index;
mod models;
mod neo4j;
mod output;
mod progress;
mod project;
mod savings;
mod schema;
mod search;
mod secrets;
mod skill;

use clap::{Parser, Subcommand};

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

    /// Enable GGML/llama.cpp debug output (suppressed by default)
    #[arg(long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Initialize project context (.gobby/gcode.json)
    Init,
    /// Index a directory (full or incremental)
    Index {
        /// Path to index (default: project root)
        path: Option<String>,
        /// Index only specific files
        #[arg(long, num_args = 1..)]
        files: Option<Vec<String>>,
        /// Force full reindex (skip incremental hash check)
        #[arg(long)]
        full: bool,
    },
    /// Show project index status
    Status,
    /// Clear index and force re-index
    Invalidate {
        /// Skip confirmation prompt
        #[arg(long)]
        force: bool,
    },

    /// Hybrid search: FTS5 + semantic + graph boost
    Search {
        query: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by symbol kind
        #[arg(long)]
        kind: Option<String>,
    },
    /// FTS5 search on symbol metadata (names, signatures, docstrings)
    SearchText {
        query: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// FTS5 search on file content chunks
    SearchContent {
        query: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },

    /// Hierarchical symbol tree for a file
    Outline { file: String },
    /// Fetch symbol source code by ID (byte-offset read)
    Symbol { id: String },
    /// Batch retrieve symbols by ID
    Symbols { ids: Vec<String> },
    /// File tree with symbol counts
    Tree,

    /// Find callers of a symbol
    Callers {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Find all usages of a symbol (calls + imports)
    Usages {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Show import graph for a file
    Imports { file: String },
    /// Transitive impact analysis
    BlastRadius {
        /// Symbol name or file path
        target: String,
        #[arg(long, default_value = "3")]
        depth: usize,
    },

    /// Return cached LLM summary for a symbol
    Summary { symbol_id: String },
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

fn main() -> anyhow::Result<()> {
    // Force-enable Metal tensor API on all Apple Silicon. Must be set BEFORE
    // any threads are spawned to avoid Undefined Behavior (segfaults) when
    // other threads read the environment concurrently.
    #[cfg(target_os = "macos")]
    unsafe {
        std::env::set_var("GGML_METAL_TENSOR_ENABLE", "1")
    };

    let cli = Cli::parse();
    search::semantic::configure_logging(cli.verbose);

    // Commands that must run before Context::resolve() (work on uninitialized projects)
    match &cli.command {
        Command::Init => {
            let root = match &cli.project {
                Some(p) => std::path::PathBuf::from(p).canonicalize()?,
                None => config::detect_project_root()?,
            };
            return commands::init::run(&root, cli.format, cli.quiet);
        }
        Command::Projects => {
            return commands::status::projects(cli.format);
        }
        Command::Prune { force } => {
            return commands::status::prune(*force);
        }
        _ => {}
    }

    let ctx = config::Context::resolve(cli.project.as_deref(), cli.quiet)?;

    // Drop embedding model before exit to avoid Metal residency set assertion
    // crash during static destructor teardown (ggml-metal-device.m:612).
    let result = match cli.command {
        Command::Init | Command::Projects | Command::Prune { .. } => unreachable!(),
        Command::Index { path, files, full } => commands::index::run(&ctx, path, files, full),
        Command::Status => commands::status::run(&ctx, cli.format),
        Command::Invalidate { force } => commands::status::invalidate(&ctx, force),

        Command::Search {
            query,
            limit,
            offset,
            kind,
        } => commands::search::search(
            &ctx,
            &query,
            limit,
            offset,
            kind.as_deref(),
            cli.format,
            cli.verbose,
        ),
        Command::SearchText {
            query,
            limit,
            offset,
        } => commands::search::search_text(&ctx, &query, limit, offset, cli.format),
        Command::SearchContent {
            query,
            limit,
            offset,
        } => commands::search::search_content(&ctx, &query, limit, offset, cli.format),

        Command::Outline { file } => {
            commands::symbols::outline(&ctx, &file, cli.format, cli.verbose)
        }
        Command::Symbol { id } => commands::symbols::symbol(&ctx, &id, cli.format),
        Command::Symbols { ids } => commands::symbols::symbols(&ctx, &ids, cli.format),
        Command::Tree => commands::symbols::tree(&ctx, cli.format),

        Command::Callers {
            symbol_name,
            limit,
            offset,
        } => commands::graph::callers(&ctx, &symbol_name, limit, offset, cli.format),
        Command::Usages {
            symbol_name,
            limit,
            offset,
        } => commands::graph::usages(&ctx, &symbol_name, limit, offset, cli.format),
        Command::Imports { file } => commands::graph::imports(&ctx, &file, cli.format),
        Command::BlastRadius { target, depth } => {
            commands::graph::blast_radius(&ctx, &target, depth, cli.format)
        }

        Command::Summary { symbol_id } => commands::summary::summary(&ctx, &symbol_id, cli.format),
        Command::RepoOutline => commands::summary::repo_outline(&ctx, cli.format),
    };

    search::semantic::shutdown();
    result
}
