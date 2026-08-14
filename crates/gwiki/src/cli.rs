use std::ffi::{OsStr, OsString};
use std::path::PathBuf;

use clap::{ArgGroup, Args, Parser, Subcommand, ValueEnum};
use gobby_wiki::{BenchmarkOptions, Command, GraphInclude, UpkeepOptions, WikiError, output};

mod code;
mod mapping;

use code::CodeArgs;

const CLI_SUBCOMMANDS: &[&str] = &[
    "init",
    "contract",
    "index",
    "collect",
    "code",
    "ingest-file",
    "ingest-url",
    "sync-sessions",
    "refresh",
    "sources",
    "remove-source",
    "purge",
    "prune",
    "search",
    "read",
    "pages",
    "page",
    "backlinks",
    "link-suggest",
    "benchmark",
    "citation-quality",
    "compile",
    "export",
    "graph",
    "graph-context",
    "review-report",
    "audit",
    "lint",
    "normalize",
    "health",
    "librarian",
    "upkeep",
    "recap",
    "status",
    "trust",
];

#[derive(Debug, Parser)]
#[command(name = "gwiki", version, about = "Gobby wiki CLI")]
struct Cli {
    #[command(flatten)]
    scope: ScopeArgs,

    /// Output format.
    #[arg(long, global = true, default_value = "json")]
    format: output::Format,

    /// Suppress status messages.
    #[arg(short = 'q', long, global = true, conflicts_with = "verbose")]
    quiet: bool,

    /// Enable verbose diagnostic messages.
    #[arg(short = 'v', long, global = true, conflicts_with = "quiet")]
    verbose: bool,

    #[command(subcommand)]
    command: CliCommand,
}

pub(super) struct Invocation {
    scope: ScopeArgs,
    format: output::Format,
    quiet: bool,
    verbose: bool,
    command: CliCommand,
}

impl Invocation {
    pub(super) fn parse_env() -> Self {
        let Cli {
            scope,
            format,
            quiet,
            verbose,
            command,
        } = Cli::parse_from(normalize_project_flag_args(std::env::args_os()));
        Self {
            scope,
            format,
            quiet,
            verbose,
            command,
        }
    }

    pub(super) fn format(&self) -> output::Format {
        self.format
    }

    pub(super) fn quiet(&self) -> bool {
        self.quiet
    }

    pub(super) fn verbose(&self) -> bool {
        self.verbose
    }

    pub(super) fn is_contract(&self) -> bool {
        matches!(self.command, CliCommand::Contract)
    }

    pub(super) fn into_command(self) -> Result<Command, WikiError> {
        mapping::command_from_cli_with_runtime(
            self.command,
            self.scope.into(),
            self.quiet,
            self.verbose,
        )
    }
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Emit the CLI contract for daemon conformance tests.
    Contract,

