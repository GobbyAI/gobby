use std::ops::Range;

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};

use crate::commands::codewiki::inline_code;

pub(super) fn sanitize_model_markdown_links(text: &str) -> String {
    let replacements = unsafe_link_replacements(text);
    apply_replacements(text, replacements)
}

pub(crate) fn neutralize_symbol_purpose_links(text: &str) -> String {
    let code_ranges = markdown_code_ranges(text);
    let mut replacements = markdown_link_replacements(text);
    replacements.extend(wikilink_replacements(text, &code_ranges));
    apply_replacements(text, replacements)
}

fn markdown_link_replacements(text: &str) -> Vec<Replacement> {
    Parser::new_ext(text, Options::empty())
        .into_offset_iter()
        .filter_map(|(event, range)| match event {
            Event::Start(Tag::Link { .. }) => Some(replacement_for_range(text, range)),
            _ => None,
        })
        .collect()
}

fn markdown_code_ranges(text: &str) -> Vec<Range<usize>> {
    Parser::new_ext(text, Options::empty())
        .into_offset_iter()
        .filter_map(|(event, range)| match event {
            Event::Code(_) | Event::Start(Tag::CodeBlock(_)) => Some(range),
            _ => None,
        })
        .collect()
}

fn wikilink_replacements(text: &str, code_ranges: &[Range<usize>]) -> Vec<Replacement> {
    let mut replacements = Vec::new();
    let mut cursor = 0;

    while let Some(relative_start) = text[cursor..].find("[[") {
        let start = cursor + relative_start;
        let token_body_start = start + 2;
        if range_contains(code_ranges, start) {
            cursor = token_body_start;
            continue;
        }

        let Some(relative_end) = text[token_body_start..].find("]]") else {
            break;
        };
        let end = token_body_start + relative_end + 2;
        if !range_overlaps(code_ranges, start..end) {
            replacements.push(replacement_for_range(text, start..end));
        }
        cursor = end;
    }

    replacements
}

fn replacement_for_range(text: &str, range: Range<usize>) -> Replacement {
    Replacement {
        label: inline_code(&text[range.clone()]),
        range,
    }
}

fn range_contains(ranges: &[Range<usize>], index: usize) -> bool {
    ranges
        .iter()
        .any(|range| range.start <= index && index < range.end)
}

fn range_overlaps(ranges: &[Range<usize>], candidate: Range<usize>) -> bool {
    ranges
        .iter()
        .any(|range| range.start < candidate.end && candidate.start < range.end)
}

fn apply_replacements(text: &str, mut replacements: Vec<Replacement>) -> String {
    if replacements.is_empty() {
        return text.to_owned();
    }
    replacements.sort_by_key(|replacement| (replacement.range.start, replacement.range.end));

    let mut out = String::with_capacity(text.len());
    let mut cursor = 0;
    for replacement in replacements {
        if replacement.range.start < cursor {
            continue;
        }

        out.push_str(&text[cursor..replacement.range.start]);
        out.push_str(&replacement.label);
        cursor = replacement.range.end;
    }
    out.push_str(&text[cursor..]);
    out
}

#[derive(Debug)]
struct LinkFrame {
    range: Range<usize>,
    label: String,
}

#[derive(Debug)]
struct Replacement {
    range: Range<usize>,
    label: String,
}

