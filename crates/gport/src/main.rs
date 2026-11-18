use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use gobby_port::{census, git, ledger, record, shapes, slice, start};

#[derive(Parser)]
#[command(name = "gport", about = "Checks and records for the Gobby 1.0 port")]
struct Cli {
    /// Repository root. Defaults to the git top level of the current directory.
    #[arg(long, global = true)]
    root: Option<PathBuf>,
    #[command(subcommand)]
    verb: Verb,
}

#[derive(Subcommand)]
enum Verb {
    /// Check that a chunk's dependencies are closed and landed, and print the launch base.
    Start { chunk: String },
    /// Render the parity ledger from the frozen rows and check ownership and dispositions.
    Ledger {
        /// Print the ledger bytes and write nothing.
        #[arg(long)]
        stdout: bool,
        /// Exit 1 while any row is neither delegated nor dropped.
        #[arg(long)]
        exit_rule: bool,
        /// Print one `<row key>\t<chunk id>` line per row and nothing else.
        #[arg(long)]
        owners: bool,
    },
    /// Regenerate docs/port/record.md, keeping its hand-maintained blocks.
    Record,
    /// Check one chunk's commits and paths, or report the rate over several chunks.
    CheckSlice {
        #[arg(required = true)]
        chunks: Vec<String>,
        /// A task ref whose commits this slice may carry. Repeat for each subtask.
        #[arg(long)]
        task: Vec<String>,
        /// Print (partial+stub)/(done+partial+stub) over the named chunks.
        #[arg(long)]
        report: bool,
    },
    /// Print the caller, writer, cache, registry and surface census.
    Census {
        /// Generation store to read. Defaults to ~/.gobby/bin/generations.
        #[arg(long)]
        generations: Option<PathBuf>,
        /// Skip running the Python ledger generator for surfaces with no row.
        #[arg(long)]
        no_python: bool,
    },
    /// Print per-tool call counts and argument-key sets, with no values.
    Shapes {
        /// Transcript root to scan. Repeat for several. Defaults to the provider roots.
        #[arg(long)]
        transcripts: Vec<PathBuf>,
        /// Skip the daemon's tool metrics.
        #[arg(long)]
        no_daemon: bool,
    },
}

fn run(cli: Cli, out: &mut dyn Write) -> anyhow::Result<bool> {
    let root = match cli.root {
        Some(root) => root,
        None => git::top_level(&std::env::current_dir()?)?,
    };
    match cli.verb {
        Verb::Start { chunk } => start::run(&root, &chunk, out),
        Verb::Ledger {
            stdout,
            exit_rule,
            owners,
        } => ledger::run(
            &root,
            ledger::Options {
                stdout,
                exit_rule,
                owners,
            },
            out,
        ),
        Verb::Record => record::run(&root, out),
        Verb::CheckSlice {
            chunks,
            task,
            report,
        } => {
            if report {
                slice::report(&root, &chunks, out)
            } else if chunks.len() != 1 {
                anyhow::bail!("check-slice takes exactly one chunk id without --report")
            } else if task.is_empty() {
                anyhow::bail!("check-slice needs at least one --task <ref>")
            } else {
                slice::check(&root, &chunks[0], &task, out)
            }
        }
        Verb::Census {
            generations,
            no_python,
        } => census::run(
            &root,
            census::Options {
                generations,
                no_python,
            },
            out,
        ),
        Verb::Shapes {
            transcripts,
            no_daemon,
        } => shapes::run(
            shapes::Options {
                transcripts,
                no_daemon,
            },
            out,
        ),
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    match run(cli, &mut out) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(error) => {
            eprintln!("gport: {error:#}");
            ExitCode::from(2)
        }
    }
}
