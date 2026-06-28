use std::ffi::{OsStr, OsString};
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgGroup, Args, Parser, Subcommand, ValueEnum};
use gobby_core::config::AiRouting;
use gobby_wiki::{
    BenchmarkOptions, Command, IngestFileOptions, ReadTarget, ScopeSelection, SyncSessionsOptions,
    WikiError, output,
};
use serde_json::json;

const CLI_SUBCOMMANDS: &[&str] = &[
    "init",
    "contract",
    "setup",
    "index",
    "collect",
    "ingest-file",
    "ingest-url",
    "sync-sessions",
    "refresh",
    "sources",
    "remove-source",
    "search",
    "ask",
    "read",
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
    #[arg(long, global = true)]
    quiet: bool,

    #[command(subcommand)]
    command: CliCommand,
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Emit the CLI contract for daemon conformance tests.
    Contract,

    /// Initialize a wiki vault.
    Init,
    /// Create gwiki-owned derived storage.
    Setup(SetupArgs),
    /// Index markdown and source notes in the selected scope.
    Index,
    /// Collect recognized inbox drops into raw storage.
    Collect,
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
        /// Routing override for audio transcription and translation.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        transcription_routing: Option<AiRouting>,
        /// Routing override for vision extraction.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        vision_routing: Option<AiRouting>,
        /// Routing override for text generation.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        text_routing: Option<AiRouting>,
    },
    /// Fetch URL sources into the wiki inbox.
    IngestUrl {
        #[arg(value_name = "URL", num_args = 1..)]
        urls: Vec<String>,
    },
    /// Sync archived Gobby session transcripts into the wiki vault.
    SyncSessions(SyncSessionsArgs),
    /// Refresh URL-backed raw source records.
    Refresh(RefreshArgs),
    /// List raw source manifest entries in the selected scope.
    Sources,
    /// Remove a raw source, its manifest entry, and its raw asset.
    RemoveSource(RemoveSourceArgs),
    /// Search wiki documents in the selected scope.
    Search(SearchArgs),
    /// Ask a question about wiki documents in the selected scope.
    Ask(AskArgs),
    /// Read a wiki page or document in the selected scope.
    Read(ReadArgs),
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
    Graph,
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
    Librarian,
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
struct SetupArgs {
    /// Resolve or provision the shared Gobby hub before creating gwiki storage.
    #[arg(long)]
    standalone: bool,

    /// PostgreSQL URL to use for setup without persisting the flag value in output.
    #[arg(long = "database-url", value_name = "DSN")]
    database_url: Option<String>,

    /// Do not provision Docker services when no reachable hub is configured.
    #[arg(long)]
    no_services: bool,

    #[arg(long, value_name = "HOST")]
    falkordb_host: Option<String>,

    #[arg(long, value_name = "PORT")]
    falkordb_port: Option<u16>,

    #[arg(long, value_name = "PASSWORD")]
    falkordb_password: Option<String>,

    #[arg(long, value_name = "URL")]
    qdrant_url: Option<String>,

    #[arg(long, value_name = "PROVIDER")]
    embedding_provider: Option<String>,

    #[arg(long, value_name = "URL")]
    embedding_api_base: Option<String>,

    #[arg(long, value_name = "MODEL")]
    embedding_model: Option<String>,

    #[arg(long, value_name = "PREFIX")]
    embedding_query_prefix: Option<String>,

    #[arg(long, value_name = "DIM")]
    embedding_vector_dim: Option<usize>,

    #[arg(long, value_name = "KEY")]
    embedding_api_key: Option<String>,
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
}

#[derive(Debug, Args)]
struct AskArgs {
    #[arg(value_name = "QUESTION")]
    question: String,

    /// Synthesize an answer from retrieved wiki hits.
    #[arg(long)]
    llm: bool,

    /// AI routing override for synthesis. Inert unless --llm is set.
    #[arg(long, default_value = "auto", value_name = "auto|daemon|direct|off")]
    ai: AiRouting,

