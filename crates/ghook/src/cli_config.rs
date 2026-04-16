//! Per-CLI hook-dispatcher configuration.
//!
//! Mirrors the `CLIConfig` registry in `hook_dispatcher.py` — the set of
//! host CLIs Gobby dispatches for and, per CLI, which hooks are "critical"
//! (block on failure, exit 2) and which should carry enriched terminal
//! context.

use std::collections::HashSet;

/// Per-CLI dispatcher knobs. Frozen at compile time — CLIs are a closed set.
#[derive(Debug, Clone)]
pub struct CliConfig {
    /// Source identifier sent to the daemon.
    pub source: &'static str,
    /// Hooks where failure should fail-closed (exit 2).
    pub critical_hooks: HashSet<&'static str>,
    /// Hooks that should carry enriched terminal context in `input_data`.
    pub terminal_context_hooks: HashSet<&'static str>,
}

impl CliConfig {
    pub fn for_cli(cli: &str) -> Option<Self> {
        match cli.to_ascii_lowercase().as_str() {
            "claude" => Some(Self {
                source: "claude",
                critical_hooks: ["session-start", "session-end", "pre-compact"]
                    .into_iter()
                    .collect(),
                terminal_context_hooks: ["session-start"].into_iter().collect(),
            }),
            "gemini" => Some(Self {
                source: "gemini",
                critical_hooks: ["SessionStart"].into_iter().collect(),
                terminal_context_hooks: ["SessionStart"].into_iter().collect(),
            }),
            "qwen" => Some(Self {
                source: "qwen",
                critical_hooks: ["SessionStart"].into_iter().collect(),
                terminal_context_hooks: ["SessionStart"].into_iter().collect(),
            }),
            "codex" => Some(Self {
                source: "codex",
                critical_hooks: ["SessionStart", "Stop"].into_iter().collect(),
                terminal_context_hooks: [
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                ]
                .into_iter()
                .collect(),
            }),
            _ => None,
        }
    }

    pub fn wants_terminal_context(&self, hook_type: &str) -> bool {
        self.terminal_context_hooks.contains(hook_type)
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
    fn codex_terminal_context_broad() {
        let c = CliConfig::for_cli("codex").unwrap();
        assert!(c.wants_terminal_context("PreToolUse"));
        assert!(c.wants_terminal_context("Stop"));
        assert!(!c.wants_terminal_context("session-start"));
    }

    #[test]
    fn gemini_session_only_terminal_context() {
        let c = CliConfig::for_cli("gemini").unwrap();
        assert!(c.wants_terminal_context("SessionStart"));
        assert!(!c.wants_terminal_context("PreToolUse"));
    }

    #[test]
    fn unknown_cli_returns_none() {
        assert!(CliConfig::for_cli("cursor").is_none());
    }

    #[test]
    fn cli_name_is_case_insensitive() {
        assert!(CliConfig::for_cli("CLAUDE").is_some());
        assert!(CliConfig::for_cli("Codex").is_some());
    }
}
