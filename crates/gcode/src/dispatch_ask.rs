use std::path::PathBuf;

use chrono::Utc;
use serde_json::{Value, json};

use crate::ask_client::{AskClient, StartAskRequest};
use crate::cli::{AskAction, AskArgs};
use crate::cli_error::CliError;
use crate::{config, daemon, output};

pub(crate) fn run(
    args: &AskArgs,
    project_override: Option<&str>,
    format: output::Format,
) -> anyhow::Result<()> {
    let project_id = resolve_project_id(project_override)?;
    let client = AskClient::new(project_id)?;

    match args.action() {
        AskAction::Start {
            question,
            commit_ref,
            timeout_seconds,
            retrieval,
            background,
        } => {
            let request = StartAskRequest {
                question,
                project_id: client.project_id(),
                commit_ref,
                timeout_seconds,
                retrieval_mode: retrieval.as_str(),
            };
            let started = client.start(&request)?;
            if background || is_terminal(&started) {
                return print_and_classify(&started, format);
            }
            let run_id = run_id(&started)?;
            eprintln!("Ask run {run_id}");
            let completed = wait_for_run(&client, run_id, Some(timeout_seconds))?;
            print_and_classify(&completed, format)
        }
        AskAction::Status { run_id } => {
            let result = client.get(run_id)?;
            print_and_classify(&result, format)
        }
        AskAction::Resume { run_id } => {
            let resumed = client.resume(run_id)?;
            if is_terminal(&resumed) {
                return print_and_classify(&resumed, format);
            }
            eprintln!("Ask run {run_id}");
            let completed = wait_for_run(&client, run_id, remaining_seconds(&resumed))?;
            print_and_classify(&completed, format)
        }
        AskAction::Cancel { run_id } => {
            let cancelled = client.cancel(run_id)?;
            print_and_classify(&cancelled, format)
        }
        AskAction::Export { run_id, output } => {
            let output_path = client.export(run_id, output)?;
            match format {
                output::Format::Json => output::print_json(&json!({
                    "run_id": run_id,
                    "status": "exported",
                    "output": output_path,
                }))?,
                output::Format::Text => output::print_text(&format!(
                    "Ask run {run_id} exported to {}",
                    output_path.display()
                ))?,
            }
            Ok(())
        }
    }
}

fn resolve_project_id(project_override: Option<&str>) -> anyhow::Result<String> {
    let root = match project_override {
        Some(value) => {
            let path = PathBuf::from(value);
            if path.is_dir() {
                path.canonicalize()?
            } else {
                let project = daemon::lookup_project_by_name(value)?;
                return Ok(project.id);
            }
        }
        None => config::detect_project_root()?,
    };
    Ok(config::resolve_project_identity(&root)?.project_id)
}

fn wait_for_run(
    client: &AskClient,
    run_id: &str,
    timeout_seconds: Option<u64>,
) -> Result<Value, CliError> {
    match client.wait(run_id, timeout_seconds) {
        Ok(result) => Ok(result),
        Err(error) if error.code == "ask_daemon_unavailable" => Err(CliError {
            code: "ask_wait_disconnected",
            message: format!(
                "Ask run {run_id} remains durable after the wait connection failed: {}",
                error.message
            ),
            recovery: Some(format!("resume with `gcode ask --resume {run_id}`")),
            exit_status: error.exit_status,
        }),
        Err(error) => Err(error),
    }
}

fn run_id(result: &Value) -> Result<&str, CliError> {
    result
        .get("run_id")
        .and_then(Value::as_str)
        .ok_or_else(|| CliError {
            code: "malformed_ask_response",
            message: "Ask response did not contain a run_id".to_owned(),
            recovery: None,
            exit_status: 2,
        })
}

fn is_terminal(result: &Value) -> bool {
    matches!(
        result.get("status").and_then(Value::as_str),
        Some("completed" | "failed" | "cancelled")
    )
}

fn remaining_seconds(result: &Value) -> Option<u64> {
    let deadline = result.get("deadline_at")?.as_str()?;
    let deadline = chrono::DateTime::parse_from_rfc3339(deadline).ok()?;
    let seconds = deadline.signed_duration_since(Utc::now()).num_seconds();
    Some(seconds.max(1) as u64)
}

fn print_and_classify(result: &Value, format: output::Format) -> anyhow::Result<()> {
    print_result(result, format)?;
    match result.get("status").and_then(Value::as_str) {
        Some("failed") => Err(run_failure(result, "ask_failed", 2).into()),
        Some("cancelled") => Err(run_failure(result, "ask_cancelled", 2).into()),
        _ => Ok(()),
    }
}

fn print_result(result: &Value, format: output::Format) -> anyhow::Result<()> {
    match format {
        output::Format::Json => output::print_json(result),
        output::Format::Text => {
            let mut fields = vec![
                format!("run_id: {}", display_field(result, "run_id")),
                format!("status: {}", display_field(result, "status")),
            ];
            for (key, label) in [
                ("current_stage", "stage"),
                ("answer_outcome", "outcome"),
                ("deadline_at", "deadline"),
            ] {
                if let Some(value) = result.get(key).and_then(Value::as_str) {
                    fields.push(format!("{label}: {value}"));
                }
            }
            if let Some(error) = result.get("typed_error").filter(|value| !value.is_null()) {
                fields.push(format!("error: {error}"));
            }
            output::print_text(&fields.join("\n"))
        }
    }
}

fn display_field<'a>(result: &'a Value, key: &str) -> &'a str {
    result.get(key).and_then(Value::as_str).unwrap_or("unknown")
}

fn run_failure(result: &Value, code: &'static str, exit_status: u8) -> CliError {
    let run_id = display_field(result, "run_id");
    let detail = result
        .get("typed_error")
        .filter(|value| !value.is_null())
        .map(Value::to_string)
        .unwrap_or_else(|| display_field(result, "status").to_owned());
    CliError {
        code,
        message: format!("Ask run {run_id} ended without a successful answer: {detail}"),
        recovery: None,
        exit_status,
    }
}
