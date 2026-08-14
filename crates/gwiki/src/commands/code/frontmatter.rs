use super::{AiGenerationStatus, CodewikiAiOutcome};
use gobby_core::codewiki_contract::{AI_FALLBACK_KEY, AI_GENERATION_STATUS_KEY, AI_ROUTE_KEY};
use gobby_core::config::AiRouting;
use std::collections::BTreeSet;

const AI_NOTICE_START: &str = "<!-- codewiki-ai-notice:start -->\n";
const AI_NOTICE_END: &str = "<!-- codewiki-ai-notice:end -->\n";

pub(super) fn apply_ai_outcome_to_markdown(content: &str, outcome: CodewikiAiOutcome) -> String {
    let Some((frontmatter_body, rest)) = split_frontmatter(content) else {
        return content.to_string();
    };

    let top_level_indent = frontmatter_top_level_indent(frontmatter_body);
    let mut out = String::from("---\n");
    for line in frontmatter_body.lines() {
        if !is_ai_frontmatter_line(line, top_level_indent) {
            out.push_str(line);
            out.push('\n');
        }
    }
    push_ai_frontmatter_line(
        &mut out,
        top_level_indent,
        AI_ROUTE_KEY,
        outcome.route_label(),
    );
    push_ai_frontmatter_line(
        &mut out,
        top_level_indent,
        AI_FALLBACK_KEY,
        if outcome.fallback { "true" } else { "false" },
    );
    push_ai_frontmatter_line(
        &mut out,
        top_level_indent,
        AI_GENERATION_STATUS_KEY,
        outcome.status.as_str(),
    );
    out.push_str("---\n");

    let rest = strip_existing_ai_notice(rest);
    if let Some(note) = ai_body_note(outcome) {
        out.push('\n');
        out.push_str(AI_NOTICE_START);
        out.push_str("> **AI notice:** ");
        out.push_str(note);
        out.push_str("\n\n");
        out.push_str(AI_NOTICE_END);
    }
    out.push_str(rest);
    out
}

fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    let first_line_end = content.find('\n')? + 1;
    if content[..first_line_end].trim_end_matches(['\r', '\n']) != "---" {
        return None;
    }

    let mut cursor = first_line_end;
    for line in content[first_line_end..].split_inclusive('\n') {
        let line_end = cursor + line.len();
        if line.trim_end_matches(['\r', '\n']) == "---" {
            return Some((&content[first_line_end..cursor], &content[line_end..]));
        }
        cursor = line_end;
    }
    None
}

fn push_ai_frontmatter_line(out: &mut String, indent: &str, key: &str, value: &str) {
    out.push_str(indent);
    out.push_str(key);
    out.push_str(": ");
    out.push_str(value);
    out.push('\n');
}

/// Tool-loop observability fields parsed back out of a page's rendered
/// frontmatter, mirrored into `_meta/codewiki.json` (#978). Only the tool-loop
/// fields are captured; every other frontmatter key is ignored.
#[derive(Default, serde::Deserialize)]
pub(super) struct LaneObservability {
    pub(super) lane: Option<String>,
    pub(super) tool_call_count: Option<usize>,
    pub(super) turns: Option<usize>,
}

/// Read the tool-loop `lane`/`tool_call_count`/`turns` keys from a doc's rendered
/// frontmatter. Returns defaults (all `None`) for pages with no frontmatter or
/// no tool-loop keys (one-shot / leaf / deterministic pages).
pub(super) fn lane_observability_from_content(content: &str) -> LaneObservability {
    let Some((frontmatter_body, _)) = split_frontmatter(content) else {
        return LaneObservability::default();
    };
    serde_yaml::from_str(frontmatter_body).unwrap_or_default()
}

/// Generation-health fields parsed back out of a page's rendered frontmatter;
/// every other key is ignored.
#[derive(Default, serde::Deserialize)]
struct PageHealthFrontmatter {
    degraded: Option<bool>,
    ai_generation_status: Option<String>,
}

/// True when a page's own on-disk frontmatter records a degraded generation.
/// The manifest entry can claim a page is healthy while the page itself
/// carries a degraded stamp — an emit site that under-reports degradation, or
/// a run killed between the page write and the manifest flush (#18291). The
/// page is the last word: a degraded page never satisfies reuse (#687).
pub(crate) fn page_frontmatter_blocks_reuse(content: &str) -> bool {
    let Some((frontmatter_body, _)) = split_frontmatter(content) else {
        return false;
    };
    let Ok(health) = serde_yaml::from_str::<PageHealthFrontmatter>(frontmatter_body) else {
        return false;
    };
    health.degraded == Some(true)
        || health.ai_generation_status.as_deref() == Some(AiGenerationStatus::Degraded.as_str())
}

fn is_ai_frontmatter_line(line: &str, top_level_indent: &str) -> bool {
    frontmatter_line_has_key(line, top_level_indent, AI_ROUTE_KEY)
        || frontmatter_line_has_key(line, top_level_indent, AI_FALLBACK_KEY)
        || frontmatter_line_has_key(line, top_level_indent, AI_GENERATION_STATUS_KEY)
}

fn frontmatter_line_has_key(line: &str, top_level_indent: &str, key: &str) -> bool {
    let Some(candidate) = line.strip_prefix(top_level_indent) else {
        return false;
    };
    if starts_with_frontmatter_indentation(candidate) {
        return false;
    }
    candidate
        .strip_prefix(key)
        .is_some_and(|suffix| suffix.starts_with(':'))
}

fn starts_with_frontmatter_indentation(line: &str) -> bool {
    line.as_bytes()
        .first()
        .is_some_and(|byte| matches!(byte, b' ' | b'\t'))
}