    /// Initialize a wiki vault.
    Init,
    /// Index markdown and source notes in the selected scope.
    Index {
        /// Re-index documents even when content hashes are unchanged.
        #[arg(long)]
        force: bool,
    },
    /// Collect recognized inbox drops into raw storage.
    Collect,
    /// Generate vault-ready hierarchical code documentation.
    Code(CodeArgs),
    /// Capture a local source file into the wiki inbox.
    IngestFile {
        #[arg(value_name = "PATH")]
        path: PathBuf,
        /// Disable AI-backed media extraction for this ingest.
        #[arg(long)]
        no_ai: bool,
        /// Prefer audio translation over transcription where a backend is available.
        #[arg(long)]
        translate: bool,
        /// Target language for audio translation.
        #[arg(long, value_name = "LANG")]
        target_lang: Option<String>,
        /// Seconds between sampled video frames; 0 disables frames.
        #[arg(long = "video-frame-interval", value_name = "SECONDS")]
        video_frame_interval_seconds: Option<u32>,
    },
    /// Fetch URL sources into the wiki inbox.
    IngestUrl {
        #[arg(value_name = "URL", num_args = 1..)]
        urls: Vec<String>,
        /// Reuse an existing URL capture no older than this many hours.
        #[arg(
            long,
            value_name = "HOURS",
            default_value_t = 24,
            value_parser = clap::value_parser!(u64).range(0..=8760)
        )]
        max_age_hours: u64,
    },
    /// Sync archived Gobby session transcripts into the wiki vault.
    SyncSessions(SyncSessionsArgs),
    /// Refresh URL-backed raw source records.
    Refresh(RefreshArgs),
    /// List raw source manifest entries in the selected scope.
    Sources,
    /// Remove a raw source, its manifest entry, and its raw asset.
    RemoveSource(RemoveSourceArgs),
    /// Purge generated/indexed wiki state in the selected scope.
    Purge(PurgeArgs),
    /// Reconcile generated project state whose authoritative project row is absent.
    Prune(PruneArgs),
    /// Search wiki documents in the selected scope.
    Search(SearchArgs),
    /// Read a wiki page or document in the selected scope.
    Read(ReadArgs),
    /// List indexed wiki pages and unindexed outputs reports.
    Pages(PagesArgs),
    /// Write or delete knowledge/ wiki pages in the selected scope.
    Page(PageArgs),
    /// Show backlinks for a wiki page.
    Backlinks(BacklinksArgs),
    /// Suggest unresolved wiki links in the selected scope.
    LinkSuggest(LinkSuggestArgs),
    /// Report benchmark metrics for an indexed seeded project.
    Benchmark(BenchmarkArgs),
    /// Compile accepted research notes into wiki articles.
    Compile(CompileArgs),
    /// Export generated bundles and reports under outputs/.
    Export(ExportArgs),
    /// Export unified wiki graph artifacts under outputs/.
    Graph(GraphArgs),
    /// Build a compact wiki graph context pack.
    GraphContext,
    ReviewReport(ReviewReportArgs),
    /// Report claims that lack source support.
    Audit,
    /// Detect broken links and vault hygiene issues.
    Lint,
    /// Normalize whitespace in already-written vault markdown (markdownlint repair).
    Normalize(NormalizeArgs),
    /// Write wiki health snapshots under meta/health.
    Health,
    /// Propose wiki upkeep tasks and patches without rewriting pages.
    Librarian(LibrarianArgs),
    /// Drain pending sources into entity concept pages.
    Upkeep(UpkeepArgs),
    /// Write the day's session recap page.
    Recap(RecapArgs),
    /// Show shell readiness.
    Status,
    /// Show search, graph, freshness, and audit trust status.
    Trust,
    /// Emit a Markdown report on source citation quality.
    CitationQuality,
}

#[derive(Debug, Args)]
struct ScopeArgs {
    /// Use a Gobby project's wiki scope. Bare --project uses the current directory.
    #[arg(
        long,
        global = true,
        conflicts_with = "topic",
        value_name = "ROOT",
        num_args = 0..=1,
        default_missing_value = ".",
    )]
    project: Option<PathBuf>,

    /// Use a named topic wiki scope.
    #[arg(long, global = true, value_name = "NAME")]
    topic: Option<String>,
}

#[derive(Debug, Args)]
struct NormalizeArgs {
    /// Report which authored docs need normalization without rewriting them.
    #[arg(long)]
    check: bool,
}

#[derive(Debug, Args)]
struct GraphArgs {
    /// Print the graph JSON envelope to stdout instead of writing artifacts.
    #[arg(long)]
    stdout: bool,
    /// Restrict the graph to knowledge or code facts.
    #[arg(long, value_enum, default_value = "all")]
    include: GraphInclude,
}

#[derive(Debug, Args)]
struct PurgeArgs {
    /// Purge a project scope directly by UUID without resolving a project root.
    #[arg(long, value_name = "UUID", conflicts_with_all = ["project", "topic"])]
    project_id: Option<uuid::Uuid>,

    /// Confirm destructive purge of generated/indexed wiki state for the selected scope.
    #[arg(long)]
    yes: bool,
}

#[derive(Debug, Args)]
struct PruneArgs {
    /// Skip the destructive reconciliation confirmation prompt.
    #[arg(long)]
    force: bool,
}

#[derive(Debug, Args)]
struct SearchArgs {
    #[arg(value_name = "QUERY")]
    query: String,

    #[arg(long, default_value = "10")]
    limit: usize,

    /// Disable semantic vector search for this query.
    #[arg(long = "no-semantic")]
    no_semantic: bool,

    /// Trim results to fit an approximate token budget, emitting a narrowing hint.
    #[arg(long = "token-budget", value_name = "N", value_parser = parse_positive_usize)]
    token_budget: Option<usize>,

    /// Also return quarantined candidate pages (librarian/upkeep loops).
    #[arg(long = "include-candidates")]
    include_candidates: bool,
}

#[derive(Debug, Args)]
struct SyncSessionsArgs {
    /// Directory containing archived *.jsonl.gz session transcripts.
    #[arg(long, value_name = "PATH")]
    archive_dir: Option<PathBuf>,

    /// Directory containing daemon-synthesized session wiki *.md files.
    #[arg(long, value_name = "PATH")]
    wiki_dir: Option<PathBuf>,

    /// Maximum number of archives to process.
    #[arg(long, value_name = "N", value_parser = parse_positive_usize)]
    limit: Option<usize>,

