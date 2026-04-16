//! `ghook --diagnose` — print what *would* happen for a given CLI/hook combo.
//!
//! Emits a JSON object validated against
//! `schemas/diagnose-output.v1.schema.json`. No network I/O, no envelope
//! write — this is a pure introspection surface so operators can confirm
//! configuration without spamming the inbox.

use crate::cli_config::CliConfig;
use gobby_core::{bootstrap, daemon_url, project};
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;

#[derive(Debug, Serialize)]
pub struct DiagnoseOutput {
    pub schema_version: u32,
    pub ghook_version: &'static str,
    pub cli: String,
    pub hook_type: String,
    pub source: Option<String>,
    pub critical: bool,
    pub terminal_context_enabled: bool,
    pub daemon_url: String,
    pub daemon_host: String,
    pub daemon_port: u16,
    pub project_root: Option<PathBuf>,
    pub project_id: Option<String>,
    pub terminal_context_preview: Option<Value>,
    pub cli_recognized: bool,
}

pub const DIAGNOSE_SCHEMA_VERSION: u32 = 1;
pub const GHOOK_VERSION: &str = env!("CARGO_PKG_VERSION");

pub fn diagnose(cli: &str, hook_type: &str) -> DiagnoseOutput {
    let cfg = CliConfig::for_cli(cli);
    let cli_recognized = cfg.is_some();

    let endpoint = bootstrap::read_daemon_endpoint();
    let url = daemon_url::daemon_url();

    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_root = project::find_project_root(&cwd);
    let project_id = project_root
        .as_ref()
        .and_then(|r| project::read_project_id(r).ok());

    let (source, critical, terminal_context_enabled, terminal_context_preview) = match cfg {
        Some(c) => {
            let critical = c.critical_hooks.contains(hook_type);
            let wants_ctx = c.wants_terminal_context(hook_type);
            let preview = if wants_ctx {
                Some(crate::terminal_context::capture())
            } else {
                None
            };
            (Some(c.source.to_string()), critical, wants_ctx, preview)
        }
        None => (None, false, false, None),
    };

    DiagnoseOutput {
        schema_version: DIAGNOSE_SCHEMA_VERSION,
        ghook_version: GHOOK_VERSION,
        cli: cli.to_string(),
        hook_type: hook_type.to_string(),
        source,
        critical,
        terminal_context_enabled,
        daemon_url: url,
        daemon_host: endpoint.host,
        daemon_port: endpoint.port,
        project_root,
        project_id,
        terminal_context_preview,
        cli_recognized,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_cli_marked_not_recognized() {
        let d = diagnose("cursor", "session-start");
        assert!(!d.cli_recognized);
        assert!(d.source.is_none());
        assert!(!d.critical);
        assert!(!d.terminal_context_enabled);
    }

    #[test]
    fn claude_session_start_is_critical_with_terminal_context() {
        let d = diagnose("claude", "session-start");
        assert!(d.cli_recognized);
        assert_eq!(d.source.as_deref(), Some("claude"));
        assert!(d.critical);
        assert!(d.terminal_context_enabled);
        assert!(d.terminal_context_preview.is_some());
    }

    #[test]
    fn codex_pre_tool_use_noncritical_with_terminal_context() {
        let d = diagnose("codex", "PreToolUse");
        assert!(d.cli_recognized);
        assert!(!d.critical);
        assert!(d.terminal_context_enabled);
    }

    #[test]
    fn diagnose_output_validates_against_v1_schema() {
        let schema_bytes = include_bytes!("../schemas/diagnose-output.v1.schema.json");
        let schema: serde_json::Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let out = diagnose("claude", "session-start");
        let v = serde_json::to_value(&out).unwrap();
        if let Err(errs) = compiled.validate(&v) {
            let msgs: Vec<_> = errs.map(|e| format!("{e}")).collect();
            panic!("diagnose output failed schema validation: {msgs:?}");
        }
    }

    #[test]
    fn diagnose_output_for_unknown_cli_validates() {
        let schema_bytes = include_bytes!("../schemas/diagnose-output.v1.schema.json");
        let schema: serde_json::Value = serde_json::from_slice(schema_bytes).unwrap();
        let compiled = jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles");
        let out = diagnose("cursor", "session-start");
        let v = serde_json::to_value(&out).unwrap();
        if let Err(errs) = compiled.validate(&v) {
            let msgs: Vec<_> = errs.map(|e| format!("{e}")).collect();
            panic!("diagnose output failed schema validation: {msgs:?}");
        }
    }
}
