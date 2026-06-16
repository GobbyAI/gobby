use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde_json::Value;

use super::{ParsedSession, non_empty_string};
use crate::ingest::MetadataValue;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct ParsedSessionMetadata {
    pub(crate) model: Option<String>,
    pub(crate) token_totals: BTreeMap<String, u64>,
    pub(crate) git_branch: Option<String>,
    pub(crate) is_subagent: bool,
}

impl ParsedSessionMetadata {
    pub(crate) fn set_model_once(&mut self, model: Option<&str>) {
        if self.model.is_none() {
            self.model = model.and_then(non_empty_string);
        }
    }

    pub(crate) fn set_git_branch_once(&mut self, git_branch: Option<&str>) {
        if self.git_branch.is_none() {
            self.git_branch = git_branch.and_then(non_empty_string);
        }
    }

    pub(crate) fn mark_subagent(&mut self) {
        self.is_subagent = true;
    }

    pub(crate) fn add_token_usage(&mut self, usage: &Value) {
        add_json_number_fields(&mut self.token_totals, usage);
    }

    pub(crate) fn set_token_totals(&mut self, usage: &Value) {
        let mut totals = BTreeMap::new();
        add_json_number_fields(&mut totals, usage);
        if !totals.is_empty() {
            self.token_totals = totals;
        }
    }
}

pub(crate) fn session_metadata_fields(
    parsed: &ParsedSession,
) -> Vec<(&'static str, MetadataValue)> {
    let mut fields = Vec::new();

    if let Some(model) = &parsed.metadata.model {
        fields.push(("model", MetadataValue::string(model.clone())));
    }

    let tool_counts = tool_counts(parsed);
    if !tool_counts.is_empty() {
        fields.push(("tool_counts", MetadataValue::json(&tool_counts)));
    }

    if !parsed.metadata.token_totals.is_empty() {
        fields.push((
            "token_totals",
            MetadataValue::json(&parsed.metadata.token_totals),
        ));
    }

    if let Some(duration_seconds) = duration_seconds(parsed) {
        fields.push(("duration_seconds", MetadataValue::number(duration_seconds)));
    }

    let hour_buckets = hour_buckets(parsed);
    if !hour_buckets.is_empty() {
        fields.push(("hour_buckets", MetadataValue::json(&hour_buckets)));
    }

    fields.push((
        "is_subagent",
        MetadataValue::bool(parsed.metadata.is_subagent),
    ));

    if let Some(git_branch) = &parsed.metadata.git_branch {
        fields.push(("gitBranch", MetadataValue::string(git_branch.clone())));
    }

    fields
}

fn add_json_number_fields(totals: &mut BTreeMap<String, u64>, usage: &Value) {
    let Some(usage) = usage.as_object() else {
        return;
    };

    for (key, value) in usage {
        let Some(number) = json_u64(value) else {
            continue;
        };
        *totals.entry(key.clone()).or_default() += number;
    }
}

fn json_u64(value: &Value) -> Option<u64> {
    value
        .as_u64()
        .or_else(|| value.as_i64().and_then(|number| u64::try_from(number).ok()))
}

fn tool_counts(parsed: &ParsedSession) -> BTreeMap<String, u64> {
    let mut counts = BTreeMap::new();
    for message in &parsed.messages {
        for tool_name in &message.tool_names {
            let Some(tool_name) = non_empty_string(tool_name) else {
                continue;
            };
            *counts.entry(tool_name).or_default() += 1;
        }
    }
    counts
}

fn duration_seconds(parsed: &ParsedSession) -> Option<i64> {
    let mut timestamps = parsed
        .messages
        .iter()
        .filter_map(|message| message.timestamp.as_deref())
        .filter_map(parse_timestamp);
    let first = timestamps.next()?;
    let (earliest, latest) = timestamps.fold((first, first), |(earliest, latest), timestamp| {
        (earliest.min(timestamp), latest.max(timestamp))
    });
    Some(latest.signed_duration_since(earliest).num_seconds())
}

fn hour_buckets(parsed: &ParsedSession) -> BTreeMap<String, u64> {
    let mut buckets = BTreeMap::new();
    for timestamp in parsed
        .messages
        .iter()
        .filter_map(|message| message.timestamp.as_deref())
        .filter_map(parse_timestamp)
    {
        let hour = timestamp.format("%Y-%m-%dT%H:00:00Z").to_string();
        *buckets.entry(hour).or_default() += 1;
    }
    buckets
}

fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|timestamp| timestamp.with_timezone(&Utc))
}
