use super::types::SynthesisSource;

pub(super) fn render_frontmatter(
    markdown: &mut String,
    title: &str,
    source_kind: &str,
    handoff_id: &str,
    synthesis_mode: &str,
    degraded_sources: &[&str],
) {
    markdown.push_str("---\n");
    markdown.push_str("title: ");
    markdown.push_str(&yaml_scalar(title));
    markdown.push('\n');
    markdown.push_str("source_kind: ");
    markdown.push_str(&yaml_scalar(source_kind));
    markdown.push('\n');
    markdown.push_str("tags:\n");
    markdown.push_str("  - gwiki\n");
    markdown.push_str("  - compiled\n");
    markdown.push_str("compile_handoff: ");
    markdown.push_str(&yaml_scalar(handoff_id));
    markdown.push('\n');
    markdown.push_str("synthesis_mode: ");
    markdown.push_str(&yaml_scalar(synthesis_mode));
    markdown.push('\n');
    if !degraded_sources.is_empty() {
        markdown.push_str("degraded: true\n");
        markdown.push_str("degraded_sources:\n");
        for source in degraded_sources {
            markdown.push_str("  - ");
            markdown.push_str(source);
            markdown.push('\n');
        }
    }
    markdown.push_str("---\n\n");
}

pub(super) fn render_source_excerpts(markdown: &mut String, sources: &[SynthesisSource]) {
    if sources.is_empty() {
        markdown.push_str("No accepted sources were recorded.\n\n");
        return;
    }

    for source in sources {
        markdown.push_str("- ");
        markdown.push_str(&source.title);
        markdown.push_str(": ");
        if let Some(chunk) = source.chunks.first() {
            markdown.push_str(chunk);
        } else {
            markdown.push_str("Accepted source has no extracted body text.");
        }
        markdown.push('\n');
    }
    markdown.push('\n');
}

pub(super) fn render_list_section(markdown: &mut String, title: &str, values: &[String]) {
    markdown.push_str("## ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    if values.is_empty() {
        markdown.push_str("- None recorded.\n\n");
        return;
    }
    for value in values {
        markdown.push_str("- ");
        markdown.push_str(value);
        markdown.push('\n');
    }
    markdown.push('\n');
}

pub(super) fn yaml_scalar(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\0' => escaped.push_str("\\u0000"),
            '\u{0008}' => escaped.push_str("\\b"),
            '\u{000C}' => escaped.push_str("\\f"),
            '\u{007F}' => escaped.push_str("\\u007f"),
            ch if ch.is_control() => escaped.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    format!("\"{escaped}\"")
}