    /// Include raw transcript archives when no daemon synthesis exists.
    #[arg(long)]
    raw: bool,

    /// For archives with no daemon synthesis, generate a daemon-equivalent
    /// summary (shared handoff prompt) instead of the structural skeleton.
    /// Processes raw archives even without --raw; degrades to the skeleton when
    /// AI is unavailable. A later daemon synthesis supersedes the page.
    #[arg(long)]
    summarize: bool,

    /// Skip connection enrichment for daemon-synthesized session wiki pages.
    #[arg(long = "no-enrich")]
    no_enrich: bool,
}

#[derive(Debug, Args)]
struct BenchmarkArgs {
    /// Seeded retrieval precision probes to run.
    #[arg(
        long = "retrieval-candidates",
        default_value_t = BenchmarkOptions::DEFAULT_RETRIEVAL_CANDIDATES,
        value_parser = parse_positive_usize
    )]
    retrieval_candidates: usize,
}

fn parse_positive_usize(value: &str) -> Result<usize, String> {
    value
        .parse::<usize>()
        .map_err(|error| error.to_string())
        .and_then(|value| {
            if value > 0 {
                Ok(value)
            } else {
                Err("must be greater than zero".to_string())
            }
        })
}

#[derive(Debug, Args)]
struct RemoveSourceArgs {
    #[arg(long, value_name = "SOURCE_ID")]
    id: String,

    /// Preview file and manifest changes without mutating the vault.
    #[arg(long)]
    dry_run: bool,

    /// Confirm destructive removal.
    #[arg(long)]
    yes: bool,

    /// Preserve the raw source asset referenced by source_asset frontmatter.
    #[arg(long)]
    keep_asset: bool,
}

#[derive(Debug, Args)]
struct RefreshArgs {
    /// Source ID to refresh. Repeat to refresh multiple explicit sources.
    #[arg(long = "id", value_name = "SOURCE_ID")]
    id: Vec<String>,

    /// Preview refresh candidates without fetching, writing, deleting, or indexing.
    #[arg(long)]
    dry_run: bool,
}

#[derive(Debug, Args)]
#[command(group(
    ArgGroup::new("target")
        .required(true)
        .args(["path", "title"])
))]
struct ReadArgs {
    /// Vault-relative wiki path to read.
    #[arg(long, value_name = "PATH")]
    path: Option<PathBuf>,

    /// First-heading title to resolve inside the selected scope.
    #[arg(long, value_name = "TITLE")]
    title: Option<String>,
}

#[derive(Debug, Args)]
struct PagesArgs {
    /// Only list pages whose wiki path starts with this prefix (e.g. code/).
    #[arg(long, value_name = "PREFIX")]
    prefix: Option<String>,
}

#[derive(Debug, Args)]
struct PageArgs {
    #[command(subcommand)]
    command: PageSubcommand,
}

#[derive(Debug, Subcommand)]
enum PageSubcommand {
    /// Write a knowledge/ page from stdin content.
    Write(PageWriteArgs),
    /// Delete a knowledge/ page.
    Delete(PageDeleteArgs),
}

#[derive(Debug, Args)]
struct PageWriteArgs {
    /// Vault-relative markdown path under knowledge/ (e.g. knowledge/topics/x.md).
    #[arg(long, value_name = "PATH")]
    path: String,

    /// upsert overwrites or creates; create fails if the page exists.
    #[arg(
        long,
        value_enum,
        default_value = "upsert",
        value_name = "upsert|create"
    )]
    mode: PageMode,

    /// Require this SHA-256 content hash on disk before writing (upsert only).
    #[arg(long, value_name = "SHA256")]
    expected_hash: Option<String>,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum PageMode {
    Upsert,
    Create,
}

#[derive(Debug, Args)]
struct PageDeleteArgs {
    /// Vault-relative markdown path under knowledge/ (e.g. knowledge/topics/x.md).
    #[arg(long, value_name = "PATH")]
    path: String,
}

#[derive(Debug, Args)]
struct BacklinksArgs {
    #[arg(value_name = "PAGE")]
    page: String,
}

#[derive(Debug, Args)]
struct LinkSuggestArgs {
    #[arg(long, default_value = "10")]
    limit: usize,
}

#[derive(Debug, Args)]
struct LibrarianArgs {
    /// Disable daemon-backed patch suggestions for this invocation.
    #[arg(long)]
    no_ai: bool,
}

