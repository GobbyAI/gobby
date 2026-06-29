use super::CuratedPageKind;

/// Filesystem-safe slug for a curated page title, used to name a Lane B failure
/// dump. Non-alphanumeric runs collapse to single underscores so the path stays
/// predictable for offline replay.
fn lane_b_dump_slug(title: &str) -> String {
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

/// Diagnostic-only dump of a Lane B curated hard-fail. When
/// `GOBBY_CODEWIKI_LANE_B_DUMP_DIR` is set, write the page's system prompt, seed
/// prompt, raw model output, post-verify text, and grounded text to
/// `<dir>/<slug>.dump.md` so a hard-fail is reproducible offline (replay the
/// captured prompt against the model) without re-running the whole pipeline.
/// Off by default: no env var means a no-op, so production runs are unaffected.
pub(super) fn maybe_dump_lane_b_failure(
    kind: CuratedPageKind,
    title: &str,
    system: &str,
    prompt: &str,
    raw_text: &str,
    verified_text: &str,
    grounded: &str,
) {
    let Ok(dir) = std::env::var("GOBBY_CODEWIKI_LANE_B_DUMP_DIR") else {
        return;
    };
    if dir.trim().is_empty() {
        return;
    }
    let kind_name = match kind {
        CuratedPageKind::Concept => "Concept",
        CuratedPageKind::Narrative => "Narrative",
    };
    let path = std::path::Path::new(&dir).join(format!("{}.dump.md", lane_b_dump_slug(title)));
    let dump = format!(
        "# Lane B curated hard-fail dump\n\n\
         - title: {title}\n- kind: {kind_name}\n\
         - raw_bytes: {}\n- verified_bytes: {}\n- grounded_bytes: {}\n\n\
         ## SYSTEM\n\n{system}\n\n## SEED PROMPT\n\n{prompt}\n\n\
         ## RAW MODEL OUTPUT\n\n{raw_text}\n\n\
         ## POST-VERIFY\n\n{verified_text}\n\n## GROUNDED\n\n{grounded}\n",
        raw_text.len(),
        verified_text.len(),
        grounded.trim().len(),
    );
    if let Err(err) = std::fs::create_dir_all(&dir).and_then(|()| std::fs::write(&path, dump)) {
        eprintln!("warning: failed to write Lane B failure dump to {path:?}: {err}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lane_b_dump_slug_collapses_punctuation_and_trims() {
        assert_eq!(lane_b_dump_slug("Core Logic Engine"), "core_logic_engine");
        assert_eq!(lane_b_dump_slug("indexing_engine"), "indexing_engine");
        assert_eq!(lane_b_dump_slug("  CLI / API!  "), "cli_api");
        // Degenerate titles still produce a usable filename stem.
        assert_eq!(lane_b_dump_slug("///"), "page");
    }
}
