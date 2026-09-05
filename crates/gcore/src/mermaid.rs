//! Structural Mermaid validation and safe graph label escaping.

/// Recognised Mermaid diagram headers the validator accepts. `graph` and
/// `flowchart` are the two spellings of the flow diagram; `sequenceDiagram`
/// drives runtime-flow diagrams.
pub const VALID_DIAGRAM_HEADERS: [&str; 3] = ["flowchart", "graph", "sequenceDiagram"];

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

#[cfg(test)]
mod tests {
    use super::*;

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
