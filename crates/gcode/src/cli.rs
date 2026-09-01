use crate::output;
use clap::{ArgGroup, Args, FromArgMatches, Parser, Subcommand, ValueEnum};

const DEFAULT_SYMBOL_PATH_MAX_DEPTH: usize =
    crate::graph::code_graph::DEFAULT_SYMBOL_PATH_MAX_DEPTH;
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

    /// Allow stale index data by skipping read-time freshness checks
    #[arg(long, global = true)]
    pub(crate) allow_stale: bool,

    #[command(subcommand)]
    pub(crate) command: Command,
}

#[derive(Subcommand)]
pub(crate) enum Command {
    /// Emit the CLI contract for daemon conformance tests
    Contract,
    /// Print the embedded schema identity.
    SchemaIdentity {
        #[arg(long)]
        json: bool,
    },

    // ── Project Setup ────────────────────────────────────────────────
    /// Index this machine's registered Gobby checkout and install gcode skills
    Init,
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
        /// Skip (exit 3) instead of blocking when another indexer holds the
        /// project index lock. Used by daemon-triggered per-file flushes so a
        /// concurrent reindex does not cause a blocking-waiter pileup (#17701).
        #[arg(long)]
        skip_if_locked: bool,
    },
    /// Reconcile stranded index state and graph projection drift
    Repair,
    /// Show project index status
    Status,
    /// Clear index and force re-index
    Invalidate {
        /// Clear index state for this project id without resolving cwd project context
        #[arg(long)]
        project_id: Option<String>,
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
        /// Page complete results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
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
        /// Page results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
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
        /// Page results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
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
        /// Page results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// Indexed grep: exact pattern search on content chunks
    #[command(
        after_help = "gcode grep is indexed search over code_content_chunks. Accepted flags: -F -i -w -l -m -A -B -C -g plus accepted no-ops -E -n -r -R. Remaining grep/rg flags are not supported; use raw `rg` for filesystem grep.\n\nPatterns are Rust regex, not grep BRE: write alternation as `a|b`, because `a\\|b` matches a literal pipe and returns no matches."
    )]
    Grep {
        /// Pattern to search for (Rust regex, or fixed string with -F)
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
        /// List matching file paths instead of matching lines (rg -l)
        #[arg(short = 'l', long)]
        files_with_matches: bool,
        /// Accepted no-op: patterns are already extended regex (grep/rg -E)
        #[arg(short = 'E', long = "extended-regexp", hide = true)]
        extended_regexp: bool,
        /// Accepted no-op: line numbers are always shown (grep/rg -n)
        #[arg(short = 'n', long = "line-number", hide = true)]
        line_number: bool,
        /// Accepted no-op: indexed grep is always recursive (grep -r/-R)
        #[arg(short = 'r', long = "recursive", hide = true)]
        recursive: bool,
        #[arg(short = 'R', hide = true)]
        recursive_dereference: bool,
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
        #[arg(short = 'm', long = "limit", visible_alias = "max-count", value_parser = grep_max_count)]
        max_count: Option<usize>,
        /// Skip first N matches (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Page results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },

    // ── Symbol Retrieval (works in all modes) ────────────────────────
    /// Hierarchical symbol tree for a file
    Outline {
        file: String,
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
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
    /// Batch retrieve symbol source by ID and report stale or missing IDs
    #[command(
        after_help = "Edited files invalidate content-derived symbol IDs. Rerun `gcode outline <file> --verbose` for current IDs, or use `gcode symbol-at <path:line>`."
    )]
    Symbols {
        ids: Vec<String>,
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// List distinct symbol kinds in the index
    Kinds {
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// File tree with symbol counts
    Tree {
        /// Project file, directory, or glob filter (repeatable; OR semantics)
        #[arg(value_name = "PATH")]
        paths: Vec<String>,
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
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
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// Find callees of a symbol query, resolved to a canonical symbol ID [requires graph backend]
    Callees {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// Find incoming call usages of a symbol query, resolved to a canonical symbol ID [requires graph backend]
    Usages {
        symbol_name: String,
        #[arg(long, default_value = "10")]
        limit: usize,
        /// Skip first N results (for pagination)
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Page complete results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// Show import graph for a file [requires graph backend]
    Imports {
        file: String,
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// Shortest CALLS path from one symbol query to another [requires graph backend]
    Path {
        /// Source symbol query
        #[arg(value_name = "SYMBOL_A")]
        symbol_a: String,
        /// Target symbol query
        #[arg(value_name = "SYMBOL_B")]
        symbol_b: String,
        /// Maximum CALLS hops to search
        #[arg(long, default_value_t = DEFAULT_SYMBOL_PATH_MAX_DEPTH, value_parser = positive_usize)]
        max_depth: usize,
    },
    /// Transitive impact analysis for a symbol query, resolved to a canonical symbol ID [requires graph backend]
    BlastRadius {
        /// Symbol query
        target: String,
        #[arg(long, default_value = "3")]
        depth: usize,
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        /// Page results to an approximate token budget
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },

    // ── Project Management ───────────────────────────────────────────
    /// Directory-grouped project stats
    RepoOutline {
        #[arg(long, value_parser = positive_usize)]
        limit: Option<usize>,
        #[arg(long, default_value = "0")]
        offset: usize,
        #[arg(long, value_parser = positive_usize)]
        token_budget: Option<usize>,
    },
    /// List indexed projects
    Projects,
    /// Remove stale projects and reconcile orphaned graph + vector projection state across indexed projects
    Prune {
        /// Skip confirmation prompt
        #[arg(long)]
        force: bool,
        /// Retain unreferenced content and recent Git history for this many days
        /// (matches the daemon's `code_index.content_retention_days` default)
        #[arg(long, default_value_t = 1, value_parser = clap::value_parser!(u32).range(1..))]
        retention_days: u32,
        /// Stop content garbage collection after this many seconds and report the
        /// remaining versions as deferred to a later run (requires --project)
        #[arg(long, value_parser = clap::value_parser!(u64).range(1..))]
        max_seconds: Option<u64>,
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
    /// Remove project-wide orphaned graph nodes (run periodically; not on every file sync)
    CleanupOrphans,
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
    /// Render a scoped graph view (fcg, mcg, or class-hierarchy)
    View(GraphViewArgs),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
pub(crate) enum GraphViewKind {
    Fcg,
    Mcg,
    #[value(name = "class-hierarchy")]
    ClassHierarchy,
}

impl GraphViewKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Fcg => "fcg",
            Self::Mcg => "mcg",
            Self::ClassHierarchy => "class-hierarchy",
        }
    }

    pub(crate) fn default_depth(self) -> u32 {
        match self {
            Self::ClassHierarchy => 8,
            Self::Fcg | Self::Mcg => 1,
        }
    }

    pub(crate) fn effective_depth(self, depth: Option<u32>) -> u32 {
        depth.unwrap_or_else(|| self.default_depth())
    }

    pub(crate) fn allows_row_limits(self) -> bool {
        matches!(self, Self::Fcg | Self::Mcg)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum GraphViewSeed {
    File(String),
    Module(String),
    Symbol(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct GraphViewArgs {
    pub view: GraphViewKind,
    pub seed: GraphViewSeed,
    pub depth: Option<u32>,
    pub incoming_limit: Option<usize>,
    pub outgoing_limit: Option<usize>,
}

impl GraphViewArgs {
    pub(crate) fn effective_depth(&self) -> u32 {
        self.view.effective_depth(self.depth)
    }
}

#[derive(Args, Clone, Debug)]
#[command(group(
    ArgGroup::new("seed")
        .required(true)
        .multiple(false)
        .args(["file", "module", "symbol"])
))]
struct GraphViewArgsRaw {
    /// View kind: fcg, mcg, or class-hierarchy
    #[arg(long, value_enum)]
    view: GraphViewKind,
    /// Project file seed (mcg only)
    #[arg(long, value_name = "FILE")]
    file: Option<String>,
    /// Module seed (mcg only)
    #[arg(long, value_name = "MODULE")]
    module: Option<String>,
    /// Symbol query seed (fcg and class-hierarchy only)
    #[arg(long, value_name = "SYMBOL")]
    symbol: Option<String>,
    /// Hop depth (1..=16). Omitted: 8 for class-hierarchy, 1 for fcg/mcg
    #[arg(
        long,
        value_parser = clap::value_parser!(u32)
            .range(1..=crate::graph::code_graph::MAX_SYMBOL_PATH_DEPTH as i64)
    )]
    depth: Option<u32>,
    /// Incoming neighbor limit (fcg and mcg only)
    #[arg(long, value_parser = positive_usize)]
    incoming_limit: Option<usize>,
    /// Outgoing neighbor limit (fcg and mcg only)
    #[arg(long, value_parser = positive_usize)]
    outgoing_limit: Option<usize>,
}

impl FromArgMatches for GraphViewArgs {
    fn from_arg_matches(matches: &clap::ArgMatches) -> Result<Self, clap::Error> {
        Self::from_arg_matches_mut(&mut matches.clone())
    }

    fn from_arg_matches_mut(matches: &mut clap::ArgMatches) -> Result<Self, clap::Error> {
        let raw = GraphViewArgsRaw::from_arg_matches_mut(matches)?;
        if !raw.view.allows_row_limits()
            && (raw.incoming_limit.is_some() || raw.outgoing_limit.is_some())
        {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                "--incoming-limit and --outgoing-limit cannot be used with --view=class-hierarchy",
            ));
        }
        let seed = match (raw.file, raw.module, raw.symbol) {
            (Some(file), None, None) => GraphViewSeed::File(file),
            (None, Some(module), None) => GraphViewSeed::Module(module),
            (None, None, Some(symbol)) => GraphViewSeed::Symbol(symbol),
            _ => {
                return Err(clap::Error::raw(
                    clap::error::ErrorKind::MissingRequiredArgument,
                    "exactly one of --file, --module, or --symbol is required",
                ));
            }
        };
        let selector_is_valid = matches!(
            (raw.view, &seed),
            (
                GraphViewKind::Mcg,
                GraphViewSeed::File(_) | GraphViewSeed::Module(_)
            ) | (
                GraphViewKind::Fcg | GraphViewKind::ClassHierarchy,
                GraphViewSeed::Symbol(_)
            )
        );
        if !selector_is_valid {
            return Err(clap::Error::raw(
                clap::error::ErrorKind::ArgumentConflict,
                match raw.view {
                    GraphViewKind::Mcg => "--view=mcg requires --file or --module",
                    GraphViewKind::Fcg | GraphViewKind::ClassHierarchy => {
                        "--view=fcg and --view=class-hierarchy require --symbol"
                    }
                },
            ));
        }
        Ok(Self {
            view: raw.view,
            seed,
            depth: raw.depth,
            incoming_limit: raw.incoming_limit,
            outgoing_limit: raw.outgoing_limit,
        })
    }

    fn update_from_arg_matches(&mut self, matches: &clap::ArgMatches) -> Result<(), clap::Error> {
        self.update_from_arg_matches_mut(&mut matches.clone())
    }

    fn update_from_arg_matches_mut(
        &mut self,
        matches: &mut clap::ArgMatches,
    ) -> Result<(), clap::Error> {
        *self = Self::from_arg_matches_mut(matches)?;
        Ok(())
    }
}

impl Args for GraphViewArgs {
    fn group_id() -> Option<clap::Id> {
        GraphViewArgsRaw::group_id()
    }

    fn augment_args(cmd: clap::Command) -> clap::Command {
        GraphViewArgsRaw::augment_args(cmd)
    }

    fn augment_args_for_update(cmd: clap::Command) -> clap::Command {
        GraphViewArgsRaw::augment_args_for_update(cmd)
    }
}

#[derive(Subcommand)]
pub(crate) enum VectorCommand {
    /// Sync one indexed file into the code-symbol vector projection
    SyncFile {
        /// Indexed file path to sync
        #[arg(long)]
        file: String,
        /// Skip sync if indexed file not found (daemon/background-worker only)
        #[arg(long)]
        allow_missing_indexed_file: bool,
    },
    /// Clear the current project's code-symbol vector projection
    Clear {
        /// Clear vector projection for this project id without resolving cwd project context
        #[arg(long)]
        project_id: Option<String>,
        /// Delete the project's whole code-symbol collection instead of its points (project purge)
        #[arg(long)]
        drop_collection: bool,
    },
    /// Rebuild the current project's code-symbol vector projection from PostgreSQL facts
    Rebuild,
    /// Remove code-symbol vectors for files no longer indexed in PostgreSQL
    CleanupOrphans,
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
    bounded_positive_usize(value, MAX_GREP_MAX_COUNT, "--limit")
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
    explicit_format.unwrap_or_else(|| {
        if command.is_navigation() {
            output::Format::Text
        } else {
            output::Format::Json
        }
    })
}

pub(crate) fn effective_token_budget(format: output::Format, command: &Command) -> Option<usize> {
    command.requested_token_budget().or_else(|| {
        (matches!(format, output::Format::Text) && command.is_paged_navigation())
            .then_some(crate::commands::token_budget::AUTOMATIC_TEXT_TOKEN_BUDGET)
    })
}

impl Command {
    fn is_navigation(&self) -> bool {
        matches!(
            self,
            Self::Search { .. }
                | Self::SearchSymbol { .. }
                | Self::SearchText { .. }
                | Self::SearchContent { .. }
                | Self::Grep { .. }
                | Self::Outline { .. }
                | Self::Symbol { .. }
                | Self::SymbolAt { .. }
                | Self::Symbols { .. }
                | Self::Kinds { .. }
                | Self::Tree { .. }
                | Self::Callers { .. }
                | Self::Callees { .. }
                | Self::Usages { .. }
                | Self::Imports { .. }
                | Self::Path { .. }
                | Self::BlastRadius { .. }
                | Self::RepoOutline { .. }
        )
    }

    fn is_paged_navigation(&self) -> bool {
        self.is_navigation()
            && !matches!(
                self,
                Self::Symbol { .. } | Self::SymbolAt { .. } | Self::Path { .. }
            )
    }

    fn requested_token_budget(&self) -> Option<usize> {
        match self {
            Self::Search { token_budget, .. }
            | Self::SearchSymbol { token_budget, .. }
            | Self::SearchText { token_budget, .. }
            | Self::SearchContent { token_budget, .. }
            | Self::Grep { token_budget, .. }
            | Self::Outline { token_budget, .. }
            | Self::Symbols { token_budget, .. }
            | Self::Kinds { token_budget, .. }
            | Self::Tree { token_budget, .. }
            | Self::Callers { token_budget, .. }
            | Self::Callees { token_budget, .. }
            | Self::Usages { token_budget, .. }
            | Self::Imports { token_budget, .. }
            | Self::BlastRadius { token_budget, .. }
            | Self::RepoOutline { token_budget, .. } => *token_budget,
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod symbol_at_tests;
