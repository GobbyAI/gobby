//! The native capability port matches Python's capabilities_from_snapshot for every
//! committed vector.

use std::collections::BTreeMap;

use anyhow::{Result, ensure};
use gobby_core::grant::{GrantCapabilities, PostgresCapability};
use gobby_runtime::capabilities::capabilities_from_snapshot;
use serde_json::{Map, Value};

const VECTORS: &str = include_str!("fixtures/capability_vectors.json");

#[test]
fn capabilities_match_python_vectors() -> Result<()> {
    let document: Value = serde_json::from_str(VECTORS)?;
    let cases = document["cases"].as_array().cloned().unwrap_or_default();
    ensure!(
        !cases.is_empty(),
        "capability vectors must be generated from Python"
    );
    let postgres: PostgresCapability = serde_json::from_value(document["postgres"].clone())?;
    for case in &cases {
        let name = case["name"].as_str().unwrap_or_default();
        let values: Map<String, Value> = serde_json::from_value(case["values"].clone())?;
        let secrets: BTreeMap<String, Option<String>> =
            serde_json::from_value(case["secrets"].clone())?;
        let expected: GrantCapabilities = serde_json::from_value(case["expected"].clone())?;
        let actual = capabilities_from_snapshot(&values, &secrets, postgres.clone());
        assert_eq!(actual, expected, "capability vector {name}");
    }
    Ok(())
}
