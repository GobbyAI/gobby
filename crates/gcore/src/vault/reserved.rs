//! Reserved page filenames that collide with agent instruction files.
//!
//! Vault pages are synthesized from session digests and, for url/text
//! sources, third-party content. On case-insensitive filesystems (macOS APFS
//! default) a page named `claude.md` matches Claude Code's `CLAUDE.md`
//! instruction-file lookup, so reading any file in that directory injects the
//! entire wiki page into future agent contexts as trusted directory-scoped
//! instructions — a prompt-injection surface (#17645). Writers deconflict
//! slugs before deriving filenames; `gwiki upkeep` migrates pages that
//! predate the guard.

/// Case-insensitive filename stems that agent CLIs load as instruction files
/// when suffixed with `.md`. Dotfile rule names (`.cursorrules`,
/// `.clinerules`, ...) cannot collide with `<slug>.md` pages because slugs
/// never start with a dot, so they are not listed.
pub const RESERVED_INSTRUCTION_STEMS: &[&str] = &[
    "claude",
    "claude.local",
    "agents",
    "gemini",
    "qwen",
    "copilot-instructions",
];

/// True when `stem` (a page filename without its `.md` extension)
/// case-insensitively matches an agent instruction filename.
pub fn is_reserved_instruction_stem(stem: &str) -> bool {
    RESERVED_INSTRUCTION_STEMS
        .iter()
        .any(|reserved| stem.eq_ignore_ascii_case(reserved))
}

/// Append `-{suffix}` to a page slug that would collide with an agent
/// instruction filename; safe slugs pass through unchanged.
pub fn deconflict_reserved_slug(slug: &str, suffix: &str) -> String {
    if is_reserved_instruction_stem(slug) {
        format!("{slug}-{suffix}")
    } else {
        slug.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserved_stems_match_case_insensitively() {
        assert!(is_reserved_instruction_stem("claude"));
        assert!(is_reserved_instruction_stem("CLAUDE"));
        assert!(is_reserved_instruction_stem("Gemini"));
        assert!(is_reserved_instruction_stem("claude.LOCAL"));
        assert!(!is_reserved_instruction_stem("claude-concept"));
        assert!(!is_reserved_instruction_stem("claudecode"));
        assert!(!is_reserved_instruction_stem(""));
    }

    #[test]
    fn deconflict_suffixes_only_reserved_slugs() {
        assert_eq!(
            deconflict_reserved_slug("claude", "concept"),
            "claude-concept"
        );
        assert_eq!(deconflict_reserved_slug("agents", "topic"), "agents-topic");
        assert_eq!(deconflict_reserved_slug("qwen", "source"), "qwen-source");
        assert_eq!(deconflict_reserved_slug("gcode", "concept"), "gcode");
    }
}
