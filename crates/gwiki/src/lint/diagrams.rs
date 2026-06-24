use std::path::PathBuf;

use serde::Serialize;

use super::WikiPage;

/// A curated-diagram defect found while linting the generated vault: either a
/// Mermaid block that is not well-formed, or one whose node labels are not
/// grounded in the rest of the page. The generator omits such diagrams (honest
/// no-diagram beats a wrong diagram), so on a freshly refreshed vault this list
/// is empty; a non-empty entry means a wrong diagram leaked into the vault.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DiagramIssue {
    pub path: PathBuf,
    pub line: usize,
    pub reason: String,
}

pub(super) fn render_diagram_issues(text: &mut String, issues: &[DiagramIssue]) {
    text.push_str("\nInvalid diagrams:\n");
    if issues.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for issue in issues {
        text.push_str("- ");
        text.push_str(&issue.path.display().to_string());
        text.push(':');
        text.push_str(&issue.line.to_string());
        text.push_str(" -> ");
        text.push_str(&issue.reason);
        text.push('\n');
    }
}

/// Mermaid diagram headers the curated wiki generator emits. Mirrors gcode's
/// `architecture_diagrams::VALID_HEADERS` so the lint accepts exactly the diagram
/// kinds the generator can produce.
const VALID_DIAGRAM_HEADERS: [&str; 3] = ["flowchart", "graph", "sequenceDiagram"];

/// Collect curated-diagram defects across every page: a Mermaid block that is not
/// well-formed, or one whose node labels are not grounded in the surrounding page
/// prose. The generator already omits such diagrams (honest no-diagram beats a
/// wrong diagram), so this is a backstop - a real entry means a wrong diagram
/// leaked into the vault and should be omitted at the source instead.
pub(super) fn invalid_diagrams(pages: &[WikiPage]) -> Vec<DiagramIssue> {
    let mut issues = Vec::new();
    for page in pages {
        let grounding = grounding_text(&page.markdown);
        for block in mermaid_blocks(&page.markdown) {
            if !is_valid_mermaid(&block.text) {
                issues.push(DiagramIssue {
                    path: page.relative_path.clone(),
                    line: block.line,
                    reason: "invalid-mermaid".to_string(),
                });
                continue;
            }
            let ungrounded: Vec<String> = node_labels(&block.text)
                .into_iter()
                .filter(|label| !label_is_grounded(label, &grounding))
                .collect();
            if !ungrounded.is_empty() {
                issues.push(DiagramIssue {
                    path: page.relative_path.clone(),
                    line: block.line,
                    reason: format!("ungrounded: {}", ungrounded.join(", ")),
                });
            }
        }
    }
    issues
}

/// One Mermaid fenced block extracted from a page: the 1-based line of its
/// opening ```` ```mermaid ```` fence and the full fenced block text.
struct MermaidBlock {
    line: usize,
    text: String,
}

/// Extract every ```` ```mermaid ```` fenced block from the markdown. A block runs
/// from an opening `` ```mermaid `` line to the next closing `` ``` `` line; an
/// unterminated block runs to end-of-file so the validator can reject it.
fn mermaid_blocks(markdown: &str) -> Vec<MermaidBlock> {
    let lines: Vec<&str> = markdown.lines().collect();
    let mut blocks = Vec::new();
    let mut idx = 0;
    while idx < lines.len() {
        if lines[idx].trim() != "```mermaid" {
            idx += 1;
            continue;
        }
        let start = idx;
        let mut end = idx + 1;
        while end < lines.len() && lines[end].trim() != "```" {
            end += 1;
        }
        // Keep the closing fence when present so a well-formed block validates;
        // an unterminated block stops at EOF and is rejected as invalid.
        let last = end.min(lines.len().saturating_sub(1));
        blocks.push(MermaidBlock {
            line: start + 1,
            text: lines[start..=last].join("\n"),
        });
        idx = end + 1;
    }
    blocks
}