    /// Fail if synthesis is requested but no AI route succeeds.
    #[arg(long = "require-ai")]
    require_ai: bool,

    /// Trim retrieval hits to fit an approximate token budget, emitting a narrowing hint.
    #[arg(long = "token-budget", value_name = "N", value_parser = parse_positive_usize)]
    token_budget: Option<usize>,
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

    /// AI routing for explainer synthesis over accepted sources.
    #[arg(long, default_value = "auto", value_name = "auto|daemon|direct|off")]
    ai: AiRouting,
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

/// Minimal stderr logger so crate-wide `log::warn!` diagnostics are visible.
///
/// `RUST_LOG` is parsed as a plain level (`error|warn|info|debug|trace`);
/// unset keeps logging off so default invocations stay quiet, and `--quiet`
/// forces it off regardless.
struct StderrLogger;

static STDERR_LOGGER: StderrLogger = StderrLogger;

impl log::Log for StderrLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            eprintln!("gwiki: {}: {}", record.level(), record.args());
        }
    }

    fn flush(&self) {}
}

fn log_level(quiet: bool, rust_log: Option<&str>) -> log::LevelFilter {
    if quiet {
        return log::LevelFilter::Off;
    }
    rust_log
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(log::LevelFilter::Off)
}

fn init_logger(quiet: bool) {
    let rust_log = std::env::var("RUST_LOG").ok();
    let _ = log::set_logger(&STDERR_LOGGER);
    log::set_max_level(log_level(quiet, rust_log.as_deref()));
}

/// Restore the default `SIGPIPE` disposition so a closed stdout (e.g. piping to
/// `head`) terminates the process quietly instead of panicking inside `println!`.
///
/// The Rust runtime ignores `SIGPIPE` at startup, turning a closed downstream
/// pipe into an `EPIPE` that `print!`/`println!` escalate to a panic. Resetting
/// it to `SIG_DFL` makes every print site behave like a standard Unix CLI.
#[cfg(unix)]
fn reset_sigpipe() {
    // SAFETY: called once at startup before any threads are spawned; resetting a
    // signal to its default disposition is async-signal-safe.
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
}

#[cfg(not(unix))]
fn reset_sigpipe() {}

