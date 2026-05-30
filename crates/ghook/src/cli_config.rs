//! Per-CLI hook-dispatcher configuration.
//!
//! Mirrors the `CLIConfig` registry in `hook_dispatcher.py` — the set of
//! host CLIs Gobby dispatches for and, per CLI, which hooks are "critical"
//! (block on failure, exit 2).

use std::collections::HashSet;

/// Per-CLI dispatcher knobs. Frozen at compile time — CLIs are a closed set.
#[derive(Debug, Clone)]
pub struct CliConfig {
    /// Source identifier sent to the daemon.
    pub source: &'static str,
    /// Hooks where failure should fail-closed (exit 2).
    pub critical_hooks: HashSet<&'static str>,
    /// Exit code to use for malformed JSON input, matching the Python dispatcher.
    pub json_error_exit_code: u8,
}

impl CliConfig {
    pub fn for_cli(cli: &str) -> Option<Self> {
        match cli.to_ascii_lowercase().as_str() {
            "claude" => Some(Self {
                source: "claude",
                critical_hooks: ["session-start", "session-end", "pre-compact"]
                    .into_iter()
                    .collect(),
                json_error_exit_code: 2,
            }),
            "gemini" => Some(Self {
                source: "gemini",
                critical_hooks: ["SessionStart"].into_iter().collect(),
                json_error_exit_code: 1,
            }),
            "qwen" => Some(Self {
                source: "qwen",
                critical_hooks: ["SessionStart"].into_iter().collect(),
                json_error_exit_code: 1,
            }),
            "codex" => Some(Self {
                source: "codex",
                critical_hooks: ["SessionStart", "Stop"].into_iter().collect(),
                json_error_exit_code: 2,
            }),
            "droid" => Some(Self {
                source: "droid",
                critical_hooks: HashSet::new(),
                json_error_exit_code: 1,
            }),
            _ => None,
        }
    }

    pub fn for_dispatch(cli: &str) -> Self {
        Self::for_cli(cli).unwrap_or_else(|| Self::for_cli("claude").expect("claude config"))
    }

    pub fn is_critical_hook(&self, hook_type: &str) -> bool {
        self.critical_hooks.contains(hook_type)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claude_critical_hooks() {
        let c = CliConfig::for_cli("claude").unwrap();
        assert_eq!(c.source, "claude");
        assert!(c.critical_hooks.contains("session-start"));
        assert!(c.critical_hooks.contains("pre-compact"));
        assert!(!c.critical_hooks.contains("SessionStart"));
    }

    #[test]
    fn gemini_json_parse_errors_exit_one() {
        let c = CliConfig::for_cli("gemini").unwrap();
        assert_eq!(c.json_error_exit_code, 1);
    }

    #[test]
    fn codex_stop_is_critical() {
        let c = CliConfig::for_cli("codex").unwrap();
        assert!(c.is_critical_hook("Stop"));
        assert!(!c.is_critical_hook("PreToolUse"));
        assert_eq!(c.json_error_exit_code, 2);
    }

    #[test]
    fn droid_recognized_with_no_critical_hooks() {
        let c = CliConfig::for_cli("droid").unwrap();
        assert_eq!(c.source, "droid");
        assert!(c.critical_hooks.is_empty());
        assert_eq!(c.json_error_exit_code, 1);
    }

    #[test]
    fn unknown_cli_returns_none() {
        assert!(CliConfig::for_cli("cursor").is_none());
    }

    #[test]
    fn cli_name_is_case_insensitive() {
        assert!(CliConfig::for_cli("CLAUDE").is_some());
        assert!(CliConfig::for_cli("Codex").is_some());
        assert!(CliConfig::for_cli("Droid").is_some());
    }

    #[test]
    fn unknown_cli_falls_back_to_claude_for_dispatch() {
        let c = CliConfig::for_dispatch("cursor");
        assert_eq!(c.source, "claude");
        assert!(c.is_critical_hook("session-start"));
        assert_eq!(c.json_error_exit_code, 2);
    }
}
