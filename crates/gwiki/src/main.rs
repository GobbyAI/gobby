use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use gobby_wiki::{Command, ScopeSelection};

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

fn main() -> ExitCode {
    let cli = Cli::parse();

    match gobby_wiki::run(Command::from(cli.command)) {
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
            match error {
                gobby_wiki::WikiError::NotImplemented { .. } => ExitCode::from(2),
                _ => ExitCode::from(1),
            }
        }
    }
}

impl From<CliCommand> for Command {
    fn from(command: CliCommand) -> Self {
        match command {
            CliCommand::Init(scope) => Self::Init(scope.into()),
            CliCommand::Setup => Self::Setup,
            CliCommand::Index(scope) => Self::Index(scope.into()),
            CliCommand::IngestFile { path } => Self::IngestFile { path },
            CliCommand::Search(args) => Self::Search {
                query: args.query,
                scope: args.scope.into(),
            },
            CliCommand::Backlinks(args) => Self::Backlinks {
                page: args.page,
                scope: args.scope.into(),
            },
            CliCommand::Status => Self::Status,
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
