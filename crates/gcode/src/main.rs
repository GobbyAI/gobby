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
        #[arg(long, default_value = "20")]
        limit: usize,
        /// Filter by symbol kind
        #[arg(long)]
        kind: Option<String>,
    },
    /// FTS5 search on symbol metadata (names, signatures, docstrings)
    SearchText {
        query: String,
        #[arg(long, default_value = "20")]
        limit: usize,
    },
    /// FTS5 search on file content chunks
    SearchContent {
        query: String,
        #[arg(long, default_value = "20")]
        limit: usize,
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
        #[arg(long, default_value = "20")]
        limit: usize,
    },
    /// Find all usages of a symbol (calls + imports)
    Usages {
        symbol_name: String,
        #[arg(long, default_value = "20")]
        limit: usize,
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
}

fn main() -> anyhow::Result<()> {
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
        _ => {}
    }

    let ctx = config::Context::resolve(cli.project.as_deref(), cli.quiet)?;

    // Drop embedding model before exit to avoid Metal residency set assertion
    // crash during static destructor teardown (ggml-metal-device.m:612).
    let result = match cli.command {
        Command::Init | Command::Projects => unreachable!(),
        Command::Index { path, files } => commands::index::run(&ctx, path, files),
        Command::Status => commands::status::run(&ctx, cli.format),
        Command::Invalidate { force } => commands::status::invalidate(&ctx, force),

        Command::Search { query, limit, kind } => {
            commands::search::search(&ctx, &query, limit, kind.as_deref(), cli.format)
        }
        Command::SearchText { query, limit } => {
            commands::search::search_text(&ctx, &query, limit, cli.format)
        }
        Command::SearchContent { query, limit } => {
            commands::search::search_content(&ctx, &query, limit, cli.format)
        }

        Command::Outline { file } => commands::symbols::outline(&ctx, &file, cli.format),
        Command::Symbol { id } => commands::symbols::symbol(&ctx, &id, cli.format),
        Command::Symbols { ids } => commands::symbols::symbols(&ctx, &ids, cli.format),
        Command::Tree => commands::symbols::tree(&ctx, cli.format),

        Command::Callers { symbol_name, limit } => {
            commands::graph::callers(&ctx, &symbol_name, limit, cli.format)
        }
        Command::Usages { symbol_name, limit } => {
            commands::graph::usages(&ctx, &symbol_name, limit, cli.format)
        }
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
