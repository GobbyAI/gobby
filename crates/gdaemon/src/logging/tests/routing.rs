use std::process::Command;

use gobby_core::json::{Json, PYTHON, dumps};
use gobby_core::telemetry::{SampleValue, Telemetry, names::LOGGING_RECORDS_TOTAL};
use tracing::dispatcher::with_default;

use super::*;
use crate::logging::classify::{Surface, parser_cli, primary_file_name, surface_for_target};

/// Set in the child process of the pre-configuration test.
const PRE_CONFIG_CHILD: &str = "GOBBY_LOGGING_TEST_PRE_CONFIG_CHILD";

/// The files of the six primary surfaces that hold `message`.
fn holders(dir: &Path, message: &str) -> Vec<&'static str> {
    PRIMARY_FILES
        .iter()
        .copied()
        .filter(|name| read(dir, name).contains(message))
        .collect()
}

/// The `logging_records_total` count for one surface and severity.
fn record_count(surface: &str, severity: &str) -> u64 {
    Telemetry::global()
        .snapshot()
        .into_iter()
        .filter(|sample| sample.name == LOGGING_RECORDS_TOTAL)
        .filter(|sample| {
            sample.labels.contains(&("surface", surface.to_owned()))
                && sample.labels.contains(&("severity", severity.to_owned()))
        })
        .map(|sample| match sample.value {
            SampleValue::Counter(count) => count,
            _ => 0,
        })
        .sum()
}

#[test]
fn each_target_routes_to_one_primary_file() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));

    with_default(&dispatch, || {
        tracing::info!(target: "gobby.hooks", "record for hooks");
        tracing::info!(target: "gobby.ai.text_generation", "record for llm");
        tracing::info!(target: "gobby.mcp", "record for mcp");
        tracing::info!(target: "gobby.scheduler", "record for automation");
        tracing::info!(target: "gobby.nodes.registry", "record for daemon");
        tracing::info!(target: "gobby.hooksx", "record whose prefix is not a segment");
    });

    let expected = [
        ("record for hooks", "hooks.log"),
        ("record for llm", "llm.log"),
        ("record for mcp", "mcp.log"),
        ("record for automation", "automation.log"),
        ("record for daemon", "daemon.log"),
        ("record whose prefix is not a segment", "daemon.log"),
    ];
    for (message, file) in expected {
        assert_eq!(holders(dir.path(), message), vec![file], "{message}");
    }
}

#[test]
fn non_gobby_targets_are_not_recorded() {
    assert_eq!(surface_for_target("deadpool.postgres"), None);
    assert_eq!(surface_for_target("hyper_util::client::legacy"), None);
    assert_eq!(surface_for_target("gobbyx.daemon"), None);

    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));
    with_default(&dispatch, || {
        tracing::warn!(target: "deadpool.postgres", "dependency pool warning");
        tracing::warn!(target: "hyper_util::client::legacy", "dependency client warning");
        tracing::warn!(target: "gobby.daemon", "daemon warning");
    });

    let written: String = files_under(dir.path())
        .iter()
        .map(|path| std::fs::read_to_string(path).unwrap_or_default())
        .collect();
    assert!(written.contains("daemon warning"), "{written}");
    assert!(!written.contains("dependency pool warning"), "{written}");
    assert!(!written.contains("dependency client warning"), "{written}");
}