fn unsafe_link_replacements(text: &str) -> Vec<Replacement> {
    let mut active_links = Vec::<LinkFrame>::new();
    let mut replacements = Vec::new();

    for (event, range) in Parser::new_ext(text, Options::empty()).into_offset_iter() {
        match event {
            Event::Start(Tag::Link { dest_url, .. }) if is_unsafe_link_target(&dest_url) => {
                active_links.push(LinkFrame {
                    range,
                    label: String::new(),
                });
            }
            Event::End(TagEnd::Link) => {
                if let Some(frame) = active_links.pop_if(|frame| frame.range == range) {
                    replacements.push(Replacement {
                        range: frame.range,
                        label: frame.label,
                    });
                }
            }
            Event::Text(value)
            | Event::Code(value)
            | Event::InlineMath(value)
            | Event::DisplayMath(value)
            | Event::Html(value)
            | Event::InlineHtml(value) => push_label_text(&mut active_links, &value),
            Event::FootnoteReference(value) => {
                push_label_text(&mut active_links, "[^");
                push_label_text(&mut active_links, &value);
                push_label_text(&mut active_links, "]");
            }
            Event::SoftBreak | Event::HardBreak => push_label_text(&mut active_links, "\n"),
            Event::TaskListMarker(checked) => {
                push_label_text(&mut active_links, if checked { "[x] " } else { "[ ] " });
            }
            Event::Start(_) | Event::End(_) | Event::Rule => {}
        }
    }

    replacements
}

fn push_label_text(active_links: &mut [LinkFrame], value: &str) {
    for frame in active_links {
        frame.label.push_str(value);
    }
}

fn is_unsafe_link_target(target: &str) -> bool {
    let target = target.trim();
    if target.is_empty() || target.starts_with('#') {
        return false;
    }

    if starts_with_ignore_ascii_case(target, "file:") {
        return true;
    }

    if is_windows_absolute_path(target) {
        return true;
    }

    if has_uri_scheme(target) {
        return false;
    }

    target.starts_with('/')
        || target.starts_with('\\')
        || target.starts_with('~')
        || contains_parent_dir_segment(target)
}

fn is_windows_absolute_path(target: &str) -> bool {
    let bytes = target.as_bytes();
    bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'/' | b'\\')
}

fn has_uri_scheme(target: &str) -> bool {
    let Some(colon) = target.find(':') else {
        return false;
    };
    let scheme = &target[..colon];
    let mut bytes = scheme.bytes();
    bytes
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'-' | b'.'))
}

fn contains_parent_dir_segment(target: &str) -> bool {
    let path = target.split(['?', '#']).next().unwrap_or(target);
    path.split(['/', '\\']).any(|segment| segment == "..")
}

fn starts_with_ignore_ascii_case(value: &str, prefix: &str) -> bool {
    value
        .get(..prefix.len())
        .is_some_and(|head| head.eq_ignore_ascii_case(prefix))
}

#[cfg(test)]
mod tests {
    use super::super::ground_text;
    use super::*;
    use crate::commands::codewiki::SourceSpan;

    fn span(file: impl Into<String>, line_start: usize, line_end: usize) -> SourceSpan {
        SourceSpan {
            file: file.into(),
            line_start,
            line_end,
        }
    }

    #[test]
    fn ground_text_strips_absolute_markdown_links_and_keeps_valid_citations() {
        let source_file = "crates/gcode/src/index/import_resolution/parser/mod.rs";
        let valid_spans = [span(source_file, 1, 8)];
        let text = concat!(
            "The parser uses [`mod.rs`](",
            "/Users/josh/Projects/gobby/crates/gcode/src/index/import_resolution/parser/mod.rs",
            ") [crates/gcode/src/index/import_resolution/parser/mod.rs:1]"
        );

        let grounded = ground_text(text, &valid_spans, Some("[fallback.rs:1]"));

        assert_eq!(
            "The parser uses mod.rs [crates/gcode/src/index/import_resolution/parser/mod.rs:1]",
            grounded
        );
    }

    #[test]
    fn ground_text_strips_invalid_citations_then_sanitizes_links_before_fallback() {
        let valid_spans = [span("crates/gcode/src/lib.rs", 3, 3)];
        let grounded = ground_text(
            "See [bad.rs:99] through [bad](file:///tmp/bad.rs)",
            &valid_spans,
            Some("[crates/gcode/src/lib.rs:3]"),
        );

        assert!(!grounded.contains("[bad.rs:99]"));
        assert!(!grounded.contains("file:///tmp/bad.rs"));
        assert!(grounded.contains("through bad"));
        assert!(grounded.ends_with("[crates/gcode/src/lib.rs:3]"));
    }

