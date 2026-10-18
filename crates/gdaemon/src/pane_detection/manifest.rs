//! Manifest parsing and validation for the pane detection engine (#24381, plan 2.12.4).
//!
//! Mirrors `gobby.agents.detection.schema`. Fields are checked in definition order and the
//! first failure is the refusal, with the Python message, so a manifest refused by one side is
//! refused by the other with the same text.

use std::collections::HashSet;
use std::sync::OnceLock;

use fancy_regex::Regex;

use super::regions::py_strip;

/// The newest engine version this build understands, as `CURRENT_ENGINE_VERSION`.
const CURRENT_ENGINE_VERSION: i64 = 1;
/// The most lines `bottom_non_empty_lines(N)` may name, as `MAX_BOTTOM_NON_EMPTY_LINES`.
const MAX_BOTTOM_NON_EMPTY_LINES: u64 = 200;
const MAX_PRIORITY: i64 = 10_000;

const MANIFEST_KEYS: [&str; 4] = ["id", "version", "engine", "rules"];
const RULE_KEYS: [&str; 8] = [
    "contains",
    "line_regex",
    "id",
    "state",
    "reason",
    "priority",
    "region",
    "not",
];
const CLAUSE_KEYS: [&str; 2] = ["contains", "line_regex"];

/// A detection state, as `DetectionState` in `schema.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DetectionState {
    Blocked,
    Idle,
    Working,
    Stall,
}

impl DetectionState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Blocked => "blocked",
            Self::Idle => "idle",
            Self::Working => "working",
            Self::Stall => "stall",
        }
    }
}

/// A manifest the engine refuses. The message is the one Python reports.
#[derive(Debug)]
pub enum ManifestError {
    Syntax(String),
    Schema(String),
}

impl ManifestError {
    pub fn message(&self) -> &str {
        match self {
            Self::Syntax(message) | Self::Schema(message) => message,
        }
    }
}

/// Where a rule reads, as the region strings `schema.py` accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Region {
    WholeRecent,
    PromptBox,
    Composer,
    BottomLines(usize),
}

/// A conjunction of literal and line-regex conditions, as `MatchClause`.
#[derive(Debug, Default)]
pub(super) struct ParsedClause {
    pub contains: Vec<String>,
    pub line_regex: Vec<String>,
}

#[derive(Debug)]
pub(super) struct ParsedRule {
    pub id: String,
    pub state: DetectionState,
    pub reason: Option<String>,
    pub priority: i64,
    pub region: Region,
    pub clause: ParsedClause,
    pub exclusions: Vec<ParsedClause>,
}

#[derive(Debug)]
pub(super) struct ParsedManifest {
    pub id: String,
    pub rules: Vec<ParsedRule>,
}

type Table = toml::Table;
type Value = toml::Value;

fn schema(message: impl Into<String>) -> ManifestError {
    ManifestError::Schema(message.into())
}

pub(super) fn parse_manifest(content: &str) -> Result<ParsedManifest, ManifestError> {
    let table: Table =
        toml::from_str(content).map_err(|err| ManifestError::Syntax(err.to_string()))?;
    let id = manifest_id(required(&table, "id")?)?;
    version(required(&table, "version")?)?;
    engine(required(&table, "engine")?)?;
    let rules = rules(required(&table, "rules")?)?;
    check_extras(&table, &MANIFEST_KEYS)?;
    let mut seen = HashSet::new();
    if !rules.iter().all(|rule| seen.insert(rule.id.as_str())) {
        return Err(schema("manifest rule ids must be unique"));
    }
    Ok(ParsedManifest { id, rules })
}

fn required<'a>(table: &'a Table, key: &str) -> Result<&'a Value, ManifestError> {
    table.get(key).ok_or_else(|| schema("Field required"))
}

fn check_extras(table: &Table, known: &[&str]) -> Result<(), ManifestError> {
    if table.keys().all(|key| known.contains(&key.as_str())) {
        Ok(())
    } else {
        Err(schema("Extra inputs are not permitted"))
    }
}

fn string(value: &Value) -> Result<&str, ManifestError> {
    value
        .as_str()
        .ok_or_else(|| schema("Input should be a valid string"))
}

fn array(value: &Value) -> Result<&[Value], ManifestError> {
    value
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| schema("Input should be a valid tuple"))
}

fn table_of(value: &Value) -> Result<&Table, ManifestError> {
    value.as_table().ok_or_else(|| {
        schema("Input should be a valid dictionary or object to extract fields from")
    })
}

fn integer(value: &Value) -> Result<i64, ManifestError> {
    value
        .as_integer()
        .ok_or_else(|| schema("Input should be a valid integer"))
}

