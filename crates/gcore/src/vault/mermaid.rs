//! The single Mermaid-validity implementation shared by both wiki engines.
//!
//! gcode's codewiki generator gates every diagram through [`is_valid_mermaid`]
//! before writing it (write-time prevention), and gwiki's `lint` report flags
//! any invalid or ungrounded diagram that leaked into the vault. Both crates
//! previously carried byte-identical copies of this machinery; #17514 hoisted
//! it here so the two gates cannot drift.

/// Recognised Mermaid diagram headers the validator accepts. `graph` and
/// `flowchart` are the two spellings of the flow diagram; `sequenceDiagram`
/// drives runtime-flow diagrams.
pub const VALID_DIAGRAM_HEADERS: [&str; 3] = ["flowchart", "graph", "sequenceDiagram"];

/// One Mermaid fenced block extracted from a page: the 1-based line of its
/// opening ```` ```mermaid ```` fence and the full fenced block text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MermaidBlock {
    pub line: usize,
    pub text: String,
}

/// Extract every ```` ```mermaid ```` fenced block from the markdown. A block runs
/// from an opening `` ```mermaid `` line to the next closing `` ``` `` line; an
/// unterminated block runs to end-of-file so the validator can reject it.
pub fn mermaid_blocks(markdown: &str) -> Vec<MermaidBlock> {
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

/// Hand-written well-formedness gate for a single ```` ```mermaid ```` block.
///
/// A block passes only when ALL of these hold:
///
/// * It opens with a ```` ```mermaid ```` fence line and closes with a ```` ``` ````
///   fence line (the fence is balanced and properly closed).
/// * The first non-empty line inside the fence is a recognised diagram header
///   ([`VALID_DIAGRAM_HEADERS`]).
/// * There is at least one content line after the header (the diagram is not
///   empty).
/// * No interior line opens another fence (no nested/un-terminated fences).
/// * Bracket/paren/brace delimiters across the body are balanced, so no node
///   shape is left half-open.
///
/// This is intentionally conservative: it rejects anything it cannot prove
/// well-formed rather than risk emitting a fence a Markdown/Mermaid renderer
/// would choke on.
pub fn is_valid_mermaid(block: &str) -> bool {
    let lines: Vec<&str> = block.lines().collect();
    if lines.len() < 3 {
        // Need at minimum: opening fence, a header, a closing fence.
        return false;
    }
    if lines[0].trim() != "```mermaid" {
        return false;
    }
    // Exactly one closing fence, and it is the last non-empty line.
    let Some(close_idx) = lines.iter().rposition(|l| l.trim() == "```") else {
        return false;
    };
    if close_idx == 0 {
        return false;
    }
    // No stray fence markers between the open and the close.
    if lines[1..close_idx]
        .iter()
        .any(|l| l.trim_start().starts_with("```"))
    {
        return false;
    }
    // Anything after the closing fence must be blank.
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
    // At least one content line beyond the header.
    if content.next().is_none() {
        return false;
    }

    // Delimiters across the interior must balance (cheap structural check that
    // catches a half-open node like `a["b` or an unterminated subgraph node).
    balanced_delimiters(interior)
}

/// True when `(`/`)`, `[`/`]`, and `{`/`}` are balanced across the lines, with
/// quoted spans skipped so punctuation inside a `"..."` label does not count.
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
        // A label quote never spans lines in our generated diagrams.
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
pub(crate) fn node_labels(block: &str) -> Vec<String> {
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
pub(crate) fn grounding_text(markdown: &str) -> String {
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

/// Escape a label for use inside a Mermaid `["..."]` node so brackets, quotes,
/// and pipes cannot break the surrounding syntax. Uses Mermaid's native
/// `#NN;` entity codes (decoded by the Mermaid lexer itself), NOT HTML
/// `&#NN;` entities — those only decode with `htmlLabels` enabled and render
/// as literal `&#40;` garbage when it is off. `#` is escaped first so source
/// text cannot forge an entity. Mermaid quoted strings have no backslash
/// escape, so `\"` is never a valid way to embed a quote.
pub fn escape_label(text: &str) -> String {
    text.replace('#', "#35;")
        .replace('\\', "#92;")
        .replace('"', "#quot;")
        .replace('[', "#91;")
        .replace(']', "#93;")
        .replace('(', "#40;")
        .replace(')', "#41;")
        .replace('{', "#123;")
        .replace('}', "#125;")
        .replace('|', "#124;")
}

/// A node label is grounded when at least one of its meaningful words - an
/// alphanumeric run of three or more characters - appears in the page's grounding
/// corpus. This is deliberately lenient: the generator already omits any diagram
/// it cannot ground, so the lint only needs to catch nodes with no support at
/// all (a wholly fabricated subsystem), without flagging legitimate descriptive
/// labels such as "Direct to datastores / API". A label with no substantive word
/// (pure punctuation/short tokens) is treated as grounded - too thin to judge.
pub(crate) fn label_is_grounded(label: &str, grounding: &str) -> bool {
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
    use super::*;

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
    fn validator_accepts_minimal_flowchart() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n```\n";
        assert!(is_valid_mermaid(block));
    }

    #[test]
    fn validator_accepts_sequence_diagram() {
        let block = "```mermaid\nsequenceDiagram\n    participant m_a as alpha\n    participant m_b as beta\n    participant m_c as gamma\n    m_a->>m_b: calls\n    m_b->>m_c: calls\n```\n";
        assert!(is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_unrecognized_header() {
        let block = "```mermaid\nbananas\n    a --> b\n```\n";
        assert!(!is_valid_mermaid(block));
        let pie = "```mermaid\npie title Pets\n    \"Dogs\" : 1\n```";
        assert!(!is_valid_mermaid(pie));
    }

    #[test]
    fn validator_rejects_valid_header_prefix() {
        let block = "```mermaid\nflowcharting TD\n    a --> b\n```\n";
        assert!(!is_valid_mermaid(block));
        let prefixed = "```mermaid\nflowchartish LR\n    a --> b\n```";
        assert!(!is_valid_mermaid(prefixed));
    }

    #[test]
    fn validator_rejects_unclosed_fence() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_empty_diagram() {
        let block = "```mermaid\nflowchart TD\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_unbalanced_node_shape() {
        let block = "```mermaid\nflowchart TD\n    a[\"b --> c\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_nested_fence() {
        let block = "```mermaid\nflowchart TD\n```mermaid\n    a --> b\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_content_after_close() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n```\nstray text\n";
        assert!(!is_valid_mermaid(block));
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
    fn escape_label_replaces_specials_with_entity_codes() {
        assert_eq!(
            escape_label(r#"Say "hi" \ [draft] (v2) {x} a|b #1"#),
            "Say #quot;hi#quot; #92; #91;draft#93; #40;v2#41; #123;x#125; a#124;b #35;1"
        );
    }

    #[test]
    fn escape_label_cannot_be_forged_from_source_text() {
        // A literal `#quot;` in the source must not survive as an entity.
        assert_eq!(escape_label("#quot;"), "#35;quot;");
        assert_eq!(escape_label("#91;"), "#35;91;");
    }

    #[test]
    fn escape_label_leaves_no_raw_special_characters() {
        let escaped = escape_label("a\"b\\c[d]e(f)g{h}i|j#k");
        assert!(
            !escaped.contains(['"', '\\', '[', ']', '(', ')', '{', '}', '|']),
            "raw special character survived: {escaped}"
        );
    }
}
