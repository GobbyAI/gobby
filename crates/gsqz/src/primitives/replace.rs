use regex::Regex;

use crate::config::ReplaceRule;

/// Apply regex replacement rules sequentially to each line.
/// Each rule's output feeds into the next rule. Invalid regexes are skipped.
pub fn replace(lines: Vec<String>, rules: &[ReplaceRule]) -> Vec<String> {
    if rules.is_empty() {
        return lines;
    }

    let compiled: Vec<(&str, Regex)> = rules
        .iter()
        .filter_map(|r| Regex::new(&r.pattern).ok().map(|re| (r.replacement.as_str(), re)))
        .collect();

    lines
        .into_iter()
        .map(|mut line| {
            for (replacement, re) in &compiled {
                line = re.replace_all(&line, *replacement).into_owned();
            }
            line
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(pattern: &str, replacement: &str) -> ReplaceRule {
        ReplaceRule {
            pattern: pattern.to_string(),
            replacement: replacement.to_string(),
        }
    }

    #[test]
    fn test_basic_replacement() {
        let lines = vec!["hello world\n".into(), "hello rust\n".into()];
        let result = replace(lines, &[rule("hello", "hi")]);
        assert_eq!(result, vec!["hi world\n", "hi rust\n"]);
    }

    #[test]
    fn test_backreferences() {
        let lines = vec!["foo:bar\n".into(), "baz:qux\n".into()];
        let result = replace(lines, &[rule(r"(\w+):(\w+)", "$2:$1")]);
        assert_eq!(result, vec!["bar:foo\n", "qux:baz\n"]);
    }

    #[test]
    fn test_chained_rules() {
        let lines = vec!["v1.2.3 release\n".into()];
        let rules = vec![
            rule(r"v\d+\.\d+\.\d+", "vX.X.X"),
            rule("release", "build"),
        ];
        let result = replace(lines, &rules);
        assert_eq!(result, vec!["vX.X.X build\n"]);
    }

    #[test]
    fn test_invalid_regex_skipped() {
        let lines = vec!["keep this\n".into()];
        let result = replace(lines.clone(), &[rule("[invalid", "nope")]);
        assert_eq!(result, lines);
    }

    #[test]
    fn test_empty_rules() {
        let lines = vec!["unchanged\n".into()];
        let result = replace(lines.clone(), &[]);
        assert_eq!(result, lines);
    }

    #[test]
    fn test_no_match_unchanged() {
        let lines = vec!["hello world\n".into()];
        let result = replace(lines.clone(), &[rule("NOPE", "yes")]);
        assert_eq!(result, lines);
    }

    #[test]
    fn test_empty_lines() {
        let result = replace(vec![], &[rule("a", "b")]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_multiple_matches_per_line() {
        let lines = vec!["aaa bbb aaa\n".into()];
        let result = replace(lines, &[rule("aaa", "x")]);
        assert_eq!(result, vec!["x bbb x\n"]);
    }
}
