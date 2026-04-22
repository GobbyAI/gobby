//! `ghook --diagnose` — print what *would* happen for a given CLI/hook combo.
//!
//! Emits a JSON object validated against
//! `schemas/diagnose-output.v2.schema.json`. No network I/O, no envelope
//! write — this is a pure introspection surface so operators can confirm
//! configuration without spamming the inbox.

use crate::cli_config::CliConfig;
use gobby_core::{bootstrap, daemon_url, project};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

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
    pub install_method: Option<String>,
    pub install_source_url: Option<String>,
}

pub const DIAGNOSE_SCHEMA_VERSION: u32 = 2;
pub const GHOOK_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Sidecar filename written by the installer next to the `ghook` binary.
/// See `docs/guides/ghook-development-guide.md` for the contract.
pub const INSTALL_SIDECAR_FILENAME: &str = ".ghook-install.json";

#[derive(Debug, Deserialize)]
struct InstallSidecar {
    install_method: Option<String>,
    install_source_url: Option<String>,
}

/// Read the install-provenance sidecar from `dir/.ghook-install.json` and
/// return `(install_method, install_source_url)`. Any failure (missing file,
/// unreadable, malformed JSON) collapses to `(None, None)` — the sidecar is
/// best-effort metadata, never load-bearing.
pub fn read_install_provenance(dir: &Path) -> (Option<String>, Option<String>) {
    let path = dir.join(INSTALL_SIDECAR_FILENAME);
    let Ok(bytes) = std::fs::read(&path) else {
        return (None, None);
    };
    let Ok(sidecar) = serde_json::from_slice::<InstallSidecar>(&bytes) else {
        return (None, None);
    };
    (sidecar.install_method, sidecar.install_source_url)
}

fn install_provenance_for_running_binary() -> (Option<String>, Option<String>) {
    let Ok(exe) = std::env::current_exe() else {
        return (None, None);
    };
    let Some(dir) = exe.parent() else {
        return (None, None);
    };
    read_install_provenance(dir)
}

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

    let (install_method, install_source_url) = install_provenance_for_running_binary();

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
        install_method,
        install_source_url,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

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

    fn compile_v2_schema() -> jsonschema::JSONSchema {
        let schema_bytes = include_bytes!("../schemas/diagnose-output.v2.schema.json");
        let schema: serde_json::Value = serde_json::from_slice(schema_bytes).unwrap();
        jsonschema::JSONSchema::options()
            .with_draft(jsonschema::Draft::Draft7)
            .compile(&schema)
            .expect("schema compiles")
    }

    fn assert_validates(schema: &jsonschema::JSONSchema, value: &serde_json::Value) {
        if let Err(errs) = schema.validate(value) {
            let msgs: Vec<_> = errs.map(|e| format!("{e}")).collect();
            panic!("diagnose output failed schema validation: {msgs:?}");
        }
    }

    #[test]
    fn diagnose_output_validates_against_v2_schema() {
        let compiled = compile_v2_schema();
        let out = diagnose("claude", "session-start");
        let v = serde_json::to_value(&out).unwrap();
        assert_validates(&compiled, &v);
    }

    #[test]
    fn diagnose_output_for_unknown_cli_validates() {
        let compiled = compile_v2_schema();
        let out = diagnose("cursor", "session-start");
        let v = serde_json::to_value(&out).unwrap();
        assert_validates(&compiled, &v);
    }

    #[test]
    fn schema_version_is_two() {
        let d = diagnose("claude", "session-start");
        assert_eq!(d.schema_version, 2);
    }

    #[test]
    fn install_provenance_absent_when_no_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let (method, url) = read_install_provenance(dir.path());
        assert!(method.is_none());
        assert!(url.is_none());
    }

    #[test]
    fn install_provenance_read_from_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(INSTALL_SIDECAR_FILENAME);
        let mut f = std::fs::File::create(&path).unwrap();
        write!(
            f,
            r#"{{
                "install_method": "github-release",
                "install_source_url": "https://github.com/GobbyAI/gobby-cli/releases/download/ghook-v0.3.0/ghook-aarch64-apple-darwin.tar.gz",
                "installed_version": "0.3.0",
                "installed_at": "2026-04-22T18:30:00Z"
            }}"#
        )
        .unwrap();
        let (method, url) = read_install_provenance(dir.path());
        assert_eq!(method.as_deref(), Some("github-release"));
        assert_eq!(
            url.as_deref(),
            Some(
                "https://github.com/GobbyAI/gobby-cli/releases/download/ghook-v0.3.0/ghook-aarch64-apple-darwin.tar.gz"
            )
        );
    }

    #[test]
    fn install_provenance_partial_sidecar_returns_present_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(INSTALL_SIDECAR_FILENAME);
        std::fs::write(&path, r#"{"install_method": "cargo-install"}"#).unwrap();
        let (method, url) = read_install_provenance(dir.path());
        assert_eq!(method.as_deref(), Some("cargo-install"));
        assert!(url.is_none());
    }

    #[test]
    fn install_provenance_malformed_json_collapses_to_none() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(INSTALL_SIDECAR_FILENAME);
        std::fs::write(&path, "not json at all {{").unwrap();
        let (method, url) = read_install_provenance(dir.path());
        assert!(method.is_none());
        assert!(url.is_none());
    }
}
