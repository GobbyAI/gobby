use regex::Regex;

/// Remove lines matching any of the given regex patterns.
pub fn filter_lines(lines: Vec<String>, patterns: &[String]) -> Vec<String> {
    if patterns.is_empty() {
        return lines;
    }

    let compiled: Vec<Regex> = patterns.iter().filter_map(|p| Regex::new(p).ok()).collect();

    lines
        .into_iter()
        .filter(|line| !compiled.iter().any(|r| r.is_match(line)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_filter_removes_matching() {
        let lines = vec![
            "keep this".into(),
            "  ".into(),
            "also keep".into(),
            "On branch main".into(),
        ];
        let patterns = vec![r"^\s*$".into(), r"^On branch ".into()];
        let result = filter_lines(lines, &patterns);
        assert_eq!(result, vec!["keep this", "also keep"]);
    }

    #[test]
    fn test_filter_empty_patterns() {
        let lines = vec!["a".into(), "b".into()];
        let result = filter_lines(lines, &[]);
        assert_eq!(result, vec!["a", "b"]);
    }

    #[test]
    fn test_filter_empty_lines() {
        let result = filter_lines(vec![], &["something".into()]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_filter_removes_all() {
        let lines = vec!["aaa".into(), "aab".into(), "aac".into()];
        let result = filter_lines(lines, &["^aa".into()]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_filter_removes_none() {
        let lines = vec!["keep".into(), "also keep".into()];
        let result = filter_lines(lines, &["^NOPE".into()]);
        assert_eq!(result, vec!["keep", "also keep"]);
    }

    #[test]
    fn test_filter_multiple_patterns() {
        let lines = vec![
            "error: bad".into(),
            "warning: meh".into(),
            "info: ok".into(),
            "debug: verbose".into(),
        ];
        let patterns = vec!["^warning".into(), "^debug".into()];
        let result = filter_lines(lines, &patterns);
        assert_eq!(result, vec!["error: bad", "info: ok"]);
    }

    #[test]
    fn test_filter_invalid_regex_skipped() {
        let lines = vec!["keep".into(), "drop".into()];
        // Invalid regex pattern should be silently skipped (filter_map)
        let result = filter_lines(lines.clone(), &["[invalid".into()]);
        assert_eq!(result, lines);
    }
}
