//! Per-CLI hook-dispatcher configuration.
//!
//! The sole registry of host CLIs Gobby dispatches for and, per CLI, which
//! hooks are "critical" (block on failure) plus the failure exit codes; no
//! Python mirror exists.

use std::collections::HashSet;

/// Per-CLI dispatcher knobs. Frozen at compile time — CLIs are a closed set.
#[derive(Debug, Clone)]
pub struct CliConfig {
    /// Source identifier sent to the daemon.
    pub source: &'static str,
    /// Hooks where failure should fail-closed.
    pub critical_hooks: HashSet<&'static str>,
    /// Exit code for malformed JSON input. Zero fails open: the host gets its
    /// skip JSON on stdout and the parse error on stderr, because AGY blocks
    /// the tool on any non-zero `PreToolUse` exit.
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
            "qwen" => Some(Self {
                source: "qwen",
                critical_hooks: ["SessionStart", "SessionEnd", "PreCompact"]
                    .into_iter()
                    .collect(),
                json_error_exit_code: 1,
            }),
            "codex" => Some(Self {
                source: "codex",
                critical_hooks: ["SessionStart", "SessionEnd", "PreCompact"]
                    .into_iter()
                    .collect(),
                json_error_exit_code: 2,
            }),
            "agy" => Some(Self {
                source: "agy",
                critical_hooks: HashSet::new(),
                json_error_exit_code: 0,
            }),
            "grok" => Some(Self {
                source: "grok",
                critical_hooks: ["session_start", "session_end", "pre_compact"]
                    .into_iter()
                    .collect(),
                json_error_exit_code: 2,
            }),
            "droid" => Some(Self {
                source: "droid",
                critical_hooks: ["SessionStart", "SessionEnd", "PreCompact"]
                    .into_iter()
                    .collect(),
                json_error_exit_code: 1,
            }),
            _ => None,
        }
    }

    pub fn is_critical_hook(&self, hook_type: &str) -> bool {
        self.critical_hooks.contains(hook_type)
    }

    pub fn malformed_input_exit_code(&self, hook_type: &str) -> u8 {
        if self.source == "qwen" || self.source == "codex" {
            if self.is_critical_hook(hook_type) {
                2
            } else {
                1
            }
        } else {
            self.json_error_exit_code
        }
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
        assert!(c.critical_hooks.contains("session-end"));
        assert!(c.critical_hooks.contains("pre-compact"));
        assert!(!c.critical_hooks.contains("Stop"));
        assert!(!c.critical_hooks.contains("SessionStart"));
    }

    #[test]
    fn codex_lifecycle_hooks_critical_and_stop_noncritical() {
        let c = CliConfig::for_cli("codex").unwrap();
        assert!(c.is_critical_hook("SessionStart"));
        assert!(c.is_critical_hook("SessionEnd"));
        assert!(c.is_critical_hook("PreCompact"));
        assert!(!c.is_critical_hook("Stop"));
        assert!(!c.is_critical_hook("PreToolUse"));
        assert_eq!(c.json_error_exit_code, 2);
        assert_eq!(c.malformed_input_exit_code("SessionStart"), 2);
        assert_eq!(c.malformed_input_exit_code("Stop"), 1);
    }

    #[test]
    fn qwen_current_critical_hooks() {
        let c = CliConfig::for_cli("qwen").unwrap();
        assert_eq!(c.source, "qwen");
        assert!(c.is_critical_hook("SessionStart"));
        assert!(c.is_critical_hook("SessionEnd"));
        assert!(c.is_critical_hook("PreCompact"));
        assert!(!c.is_critical_hook("Stop"));
        assert!(!c.is_critical_hook("PreToolUse"));
        assert_eq!(c.malformed_input_exit_code("SessionStart"), 2);
        assert_eq!(c.malformed_input_exit_code("Stop"), 1);
        assert_eq!(c.malformed_input_exit_code("PreToolUse"), 1);
    }

    #[test]
    fn agy_uses_antigravity_hook_contract() {
        let c = CliConfig::for_cli("agy").unwrap();
        assert_eq!(c.source, "agy");
        assert!(c.critical_hooks.is_empty());
        assert!(!c.is_critical_hook("SessionStart"));
        assert_eq!(c.json_error_exit_code, 0);
        for hook in [
            "PreInvocation",
            "PreToolUse",
            "PostToolUse",
            "PostInvocation",
            "Stop",
        ] {
            assert!(!c.is_critical_hook(hook), "{hook} must not be critical");
            assert_eq!(
                c.malformed_input_exit_code(hook),
                0,
                "{hook} must fail open on malformed input"
            );
        }
    }

    #[test]
    fn grok_registry_uses_native_snake_case_hooks() {
        let c = CliConfig::for_cli("grok").unwrap();
        assert_eq!(c.source, "grok");
        assert_eq!(c.json_error_exit_code, 2);
        for hook in ["session_start", "session_end", "pre_compact"] {
            assert!(c.is_critical_hook(hook), "{hook} should be critical");
        }
        assert!(!c.is_critical_hook("stop"));
        assert!(!c.is_critical_hook("pre_tool_use"));
        assert!(!c.is_critical_hook("Stop"));
    }

    #[test]
    fn droid_lifecycle_hooks_critical_and_stop_noncritical() {
        let c = CliConfig::for_cli("droid").unwrap();
        assert_eq!(c.source, "droid");
        assert!(c.is_critical_hook("SessionStart"));
        assert!(c.is_critical_hook("SessionEnd"));
        assert!(c.is_critical_hook("PreCompact"));
        assert!(!c.is_critical_hook("Stop"));
        assert!(!c.is_critical_hook("PreToolUse"));
        assert_eq!(c.json_error_exit_code, 1);
        assert_eq!(c.malformed_input_exit_code("SessionStart"), 1);
        assert_eq!(c.malformed_input_exit_code("Stop"), 1);
    }

    #[test]
    fn unknown_cli_returns_none() {
        assert!(CliConfig::for_cli("unsupported").is_none());
    }

    #[test]
    fn cli_name_is_case_insensitive() {
        assert!(CliConfig::for_cli("CLAUDE").is_some());
        assert!(CliConfig::for_cli("Codex").is_some());
        assert!(CliConfig::for_cli("Droid").is_some());
        assert!(CliConfig::for_cli("GROK").is_some());
    }

    #[test]
    fn installer_cli_names_are_recognized_with_matching_sources() {
        for cli in ["claude", "grok", "agy", "qwen", "codex", "droid"] {
            let config = CliConfig::for_cli(cli).expect("installer CLI must be recognized");
            assert_eq!(config.source, cli, "{cli} must preserve its source");
        }
    }
}