/// Well-formedness gate for one ```` ```mermaid ```` block. Mirrors gcode's
/// `architecture_diagrams::is_valid_mermaid`: a balanced `` ```mermaid `` fence,
/// a recognised header ([`VALID_DIAGRAM_HEADERS`]), at least one content line,
/// no stray interior fences, and balanced bracket/paren/brace delimiters.
fn is_valid_mermaid(block: &str) -> bool {
    let lines: Vec<&str> = block.lines().collect();
    if lines.len() < 3 {
        return false;
    }
    if lines[0].trim() != "```mermaid" {
        return false;
    }
    let Some(close_idx) = lines.iter().rposition(|l| l.trim() == "```") else {
        return false;
    };
    if close_idx == 0 {
        return false;
    }
    if lines[1..close_idx]
        .iter()
        .any(|l| l.trim_start().starts_with("```"))
    {
        return false;
    }
    if lines[close_idx + 1..].iter().any(|l| !l.trim().is_empty()) {
        return false;
    }
    let interior = &lines[1..close_idx];
    let mut content = interior.iter().filter(|l| !l.trim().is_empty());
    let Some(header) = content.next() else {
        return false;
    };
    let Some(header_token) = header.split_whitespace().next() else {
        return false;
    };
    if !VALID_DIAGRAM_HEADERS.contains(&header_token) {
        return false;
    }
    if content.next().is_none() {
        return false;
    }
    balanced_delimiters(interior)
}

/// True when `(`/`)`, `[`/`]`, and `{`/`}` balance across the lines, with quoted
/// spans skipped so punctuation inside a `"..."` label does not count. Mirrors
/// gcode's `architecture_diagrams::balanced_delimiters`.
fn balanced_delimiters(lines: &[&str]) -> bool {
    let (mut paren, mut bracket, mut brace) = (0i32, 0i32, 0i32);
    let mut in_quote = false;
    for line in lines {
        for ch in line.chars() {
            if ch == '"' {
                in_quote = !in_quote;
                continue;
            }
            if in_quote {
                continue;
            }
            match ch {
                '(' => paren += 1,
                ')' => paren -= 1,
                '[' => bracket += 1,
                ']' => bracket -= 1,
                '{' => brace += 1,
                '}' => brace -= 1,
                _ => {}
            }
            if paren < 0 || bracket < 0 || brace < 0 {
                return false;
            }
        }
        if in_quote {
            return false;
        }
    }
    paren == 0 && bracket == 0 && brace == 0
}

/// Extract the quoted node labels (`id["label"]`, `id[("label")]`) from a
/// Mermaid block. Edge lines (`a --> b`) carry no quoted label, and
/// `subgraph ... ["Title"]` lines carry a descriptive group heading rather than
/// a subsystem node, so both contribute nothing to grounding.
fn node_labels(block: &str) -> Vec<String> {
    let mut labels = Vec::new();
    for line in block.lines() {
        if line.trim_start().starts_with("subgraph") {
            continue;
        }
        collect_quoted_labels(line, "[\"", "\"]", &mut labels);
        collect_quoted_labels(line, "[(\"", "\")]", &mut labels);
    }
    labels
}

fn collect_quoted_labels(line: &str, open: &str, close: &str, labels: &mut Vec<String>) {
    let mut rest = line;
    while let Some(open_idx) = rest.find(open) {
        let after = &rest[open_idx + open.len()..];
        let Some(close_idx) = after.find(close) else {
            break;
        };
        labels.push(after[..close_idx].to_string());
        rest = &after[close_idx + close.len()..];
    }
}

/// Normalised grounding corpus for a page: the markdown with its Mermaid blocks
/// removed (so a fabricated node cannot ground itself), then reduced to lowercase
/// alphanumerics for tolerant substring matching.
fn grounding_text(markdown: &str) -> String {
    let lines: Vec<&str> = markdown.lines().collect();
    let mut kept = String::new();
    let mut idx = 0;
    while idx < lines.len() {
        if lines[idx].trim() == "```mermaid" {
            idx += 1;
            while idx < lines.len() && lines[idx].trim() != "```" {
                idx += 1;
            }
            idx += 1; // skip the closing fence
            continue;
        }
        kept.push_str(lines[idx]);
        kept.push('\n');
        idx += 1;
    }
    normalize_alnum(&kept)
}

