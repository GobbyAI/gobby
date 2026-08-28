//! Concise clap parse-error rendering for agent-readable usage errors.

use clap::error::{ContextKind, ContextValue, ErrorKind};

use crate::cli_error::CliError;

pub(super) fn handle_parse_error(error: clap::Error) -> anyhow::Result<()> {
    if is_passthrough(&error) {
        error.print()?;
        return Ok(());
    }
    Err(usage_error_from_clap(&error).into())
}

pub(super) fn is_passthrough(error: &clap::Error) -> bool {
    matches!(
        error.kind(),
        ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
    )
}

pub(super) fn usage_error_from_clap(error: &clap::Error) -> CliError {
    CliError {
        code: "usage",
        message: concise_message(error),
        recovery: recovery_for(offending_token(error).as_deref()).map(str::to_string),
        exit_status: 2,
    }
}

fn concise_message(error: &clap::Error) -> String {
    let rendered = error.to_string();
    let mut first = None;
    let mut usage = None;
    for line in rendered.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if first.is_none() {
            first = Some(trimmed.to_string());
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("Usage:") {
            usage = Some(format!("Usage:{}", rest));
            break;
        }
    }
    match (first, usage) {
        (Some(first), Some(usage)) => format!("{first} {usage}"),
        (Some(first), None) => first,
        (None, Some(usage)) => usage,
        (None, None) => "invalid arguments".to_string(),
    }
}

fn offending_token(error: &clap::Error) -> Option<String> {
    for (kind, value) in error.context() {
        match (kind, value) {
            (ContextKind::InvalidArg, ContextValue::String(token))
            | (ContextKind::InvalidSubcommand, ContextValue::String(token)) => {
                return Some(token.clone());
            }
            _ => {}
        }
    }
    first_quoted(&error.to_string())
}

fn first_quoted(text: &str) -> Option<String> {
    let start = text.find('\'')?;
    let rest = &text[start + 1..];
    let end = rest.find('\'')?;
    Some(rest[..end].to_string())
}

fn recovery_for(token: Option<&str>) -> Option<&'static str> {
    Some(match token {
        Some("-e" | "--regexp") => "pass the pattern as the first positional argument",
        Some("-c" | "--count") => "use `--format json` and read `matched_lines`",
        Some("-t" | "--type") => "use `-g` with a glob such as `-g '*.py'`",
        Some("--no-freshness") => "use `--allow-stale`",
        Some("-S" | "--smart-case") => "use `-i`",
        Some("-v" | "--invert-match") => "not supported by indexed grep; use raw `rg`",
        _ => "run this command with --help to see supported arguments",
    })
}

#[cfg(test)]
mod tests {
    use clap::Parser as _;

    use super::{is_passthrough, usage_error_from_clap};
    use crate::cli::Cli;

    fn parse_err(args: &[&str]) -> clap::Error {
        match Cli::try_parse_from(std::iter::once("gcode").chain(args.iter().copied())) {
            Err(error) => error,
            Ok(_) => panic!("expected clap parse failure for {args:?}"),
        }
    }

    fn json_line(error: &crate::cli_error::CliError) -> String {
        serde_json::to_string(&error.json_payload()).expect("cli error json")
    }

    #[test]
    fn unknown_grep_flag_renders_one_usage_json_line() {
        let rendered = usage_error_from_clap(&parse_err(&["grep", "--count", "needle"]));
        assert_eq!(rendered.code, "usage");
        assert_eq!(rendered.exit_status, 2);
        assert!(
            rendered.message.contains("Usage:"),
            "message should include usage: {}",
            rendered.message
        );
        assert!(
            !rendered.message.contains("Commands:"),
            "command listing leaked: {}",
            rendered.message
        );
        assert_eq!(
            rendered.recovery.as_deref(),
            Some("use `--format json` and read `matched_lines`")
        );
        let line = json_line(&rendered);
        assert!(!line.contains('\n'), "stderr must be one JSON line: {line}");
        let value: serde_json::Value = serde_json::from_str(&line).expect("json");
        assert_eq!(value["error"], "usage");
        assert_eq!(
            value["recovery"],
            "use `--format json` and read `matched_lines`"
        );
        assert!(
            value["message"]
                .as_str()
                .expect("message string")
                .contains("Usage:")
        );
    }

    #[test]
    fn unknown_top_level_subcommand_omits_command_listing() {
        let rendered = usage_error_from_clap(&parse_err(&["not-a-real-command"]));
        assert_eq!(rendered.code, "usage");
        assert!(rendered.message.contains("Usage:"), "{}", rendered.message);
        assert!(
            !rendered.message.contains("Commands:"),
            "command listing leaked: {}",
            rendered.message
        );
        for command in ["blast-radius", "embeddings", "invalidate", "search-content"] {
            assert!(
                !rendered.message.contains(command),
                "command listing leaked {command}: {}",
                rendered.message
            );
        }
        assert!(rendered.recovery.is_some());
        let line = json_line(&rendered);
        assert!(!line.contains('\n'));
    }

    #[test]
    fn no_freshness_suggests_allow_stale() {
        let rendered = usage_error_from_clap(&parse_err(&["--no-freshness", "tree"]));
        assert_eq!(rendered.code, "usage");
        assert_eq!(rendered.recovery.as_deref(), Some("use `--allow-stale`"));
        assert!(rendered.message.contains("Usage:"), "{}", rendered.message);
        assert!(
            !rendered.message.contains("Commands:"),
            "{}",
            rendered.message
        );
    }

    #[test]
    fn help_and_version_stay_passthrough() {
        let help = parse_err(&["--help"]);
        assert!(is_passthrough(&help));
        assert!(help.to_string().contains("Commands:"));
        let version = parse_err(&["--version"]);
        assert!(is_passthrough(&version));
        assert!(version.to_string().contains("gcode"));
    }
}
