use regex::Regex;

use crate::config::MatchOutputRule;

/// Check the full output blob against match rules.
/// Returns `Some(message)` if a rule matches (and its `unless` doesn't), `None` otherwise.
/// First matching rule wins.
pub fn check(lines: &[String], rules: &[MatchOutputRule]) -> Option<String> {
    let blob: String = lines.concat();

    for rule in rules {
        let re = match Regex::new(&rule.pattern) {
            Ok(re) => re,
            Err(_) => continue,
        };

        if !re.is_match(&blob) {
            continue;
        }

        // Check unless pattern — if it matches, this rule doesn't fire
        if let Some(ref unless) = rule.unless {
            if let Ok(unless_re) = Regex::new(unless) {
                if unless_re.is_match(&blob) {
                    continue;
                }
            }
        }

        return Some(rule.message.clone());
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(pattern: &str, message: &str, unless: Option<&str>) -> MatchOutputRule {
        MatchOutputRule {
            pattern: pattern.to_string(),
            message: message.to_string(),
            unless: unless.map(|s| s.to_string()),
        }
    }

    fn lines(s: &str) -> Vec<String> {
        s.lines().map(|l| format!("{}\n", l)).collect()
    }

    #[test]
    fn test_basic_match_returns_message() {
        let rules = vec![rule("test result: ok", "All tests passed.", None)];
        let result = check(&lines("running 5 tests\ntest result: ok. 5 passed"), &rules);
        assert_eq!(result, Some("All tests passed.".into()));
    }

    #[test]
    fn test_unless_blocks_match() {
        let rules = vec![rule("test result: ok", "All tests passed.", Some("FAILED"))];
        let result = check(&lines("test result: ok. 4 passed; 1 FAILED"), &rules);
        assert_eq!(result, None);
    }

    #[test]
    fn test_unless_absent_allows_match() {
        let rules = vec![rule("passed", "All good.", None)];
        let result = check(&lines("5 passed, 0 failed"), &rules);
        assert_eq!(result, Some("All good.".into()));
    }

    #[test]
    fn test_no_match_returns_none() {
        let rules = vec![rule("NOPE", "msg", None)];
        let result = check(&lines("nothing matches here"), &rules);
        assert_eq!(result, None);
    }

    #[test]
    fn test_first_rule_wins() {
        let rules = vec![
            rule("passed", "First message.", None),
            rule("passed", "Second message.", None),
        ];
        let result = check(&lines("all passed"), &rules);
        assert_eq!(result, Some("First message.".into()));
    }

    #[test]
    fn test_invalid_regex_skipped() {
        let rules = vec![rule("[invalid", "bad", None), rule("ok", "good", None)];
        let result = check(&lines("ok"), &rules);
        assert_eq!(result, Some("good".into()));
    }

    #[test]
    fn test_invalid_unless_regex_treated_as_no_unless() {
        let rules = vec![rule("ok", "good", Some("[invalid"))];
        let result = check(&lines("ok"), &rules);
        assert_eq!(result, Some("good".into()));
    }

    #[test]
    fn test_empty_rules_returns_none() {
        let result = check(&lines("some output"), &[]);
        assert_eq!(result, None);
    }

    #[test]
    fn test_checks_full_blob_not_per_line() {
        // Pattern spans across what would be two lines — needs (?s) for dotall
        let rules = vec![rule("(?s)line1.*line2", "spans", None)];
        let result = check(&lines("line1\nline2"), &rules);
        assert_eq!(result, Some("spans".into()));
    }
}
