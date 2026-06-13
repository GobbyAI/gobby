use clap::{ArgGroup, Parser, Subcommand, ValueEnum};
use gobby_code::output;
use gobby_core::config::AiRouting;

const DEFAULT_CODEWIKI_GRAPH_EDGE_LIMIT: usize = 5000;
const MAX_POSITIVE_USIZE_ARG: usize = 1_000_000_000;
const MAX_GREP_MAX_COUNT: usize = 10_000;

#[derive(Parser)]
#[command(
    name = "gcode",
    version,
    about = "Fast code index CLI for Gobby",
    after_help = "Examples:
  find call sites:   gcode grep \"spawn_ui_server(\" [PATH...] -m 50
  read function:    gcode search-symbol \"spawn_ui_server\" --kind function
                    gcode symbol <id>
  locate by line:   gcode symbol-at src/auth.ts:42
  find config key:  gcode grep \"config.ui.mode\" -F [PATH...] -m 50"
)]
pub(crate) struct Cli {
    /// Override project root (default: detect from cwd)
    #[arg(long, global = true)]
    pub(crate) project: Option<String>,

    /// Output format
    #[arg(long, global = true)]
    pub(crate) format: Option<output::Format>,

    /// Suppress warnings
    #[arg(long, global = true)]
    pub(crate) quiet: bool,

    /// Enable verbose output
    #[arg(long, global = true)]
    pub(crate) verbose: bool,

    /// Skip read-time freshness checks
    #[arg(long, global = true)]
    pub(crate) no_freshness: bool,

    #[command(subcommand)]
    pub(crate) command: Command,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub(crate) enum AiRouteArg {
    Auto,
    Daemon,
    Direct,
    Off,
}

impl From<AiRouteArg> for AiRouting {
    fn from(value: AiRouteArg) -> Self {
        match value {
            AiRouteArg::Auto => AiRouting::Auto,
            AiRouteArg::Daemon => AiRouting::Daemon,
            AiRouteArg::Direct => AiRouting::Direct,
            AiRouteArg::Off => AiRouting::Off,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, ValueEnum)]
pub(crate) enum AiDepthArg {
    Sections,
    #[default]
    Files,
    Symbols,
}

impl From<AiDepthArg> for gobby_code::commands::codewiki::AiDepth {
    fn from(value: AiDepthArg) -> Self {
        match value {
            AiDepthArg::Sections => Self::Sections,
            AiDepthArg::Files => Self::Files,
            AiDepthArg::Symbols => Self::Symbols,
        }
    }
}

#[derive(Subcommand)]
pub(crate) enum Command {
    /// Emit the CLI contract for daemon conformance tests
    Contract,

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
        /// Query prefix to prepend before embedding search queries
        #[arg(long)]
        embedding_query_prefix: Option<String>,
        /// Embedding vector dimension
        #[arg(long)]
        embedding_vector_dim: Option<usize>,
        /// Embedding API key to store in local gcore.yaml
        #[arg(long)]
        embedding_api_key: Option<String>,
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
    /// Inspect embedding configuration consistency
    Embeddings {
        #[command(subcommand)]
        command: EmbeddingsCommand,
    },

