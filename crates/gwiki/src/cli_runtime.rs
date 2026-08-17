use std::process::ExitCode;

use gobby_wiki::{WikiError, output};
use serde_json::json;

use crate::cli::Invocation;

pub(super) fn run() -> ExitCode {
    reset_sigpipe();
    let invocation = Invocation::parse_env();
    let format = invocation.format();
    let quiet = invocation.quiet();
    init_logger(quiet, invocation.verbose());

    if invocation.is_contract() {
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

    let command = match invocation.into_command() {
        Ok(command) => command,
        Err(error) => {
            print_error(format, &error);
            return exit_code_for_error(&error);
        }
    };

    match gobby_wiki::run(command, gobby_wiki::RunOptions { quiet }) {
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

/// Minimal stderr logger so crate-wide `log::warn!` diagnostics are visible.
///
/// `RUST_LOG` is parsed as a plain level (`error|warn|info|debug|trace`);
/// unset keeps logging off unless `--verbose` enables debug diagnostics, and
/// `--quiet` forces it off regardless.
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

pub(super) fn log_level(quiet: bool, verbose: bool, rust_log: Option<&str>) -> log::LevelFilter {
    if quiet {
        return log::LevelFilter::Off;
    }
    let configured = rust_log
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(log::LevelFilter::Off);
    if verbose {
        configured.max(log::LevelFilter::Debug)
    } else {
        configured
    }
}

fn init_logger(quiet: bool, verbose: bool) {
    let rust_log = std::env::var("RUST_LOG").ok();
    let _ = log::set_logger(&STDERR_LOGGER);
    log::set_max_level(log_level(quiet, verbose, rust_log.as_deref()));
    log::debug!("verbose diagnostics enabled");
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

/// JSON stderr envelope for `--format json`.
///
/// Grant and non-grant errors share the same keys:
/// - `error`: stable typed code from [`WikiError::code`]
/// - `message`: human-readable text
/// - `code`: compatibility alias of `error` (same value)
fn error_payload(error: &WikiError) -> serde_json::Value {
    let code = error.code();
    json!({
        "error": code,
        "code": code,
        "message": error.to_string(),
    })
}

fn print_error(format: output::Format, error: &WikiError) {
    match format {
        output::Format::Json => {
            let mut stderr = std::io::stderr().lock();
            if output::print_json(&mut stderr, &error_payload(error)).is_err() {
                eprintln!("gwiki: {error}");
            }
        }
        output::Format::Text => eprintln!("gwiki: {error}"),
    }
}

fn exit_code_for_error(error: &WikiError) -> ExitCode {
    match error {
        WikiError::NotImplemented { .. }
        | WikiError::InvalidInput { .. }
        | WikiError::Index { .. }
        | WikiError::Search { .. }
        | WikiError::InvalidScope { .. }
        | WikiError::NotFound { .. }
        | WikiError::AlreadyExists { .. }
        | WikiError::PreconditionFailed { .. } => ExitCode::from(2),
        WikiError::Grant { source } => ExitCode::from(source.exit_status() as u8),
        WikiError::Config { .. }
        | WikiError::Io { .. }
        | WikiError::Json { .. }
        | WikiError::Yaml { .. }
        | WikiError::Registry { .. }
        | WikiError::Daemon { .. }
        | WikiError::Timeout { .. }
        | WikiError::Freshness { .. }
        | WikiError::Generation { .. } => ExitCode::from(1),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::grant::GrantError;

    fn assert_stable_envelope(error: &WikiError) {
        let payload = error_payload(error);
        let object = payload.as_object().expect("error envelope object");
        let mut keys: Vec<_> = object.keys().cloned().collect();
        keys.sort();
        assert_eq!(keys, ["code", "error", "message"]);
        assert_eq!(payload["error"], payload["code"]);
        assert_eq!(payload["error"], error.code());
        assert_eq!(payload["message"], error.to_string());
    }

    #[test]
    fn grant_json_error_uses_stable_envelope_keys() {
        let error = WikiError::from(GrantError::DaemonRequired);
        assert_stable_envelope(&error);
        assert_eq!(error_payload(&error)["error"], "daemon_required");
    }

    #[test]
    fn non_grant_json_error_uses_the_same_envelope_keys() {
        let error = WikiError::Config {
            detail: "missing hub".to_string(),
        };
        assert_stable_envelope(&error);
        assert_eq!(error_payload(&error)["error"], "config_error");
    }
}
