use crate::cli_config::CliConfig;

pub(crate) fn detect_source(cfg: &CliConfig) -> String {
    if cfg.source != "claude" {
        return cfg.source.to_string();
    }

    if let Some(source) = std::env::var_os("GOBBY_SOURCE")
        && !source.is_empty()
    {
        return source.to_string_lossy().into_owned();
    }
    cfg.source.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn clear_source_env() {
        // SAFETY: this module is the only unit-test code that touches these
        // process-wide source-detection variables.
        unsafe {
            std::env::remove_var("GOBBY_SOURCE");
            std::env::remove_var("CLAUDE_CODE_ENTRYPOINT");
        }
    }

    fn set_source_env(key: &str, value: &str) {
        // SAFETY: this module is the only unit-test code that touches these
        // process-wide source-detection variables.
        unsafe {
            std::env::set_var(key, value);
        }
    }

    struct SourceEnvGuard;

    impl SourceEnvGuard {
        fn new() -> Self {
            clear_source_env();
            Self
        }
    }

    impl Drop for SourceEnvGuard {
        fn drop(&mut self) {
            clear_source_env();
        }
    }

    #[test]
    fn detect_source_respects_override_only_for_claude() {
        let _env = SourceEnvGuard::new();

        assert_eq!(detect_source(&CliConfig::for_dispatch("claude")), "claude");

        set_source_env("CLAUDE_CODE_ENTRYPOINT", "sdk-py");
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("claude")),
            "claude",
            "entrypoint alone should not remap the source"
        );

        set_source_env("GOBBY_SOURCE", "session-override");
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("claude")),
            "session-override"
        );
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("codex")),
            "codex",
            "non-claude CLIs should keep their canonical source"
        );
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("agy")),
            "agy",
            "Antigravity CLI should keep its canonical source"
        );
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("grok")),
            "grok",
            "GOBBY_SOURCE should remain a Claude-only compatibility override"
        );

        set_source_env("GOBBY_SOURCE", "");
        assert_eq!(
            detect_source(&CliConfig::for_dispatch("claude")),
            "claude",
            "empty overrides should be ignored"
        );
    }
}
