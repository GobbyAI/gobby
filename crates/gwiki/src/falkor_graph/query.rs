use std::collections::HashMap;

use gobby_core::falkor::Row;
use serde_json::Value;

use crate::search::{SearchError, SearchScope};

pub(super) fn scope_params(scope: &SearchScope) -> Option<HashMap<String, String>> {
    // falkordb 0.2 interpolates params as raw `CYPHER key=value` text, so
    // string values must be encoded as Cypher string literals by the caller.
    scope.scope_filter().map(|(scope_kind, scope_id)| {
        HashMap::from([
            (
                "scope_kind".to_string(),
                gobby_core::falkor::escape_string(scope_kind),
            ),
            (
                "scope_id".to_string(),
                gobby_core::falkor::escape_string(scope_id),
            ),
        ])
    })
}

pub(super) fn row_string(row: &Row, key: &'static str) -> Result<String, SearchError> {
    optional_row_string(row, key)
        .ok_or_else(|| SearchError::Backend(format!("FalkorDB wiki graph row missing `{key}`")))
}

pub(super) fn optional_row_string(row: &Row, key: &'static str) -> Option<String> {
    row.get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

pub(super) fn optional_row_usize(row: &Row, key: &'static str) -> Option<usize> {
    row.get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}