/// Lowercase and keep only ASCII alphanumerics, dropping separators so
/// `gobby-code` and `gobby_code` both normalise to `gobbycode`.
fn normalize_alnum(text: &str) -> String {
    text.chars()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

/// A node label is grounded when at least one of its meaningful words - an
/// alphanumeric run of three or more characters - appears in the page's grounding
/// corpus. This is deliberately lenient: the generator already omits any diagram
/// it cannot ground, so the lint only needs to catch nodes with no support at
/// all (a wholly fabricated subsystem), without flagging legitimate descriptive
/// labels such as "Direct to datastores / API". A label with no substantive word
/// (pure punctuation/short tokens) is treated as grounded - too thin to judge.
fn label_is_grounded(label: &str, grounding: &str) -> bool {
    let words: Vec<String> = label
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|word| word.len() >= 3)
        .map(str::to_ascii_lowercase)
        .collect();
    if words.is_empty() {
        return true;
    }
    words.iter().any(|word| grounding.contains(word))
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use super::super::{LintReport, render_text, run};
    use super::*;
    use crate::ScopeIdentity;

    #[test]
    fn grounded_valid_diagram_passes_lint() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/pipeline.md",
            r#"---
title: Pipeline
---
# Pipeline
The parser feeds the chunker.

```mermaid
flowchart LR
    s0["parser — builds AST"]
    s1["chunker — splits content"]
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert!(
            report.invalid_diagrams.is_empty(),
            "{:?}",
            report.invalid_diagrams
        );
    }

    #[test]
    fn ungrounded_node_is_flagged_not_silently_kept() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/flow.md",
            r#"---
title: Flow
---
# Flow
The parser feeds downstream stages.

```mermaid
flowchart LR
    s0["parser — builds AST"]
    s1["telemetry — emits metrics"]
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert_eq!(report.invalid_diagrams.len(), 1);
        let issue = &report.invalid_diagrams[0];
        assert_eq!(issue.path, PathBuf::from("code/concepts/flow.md"));
        assert!(issue.reason.contains("ungrounded"), "{}", issue.reason);
        assert!(issue.reason.contains("telemetry"), "{}", issue.reason);
    }

    #[test]
    fn malformed_mermaid_is_flagged_invalid() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "code/concepts/broken.md",
            r#"---
title: Broken
---
# Broken
The parser stage.

```mermaid
flowchart LR
    s0["parser
    s0 --> s1
```
"#,
        );

        let report = run(root, ScopeIdentity::topic("code")).expect("lint runs");

        assert_eq!(report.invalid_diagrams.len(), 1);
        assert_eq!(report.invalid_diagrams[0].reason, "invalid-mermaid");
    }

    #[test]
    fn mermaid_blocks_capture_line_and_reject_unterminated() {
        let md = "intro\n\n```mermaid\nflowchart LR\n    a --> b\n```\n";
        let blocks = mermaid_blocks(md);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].line, 3);
        assert!(is_valid_mermaid(&blocks[0].text));

        let unterminated = "```mermaid\nflowchart LR\n    a --> b\n";
        let blocks = mermaid_blocks(unterminated);
        assert_eq!(blocks.len(), 1);
        assert!(!is_valid_mermaid(&blocks[0].text));
    }

    #[test]
    fn validator_requires_recognised_header_token() {
        let bad = "```mermaid\npie title Pets\n    \"Dogs\" : 1\n```";
        assert!(!is_valid_mermaid(bad));
        let prefixed = "```mermaid\nflowchartish LR\n    a --> b\n```";
        assert!(!is_valid_mermaid(prefixed));
        let good = "```mermaid\ngraph TD\n    a --> b\n```";
        assert!(is_valid_mermaid(good));
    }

    #[test]
    fn grounding_ignores_the_diagram_itself() {
        let md = "# Title\nNo prose mentions.\n\n```mermaid\nflowchart LR\n    s0[\"ghost — x\"]\n    s1[\"other — y\"]\n    s0 --> s1\n```\n";
        let grounding = grounding_text(md);
        assert!(!grounding.contains("ghost"));
        assert!(!label_is_grounded("ghost — x", &grounding));
    }

    #[test]
    fn node_labels_extracts_quoted_labels_only() {
        let block = "```mermaid\nflowchart LR\n    s0[\"parser — builds AST\"]\n    s1[(\"PostgreSQL hub\")]\n    s0 --> s1\n```";
        assert_eq!(
            node_labels(block),
            vec![
                "parser — builds AST".to_string(),
                "PostgreSQL hub".to_string()
            ]
        );
    }

    #[test]
    fn render_text_reports_invalid_diagrams() {
        let report = LintReport {
            command: "lint",
            scope: ScopeIdentity::topic("code"),
            root: PathBuf::from("/vault"),
            broken_links: Vec::new(),
            orphan_pages: Vec::new(),
            missing_frontmatter: Vec::new(),
            duplicate_aliases: Vec::new(),
            missing_backlinks: Vec::new(),
            invalid_diagrams: vec![DiagramIssue {
                path: PathBuf::from("code/concepts/flow.md"),
                line: 7,
                reason: "ungrounded: telemetry".to_string(),
            }],
        };

        let text = render_text(&report);

        assert!(text.contains("Invalid diagrams:"));
        assert!(text.contains("code/concepts/flow.md:7 -> ungrounded: telemetry"));
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("create parent");
        fs::write(path, markdown).expect("write page");
    }
}
