use regex::Regex;

/// Keep first `head` + last `tail` lines, replace middle with omission marker.
/// If `per_file_lines > 0` and `file_marker` is set, truncate per-section instead.
pub fn truncate(
    lines: Vec<String>,
    head: usize,
    tail: usize,
    per_file_lines: usize,
    file_marker: &str,
) -> Vec<String> {
    if per_file_lines > 0 && !file_marker.is_empty() {
        return truncate_per_section(lines, per_file_lines, file_marker);
    }

    let total = lines.len();
    if total <= head + tail {
        return lines;
    }

    let omitted = total - head - tail;
    let mut result = Vec::with_capacity(head + tail + 1);
    result.extend_from_slice(&lines[..head]);
    result.push(format!("\n[... {} lines omitted ...]\n", omitted));
    result.extend_from_slice(&lines[total - tail..]);
    result
}

fn truncate_per_section(lines: Vec<String>, max_lines: usize, marker_pattern: &str) -> Vec<String> {
    let marker_re = match Regex::new(marker_pattern) {
        Ok(r) => r,
        Err(_) => return lines,
    };

    let mut sections: Vec<Vec<String>> = Vec::new();
    let mut current: Vec<String> = Vec::new();

    for line in lines {
        if marker_re.is_match(&line) && !current.is_empty() {
            sections.push(std::mem::take(&mut current));
        }
        current.push(line);
    }
    if !current.is_empty() {
        sections.push(current);
    }

    let mut result = Vec::new();
    for section in sections {
        if section.len() > max_lines {
            let top = max_lines.div_ceil(2);
            let bottom = max_lines - top;
            let omitted = section.len() - max_lines;
            result.extend_from_slice(&section[..top]);
            result.push(format!(
                "  [... {} lines omitted in section ...]\n",
                omitted
            ));
            if bottom > 0 {
                result.extend_from_slice(&section[section.len() - bottom..]);
            }
        } else {
            result.extend(section);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_truncate_short_input() {
        let lines: Vec<String> = (0..5).map(|i| format!("line {}", i)).collect();
        let result = truncate(lines.clone(), 3, 3, 0, "");
        assert_eq!(result, lines);
    }

    #[test]
    fn test_truncate_long_input() {
        let lines: Vec<String> = (0..100).map(|i| format!("line {}", i)).collect();
        let result = truncate(lines, 3, 2, 0, "");
        assert_eq!(result.len(), 6); // 3 head + 1 marker + 2 tail
        assert!(result[3].contains("95 lines omitted"));
        assert_eq!(result[4], "line 98");
        assert_eq!(result[5], "line 99");
    }

    #[test]
    fn test_truncate_per_section() {
        let mut lines = Vec::new();
        for i in 0..10 {
            lines.push(format!("@@ section {}", i));
            for j in 0..5 {
                lines.push(format!("  content {}:{}", i, j));
            }
        }
        let result = truncate(lines, 0, 0, 3, r"^@@\s");
        // Each 6-line section (marker + 5 content) should be truncated to 3
        assert!(
            result
                .iter()
                .any(|l| l.contains("lines omitted in section"))
        );
    }

    #[test]
    fn test_truncate_empty_input() {
        let result = truncate(vec![], 5, 5, 0, "");
        assert!(result.is_empty());
    }

    #[test]
    fn test_truncate_exact_boundary() {
        // Exactly head + tail lines — no truncation
        let lines: Vec<String> = (0..10).map(|i| format!("line {}", i)).collect();
        let result = truncate(lines.clone(), 5, 5, 0, "");
        assert_eq!(result, lines);
    }

    #[test]
    fn test_truncate_one_over_boundary() {
        let lines: Vec<String> = (0..11).map(|i| format!("line {}\n", i)).collect();
        let result = truncate(lines, 5, 5, 0, "");
        assert_eq!(result.len(), 11); // 5 head + 1 marker + 5 tail
        assert!(result[5].contains("1 lines omitted"));
    }

    #[test]
    fn test_truncate_head_only() {
        let lines: Vec<String> = (0..20).map(|i| format!("line {}\n", i)).collect();
        let result = truncate(lines, 10, 0, 0, "");
        assert_eq!(result.len(), 11); // 10 head + 1 marker
        assert!(result[10].contains("10 lines omitted"));
    }

    #[test]
    fn test_truncate_tail_only() {
        let lines: Vec<String> = (0..20).map(|i| format!("line {}\n", i)).collect();
        let result = truncate(lines, 0, 5, 0, "");
        assert_eq!(result.len(), 6); // 1 marker + 5 tail
        assert!(result[0].contains("15 lines omitted"));
        assert_eq!(result[1], "line 15\n");
    }

    #[test]
    fn test_truncate_per_section_small_sections_unchanged() {
        let mut lines = Vec::new();
        for i in 0..3 {
            lines.push(format!("@@ section {}", i));
            lines.push(format!("  line {}", i));
        }
        let result = truncate(lines.clone(), 0, 0, 10, r"^@@\s");
        // Each section is only 2 lines, well under max of 10
        assert_eq!(result, lines);
    }

    #[test]
    fn test_truncate_per_section_invalid_regex() {
        let lines = vec!["a".into(), "b".into()];
        let result = truncate(lines.clone(), 0, 0, 3, "[invalid");
        // Invalid regex returns lines unchanged
        assert_eq!(result, lines);
    }

    #[test]
    fn test_truncate_preserves_head_tail_content() {
        let lines: Vec<String> = (0..100).map(|i| format!("line {}\n", i)).collect();
        let result = truncate(lines, 3, 3, 0, "");
        assert_eq!(result[0], "line 0\n");
        assert_eq!(result[1], "line 1\n");
        assert_eq!(result[2], "line 2\n");
        assert!(result[3].contains("omitted"));
        assert_eq!(result[4], "line 97\n");
        assert_eq!(result[5], "line 98\n");
        assert_eq!(result[6], "line 99\n");
    }
}
