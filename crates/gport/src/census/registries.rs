//! Section 5: every class whose name ends in `Registry`, with references from outside its module.

use std::collections::{BTreeMap, BTreeSet};

use super::scan::{TextFile, tokens};
use super::table;

const HEADING: &str = "## 5. In-memory registries";

pub fn section(python: &[&TextFile]) -> String {
    let mut homes: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for file in python {
        for line in file.text.lines() {
            let Some(rest) = line.trim_start().strip_prefix("class ") else {
                continue;
            };
            let name: String = rest
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                .collect();
            if name.ends_with("Registry") {
                homes
                    .entry(name)
                    .or_default()
                    .insert(file.inner().to_owned());
            }
        }
    }

    let mut counts: BTreeMap<&str, BTreeMap<&str, usize>> = BTreeMap::new();
    for file in python {
        let package = file.package().unwrap_or("(root)");
        for token in tokens(&file.text) {
            let Some((name, defined_in)) = homes.get_key_value(token.ident) else {
                continue;
            };
            if defined_in.contains(file.inner()) {
                continue;
            }
            *counts
                .entry(name.as_str())
                .or_default()
                .entry(package)
                .or_default() += 1;
        }
    }

    if homes.is_empty() {
        return format!("{HEADING}\n\nNo class under src/gobby has a name ending in Registry.\n");
    }
    let rows: Vec<Vec<String>> = homes
        .iter()
        .map(|(name, defined_in)| {
            let references = match counts.get(name.as_str()) {
                Some(by_package) if !by_package.is_empty() => by_package
                    .iter()
                    .map(|(package, count)| format!("{package} {count}"))
                    .collect::<Vec<_>>()
                    .join(", "),
                _ => "none".to_owned(),
            };
            let defined_in: Vec<&str> = defined_in.iter().map(String::as_str).collect();
            vec![format!("`{name}`"), defined_in.join(", "), references]
        })
        .collect();
    format!(
        "{HEADING}\n\nEvery class under src/gobby whose name ends in Registry, with its references \
         from outside its defining module, counted by top-level package.\n\n{}",
        table(&["Registry", "Defined in", "Outside references"], &rows)
    )
}
