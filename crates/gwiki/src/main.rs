use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use gobby_wiki::{Command, ScopeSelection, WikiError};

#[derive(Debug, Parser)]
#[command(name = "gwiki", version, about = "Gobby wiki CLI")]
struct Cli {
    #[command(subcommand)]
    command: CliCommand,
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Initialize a wiki vault.
    Init(ScopeArgs),
    /// Create gwiki-owned derived storage.
    Setup,
    /// Index markdown and source notes in the selected scope.
    Index(ScopeArgs),
    /// Capture a local source file into the wiki inbox.
    IngestFile {
        #[arg(value_name = "PATH")]
        path: PathBuf,
    },
    /// Search wiki documents in the selected scope.
    Search(SearchArgs),
    /// Show backlinks for a wiki page.
    Backlinks(BacklinksArgs),
    /// Dispatch research workers and checkpoint wiki research state.
    Research(ResearchArgs),
    /// Show shell readiness.
    Status,
}

#[derive(Debug, Args)]
struct ScopeArgs {
    /// Use the current Gobby project's wiki scope.
    #[arg(long, conflicts_with = "topic")]
    project: bool,

    /// Use a global topic wiki scope.
    #[arg(long, value_name = "NAME")]
    topic: Option<String>,
}

#[derive(Debug, Args)]
struct SearchArgs {
    #[command(flatten)]
    scope: ScopeArgs,

    #[arg(value_name = "QUERY")]
    query: String,
}

#[derive(Debug, Args)]
struct BacklinksArgs {
    #[command(flatten)]
    scope: ScopeArgs,

    #[arg(value_name = "PAGE")]
    page: String,
}

#[derive(Debug, Args)]
struct ResearchArgs {
    #[command(flatten)]
    scope: ScopeArgs,

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

fn main() -> ExitCode {
    let cli = Cli::parse();

    let command = match Command::try_from(cli.command) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("gwiki: {error}");
            return exit_code_for_error(&error);
        }
    };

    match gobby_wiki::run(command) {
        Ok(output) => {
            let mut stdout = std::io::stdout().lock();
            if let Err(error) = writeln!(stdout, "{}", output.message) {
                eprintln!("gwiki: failed to write output: {error}");
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

impl TryFrom<CliCommand> for Command {
    type Error = WikiError;

    fn try_from(command: CliCommand) -> Result<Self, Self::Error> {
        match command {
            CliCommand::Init(scope) => Ok(Self::Init(scope.into())),
            CliCommand::Setup => Ok(Self::Setup),
            CliCommand::Index(scope) => Ok(Self::Index(scope.into())),
            CliCommand::IngestFile { path } => Ok(Self::IngestFile { path }),
            CliCommand::Search(args) => Ok(Self::Search {
                query: args.query,
                scope: args.scope.into(),
            }),
            CliCommand::Backlinks(args) => Ok(Self::Backlinks {
                page: args.page,
                scope: args.scope.into(),
            }),
            CliCommand::Research(args) => {
                let scope_selection = ScopeSelection::from(args.scope);
                Ok(Self::Research(gobby_wiki::research::ResearchOptions {
                    question: args.question,
                    scope: gobby_wiki::research::resolve_scope(&scope_selection)?,
                    source_constraints: args.source_constraints,
                    agent_count: args.agent_count,
                    dispatch_task_id: args.task_id,
                    resume: args.resume,
                    accepted_notes: Vec::new(),
                }))
            }
            CliCommand::Status => Ok(Self::Status),
        }
    }
}

impl From<ScopeArgs> for ScopeSelection {
    fn from(scope: ScopeArgs) -> Self {
        Self {
            project: scope.project,
            topic: scope.topic,
        }
    }
}

fn exit_code_for_error(error: &WikiError) -> ExitCode {
    match error {
        WikiError::NotImplemented { .. } | WikiError::InvalidInput { .. } => ExitCode::from(2),
        WikiError::Io { .. } | WikiError::Json { .. } | WikiError::Daemon { .. } => {
            ExitCode::from(1)
        }
    }
}