    // ── Search (works in all modes) ──────────────────────────────────
    /// Hybrid search: pg_search BM25 + semantic (Qdrant) + graph boost (FalkorDB)
    #[command(
        after_help = "`gcode search` is hybrid/fuzzy concept search. Use `gcode grep \"pattern\" [PATH...] -m 50` for exact literals, call sites, dotted config keys, quoted strings, and paths. Use `gcode search-content \"query\" [PATH...]` for ranked file-content matches."
    )]
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
        /// Include FalkorDB graph neighbors in the exact-first ranking [requires graph backend]
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
    /// Indexed grep: exact pattern search on content chunks
    #[command(
        after_help = "gcode grep is indexed search over code_content_chunks. Unsupported grep/rg flags are intentionally rejected; use raw `rg` for filesystem grep."
    )]
    Grep {
        /// Pattern to search for (regex or fixed string)
        #[arg(value_parser = non_empty_grep_pattern)]
        pattern: String,
        /// Optional file paths or globs to filter results
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        /// Treat pattern as fixed string, not regex
        #[arg(short = 'F', long)]
        fixed_strings: bool,
        /// Match case-insensitively
        #[arg(short = 'i', long)]
        ignore_case: bool,
        /// Match only standalone ASCII identifier words
        #[arg(short = 'w', long)]
        word: bool,
        /// Show N context lines before match
        #[arg(short = 'B', long)]
        before_context: Option<usize>,
        /// Show N context lines after match
        #[arg(short = 'A', long)]
        after_context: Option<usize>,
        /// Show N context lines before and after match
        #[arg(short = 'C', long)]
        context: Option<usize>,
        /// Glob pattern to filter files, ANDed with PATH filters when both are present
        /// (bare globs match basenames; slash globs match paths)
        #[arg(short = 'g', long)]
        glob: Vec<String>,
        /// Maximum matching lines to include, up to 10000
        #[arg(short = 'm', long, value_parser = grep_max_count)]
        max_count: Option<usize>,
    },

    // ── Symbol Retrieval (works in all modes) ────────────────────────
    /// Hierarchical symbol tree for a file
    Outline {
        /// Use text generation to produce a natural-language outline when configured
        #[arg(long)]
        summarize: bool,
        file: String,
    },
    /// Fetch symbol source code by ID (byte-offset read)
    Symbol { id: String },
    /// Fetch symbol source code at PATH:LINE or PATH:LINE:COLUMN
    SymbolAt {
        /// Location containing line information; conflicts with separate LINE
        #[arg(value_name = "PATH[:LINE[:COLUMN]]")]
        location: String,
        /// 1-based line number; do not pass when LOCATION already includes a line
        #[arg(value_name = "LINE", value_parser = positive_usize)]
        line: Option<usize>,
    },
    /// Batch retrieve symbols by ID
    Symbols { ids: Vec<String> },
    /// List distinct symbol kinds in the index
    Kinds,
    /// File tree with symbol counts
    Tree,
    /// Generate vault-ready hierarchical code documentation
    Codewiki {
        /// Output directory for generated Markdown docs
        #[arg(long)]
        out: Option<String>,
        /// Limit docs to indexed files under one or more paths
        #[arg(long, num_args = 1.., value_name = "PATH")]
        scope: Vec<String>,
        /// Override AI routing for generated summaries
        #[arg(long, value_enum)]
        ai: Option<AiRouteArg>,
        /// AI prose depth: sections (architecture/modules/repo), files (+ per-file
        /// summaries), symbols (+ one call per symbol — expensive on large repos)
        #[arg(long, value_enum, default_value_t = AiDepthArg::Files)]
        ai_depth: AiDepthArg,
        /// Daemon feature profile for aggregate docs (architecture/modules/repo)
        /// [default: feature_mid]
        #[arg(long, value_name = "PROFILE")]
        ai_aggregate_profile: Option<String>,
        /// Maximum graph edges to fetch from FalkorDB
        #[arg(long, default_value_t = DEFAULT_CODEWIKI_GRAPH_EDGE_LIMIT, value_parser = positive_usize)]
        edge_limit: usize,
        /// Also document content-only files (markdown, plain text). By default
        /// codewiki documents only code and structured config (json/yaml);
        /// narrative markdown belongs to gwiki.
        #[arg(long)]
        include_docs: bool,
    },

    // ── Dependency Graph (requires graph backend) ──────────────────────
    /// Find callers of a symbol query, resolved to a canonical symbol ID [requires graph backend]
    Callers {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Find incoming call usages of a symbol query, resolved to a canonical symbol ID [requires graph backend]
    Usages {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
    },
    /// Show import graph for a file [requires graph backend]
    Imports { file: String },
    /// Transitive impact analysis for a symbol query, resolved to a canonical symbol ID [requires graph backend]
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
pub(crate) enum GraphCommand {
    /// Sync one indexed file into the code-index graph projection
    SyncFile {
        /// Indexed file path to sync
        #[arg(long)]
        file: String,
        /// Skip sync if indexed file not found (daemon/background-worker only)
        #[arg(long)]
        allow_missing_indexed_file: bool,
    },
    /// Clear the current project's code-index graph projection
    Clear {
        /// Clear graph projection for this project id without resolving cwd project context
        #[arg(long)]
        project_id: Option<String>,
    },
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
pub(crate) enum VectorCommand {
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

#[derive(Subcommand)]
pub(crate) enum EmbeddingsCommand {
    /// Emit embedding configuration doctor JSON
    Doctor,
}

fn non_empty_grep_pattern(value: &str) -> Result<String, String> {
    if value.is_empty() {
        Err("gcode grep pattern cannot be empty".to_string())
    } else {
        Ok(value.to_string())
    }
}

fn positive_usize(value: &str) -> Result<usize, String> {
    bounded_positive_usize(value, MAX_POSITIVE_USIZE_ARG, "value")
}

fn grep_max_count(value: &str) -> Result<usize, String> {
    bounded_positive_usize(value, MAX_GREP_MAX_COUNT, "--max-count")
}

fn bounded_positive_usize(value: &str, max: usize, name: &str) -> Result<usize, String> {
    let parsed = value
        .parse::<usize>()
        .map_err(|_| format!("{name} must be a positive integer"))?;
    if parsed == 0 {
        Err(format!("{name} must be a positive integer"))
    } else if parsed > max {
        Err(format!("{name} must be no more than {max}"))
    } else {
        Ok(parsed)
    }
}

pub(crate) fn effective_format(
    explicit_format: Option<output::Format>,
    command: &Command,
) -> output::Format {
    explicit_format.unwrap_or(match command {
        Command::Grep { .. } => output::Format::Text,
        _ => output::Format::Json,
    })
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod symbol_at_tests;