fn frontmatter_top_level_indent(frontmatter_body: &str) -> &str {
    frontmatter_body
        .lines()
        .filter_map(frontmatter_mapping_indent)
        .min_by_key(|indent| indent.len())
        .unwrap_or("")
}

fn frontmatter_mapping_indent(line: &str) -> Option<&str> {
    let indent_len = line
        .as_bytes()
        .iter()
        .take_while(|byte| matches!(byte, b' ' | b'\t'))
        .count();
    let indent = &line[..indent_len];
    let candidate = &line[indent_len..];
    if candidate.is_empty() || candidate.starts_with('#') || candidate.starts_with('-') {
        return None;
    }
    let (key, _) = candidate.split_once(':')?;
    if key.trim().is_empty() {
        return None;
    }
    Some(indent)
}

fn strip_existing_ai_notice(rest: &str) -> &str {
    let Some(start) = rest.find(AI_NOTICE_START) else {
        return rest;
    };
    if rest[..start].trim().is_empty()
        && let Some(end) = rest[start + AI_NOTICE_START.len()..].find(AI_NOTICE_END)
    {
        let after = start + AI_NOTICE_START.len() + end + AI_NOTICE_END.len();
        return &rest[after..];
    }
    rest
}

fn ai_body_note(outcome: CodewikiAiOutcome) -> Option<&'static str> {
    match outcome.status {
        AiGenerationStatus::Degraded => {
            Some("AI generation failed for this page; structural fallback content is shown.")
        }
        AiGenerationStatus::Skipped if outcome.fallback || outcome.route != AiRouting::Off => {
            Some("AI generation did not run; this page contains structural documentation only.")
        }
        AiGenerationStatus::Generated if outcome.fallback && outcome.route == AiRouting::Daemon => {
            Some(
                "Auto routing could not use the daemon, so this page was generated through the Direct route.",
            )
        }
        _ => None,
    }
}

pub(crate) fn source_files_from_frontmatter(content: &str) -> BTreeSet<String> {
    let mut files = BTreeSet::new();

    let mut lines = content.lines();
    if lines.next() != Some("---") {
        return files;
    }
    let frontmatter = lines
        .take_while(|line| *line != "---")
        .collect::<Vec<_>>()
        .join("\n");
    let Ok(serde_yaml::Value::Mapping(frontmatter)) =
        serde_yaml::from_str::<serde_yaml::Value>(&frontmatter)
    else {
        return files;
    };

    for key in [gobby_core::codewiki_contract::PROVENANCE_KEY] {
        let key = serde_yaml::Value::String(key.to_string());
        let Some(serde_yaml::Value::Sequence(sources)) = frontmatter.get(&key) else {
            continue;
        };
        for source in sources {
            let serde_yaml::Value::Mapping(source) = source else {
                continue;
            };
            let file_key = serde_yaml::Value::String(
                gobby_core::codewiki_contract::PROVENANCE_FILE_KEY.to_string(),
            );
            if let Some(serde_yaml::Value::String(file)) = source.get(&file_key) {
                files.insert(file.clone());
            }
        }
    }
    files
}

#[cfg(test)]
pub(crate) fn unquote_yaml_string(value: &str) -> Option<String> {
    let value = value.trim();
    let inner = value.strip_prefix('"')?.strip_suffix('"')?;
    let mut out = String::new();
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            out.push(match chars.next()? {
                '0' => '\0',
                'a' => '\u{0007}',
                'b' => '\u{0008}',
                't' => '\t',
                'n' => '\n',
                'v' => '\u{000b}',
                'f' => '\u{000c}',
                'r' => '\r',
                'e' => '\u{001b}',
                '"' => '"',
                '/' => '/',
                '\\' => '\\',
                'x' => decode_hex_escape(&mut chars, 2)?,
                'u' => decode_hex_escape(&mut chars, 4)?,
                'U' => decode_hex_escape(&mut chars, 8)?,
                _ => return None,
            });
        } else {
            out.push(ch);
        }
    }
    Some(out)
}

#[cfg(test)]
fn decode_hex_escape(chars: &mut std::str::Chars<'_>, digits: usize) -> Option<char> {
    let mut value = 0_u32;
    for _ in 0..digits {
        value = value.checked_mul(16)?;
        value = value.checked_add(chars.next()?.to_digit(16)?)?;
    }
    char::from_u32(value)
}

#[cfg(test)]
mod lane_meta_tests {
    use super::*;

    #[test]
    fn lane_observability_is_mirrored_from_tool_loop_frontmatter() {
        let content = "---\ntitle: Repository Overview\ntype: code_repo\nlane: tool_loop\n\
                       tool_call_count: 7\nturns: 4\n---\n\n# Repository Overview\n";
        let lane = lane_observability_from_content(content);
        assert_eq!(lane.lane.as_deref(), Some("tool_loop"));
        assert_eq!(lane.tool_call_count, Some(7));
        assert_eq!(lane.turns, Some(4));
    }

    #[test]
    fn lane_observability_is_absent_for_one_shot_and_unframed_pages() {
        let one_shot = "---\ntitle: A File\ntype: code_file\n---\n\n# A File\n";
        let parsed = lane_observability_from_content(one_shot);
        assert_eq!(parsed.lane, None);
        assert_eq!(parsed.tool_call_count, None);
        assert_eq!(parsed.turns, None);
        // No frontmatter at all yields defaults rather than an error.
        let bare = lane_observability_from_content("# Just a heading\n");
        assert_eq!(bare.lane, None);
    }
}