#[test]
fn surface_table_matches_python_namespaces() {
    let table = [
        ("gobby.rule_allow_audit", Surface::AllowAudit),
        ("gobby.rule_allow_audit.x", Surface::AllowAudit),
        ("gobby.hooks", Surface::Hooks),
        ("gobby.hooks.ingress", Surface::Hooks),
        ("gobby.hooksx", Surface::Daemon),
        ("gobby.ai.text_generation", Surface::Llm),
        ("gobby.ai.text_generation.x", Surface::Llm),
        ("gobby.ai", Surface::Daemon),
        ("gobby.mcp", Surface::Mcp),
        ("gobby.mcp_proxy", Surface::Mcp),
        ("gobby.mcp_proxy.x", Surface::Mcp),
        ("gobby.servers.routes.mcp", Surface::Mcp),
        ("gobby.servers.routes.mcp.x", Surface::Mcp),
        ("gobby.servers.routes", Surface::Daemon),
        ("gobby.scheduler", Surface::Automation),
        ("gobby.system_automation", Surface::Automation),
        ("gobby.workflows.pipeline", Surface::Automation),
        ("gobby.workflows.pipeline_executor", Surface::Automation),
        ("gobby.workflows.pipeline_heartbeat", Surface::Automation),
        ("gobby.workflows.pipelinex", Surface::Daemon),
        ("gobby.parser_error.codex", Surface::Parser),
        ("gobby.daemon", Surface::Daemon),
        ("gobby", Surface::Daemon),
    ];
    let classified: Vec<(&str, Option<Surface>)> = table
        .iter()
        .map(|(target, _)| (*target, surface_for_target(target)))
        .collect();
    let expected: Vec<(&str, Option<Surface>)> = table
        .iter()
        .map(|(target, surface)| (*target, Some(*surface)))
        .collect();
    assert_eq!(classified, expected);

    let files = [
        (Surface::Daemon, "daemon.log"),
        (Surface::Hooks, "hooks.log"),
        (Surface::Llm, "llm.log"),
        (Surface::Mcp, "mcp.log"),
        (Surface::Automation, "automation.log"),
        (Surface::AllowAudit, "rule-allow-audit.jsonl"),
    ];
    for (surface, file) in files {
        assert_eq!(primary_file_name(surface), file, "{surface:?}");
    }
}

#[test]
fn warn_and_above_reach_errors_log_and_info_does_not() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));

    with_default(&dispatch, || {
        tracing::info!(target: "gobby.hooks", "hooks info record");
        tracing::warn!(target: "gobby.hooks", "hooks warning record");
        tracing::error!(target: "gobby.hooks", "hooks error record");
    });

    let hooks = read(dir.path(), "hooks.log");
    let errors = read(dir.path(), "errors.log");
    for message in [
        "hooks info record",
        "hooks warning record",
        "hooks error record",
    ] {
        assert!(
            hooks.contains(message),
            "{message} missing from hooks.log: {hooks}"
        );
    }
    assert!(errors.contains("hooks warning record"), "{errors}");
    assert!(errors.contains("hooks error record"), "{errors}");
    assert!(!errors.contains("hooks info record"), "{errors}");
}

#[test]
fn pipeline_guard_refused_warning_stays_out_of_errors_log() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));

    with_default(&dispatch, || {
        tracing::warn!(
            target: "gobby.workflows.pipeline",
            pipeline_guard_refused = true,
            "guard refused warning"
        );
        tracing::error!(
            target: "gobby.workflows.pipeline",
            pipeline_guard_refused = true,
            "guard refused error"
        );
    });

    let automation = read(dir.path(), "automation.log");
    let errors = read(dir.path(), "errors.log");
    assert!(automation.contains("guard refused warning"), "{automation}");
    assert!(automation.contains("guard refused error"), "{automation}");
    assert!(errors.contains("guard refused error"), "{errors}");
    assert!(!errors.contains("guard refused warning"), "{errors}");
}

#[test]
fn parser_target_writes_message_only_at_info_regardless_of_level() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let settings = LogSettings {
        level: Level::Warning,
        ..settings_in(dir.path())
    };
    let (dispatch, _handle) = routed(settings);

    with_default(&dispatch, || {
        tracing::info!(target: "gobby.parser_error.codex", "raw parser line");
        tracing::warn!(target: "gobby.parser_error.codex", "parser warning line");
    });

    assert_eq!(
        read(dir.path(), "codex-parser-error.log"),
        "raw parser line\nparser warning line\n"
    );
    let daemon = read(dir.path(), "daemon.log");
    assert!(!daemon.contains("raw parser line"), "{daemon}");
    assert!(!daemon.contains("parser warning line"), "{daemon}");
    let errors = read(dir.path(), "errors.log");
    assert!(errors.contains("parser warning line"), "{errors}");
    assert!(!errors.contains("raw parser line"), "{errors}");
}

