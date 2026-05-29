use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use gobby_wiki::{Command, ScopeSelection, output};

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

fn main() -> ExitCode {
    let cli = Cli::parse();

    match gobby_wiki::run(command_from_cli(cli.command, cli.scope.into())) {
        Ok(outcome) => {
            if !cli.quiet {
                let mut stderr = std::io::stderr().lock();
                for message in &outcome.status_messages {
                    if let Err(error) = output::print_status(&mut stderr, message) {
                        eprintln!("gwiki: failed to write status: {error}");
                        return ExitCode::from(1);
                    }
                }
            }

            let stdout = std::io::stdout().lock();
            if let Err(error) = output::print_result(stdout, cli.format, &outcome.result) {
                eprintln!("gwiki: {error}");
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("gwiki: {error}");
            match error {
                gobby_wiki::WikiError::NotImplemented { .. } => ExitCode::from(2),
            }
        }
    }
}

fn command_from_cli(command: CliCommand, scope: ScopeSelection) -> Command {
    match command {
        CliCommand::Init => Command::Init { scope },
        CliCommand::Setup => Command::Setup { scope },
        CliCommand::Index => Command::Index { scope },
        CliCommand::IngestFile { path } => Command::IngestFile { path, scope },
        CliCommand::Search(args) => Command::Search {
            query: args.query,
            scope,
            limit: args.limit,
        },
        CliCommand::Backlinks(args) => Command::Backlinks {
            page: args.page,
            scope,
        },
        CliCommand::LinkSuggest(args) => Command::LinkSuggest {
            scope,
            limit: args.limit,
        },
        CliCommand::Status => Command::Status { scope },
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
