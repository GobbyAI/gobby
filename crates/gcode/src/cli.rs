use clap::{ArgAction, ArgGroup, Parser, Subcommand};
use gobby_code::output;

#[derive(Parser)]
#[command(name = "gcode", version, about = "Fast code index CLI for Gobby")]
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

#[derive(Subcommand)]
pub(crate) enum Command {
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
    /// Inspect embedding configuration consistency
    Embeddings {
        #[command(subcommand)]
        command: EmbeddingsCommand,
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
        /// Show N context lines before match
        #[arg(short = 'B', long)]
        before_context: Option<usize>,
        /// Show N context lines after match
        #[arg(short = 'A', long)]
        after_context: Option<usize>,
        /// Show N context lines before and after match
        #[arg(short = 'C', long)]
        context: Option<usize>,
        /// Glob pattern to filter files (bare globs match basenames; slash globs match paths)
        #[arg(short = 'g', long)]
        glob: Vec<String>,
        /// Maximum matching lines to include
        #[arg(short = 'm', long)]
        max_count: Option<usize>,
        /// Show line numbers (always shown, deprecated flag for compatibility)
        #[arg(short = 'n', long)]
        line_number: bool,
        /// Unsupported: use -m/--max-count for indexed grep caps
        #[arg(long = "limit", hide = true, action = ArgAction::SetTrue)]
        unsupported_limit: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'l', long = "files-with-matches", hide = true, action = ArgAction::SetTrue)]
        unsupported_files_with_matches: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'L', long = "files-without-match", hide = true, action = ArgAction::SetTrue)]
        unsupported_files_without_match: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'c', long = "count", hide = true, action = ArgAction::SetTrue)]
        unsupported_count: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'o', long = "only-matching", hide = true, action = ArgAction::SetTrue)]
        unsupported_only_matching: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'v', long = "invert-match", hide = true, action = ArgAction::SetTrue)]
        unsupported_invert_match: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'w', long = "word-regexp", hide = true, action = ArgAction::SetTrue)]
        unsupported_word_regexp: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'e', long = "regexp", hide = true)]
        unsupported_regexp: Option<String>,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'r', long = "recursive", hide = true, action = ArgAction::SetTrue)]
        unsupported_recursive: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 't', long = "type", hide = true)]
        unsupported_type: Option<String>,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'T', long = "type-not", hide = true)]
        unsupported_type_not: Option<String>,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'P', long = "pcre2", hide = true, action = ArgAction::SetTrue)]
        unsupported_pcre2: bool,
        /// Unsupported: use raw rg for filesystem grep
        #[arg(short = 'U', long = "multiline", hide = true, action = ArgAction::SetTrue)]
        unsupported_multiline: bool,
        /// Unsupported: use --format json for structured indexed grep output
        #[arg(long = "json", hide = true, action = ArgAction::SetTrue)]
        unsupported_json: bool,
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

pub(crate) fn effective_format(
    explicit_format: Option<output::Format>,
    command: &Command,
) -> output::Format {
    explicit_format.unwrap_or(match command {
        Command::Grep { .. } => output::Format::Text,
        _ => output::Format::Json,
    })
}

pub(crate) fn reject_unsupported_grep_flags(command: &Command) -> anyhow::Result<()> {
    let Command::Grep {
        unsupported_limit,
        unsupported_files_with_matches,
        unsupported_files_without_match,
        unsupported_count,
        unsupported_only_matching,
        unsupported_invert_match,
        unsupported_word_regexp,
        unsupported_regexp,
        unsupported_recursive,
        unsupported_type,
        unsupported_type_not,
        unsupported_pcre2,
        unsupported_multiline,
        unsupported_json,
        ..
    } = command
    else {
        return Ok(());
    };

    let flag = if *unsupported_limit {
        Some("--limit")
    } else if *unsupported_files_with_matches {
        Some("--files-with-matches")
    } else if *unsupported_files_without_match {
        Some("--files-without-match")
    } else if *unsupported_count {
        Some("--count")
    } else if *unsupported_only_matching {
        Some("--only-matching")
    } else if *unsupported_invert_match {
        Some("--invert-match")
    } else if *unsupported_word_regexp {
        Some("--word-regexp")
    } else if unsupported_regexp.is_some() {
        Some("--regexp")
    } else if *unsupported_recursive {
        Some("--recursive")
    } else if unsupported_type.is_some() {
        Some("--type")
    } else if unsupported_type_not.is_some() {
        Some("--type-not")
    } else if *unsupported_pcre2 {
        Some("--pcre2")
    } else if *unsupported_multiline {
        Some("--multiline")
    } else if *unsupported_json {
        Some("--json")
    } else {
        None
    };

    if let Some(flag) = flag {
        anyhow::bail!(
            "gcode grep is indexed search; unsupported grep/rg flag `{flag}`. Use raw `rg` for filesystem grep."
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests;