fn is_identifier(value: &str) -> bool {
    let mut chars = value.chars();
    chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

fn manifest_id(value: &Value) -> Result<String, ManifestError> {
    let id = string(value)?;
    if !is_identifier(id) {
        return Err(schema(
            "manifest id must be a lowercase provider identifier",
        ));
    }
    Ok(id.to_owned())
}

fn version(value: &Value) -> Result<(), ManifestError> {
    static VERSION_RE: OnceLock<Regex> = OnceLock::new();
    let re = VERSION_RE.get_or_init(|| {
        Regex::new(r"\A\d+(?:\.\d+)*\z").expect("constant version pattern compiles")
    });
    let value = string(value)?;
    if re.is_match(value).is_ok_and(|matched| matched) {
        Ok(())
    } else {
        Err(schema("manifest version must be dotted-numeric"))
    }
}

fn engine(value: &Value) -> Result<(), ManifestError> {
    let value = integer(value)?;
    if value < 1 {
        return Err(schema("Input should be greater than or equal to 1"));
    }
    if value > CURRENT_ENGINE_VERSION {
        return Err(schema(format!(
            "manifest engine {value} is newer than supported engine {CURRENT_ENGINE_VERSION}"
        )));
    }
    Ok(())
}

fn rules(value: &Value) -> Result<Vec<ParsedRule>, ManifestError> {
    let items = array(value)?;
    if items.is_empty() {
        return Err(schema(
            "Tuple should have at least 1 item after validation, not 0",
        ));
    }
    items.iter().map(rule).collect()
}

/// Strings of one clause list: every item is typed first, then every item is checked non-blank.
fn clause_strings(value: Option<&Value>) -> Result<Vec<String>, ManifestError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let values = array(value)?
        .iter()
        .map(|item| string(item).map(str::to_owned))
        .collect::<Result<Vec<_>, _>>()?;
    if values.iter().any(|item| py_strip(item).is_empty()) {
        return Err(schema("match values must be non-empty strings"));
    }
    Ok(values)
}

fn clause(value: &Value) -> Result<ParsedClause, ManifestError> {
    let table = table_of(value)?;
    let contains = clause_strings(table.get("contains"))?;
    let line_regex = clause_strings(table.get("line_regex"))?;
    check_extras(table, &CLAUSE_KEYS)?;
    require_condition(&contains, &line_regex)?;
    Ok(ParsedClause {
        contains,
        line_regex,
    })
}

fn require_condition(contains: &[String], line_regex: &[String]) -> Result<(), ManifestError> {
    if contains.is_empty() && line_regex.is_empty() {
        return Err(schema("a match clause requires contains or line_regex"));
    }
    Ok(())
}

fn rule(value: &Value) -> Result<ParsedRule, ManifestError> {
    let table = table_of(value)?;
    let contains = clause_strings(table.get("contains"))?;
    let line_regex = clause_strings(table.get("line_regex"))?;
    let id = rule_id(required(table, "id")?)?;
    let state = state(required(table, "state")?)?;
    let reason = match table.get("reason") {
        Some(value) => Some(string(value)?.to_owned()),
        None => None,
    };
    let priority = priority(required(table, "priority")?)?;
    let region = region(required(table, "region")?)?;
    let exclusions = match table.get("not") {
        Some(value) => array(value)?
            .iter()
            .map(clause)
            .collect::<Result<Vec<_>, _>>()?,
        None => Vec::new(),
    };
    check_extras(table, &RULE_KEYS)?;
    require_condition(&contains, &line_regex)?;
    if state == DetectionState::Blocked && reason.as_deref().is_none_or(|r| py_strip(r).is_empty())
    {
        return Err(schema("blocked rules require a reason"));
    }
    Ok(ParsedRule {
        id,
        state,
        reason,
        priority,
        region,
        clause: ParsedClause {
            contains,
            line_regex,
        },
        exclusions,
    })
}

fn rule_id(value: &Value) -> Result<String, ManifestError> {
    let id = string(value)?;
    if !is_identifier(id) {
        return Err(schema("rule id must be a lowercase identifier"));
    }
    Ok(id.to_owned())
}

fn state(value: &Value) -> Result<DetectionState, ManifestError> {
    let parsed = value.as_str().and_then(|value| match value {
        "blocked" => Some(DetectionState::Blocked),
        "idle" => Some(DetectionState::Idle),
        "working" => Some(DetectionState::Working),
        "stall" => Some(DetectionState::Stall),
        _ => None,
    });
    parsed.ok_or_else(|| schema("Input should be 'blocked', 'idle', 'working' or 'stall'"))
}

fn priority(value: &Value) -> Result<i64, ManifestError> {
    let value = integer(value)?;
    if value < 0 {
        return Err(schema("Input should be greater than or equal to 0"));
    }
    if value > MAX_PRIORITY {
        return Err(schema("Input should be less than or equal to 10000"));
    }
    Ok(value)
}

fn region(value: &Value) -> Result<Region, ManifestError> {
    parse_region(string(value)?).ok_or_else(|| schema("unsupported detection region"))
}

fn parse_region(value: &str) -> Option<Region> {
    match value {
        "whole_recent" => Some(Region::WholeRecent),
        "prompt_box" => Some(Region::PromptBox),
        "composer" => Some(Region::Composer),
        _ => {
            let digits = value
                .strip_prefix("bottom_non_empty_lines(")?
                .strip_suffix(')')?;
            if digits.starts_with('0')
                || digits.is_empty()
                || !digits.bytes().all(|byte| byte.is_ascii_digit())
            {
                return None;
            }
            let count: u64 = digits.parse().ok()?;
            (count <= MAX_BOTTOM_NON_EMPTY_LINES).then_some(Region::BottomLines(count as usize))
        }
    }
}
