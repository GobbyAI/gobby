use regex::Regex;
use std::sync::LazyLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Lite,
    Standard,
    Aggressive,
}

impl Level {
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "lite" => Some(Self::Lite),
            "standard" => Some(Self::Standard),
            "aggressive" => Some(Self::Aggressive),
            _ => None,
        }
    }
}

/// Compress prose text while preserving code blocks, URLs, paths, and structure.
pub fn compress_prose(input: &str, level: Level) -> String {
    // Extract and protect fenced code blocks and YAML frontmatter
    let (text, protected) = extract_protected(input);

    let result = match level {
        Level::Lite => compress_lite(&text),
        Level::Standard => compress_standard(&text),
        Level::Aggressive => compress_aggressive(&text),
    };

    restore_protected(&result, &protected)
}

// --- Protected region handling ---

static CODE_BLOCK_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)```[^\n]*\n.*?```").unwrap());
static FRONTMATTER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)\A---\n.*?\n---\n?").unwrap());
// Inline protections: inline code, URLs, file paths, XML/HTML tags
static INLINE_CODE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`[^`]+`").unwrap());
static URL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https?://\S+|www\.\S+").unwrap());
static XML_TAG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"</?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?>").unwrap());
static FILE_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?:^|[\s(])((?:\./|/|~/)[\w./-]+)").unwrap());

fn extract_protected(input: &str) -> (String, Vec<String>) {
    let mut text = input.to_string();
    let mut protected = Vec::new();

    // YAML frontmatter first (must be at start)
    if let Some(m) = FRONTMATTER_RE.find(&text) {
        protected.push(m.as_str().to_string());
        text = format!("__GSQZ_P{}__\n{}", protected.len() - 1, &text[m.end()..]);
    }

    // Fenced code blocks
    while let Some(m) = CODE_BLOCK_RE.find(&text) {
        protected.push(m.as_str().to_string());
        text = format!(
            "{}__GSQZ_P{}__{}",
            &text[..m.start()],
            protected.len() - 1,
            &text[m.end()..]
        );
    }

    // Inline protections (applied after block-level to avoid conflicts)
    for re in [&*INLINE_CODE_RE, &*URL_RE, &*XML_TAG_RE] {
        let mut new_text = String::new();
        let mut last = 0;
        for m in re.find_iter(&text) {
            new_text.push_str(&text[last..m.start()]);
            protected.push(m.as_str().to_string());
            new_text.push_str(&format!("__GSQZ_P{}__", protected.len() - 1));
            last = m.end();
        }
        new_text.push_str(&text[last..]);
        text = new_text;
    }

    // File paths (capture group 1 is the path)
    let mut new_text = String::new();
    let mut last = 0;
    for caps in FILE_PATH_RE.captures_iter(&text) {
        if let Some(path_match) = caps.get(1) {
            new_text.push_str(&text[last..path_match.start()]);
            protected.push(path_match.as_str().to_string());
            new_text.push_str(&format!("__GSQZ_P{}__", protected.len() - 1));
            last = path_match.end();
        }
    }
    new_text.push_str(&text[last..]);
    text = new_text;

    (text, protected)
}

fn restore_protected(text: &str, protected: &[String]) -> String {
    let mut result = text.to_string();
    for (i, content) in protected.iter().enumerate() {
        let placeholder = format!("__GSQZ_P{}__", i);
        result = result.replace(&placeholder, content);
    }
    result
}

// --- Lite compression ---

static HTML_COMMENT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)<!--.*?-->").unwrap());
static MULTI_BLANK_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n{3,}").unwrap());

fn compress_lite(text: &str) -> String {
    let text = HTML_COMMENT_RE.replace_all(text, "");
    let text = MULTI_BLANK_RE.replace_all(&text, "\n\n");
    // Trim trailing whitespace per line
    text.lines()
        .map(|l| l.trim_end())
        .collect::<Vec<_>>()
        .join("\n")
}

// --- Standard compression ---

static FILLER_PHRASES: LazyLock<Vec<(Regex, &'static str)>> = LazyLock::new(|| {
    vec![
        (Regex::new(r"(?i)\bIn order to\b").unwrap(), "To"),
        (
            Regex::new(r"(?i)\bIt should be noted that\b").unwrap(),
            "Note:",
        ),
        (
            Regex::new(r"(?i)\bIt is important to note that\b").unwrap(),
            "Note:",
        ),
        (
            Regex::new(r"(?i)\bIt is worth mentioning that\b").unwrap(),
            "Note:",
        ),
        (
            Regex::new(r"(?i)\bAs a matter of fact\b").unwrap(),
            "In fact",
        ),
        (
            Regex::new(r"(?i)\bDue to the fact that\b").unwrap(),
            "Because",
        ),
        (Regex::new(r"(?i)\bAt this point in time\b").unwrap(), "Now"),
        (Regex::new(r"(?i)\bIn the event that\b").unwrap(), "If"),
        (Regex::new(r"(?i)\bFor the purpose of\b").unwrap(), "To"),
        (Regex::new(r"(?i)\bWith regard to\b").unwrap(), "About"),
        (Regex::new(r"(?i)\bWith respect to\b").unwrap(), "About"),
        (
            Regex::new(r"(?i)\bIn spite of the fact that\b").unwrap(),
            "Although",
        ),
        (Regex::new(r"(?i)\bOn the other hand\b").unwrap(), "However"),
        (
            Regex::new(r"(?i)\bAt the end of the day\b").unwrap(),
            "Ultimately",
        ),
        (Regex::new(r"(?i)\bA large number of\b").unwrap(), "Many"),
        (
            Regex::new(r"(?i)\bA significant number of\b").unwrap(),
            "Many",
        ),
        (Regex::new(r"(?i)\bIn the near future\b").unwrap(), "Soon"),
        (Regex::new(r"(?i)\bPrior to\b").unwrap(), "Before"),
        (Regex::new(r"(?i)\bSubsequent to\b").unwrap(), "After"),
        (Regex::new(r"(?i)\bIn order for\b").unwrap(), "For"),
        (Regex::new(r"(?i)\bIn regard to\b").unwrap(), "About"),
        (Regex::new(r"(?i)\bIs able to\b").unwrap(), "Can"),
        (Regex::new(r"(?i)\bHas the ability to\b").unwrap(), "Can"),
        (Regex::new(r"(?i)\bMake use of\b").unwrap(), "Use"),
    ]
});

static FILLER_WORDS_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:basically|essentially|actually|really|very|quite|rather|simply|just|literally|honestly|clearly|obviously|certainly|definitely|absolutely|completely|totally|entirely|merely|practically)\b,?\s*").unwrap()
});

static MULTI_SPACE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"  +").unwrap());

fn compress_standard(text: &str) -> String {
    let text = compress_lite(text);

    let mut result = text;
    // Apply filler phrase replacements
    for (re, replacement) in FILLER_PHRASES.iter() {
        result = re.replace_all(&result, *replacement).into_owned();
    }

    // Remove filler words (only in non-heading, non-code lines)
    result = result
        .lines()
        .map(|line| {
            if line.starts_with('#') || line.starts_with("__GSQZ_P") {
                line.to_string()
            } else {
                let cleaned = FILLER_WORDS_RE.replace_all(line, "");
                MULTI_SPACE_RE.replace_all(&cleaned, " ").trim().to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n");

    result
}

// --- Aggressive compression ---

static LIST_ITEM_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[\s]*[-*+]\s|^\s*\d+\.\s").unwrap());

fn compress_aggressive(text: &str) -> String {
    let text = compress_standard(text);

    let mut result_lines: Vec<String> = Vec::new();
    let lines: Vec<&str> = text.lines().collect();
    let mut i = 0;

    while i < lines.len() {
        let line = lines[i];

        // Detect list items — keep first 5, truncate rest
        if LIST_ITEM_RE.is_match(line) {
            let list_start = i;
            let mut list_count = 0;
            while i < lines.len() && LIST_ITEM_RE.is_match(lines[i]) {
                if list_count < 5 {
                    result_lines.push(lines[i].to_string());
                }
                list_count += 1;
                i += 1;
            }
            if list_count > 5 {
                result_lines.push(format!("  [... {} more items]", list_count - 5));
            }
            let _ = list_start; // suppress unused warning
            continue;
        }

        // Protected placeholder — keep as-is
        if line.contains("__GSQZ_P") {
            result_lines.push(line.to_string());
            i += 1;
            continue;
        }

        // Headings — keep as-is
        if line.starts_with('#') {
            result_lines.push(line.to_string());
            i += 1;
            continue;
        }

        // Blank lines — keep
        if line.trim().is_empty() {
            result_lines.push(String::new());
            i += 1;
            continue;
        }

        // Regular paragraph — keep only first sentence
        let sentences = split_sentences(line);
        if sentences.len() > 1 {
            result_lines.push(sentences[0].to_string());
        } else {
            result_lines.push(line.to_string());
        }
        i += 1;
    }

    result_lines.join("\n")
}

fn split_sentences(text: &str) -> Vec<&str> {
    static SENTENCE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[.!?]\s+").unwrap());

    let mut sentences = Vec::new();
    let mut last = 0;
    for m in SENTENCE_RE.find_iter(text) {
        let end = m.start() + 1; // include the punctuation
        if end > last {
            sentences.push(text[last..end].trim());
            last = m.end();
        }
    }
    if last < text.len() {
        let remainder = text[last..].trim();
        if !remainder.is_empty() {
            sentences.push(remainder);
        }
    }
    if sentences.is_empty() {
        vec![text]
    } else {
        sentences
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lite_collapses_blank_lines() {
        let input = "line1\n\n\n\nline2\n\n\nline3";
        let result = compress_prose(input, Level::Lite);
        assert_eq!(result, "line1\n\nline2\n\nline3");
    }

    #[test]
    fn test_lite_strips_html_comments() {
        let input = "before <!-- comment --> after";
        let result = compress_prose(input, Level::Lite);
        assert_eq!(result, "before  after");
    }

    #[test]
    fn test_lite_trims_trailing_whitespace() {
        let input = "line1   \nline2  ";
        let result = compress_prose(input, Level::Lite);
        assert_eq!(result, "line1\nline2");
    }

    #[test]
    fn test_standard_replaces_filler_phrases() {
        let input = "In order to fix this, we need to make changes.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.starts_with("To fix"));
    }

    #[test]
    fn test_standard_removes_filler_words() {
        let input = "This is basically a very important change that essentially fixes the bug.";
        let result = compress_prose(input, Level::Standard);
        assert!(!result.contains("basically"));
        assert!(!result.contains("essentially"));
    }

    #[test]
    fn test_standard_preserves_headings() {
        let input = "# This is basically important\nbasically a test";
        let result = compress_prose(input, Level::Standard);
        assert!(result.starts_with("# This is basically important"));
    }

    #[test]
    fn test_aggressive_truncates_long_lists() {
        let input = (1..=10)
            .map(|i| format!("- item {}", i))
            .collect::<Vec<_>>()
            .join("\n");
        let result = compress_prose(&input, Level::Aggressive);
        assert!(result.contains("- item 1"));
        assert!(result.contains("- item 5"));
        assert!(!result.contains("- item 6"));
        assert!(result.contains("[... 5 more items]"));
    }

    #[test]
    fn test_aggressive_keeps_first_sentence() {
        let input = "First sentence. Second sentence. Third sentence.";
        let result = compress_prose(input, Level::Aggressive);
        assert_eq!(result, "First sentence.");
    }

    #[test]
    fn test_preserves_code_blocks() {
        let input = "Before\n```rust\nfn basically() { basically(); }\n```\nAfter basically.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("fn basically()"));
        assert!(!result.contains("After basically")); // filler word removed from prose
    }

    #[test]
    fn test_preserves_yaml_frontmatter() {
        let input = "---\ntitle: Test\n---\nIn order to test this.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("---\ntitle: Test\n---"));
        assert!(result.contains("To test"));
    }

    #[test]
    fn test_empty_input() {
        assert_eq!(compress_prose("", Level::Lite), "");
        assert_eq!(compress_prose("", Level::Standard), "");
        assert_eq!(compress_prose("", Level::Aggressive), "");
    }

    #[test]
    fn test_already_concise_passthrough() {
        let input = "Fix the bug.";
        assert_eq!(compress_prose(input, Level::Standard), "Fix the bug.");
    }

    #[test]
    fn test_level_from_str() {
        assert_eq!(Level::from_str("lite"), Some(Level::Lite));
        assert_eq!(Level::from_str("standard"), Some(Level::Standard));
        assert_eq!(Level::from_str("aggressive"), Some(Level::Aggressive));
        assert_eq!(Level::from_str("invalid"), None);
    }

    #[test]
    fn test_numbered_list_truncation() {
        let input = (1..=8)
            .map(|i| format!("{}. item {}", i, i))
            .collect::<Vec<_>>()
            .join("\n");
        let result = compress_prose(&input, Level::Aggressive);
        assert!(result.contains("1. item 1"));
        assert!(result.contains("[... 3 more items]"));
    }

    #[test]
    fn test_preserves_inline_code() {
        let input = "Use `basically_important_func()` to basically fix things.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("`basically_important_func()`"));
    }

    #[test]
    fn test_preserves_urls() {
        let input = "Visit https://example.com/basically/path for info.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("https://example.com/basically/path"));
    }

    #[test]
    fn test_preserves_xml_tags() {
        let input = "Use <basically-tag attr=\"val\"> in the template.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("<basically-tag attr=\"val\">"));
    }

    #[test]
    fn test_preserves_file_paths() {
        let input = "Edit the file at ./src/basically/main.rs to fix this.";
        let result = compress_prose(input, Level::Standard);
        assert!(result.contains("./src/basically/main.rs"));
    }
}