#[test]
fn parser_cli_name_rejects_path_separators() {
    assert_eq!(parser_cli("gobby.parser_error.codex"), Some("codex"));
    assert_eq!(
        parser_cli("gobby.parser_error.claude-code_2"),
        Some("claude-code_2")
    );
    for rejected in [
        "gobby.parser_error.../etc",
        "gobby.parser_error.a/b",
        "gobby.parser_error.a.b",
        "gobby.parser_error.Codex",
        "gobby.parser_error.",
    ] {
        assert_eq!(parser_cli(rejected), None, "{rejected}");
    }

    let root = tempfile::tempdir().expect("temporary root");
    let logs = root.path().join("logs");
    let (dispatch, _handle) = routed(settings_in(&logs));
    with_default(&dispatch, || {
        tracing::info!(target: "gobby.parser_error.../etc", "escape attempt");
    });

    let daemon = read(&logs, "daemon.log");
    assert!(daemon.contains("escape attempt"), "{daemon}");
    assert!(
        daemon
            .lines()
            .any(|line| line.contains("WARNING") && line.contains("parser")),
        "no warning about the rejected parser name: {daemon}"
    );
    let outside: Vec<PathBuf> = files_under(root.path())
        .into_iter()
        .filter(|path| !path.starts_with(&logs))
        .collect();
    assert_eq!(outside, Vec::<PathBuf>::new());
    let parser_files: Vec<PathBuf> = files_under(root.path())
        .into_iter()
        .filter(|path| path.to_string_lossy().ends_with("-parser-error.log"))
        .collect();
    assert_eq!(parser_files, Vec::<PathBuf>::new());
}

#[test]
fn records_before_config_go_to_stderr_verbatim_and_create_no_file() {
    if std::env::var_os(PRE_CONFIG_CHILD).is_some() {
        // Child: only the pre-configuration sink is installed.
        install_pre_config();
        tracing::warn!(target: "gobby.daemon", "pre-config warning line");
        tracing::info!(target: "gobby.hooks", "pre-config info line");
        return;
    }

    let home = tempfile::tempdir().expect("temporary gobby home");
    let logs = home.path().join("logs");
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "logging::tests::routing::records_before_config_go_to_stderr_verbatim_and_create_no_file",
            "--nocapture",
        ])
        .env(PRE_CONFIG_CHILD, "1")
        .env("GOBBY_HOME", home.path())
        .env("GOBBY_LOGGING_DIR", &logs)
        .output()
        .expect("child test runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{stderr}");
    let lines: Vec<&str> = stderr.lines().collect();
    assert!(lines.contains(&"pre-config warning line"), "{stderr}");
    assert!(lines.contains(&"pre-config info line"), "{stderr}");
    assert!(
        !logs.exists(),
        "the pre-config sink created {}",
        logs.display()
    );
}

#[test]
fn llm_surface_rotates_on_llm_keys_only() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let settings = LogSettings {
        max_size_mb: 1,
        backup_count: 2,
        llm_max_size_mb: 4,
        llm_backup_count: 2,
        ..settings_in(dir.path())
    };
    let (dispatch, _handle) = routed(settings);
    let large = large_message('d');

    with_default(&dispatch, || {
        for _ in 0..2 {
            tracing::info!(target: "gobby.nodes.registry", "{large}");
            tracing::info!(target: "gobby.ai.text_generation", "{large}");
        }
    });

    assert!(
        exists(dir.path(), "daemon.log.1"),
        "daemon.log did not rotate at 1 MB"
    );
    assert!(
        !exists(dir.path(), "llm.log.1"),
        "llm.log rotated on the daemon keys"
    );
    let llm = read(dir.path(), "llm.log");
    assert!(llm.len() > 2 * MIB, "llm.log holds {} bytes", llm.len());
}

