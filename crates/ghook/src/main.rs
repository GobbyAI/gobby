//! ghook — sandbox-tolerant hook dispatcher.
//!
//! Three modes:
//!   `ghook --gobby-owned --cli=<c> --type=<t> [--detach]`
//!   `ghook --diagnose    --cli=<c> --type=<t>`
//!   `ghook --version`
//!
//! Mode 1 enqueues an envelope to `~/.gobby/hooks/inbox/` and attempts a
//! POST to the daemon. The enqueue-first transport is an internal detail;
//! stdout, stderr, and exit codes follow the current per-CLI hook protocol.
//!
//! Mode 2 prints a JSON diagnostic, no network, no envelope write.
//!
//! Mode 3 prints the ghook version and writes
//! `~/.gobby/bin/.ghook-runtime.json` with `{schema_version, ghook_version}`.

use clap::Parser;
use std::process::ExitCode;

mod action;
mod args;
mod cli_config;
mod detach;
mod diagnose;
mod diagnostics;
mod dispatch;
mod envelope;
mod json_value;
mod output;
mod planned_shutdown;
mod runtime;
mod source;
mod statusline;
mod terminal_context;
#[cfg(test)]
mod test_http;
mod transport;

use args::Args;

fn main() -> ExitCode {
    let args = Args::parse();

    if args.version {
        return match runtime::write_runtime_stamp() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                // Still print the version; stamp-write failure is non-fatal.
                output::stdout(format_args!("ghook {}\n", diagnose::GHOOK_VERSION));
                output::stderr(format_args!("note: could not write runtime stamp: {e}\n"));
                ExitCode::SUCCESS
            }
        };
    }
    if args.diagnose {
        return run_diagnose(&args);
    }
    if args.gobby_owned {
        return dispatch::run_gobby_owned(&args);
    }

    eprintln!("ghook: no mode specified; use one of --gobby-owned, --diagnose, or --version");
    ExitCode::from(2)
}

fn run_diagnose(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        eprintln!("--diagnose requires --cli and --type");
        return ExitCode::from(2);
    };
    let out = diagnose::diagnose(cli, hook_type);
    match serde_json::to_string_pretty(&out) {
        Ok(s) => {
            output::stdout(format_args!("{s}\n"));
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("diagnose serialization failed: {e}");
            ExitCode::from(2)
        }
    }
}
