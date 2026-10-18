//! Compiled manifests and matching (#24381, plan 2.12.2), as `CompiledManifest` in
//! `gobby.agents.detection.matcher`.

use fancy_regex::{Regex, RegexBuilder};

use super::manifest::{DetectionState, ManifestError, ParsedClause, ParsedRule, Region, parse_manifest};
use super::regions::{casefold, select_region};

/// The longest pattern accepted, in code points, as `MAX_PATTERN_SIZE` in `safe_regex.py`.
const MAX_PATTERN_SIZE: usize = 2_048;
/// Backtracking steps one search may take before it reads as `pattern_timeout`.
const BACKTRACK_LIMIT: usize = 1_000_000;

/// Why a pattern was dropped or abandoned, as `IssueCode` in `matcher.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IssueCode {
    InvalidPattern,
    PatternTimeout,
}

impl IssueCode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidPattern => "invalid_pattern",
            Self::PatternTimeout => "pattern_timeout",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestIssue {
    pub rule_id: String,
    pub code: IssueCode,
    pub pattern: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetectionMatch {
    pub rule_id: String,
    pub state: DetectionState,
    pub reason: Option<String>,
    pub priority: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MatchEvaluation {
    pub matched: Option<DetectionMatch>,
    pub issues: Vec<ManifestIssue>,
}

#[derive(Debug)]
pub struct CompiledManifest {
    id: String,
    rules: Vec<CompiledRule>,
    issues: Vec<ManifestIssue>,
}

#[derive(Debug)]
struct CompiledRule {
    id: String,
    state: DetectionState,
    reason: Option<String>,
    priority: i64,
    region: Region,
    clause: CompiledClause,
    exclusions: Vec<CompiledClause>,
}

#[derive(Debug)]
struct CompiledClause {
    contains: Vec<String>,
    line_regex: Vec<SafeRegex>,
}

#[derive(Debug)]
struct SafeRegex {
    source: String,
    compiled: Regex,
}

/// Compiles parsed manifest content. Rules compile in descending priority, and the sort is
/// stable, as `_compile_fingerprint` sorts them. A rule with an invalid pattern is dropped
/// with an `invalid_pattern` issue.
pub fn compile_manifest(content: &str) -> Result<CompiledManifest, ManifestError> {
    let manifest = parse_manifest(content)?;
    let mut ordered = manifest.rules;
    ordered.sort_by(|left, right| right.priority.cmp(&left.priority));
    let mut rules = Vec::new();
    let mut issues = Vec::new();
    for rule in ordered {
        let ParsedRule {
            id,
            state,
            reason,
            priority,
            region,
            clause,
            exclusions,
        } = rule;
        let clause = match compile_clause(&clause, &id) {
            Ok(clause) => clause,
            Err(issue) => {
                issues.push(issue);
                continue;
            }
        };
        let mut compiled_exclusions = Vec::with_capacity(exclusions.len());
        let mut invalid = false;
        for excluded in &exclusions {
            match compile_clause(excluded, &id) {
                Ok(compiled) => compiled_exclusions.push(compiled),
                Err(issue) => {
                    issues.push(issue);
                    invalid = true;
                    break;
                }
            }
        }
        if invalid {
            continue;
        }
        rules.push(CompiledRule {
            id,
            state,
            reason,
            priority,
            region,
            clause,
            exclusions: compiled_exclusions,
        });
    }
    Ok(CompiledManifest {
        id: manifest.id,
        rules,
        issues,
    })
}

fn compile_clause(clause: &ParsedClause, rule_id: &str) -> Result<CompiledClause, ManifestIssue> {
    let mut line_regex = Vec::with_capacity(clause.line_regex.len());
    for pattern in &clause.line_regex {
        let Some(compiled) = compile_pattern(pattern) else {
            return Err(ManifestIssue {
                rule_id: rule_id.to_owned(),
                code: IssueCode::InvalidPattern,
                pattern: pattern.clone(),
            });
        };
        line_regex.push(SafeRegex {
            source: pattern.clone(),
            compiled,
        });
    }
    Ok(CompiledClause {
        contains: clause.contains.iter().map(|needle| casefold(needle)).collect(),
        line_regex,
    })
}

/// Compiles one pattern as Python's `compile_safe_regex` does: under the size limit, with
/// `MULTILINE`, and with a backtrack budget. The `(?m)` prefix gives Python's `re.M`, which
/// fancy-regex has no builder option for.
fn compile_pattern(pattern: &str) -> Option<Regex> {
    if pattern.chars().count() > MAX_PATTERN_SIZE {
        return None;
    }
    let multiline = format!("(?m){pattern}");
    RegexBuilder::new(&multiline)
        .backtrack_limit(BACKTRACK_LIMIT)
        .build()
        .ok()
}

impl CompiledClause {
    /// Whether the clause holds for `text`. An `Err` is a pattern that ran out of budget.
    fn evaluate(&self, text: &str, rule_id: &str) -> Result<bool, ManifestIssue> {
        if !self.contains.is_empty() {
            let folded = casefold(text);
            if self
                .contains
                .iter()
                .any(|needle| !folded.contains(needle.as_str()))
            {
                return Ok(false);
            }
        }
        for pattern in &self.line_regex {
            match pattern.compiled.is_match(text) {
                Ok(true) => {}
                Ok(false) => return Ok(false),
                // Any runtime error is a search that could not finish: fancy-regex reports
                // the backtrack budget as one, and so does the Python matcher.
                Err(_) => {
                    return Err(ManifestIssue {
                        rule_id: rule_id.to_owned(),
                        code: IssueCode::PatternTimeout,
                        pattern: pattern.source.clone(),
                    });
                }
            }
        }
        Ok(true)
    }
}

impl CompiledManifest {
    pub(super) fn manifest_id(&self) -> &str {
        &self.id
    }

    /// The first rule to match, in priority order, with every issue seen on the way.
    pub fn detect(&self, pane: &str) -> MatchEvaluation {
        let mut issues = self.issues.clone();
        for rule in &self.rules {
            if let Some(matched) = evaluate_rule(rule, pane, &mut issues) {
                return MatchEvaluation {
                    matched: Some(matched),
                    issues,
                };
            }
        }
        MatchEvaluation {
            matched: None,
            issues,
        }
    }

    /// Evaluates one named rule, as `match_rule`. A rule dropped at compile time is absent.
    pub fn match_rule(&self, rule_id: &str, pane: &str) -> MatchEvaluation {
        let mut issues = self.issues.clone();
        let matched = self
            .rules
            .iter()
            .find(|rule| rule.id == rule_id)
            .and_then(|rule| evaluate_rule(rule, pane, &mut issues));
        MatchEvaluation { matched, issues }
    }

    pub fn issues(&self) -> &[ManifestIssue] {
        &self.issues
    }

    pub fn rule_ids(&self) -> Vec<&str> {
        self.rules.iter().map(|rule| rule.id.as_str()).collect()
    }
}

fn evaluate_rule(
    rule: &CompiledRule,
    pane: &str,
    issues: &mut Vec<ManifestIssue>,
) -> Option<DetectionMatch> {
    let region_text = select_region(pane, rule.region);
    match rule.clause.evaluate(&region_text, &rule.id) {
        Ok(true) => {}
        Ok(false) => return None,
        Err(issue) => {
            issues.push(issue);
            return None;
        }
    }
    for exclusion in &rule.exclusions {
        match exclusion.evaluate(&region_text, &rule.id) {
            Ok(false) => {}
            Ok(true) => return None,
            Err(issue) => {
                issues.push(issue);
                return None;
            }
        }
    }
    Some(DetectionMatch {
        rule_id: rule.id.clone(),
        state: rule.state,
        reason: rule.reason.clone(),
        priority: rule.priority,
    })
}