#[derive(Debug, Args)]
struct UpkeepArgs {
    /// Maximum concept pages synthesized in one run.
    #[arg(
        long = "max-pages",
        value_name = "N",
        default_value_t = UpkeepOptions::DEFAULT_MAX_PAGES
    )]
    max_pages: usize,

    /// Minimum digest mentions before an unresolved target forms a cluster.
    #[arg(
        long = "min-mentions",
        value_name = "N",
        default_value_t = UpkeepOptions::DEFAULT_MIN_MENTIONS
    )]
    min_mentions: usize,

    /// Maximum accepted sources compiled into one concept page.
    #[arg(
        long = "max-sources-per-page",
        value_name = "N",
        default_value_t = UpkeepOptions::DEFAULT_MAX_SOURCES_PER_PAGE
    )]
    max_sources_per_page: usize,

    /// Plan the run without writing anything to the vault.
    #[arg(long = "dry-run")]
    dry_run: bool,

    /// Stop scheduling synthesis clusters after this maintenance budget.
    #[arg(
        long = "time-budget-seconds",
        value_name = "SECONDS",
        value_parser = clap::value_parser!(u64).range(1..)
    )]
    time_budget_seconds: Option<u64>,

    /// Disable daemon-backed concept-page synthesis for this invocation.
    #[arg(long)]
    no_ai: bool,
}

#[derive(Debug, Args)]
struct RecapArgs {
    /// Target day (YYYY-MM-DD, UTC session-day attribution); defaults to today.
    #[arg(long, value_name = "YYYY-MM-DD")]
    date: Option<String>,

    /// Disable daemon-backed recap synthesis for this invocation.
    #[arg(long)]
    no_ai: bool,
}

#[derive(Debug, Args)]
struct CompileArgs {
    // The id must differ from the global `--topic` scope arg: clap propagates
    // global args by id, so an id of `topic` lets this positional hijack the
    // scope selection (#701).
    #[arg(id = "compile_topic", value_name = "TOPIC")]
    topic: Option<String>,

    #[arg(long = "outline", value_name = "HEADING")]
    outline: Vec<String>,

    #[arg(long = "source", value_name = "SOURCE_ID_OR_PATH")]
    source: Vec<String>,

    #[arg(long, value_enum, default_value = "topic")]
    kind: CompileKind,

    #[arg(long, value_name = "PAGE")]
    target: Option<PathBuf>,

    #[arg(long = "write-intent")]
    write_intent: bool,

    /// Disable daemon-backed explainer synthesis for this invocation.
    #[arg(long)]
    no_ai: bool,
}

#[derive(Debug, Args)]
struct ExportArgs {
    #[command(subcommand)]
    command: ExportSubcommand,
}

#[derive(Debug, Args)]
struct ReviewReportArgs {
    #[arg(long = "file", value_name = "PATH")]
    files: Vec<String>,
    #[arg(long = "symbol", value_name = "SYMBOL_ID")]
    symbols: Vec<String>,
    #[arg(long = "diff", value_name = "PATH")]
    diff_path: Option<PathBuf>,
    #[arg(
        long = "output",
        default_value = "review-report.md",
        value_name = "FILE"
    )]
    output: String,
}

#[derive(Debug, Subcommand)]
enum ExportSubcommand {
    /// Export per-page .json metadata siblings under outputs/pages/.
    Pages,
    /// Export bundled workflow prompts and skill assets.
    WorkflowAssets {
        #[arg(long, default_value = "workflow-assets.md", value_name = "FILE")]
        output: String,
    },
    /// Export an existing generated report file.
    Report {
        #[arg(long, value_name = "FILE")]
        output: String,

        #[arg(long = "from", value_name = "PATH")]
        source: PathBuf,
    },
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum CompileKind {
    Source,
    Concept,
    Topic,
}

/// Pre-parse workaround for bare `--project` (optional value): clap would
/// otherwise consume the following subcommand name as the project root
/// (`gwiki --project status` → project "status"). Inserting an explicit "."
/// keeps bare `--project` meaning the current directory. Deliberate — do not
/// "simplify" by making the value required or removing the lookahead.
fn normalize_project_flag_args<I, S>(args: I) -> Vec<OsString>
where
    I: IntoIterator<Item = S>,
    S: Into<OsString>,
{
    let args = args.into_iter().map(Into::into).collect::<Vec<_>>();
    let mut normalized = Vec::with_capacity(args.len() + 1);
    for (index, arg) in args.iter().enumerate() {
        normalized.push(arg.clone());
        if arg == OsStr::new("--project")
            && args
                .get(index + 1)
                .and_then(|next| next.to_str())
                .is_some_and(is_cli_subcommand)
        {
            normalized.push(OsString::from("."));
        }
    }
    normalized
}

fn is_cli_subcommand(value: &str) -> bool {
    CLI_SUBCOMMANDS.contains(&value)
}

#[cfg(test)]
mod tests;
