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

    /// Enable verbose output
    #[arg(long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    // ── Project Setup ────────────────────────────────────────────────
    /// Initialize project context (.gobby/gcode.json)
    Init,
    /// Index a directory (full or incremental). Writes symbols, files, and chunks to SQLite
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
    /// Manage code-index graph lifecycle through the Gobby daemon; read graph queries remain top-level [requires Gobby]
    Graph {
        #[command(subcommand)]
        command: GraphCommand,
    },

    // ── Search (works in all modes) ──────────────────────────────────
    /// Hybrid search: FTS5 + optional semantic (Qdrant) + optional graph boost (Neo4j)
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
        /// Filter by file path glob (e.g. "src/**/*.rs", "*.py")
        #[arg(long)]
        path: Option<String>,
    },
    /// FTS5 search on symbol metadata (names, signatures, docstrings)
    SearchText {
        query: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by file path glob (e.g. "src/**/*.rs", "*.py")
        #[arg(long)]
        path: Option<String>,
    },
    /// FTS5 search on file content chunks
    SearchContent {
        query: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Filter by file path glob (e.g. "src/**/*.rs", "*.py")
        #[arg(long)]
        path: Option<String>,
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
    /// Clear the current project's code-index graph projection via the Gobby daemon [requires Gobby]
    Clear,
    /// Rebuild the current project's code-index graph projection via the Gobby daemon [requires Gobby]
    Rebuild,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

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

    match cli.command {
        Command::Init | Command::Projects | Command::Prune { .. } => unreachable!(),
        Command::Index { path, files, full } => commands::index::run(&ctx, path, files, full),
        Command::Status => commands::status::run(&ctx, cli.format),
        Command::Invalidate { force } => commands::status::invalidate(&ctx, force),
        Command::Graph {
            command: GraphCommand::Clear,
        } => commands::graph::clear(&ctx, cli.format),
        Command::Graph {
            command: GraphCommand::Rebuild,
        } => commands::graph::rebuild(&ctx, cli.format),

        Command::Search {
            query,
            limit,
            offset,
            kind,
            path,
        } => commands::search::search(
            &ctx,
            &query,
            limit,
            offset,
            kind.as_deref(),
            path.as_deref(),
            cli.format,
        ),
        Command::SearchText {
            query,
            limit,
            offset,
            path,
        } => {
            commands::search::search_text(&ctx, &query, limit, offset, path.as_deref(), cli.format)
        }
        Command::SearchContent {
            query,
            limit,
            offset,
            path,
        } => commands::search::search_content(
            &ctx,
            &query,
            limit,
            offset,
            path.as_deref(),
            cli.format,
        ),

        Command::Outline { file } => {
            commands::symbols::outline(&ctx, &file, cli.format, cli.verbose)
        }
        Command::Symbol { id } => commands::symbols::symbol(&ctx, &id, cli.format),
        Command::Symbols { ids } => commands::symbols::symbols(&ctx, &ids, cli.format),
        Command::Kinds => commands::symbols::kinds(&ctx, cli.format),
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

        Command::RepoOutline => commands::status::repo_outline(&ctx, cli.format),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn test_parse_graph_clear() {
        let cli = Cli::try_parse_from(["gcode", "graph", "clear"]).expect("graph clear parses");

        assert!(matches!(
            cli.command,
            Command::Graph {
                command: GraphCommand::Clear
            }
        ));
    }

    #[test]
    fn test_parse_graph_rebuild() {
        let cli = Cli::try_parse_from(["gcode", "graph", "rebuild"]).expect("graph rebuild parses");

        assert!(matches!(
            cli.command,
            Command::Graph {
                command: GraphCommand::Rebuild
            }
        ));
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
}
