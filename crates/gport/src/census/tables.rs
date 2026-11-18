//! Section 3: the modules that write each table, by `INSERT INTO`, `UPDATE ... SET` or
//! `DELETE FROM`.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::anyhow;
use fancy_regex::Regex;

use super::scan::TextFile;
use super::table;

const HEADING: &str = "## 3. Writers by table";

pub fn section(python: &[&TextFile]) -> anyhow::Result<String> {
    let inserts = pattern(r"\b(?:INSERT\s+INTO|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)")?;
    let updates = pattern(r"\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\s+SET\b")?;
    let mut writers: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for file in python {
        for regex in [&inserts, &updates] {
            for found in regex.captures_iter(&file.text) {
                let found = found.map_err(|error| anyhow!("cannot scan {}: {error}", file.rel))?;
                if let Some(name) = found.get(1) {
                    writers
                        .entry(name.as_str().to_owned())
                        .or_default()
                        .insert(file.inner().to_owned());
                }
            }
        }
    }
    let body = if writers.is_empty() {
        "No module under src/gobby writes a table.\n".to_owned()
    } else {
        let rows: Vec<Vec<String>> = writers
            .iter()
            .map(|(name, modules)| {
                let modules: Vec<&str> = modules.iter().map(String::as_str).collect();
                vec![name.clone(), modules.join(", ")]
            })
            .collect();
        table(&["Table", "Writer modules"], &rows)
    };
    Ok(format!(
        "{HEADING}\n\nModules under src/gobby with an INSERT INTO, UPDATE ... SET or DELETE FROM \
         statement, per table.\n\n{body}"
    ))
}

fn pattern(source: &str) -> anyhow::Result<Regex> {
    Regex::new(source).map_err(|error| anyhow!("bad census pattern {source}: {error}"))
}
