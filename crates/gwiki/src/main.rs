use std::process::ExitCode;

mod cli;
mod cli_runtime;

fn main() -> ExitCode {
    cli_runtime::run()
}
