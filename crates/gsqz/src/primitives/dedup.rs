use regex::Regex;

use std::sync::LazyLock;

static NUMBER_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d+").unwrap());

/// Collapse consecutive identical or near-identical lines.
/// Near-identical: lines that differ only in numbers.
pub fn dedup(lines: Vec<String>) -> Vec<String> {
    if lines.is_empty() {
        return lines;
    }

    let mut result: Vec<String> = Vec::new();
    let mut prev_normalized: Option<String> = None;
    let mut prev_line = String::new();
    let mut count: usize = 0;

    for line in &lines {
        let normalized = NUMBER_RE.replace_all(line.trim(), "N").to_string();
        if Some(&normalized) == prev_normalized.as_ref() {
            count += 1;
        } else {
            if count > 1 {
                result.push(format!("  [repeated {} times]\n", count));
            } else if count == 1 {
                result.push(prev_line.clone());
            }
            prev_normalized = Some(normalized);
            prev_line = line.clone();
            count = 1;
        }
    }

    // Flush last group
    if count > 1 {
        result.push(prev_line);
        result.push(format!("  [repeated {} times]\n", count));
    } else if count == 1 {
        result.push(prev_line);
    }

    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dedup_identical() {
        let lines = vec!["same\n".into(), "same\n".into(), "same\n".into()];
        let result = dedup(lines);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0], "same\n");
        assert!(result[1].contains("repeated 3 times"));
    }

    #[test]
    fn test_dedup_near_identical() {
        let lines = vec![
            "line 1: error at pos 42\n".into(),
            "line 2: error at pos 99\n".into(),
            "line 3: error at pos 7\n".into(),
        ];
        let result = dedup(lines);
        assert_eq!(result.len(), 2); // first line + "repeated 3 times"
        assert!(result[1].contains("repeated 3 times"));
    }

    #[test]
    fn test_dedup_different() {
        let lines = vec!["a\n".into(), "b\n".into(), "c\n".into()];
        let result = dedup(lines);
        assert_eq!(result, vec!["a\n", "b\n", "c\n"]);
    }

    #[test]
    fn test_dedup_empty() {
        let result = dedup(vec![]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_dedup_single_line() {
        let result = dedup(vec!["only one\n".into()]);
        assert_eq!(result, vec!["only one\n"]);
    }

    #[test]
    fn test_dedup_two_identical() {
        let result = dedup(vec!["same\n".into(), "same\n".into()]);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0], "same\n");
        assert!(result[1].contains("repeated 2 times"));
    }

    #[test]
    fn test_dedup_mixed_groups() {
        let lines = vec![
            "error at line 1\n".into(),
            "error at line 2\n".into(),
            "error at line 3\n".into(),
            "warning: something\n".into(),
            "warning: something\n".into(),
            "different line\n".into(),
        ];
        let result = dedup(lines);
        // First group: "error at line N" x3 -> line + repeated
        // Second group: "warning" x2 -> line + repeated
        // Third: different line
        assert!(result.iter().any(|l| l.contains("repeated 3 times")));
        assert!(result.iter().any(|l| l.contains("repeated 2 times")));
        assert!(result.iter().any(|l| l.contains("different line")));
    }

    #[test]
    fn test_dedup_non_consecutive_identical_not_collapsed() {
        let lines = vec!["aaa\n".into(), "bbb\n".into(), "aaa\n".into()];
        let result = dedup(lines);
        // Non-consecutive identical lines should NOT be collapsed
        assert_eq!(result, vec!["aaa\n", "bbb\n", "aaa\n"]);
    }
}
