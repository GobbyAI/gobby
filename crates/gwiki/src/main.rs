use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand, ValueEnum};
use gobby_wiki::{Command, ScopeSelection, WikiError, output};

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
    /// Initialize a wiki vault.
    Init,
    /// Create gwiki-owned derived storage.
    Setup,
    /// Index markdown and source notes in the selected scope.
    Index,
    /// Collect recognized inbox drops into raw storage.
    Collect,
    /// Capture a local source file into the wiki inbox.
    IngestFile {
        #[arg(value_name = "PATH")]
        path: PathBuf,
    },
    /// Search wiki documents in the selected scope.
    Search(SearchArgs),
    /// Show backlinks for a wiki page.
    Backlinks(BacklinksArgs),
    /// Suggest unresolved wiki links in the selected scope.
    LinkSuggest(LinkSuggestArgs),
    /// Dispatch research workers and checkpoint wiki research state.
    Research(ResearchArgs),
    /// Compile accepted research notes into wiki articles.
    Compile(CompileArgs),
    /// Export generated bundles and reports under outputs/.
    Export(ExportArgs),
    /// Report claims that lack source support.
    Audit,
    /// Detect broken links and vault hygiene issues.
    Lint,
    /// Write wiki health snapshots under meta/health.
    Health,
    /// Show shell readiness.
    Status,
}

#[derive(Debug, Args)]
struct ScopeArgs {
    /// Use the current Gobby project's wiki scope.
    #[arg(long, global = true, conflicts_with = "topic")]
    project: bool,

    /// Use a global topic wiki scope.
    #[arg(long, global = true, value_name = "NAME")]
    topic: Option<String>,
}

#[derive(Debug, Args)]
struct SearchArgs {
    #[arg(value_name = "QUERY")]
    query: String,

    #[arg(long, default_value = "10")]
    limit: usize,
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
struct ResearchArgs {
    #[arg(value_name = "QUESTION")]
    question: String,

    #[arg(long = "source-constraint", value_name = "TEXT")]
    source_constraints: Vec<String>,

    #[arg(long, default_value_t = 3, value_name = "N")]
    agent_count: usize,

    #[arg(long, value_name = "TASK")]
    task_id: Option<String>,

    #[arg(long)]
    resume: bool,
}

#[derive(Debug, Args)]
struct CompileArgs {
    #[arg(value_name = "TOPIC")]
    topic: Option<String>,

    #[arg(long = "outline", value_name = "HEADING")]
    outline: Vec<String>,

    #[arg(long, value_enum, default_value = "topic")]
    kind: CompileKind,

    #[arg(long, value_name = "PAGE")]
    target: Option<PathBuf>,

    #[arg(long = "write-intent")]
    write_intent: bool,
}

#[derive(Debug, Args)]
struct ExportArgs {
    #[command(subcommand)]
    command: ExportSubcommand,
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

fn main() -> ExitCode {
    let Cli {
        scope,
        format,
        quiet,
        command,
    } = Cli::parse();

    let command = match command_from_cli(command, scope.into()) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("gwiki: {error}");
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
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("gwiki: {error}");
            exit_code_for_error(&error)
        }
    }
}

fn command_from_cli(command: CliCommand, scope: ScopeSelection) -> Result<Command, WikiError> {
    match command {
        CliCommand::Init => Ok(Command::Init { scope }),
        CliCommand::Setup => Ok(Command::Setup { scope }),
        CliCommand::Index => Ok(Command::Index { scope }),
        CliCommand::Collect => Ok(Command::Collect { scope }),
        CliCommand::IngestFile { path } => Ok(Command::IngestFile { path, scope }),
        CliCommand::Search(args) => Ok(Command::Search {
            query: args.query,
            scope,
            limit: args.limit,
        }),
        CliCommand::Backlinks(args) => Ok(Command::Backlinks {
            page: args.page,
            scope,
        }),
        CliCommand::LinkSuggest(args) => Ok(Command::LinkSuggest {
            scope,
            limit: args.limit,
        }),
        CliCommand::Research(args) => {
            let research_scope = gobby_wiki::resolve_research_scope(&scope)?;
            Ok(Command::Research(gobby_wiki::research::ResearchOptions {
                question: args.question,
                scope: research_scope,
                source_constraints: args.source_constraints,
                agent_count: args.agent_count,
                dispatch_task_id: args.task_id,
                resume: args.resume,
                accepted_notes: Vec::new(),
            }))
        }
        CliCommand::Compile(args) => Ok(Command::Compile {
            topic: args.topic,
            outline: args.outline,
            target_kind: args.kind.into(),
            target_page: args.target,
            write_intent: args.write_intent,
            scope,
        }),
        CliCommand::Export(args) => Ok(Command::Export {
            scope,
            command: args.into(),
        }),
        CliCommand::Audit => Ok(Command::Audit { scope }),
        CliCommand::Lint => Ok(Command::Lint { scope }),
        CliCommand::Health => Ok(Command::Health { scope }),
        CliCommand::Status => Ok(Command::Status { scope }),
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

impl From<ScopeArgs> for ScopeSelection {
    fn from(scope: ScopeArgs) -> Self {
        if scope.project {
            Self::project()
        } else if let Some(topic) = scope.topic {
            Self::topic(topic)
        } else {
            Self::global()
        }
    }
}

fn exit_code_for_error(error: &WikiError) -> ExitCode {
    match error {
        WikiError::NotImplemented { .. }
        | WikiError::InvalidInput { .. }
        | WikiError::Index { .. }
        | WikiError::Search { .. }
        | WikiError::InvalidScope { .. } => ExitCode::from(2),
        WikiError::Config { .. }
        | WikiError::Io { .. }
        | WikiError::Json { .. }
        | WikiError::Yaml { .. }
        | WikiError::Registry { .. }
        | WikiError::Daemon { .. } => ExitCode::from(1),
    }
}