fn main() -> ExitCode {
    reset_sigpipe();
    let Cli {
        scope,
        format,
        quiet,
        command,
    } = Cli::parse_from(normalize_project_flag_args(std::env::args_os()));
    init_logger(quiet);

    if matches!(&command, CliCommand::Contract) {
        let mut stdout = std::io::stdout().lock();
        let result = match format {
            output::Format::Json => {
                output::print_json(&mut stdout, &gobby_wiki::contract::contract())
            }
            output::Format::Text => output::print_text(&mut stdout, "gwiki CLI contract v1"),
        };
        if let Err(error) = result {
            eprintln!("gwiki: {error}");
            return ExitCode::from(1);
        }
        return ExitCode::SUCCESS;
    }

    let command = match command_from_cli(command, scope.into()) {
        Ok(command) => command,
        Err(error) => {
            print_error(format, &error);
            return exit_code_for_error(&error);
        }
    };

    match gobby_wiki::run(command) {
        Ok(outcome) => {
            if !quiet {
                for message in &outcome.status_messages {
                    output::print_status(message);
                }
            }

            let stdout = std::io::stdout().lock();
            if let Err(error) = output::print_result(stdout, format, &outcome.result) {
                eprintln!("gwiki: {error}");
                return ExitCode::from(1);
            }
            ExitCode::from(outcome.exit_code)
        }
        Err(error) => {
            print_error(format, &error);
            exit_code_for_error(&error)
        }
    }
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

fn print_error(format: output::Format, error: &WikiError) {
    match format {
        output::Format::Json => {
            let payload = json!({
                "code": error.code(),
                "message": error.to_string(),
            });
            let mut stderr = std::io::stderr().lock();
            if output::print_json(&mut stderr, &payload).is_err() {
                eprintln!("gwiki: {error}");
            }
        }
        output::Format::Text => eprintln!("gwiki: {error}"),
    }
}

fn command_from_cli(command: CliCommand, scope: ScopeSelection) -> Result<Command, WikiError> {
    match command {
        CliCommand::Contract => unreachable!("contract command is handled before runtime dispatch"),
        CliCommand::Init => Ok(Command::Init { scope }),
        CliCommand::Setup(args) => Ok(Command::Setup {
            scope,
            options: args.into(),
        }),
        CliCommand::Index => Ok(Command::Index { scope }),
        CliCommand::Collect => Ok(Command::Collect { scope }),
        CliCommand::IngestFile {
            path,
            no_ai,
            translate,
            target_lang,
            video_frame_interval_seconds,
            transcription_routing,
            vision_routing,
            text_routing,
        } => Ok(Command::IngestFile {
            path,
            scope,
            options: IngestFileOptions {
                no_ai,
                translate,
                target_lang,
                video_frame_interval_seconds,
                transcription_routing,
                vision_routing,
                text_routing,
            },
        }),
        CliCommand::IngestUrl { urls } => Ok(Command::IngestUrl { urls, scope }),
        CliCommand::SyncSessions(args) => Ok(Command::SyncSessions {
            scope,
            options: SyncSessionsOptions {
                archive_dir: args.archive_dir,
                wiki_dir: args.wiki_dir,
                limit: args.limit,
                raw: args.raw,
                summarize: args.summarize,
            },
        }),
        CliCommand::Refresh(args) => Ok(Command::Refresh {
            scope,
            source_ids: args.id,
            dry_run: args.dry_run,
        }),
        CliCommand::Sources => Ok(Command::Sources { scope }),
        CliCommand::RemoveSource(args) => {
            if args.dry_run && args.yes {
                return Err(WikiError::InvalidInput {
                    field: "remove-source",
                    message: "pass only one of --dry-run or --yes".to_string(),
                });
            }
            if !args.dry_run && !args.yes {
                return Err(WikiError::InvalidInput {
                    field: "remove-source",
                    message: "destructive source removal requires --yes; use --dry-run to preview"
                        .to_string(),
                });
            }
            Ok(Command::RemoveSource {
                id: args.id,
                scope,
                dry_run: args.dry_run,
                keep_asset: args.keep_asset,
            })
        }
        CliCommand::Search(args) => Ok(Command::Search {
            query: args.query,
            scope,
            limit: args.limit,
            include_semantic: !args.no_semantic,
            token_budget: args.token_budget,
        }),
        CliCommand::Ask(args) => Ok(Command::Ask {
            query: args.question,
            scope,
            llm: args.llm,
            ai: args.ai,
            require_ai: args.require_ai,
            token_budget: args.token_budget,
        }),
        CliCommand::Read(args) => {
            let target = match (args.path, args.title) {
                (Some(path), None) => ReadTarget::Path(path),
                (None, Some(title)) => ReadTarget::Title(title),
                _ => {
                    return Err(WikiError::InvalidInput {
                        field: "read",
                        message: "pass exactly one of --path or --title".to_string(),
                    });
                }
            };
            Ok(Command::Read { target, scope })
        }
        CliCommand::Backlinks(args) => Ok(Command::Backlinks {
            page: args.page,
            scope,
        }),
        CliCommand::LinkSuggest(args) => Ok(Command::LinkSuggest {
            scope,
            limit: args.limit,
        }),
        CliCommand::Benchmark(args) => Ok(Command::Benchmark {
            scope,
            options: BenchmarkOptions {
                retrieval_candidates: args.retrieval_candidates,
            },
        }),
        CliCommand::Compile(args) => Ok(Command::Compile {
            topic: args.topic,
            outline: args.outline,
            source: args.source,
            target_kind: args.kind.into(),
            target_page: args.target,
            write_intent: args.write_intent,
            ai: args.ai,
            scope,
        }),
        CliCommand::Export(args) => Ok(Command::Export {
            scope,
            command: args.into(),
        }),
        CliCommand::Graph => Ok(Command::Graph { scope }),
        CliCommand::GraphContext => Ok(Command::GraphContext { scope }),
        CliCommand::ReviewReport(args) => Ok(Command::ReviewReport {
            scope,
            options: args.into(),
        }),
        CliCommand::Audit => Ok(Command::Audit { scope }),
        CliCommand::Lint => Ok(Command::Lint { scope }),
        CliCommand::Normalize(args) => Ok(Command::Normalize {
            scope,
            check: args.check,
        }),
        CliCommand::Health => Ok(Command::Health { scope }),
        CliCommand::Librarian => Ok(Command::Librarian { scope }),
        CliCommand::Status => Ok(Command::Status { scope }),
        CliCommand::Trust => Ok(Command::Trust { scope }),
        CliCommand::CitationQuality => Ok(Command::CitationQuality { scope }),
    }
}

impl From<CompileKind> for gobby_wiki::synthesis::ArticleKind {
    fn from(kind: CompileKind) -> Self {
        match kind {
            CompileKind::Source => Self::Source,
            CompileKind::Concept => Self::Concept,
            CompileKind::Topic => Self::Topic,
        }
    }
}

impl From<ExportArgs> for gobby_wiki::exports::ExportCommand {
    fn from(args: ExportArgs) -> Self {
        match args.command {
            ExportSubcommand::WorkflowAssets { output } => {
                Self::WorkflowAssets { filename: output }
            }
            ExportSubcommand::Report { output, source } => Self::ReportFile {
                filename: output,
                source_path: source,
            },
        }
    }
}

impl From<ReviewReportArgs> for gobby_wiki::ReviewReportOptions {
    fn from(args: ReviewReportArgs) -> Self {
        Self {
            files: args.files,
            symbols: args.symbols,
            diff_path: args.diff_path,
            output: args.output,
        }
    }
}

impl From<ScopeArgs> for ScopeSelection {
    fn from(scope: ScopeArgs) -> Self {
        if let Some(topic) = scope.topic {
            Self::topic(topic)
        } else if let Some(project_root) = scope.project {
            Self::project(project_root)
        } else {
            Self::detect()
        }
    }
}

fn exit_code_for_error(error: &WikiError) -> ExitCode {
    match error {
        WikiError::NotImplemented { .. }
        | WikiError::InvalidInput { .. }
        | WikiError::Index { .. }
        | WikiError::Search { .. }
        | WikiError::InvalidScope { .. }
        | WikiError::NotFound { .. } => ExitCode::from(2),
        WikiError::Config { .. }
        | WikiError::Io { .. }
        | WikiError::Json { .. }
        | WikiError::Yaml { .. }
        | WikiError::Registry { .. }
        | WikiError::Daemon { .. }
        | WikiError::Timeout { .. }
        | WikiError::Setup { .. }
        | WikiError::Generation { .. } => ExitCode::from(1),
    }
}

impl From<SetupArgs> for gobby_wiki::SetupOptions {
    fn from(args: SetupArgs) -> Self {
        Self {
            standalone: args.standalone,
            database_url: args.database_url,
            no_services: args.no_services,
            falkordb_host: args.falkordb_host,
            falkordb_port: args.falkordb_port,
            falkordb_password: args.falkordb_password,
            qdrant_url: args.qdrant_url,
            embedding_provider: args.embedding_provider,
            embedding_api_base: args.embedding_api_base,
            embedding_model: args.embedding_model,
            embedding_query_prefix: args.embedding_query_prefix,
            embedding_vector_dim: args.embedding_vector_dim,
            embedding_api_key: args.embedding_api_key,
        }
    }
}

#[cfg(test)]
mod main_tests;