    #[test]
    fn ground_text_keeps_bare_anchor_and_skips_the_trailing_citation_dump() {
        // The page system prompts ask for bare `file:line` anchors, so a
        // well-cited page commonly carries no `[...]` citation. Such a page must
        // not also get the trailing bracket-citation dump appended on top of it
        // (#895): the bare anchor already grounds the claim.
        let file = "crates/gcode/src/search/rrf.rs";
        let valid_spans = [span(file, 1, 20)];
        let text = "The merge function delegates to gobby_core crates/gcode/src/search/rrf.rs:15-20.";
        let grounded = ground_text(
            text,
            &valid_spans,
            Some("[crates/gcode/src/search/rrf.rs:1]\n[crates/gcode/src/search/rrf.rs:7]"),
        );

        assert_eq!(text, grounded);
        assert!(!grounded.contains("[crates/gcode/src/search/rrf.rs:1]"));
    }

    #[test]
    fn ground_text_still_appends_fallback_when_page_is_uncited() {
        // The fallback dump is preserved for a genuinely uncited page, so the
        // #895 fix does not strip the only anchor from an otherwise-bare page.
        let valid_spans = [span("crates/gcode/src/lib.rs", 3, 3)];
        let grounded = ground_text(
            "A summary paragraph with no anchors at all.",
            &valid_spans,
            Some("[crates/gcode/src/lib.rs:3]"),
        );

        assert!(grounded.ends_with("[crates/gcode/src/lib.rs:3]"));
    }

    #[test]
    fn strips_traversal_windows_unc_file_and_tilde_targets_to_label_text() {
        let text = concat!(
            "[up](../secret.md) ",
            "[drive](C:/Users/josh/file.rs) ",
            r"[unc](\\server\share\file.rs) ",
            "[file](file:///tmp/file.rs) ",
            "[tilde](~/file.rs)"
        );

        let sanitized = sanitize_model_markdown_links(text);

        assert_eq!("up drive unc file tilde", sanitized);
    }

    #[test]
    fn keeps_external_anchors_safe_relative_plain_brackets_and_code_links() {
        let text = concat!(
            "Keep [site](https://example.com/a) ",
            "[anchor](#section) ",
            "[rel](docs/guide.md) ",
            "[plain brackets] ",
            "`[bad](/Users/josh/file.rs)`\n\n",
            "```md\n[bad](/Users/josh/file.rs)\n```"
        );

        assert_eq!(text, sanitize_model_markdown_links(text));
    }

    #[test]
    fn neutralizes_literal_wikilinks_in_symbol_purpose() {
        let text = "The renderer emits [[relative_path|title]] as prose.";

        assert_eq!(
            "The renderer emits `[[relative_path|title]]` as prose.",
            neutralize_symbol_purpose_links(text)
        );
    }

    #[test]
    fn neutralizes_literal_markdown_links_in_symbol_purpose() {
        let text = "The renderer mentions [text](path/to/page.md) as prose.";

        assert_eq!(
            "The renderer mentions `[text](path/to/page.md)` as prose.",
            neutralize_symbol_purpose_links(text)
        );
    }

    #[test]
    fn neutralizing_symbol_purpose_links_leaves_code_spans_and_fences_unchanged() {
        let text = concat!(
            "Keep `[[inline|code]]` and `[code](path.md)`.\n\n",
            "```md\n",
            "[[fenced|code]]\n",
            "[fenced](path.md)\n",
            "```"
        );

        assert_eq!(text, neutralize_symbol_purpose_links(text));
    }

    #[test]
    fn neutralizing_symbol_purpose_links_leaves_source_citations_plain() {
        let text = "Purpose stays grounded [file.rs:10].";

        assert_eq!(text, neutralize_symbol_purpose_links(text));
    }
}
