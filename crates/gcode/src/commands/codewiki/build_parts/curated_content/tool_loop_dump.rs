use std::path::{Path, PathBuf};

use super::CuratedPageKind;

/// Where tool-loop failure dumps land for a run (#17533). The
/// `GOBBY_CODEWIKI_TOOL_LOOP_DUMP_DIR` override redirects dumps to a scratch
/// directory (e.g. mid-bakeoff); otherwise they default to the output's
/// `_meta/tool_loop/`, which the doc walkers never visit, so diagnostics are
/// always captured without polluting the page/ingest surfaces. Resolved once
/// by the CLI runtime and threaded down as data — library code never reads
/// the environment.
pub(crate) fn resolve_tool_loop_dump_dir(env_override: Option<&str>, out_dir: &Path) -> PathBuf {
    match env_override {
        Some(dir) if !dir.trim().is_empty() => PathBuf::from(dir),
        _ => out_dir.join("_meta").join("tool_loop"),
    }
}

/// Filesystem-safe slug for a curated page title, used to name a tool-loop failure
/// dump. Non-alphanumeric runs collapse to single underscores so the path stays
/// predictable for offline replay.
fn tool_loop_dump_slug(title: &str) -> String {
    let mut slug = String::with_capacity(title.len());
    let mut last_underscore = false;
    for c in title.chars() {
        if c.is_ascii_alphanumeric() {
            slug.push(c.to_ascii_lowercase());
            last_underscore = false;
        } else if !last_underscore {
            slug.push('_');
            last_underscore = true;
        }
    }
    let trimmed = slug.trim_matches('_');
    if trimmed.is_empty() {
        "page".to_string()
    } else {
        trimmed.to_string()
    }
}

/// Diagnostic dump of a tool-loop curated hard-fail: write the page's system
/// prompt, seed prompt, raw model output, post-verify text, and grounded text
/// to `<dump_dir>/<slug>.dump.md` so a hard-fail is reproducible offline
/// (replay the captured prompt against the model) without re-running the whole
/// pipeline. `dump_dir` comes from [`resolve_tool_loop_dump_dir`] via the run
/// options; `None` (tests, library callers) is a no-op.
// Each parameter is one section of the dump artifact; a struct would only
// restate the dump format.
#[allow(clippy::too_many_arguments)]
pub(super) fn maybe_dump_tool_loop_failure(
    dump_dir: Option<&Path>,
    kind: CuratedPageKind,
    title: &str,
    system: &str,
    prompt: &str,
    raw_text: &str,
    verified_text: &str,
    grounded: &str,
) {
    let Some(dir) = dump_dir else {
        return;
    };
    let kind_name = match kind {
        CuratedPageKind::Concept => "Concept",
        CuratedPageKind::Narrative => "Narrative",
    };
    let path = dir.join(format!("{}.dump.md", tool_loop_dump_slug(title)));
    let dump = format!(
        "# Tool-loop curated hard-fail dump\n\n\
         - title: {title}\n- kind: {kind_name}\n\
         - raw_bytes: {}\n- verified_bytes: {}\n- grounded_bytes: {}\n\n\
         ## SYSTEM\n\n{system}\n\n## SEED PROMPT\n\n{prompt}\n\n\
         ## RAW MODEL OUTPUT\n\n{raw_text}\n\n\
         ## POST-VERIFY\n\n{verified_text}\n\n## GROUNDED\n\n{grounded}\n",
        raw_text.len(),
        verified_text.len(),
        grounded.trim().len(),
    );
    if let Err(err) = std::fs::create_dir_all(dir).and_then(|()| std::fs::write(&path, dump)) {
        eprintln!("warning: failed to write tool-loop failure dump to {path:?}: {err}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_loop_dump_slug_collapses_punctuation_and_trims() {
        assert_eq!(
            tool_loop_dump_slug("Core Logic Engine"),
            "core_logic_engine"
        );
        assert_eq!(tool_loop_dump_slug("indexing_engine"), "indexing_engine");
        assert_eq!(tool_loop_dump_slug("  CLI / API!  "), "cli_api");
        // Degenerate titles still produce a usable filename stem.
        assert_eq!(tool_loop_dump_slug("///"), "page");
    }

    #[test]
    fn dump_dir_defaults_under_meta_and_env_override_wins() {
        let out = Path::new("/vault/wiki");
        // Default: diagnostics live under `_meta/`, which the doc walkers
        // never visit — dumps can never appear among generated pages (#17533).
        assert_eq!(
            resolve_tool_loop_dump_dir(None, out),
            Path::new("/vault/wiki/_meta/tool_loop")
        );
        assert_eq!(
            resolve_tool_loop_dump_dir(Some("/scratch/arm-s"), out),
            Path::new("/scratch/arm-s")
        );
        // A blank override is treated as unset, not as the output tree root.
        assert_eq!(
            resolve_tool_loop_dump_dir(Some("   "), out),
            Path::new("/vault/wiki/_meta/tool_loop")
        );
    }

    #[test]
    fn dump_writes_slugged_file_into_dump_dir_and_none_is_a_noop() {
        let dir = tempfile::tempdir().expect("dump tempdir");
        let dump_dir = dir.path().join("_meta").join("tool_loop");
        maybe_dump_tool_loop_failure(
            Some(&dump_dir),
            CuratedPageKind::Concept,
            "Core Logic Engine",
            "SYSTEM PROMPT",
            "SEED PROMPT",
            "raw output",
            "verified output",
            "grounded output",
        );
        let written = std::fs::read_to_string(dump_dir.join("core_logic_engine.dump.md"))
            .expect("dump file exists at <dir>/<slug>.dump.md");
        for fragment in [
            "- title: Core Logic Engine",
            "- kind: Concept",
            "SYSTEM PROMPT",
            "SEED PROMPT",
            "raw output",
            "verified output",
            "grounded output",
        ] {
            assert!(written.contains(fragment), "dump is missing {fragment:?}");
        }

        let untouched = dir.path().join("untouched");
        maybe_dump_tool_loop_failure(
            None,
            CuratedPageKind::Narrative,
            "No Dump",
            "s",
            "p",
            "r",
            "v",
            "g",
        );
        assert!(
            !untouched.exists() && !dump_dir.join("no_dump.dump.md").exists(),
            "a `None` dump dir must write nothing"
        );
    }
}