#[test]
fn text_line_and_json_fields_match_python_layout() {
    let record = Record {
        time: "2026-11-19T10:00:00".to_owned(),
        level: Level::Warning,
        target: "gobby.hooks".to_owned(),
        module: "ingress".to_owned(),
        message: "message".to_owned(),
        extras: vec![("k".to_owned(), "v".to_owned())],
    };
    assert_eq!(
        format_text(&record),
        "2026-11-19 10:00:00 - WARNING  - hooks - message | k=v"
    );
    let expected_json = r#"{"time": "2026-11-19T10:00:00", "level": "WARNING", "name": "gobby.hooks", "module": "ingress", "message": "message", "k": "v"}"#;
    assert_eq!(format_json(&record), Ok(expected_json.to_owned()));
    let same_object = Json::Object(
        [
            ("time", "2026-11-19T10:00:00"),
            ("level", "WARNING"),
            ("name", "gobby.hooks"),
            ("module", "ingress"),
            ("message", "message"),
            ("k", "v"),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_owned(), Json::Str(value.to_owned())))
        .collect(),
    );
    assert_eq!(format_json(&record), dumps(&same_object, PYTHON));

    // A message cannot forge a second line.
    let forged = Record {
        message: "first\r\n2026-01-01 00:00:00 - ERROR    - daemon - forged\u{1}\u{7f}".to_owned(),
        ..record
    };
    let line = format_text(&forged);
    assert!(line.contains(r"first\r\n2026-01-01"), "{line:?}");
    assert!(!line.chars().any(char::is_control), "{line:?}");

    // Through the layer, with the real clock and source module.
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));
    with_default(&dispatch, || {
        tracing::warn!(target: "gobby.hooks", k = "v", "message");
    });
    let text_line = fancy_regex::Regex::new(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - WARNING  - hooks - message \| k=v$",
    )
    .expect("pattern compiles");
    let hooks = read(dir.path(), "hooks.log");
    let lines: Vec<&str> = hooks.lines().collect();
    assert_eq!(lines.len(), 1, "{hooks}");
    assert_eq!(text_line.is_match(lines[0]).ok(), Some(true), "{hooks}");

    let json_dir = tempfile::tempdir().expect("temporary logs directory");
    let settings = LogSettings {
        format: Format::Json,
        ..settings_in(json_dir.path())
    };
    let (dispatch, _handle) = routed(settings);
    with_default(&dispatch, || {
        tracing::warn!(target: "gobby.hooks", k = "v", "message");
    });
    let hooks = read(json_dir.path(), "hooks.log");
    let lines: Vec<&str> = hooks.lines().collect();
    assert_eq!(lines.len(), 1, "{hooks}");
    let parsed: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(lines[0]).unwrap_or_default();
    let keys: Vec<&str> = parsed.keys().map(String::as_str).collect();
    assert_eq!(keys, ["time", "level", "name", "module", "message", "k"]);
    assert_eq!(parsed["level"], "WARNING");
    assert_eq!(parsed["name"], "gobby.hooks");
    assert_eq!(parsed["module"], "routing");
    assert_eq!(parsed["message"], "message");
    let time = fancy_regex::Regex::new(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        .expect("pattern compiles");
    let stamp = parsed["time"].as_str().unwrap_or_default();
    assert_eq!(time.is_match(stamp).ok(), Some(true), "{stamp}");
}

#[test]
fn warn_and_above_count_once_per_source_record() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));
    let warnings_before = record_count("hooks", "WARNING");
    let infos_before = record_count("hooks", "INFO");

    with_default(&dispatch, || {
        // This record lands in hooks.log and errors.log, and counts once.
        tracing::warn!(target: "gobby.hooks", "counted warning");
        tracing::info!(target: "gobby.hooks", "uncounted info");
    });

    assert_eq!(record_count("hooks", "WARNING"), warnings_before + 1);
    assert_eq!(record_count("hooks", "INFO"), infos_before);
}

#[test]
fn allow_audit_target_writes_rule_allow_audit_jsonl() {
    let dir = tempfile::tempdir().expect("temporary logs directory");
    let (dispatch, _handle) = routed(settings_in(dir.path()));

    with_default(&dispatch, || {
        tracing::info!(target: "gobby.rule_allow_audit", "allow audit entry");
    });

    assert_eq!(
        holders(dir.path(), "allow audit entry"),
        vec!["rule-allow-audit.jsonl"]
    );
}
