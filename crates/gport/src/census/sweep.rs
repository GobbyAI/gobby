//! Section 6: for each MCP ledger row, the modules that hold its tool name as a string literal.
//! Dispatch by name is invisible to the import graph, so this sweep shows it.

use std::collections::{BTreeMap, BTreeSet};

use super::scan::TextFile;
use super::table;
use crate::rows;

const HEADING: &str = "## 6. Tool-name sweep";

pub fn section(python: &[&TextFile], frozen: Result<&str, &str>) -> String {
    let ledger = match frozen {
        Ok(text) => text,
        Err(reason) => {
            return format!(
                "{HEADING}\n\nThe frozen ledger is not readable, so no tool name was swept: \
                 {reason}.\n"
            );
        }
    };
    let tools: Vec<(String, String)> = rows::rows(ledger)
        .into_iter()
        .filter_map(|row| {
            let tool = row.key.strip_prefix("MCP ")?.split_once(':')?.1.to_owned();
            Some((row.key, tool))
        })
        .collect();

    let mut holders: BTreeMap<&str, BTreeSet<String>> = BTreeMap::new();
    for file in python {
        for line in file.text.lines() {
            for literal in string_literals(line) {
                for (_, tool) in &tools {
                    if names_tool(literal, tool) {
                        holders
                            .entry(tool.as_str())
                            .or_default()
                            .insert(file.inner().to_owned());
                    }
                }
            }
        }
    }

    let rows: Vec<Vec<String>> = tools
        .iter()
        .map(|(key, tool)| {
            let modules: Vec<&str> = holders
                .get(tool.as_str())
                .map_or_else(Vec::new, |set| set.iter().map(String::as_str).collect());
            let cell = if modules.is_empty() {
                "none".to_owned()
            } else {
                modules.join(", ")
            };
            vec![format!("`{key}`"), cell]
        })
        .collect();
    format!(
        "{HEADING}\n\nOne row per MCP ledger row, with the modules under src/gobby whose string \
         literals hold its tool name. A bare identifier does not count.\n\n{}",
        table(
            &["Ledger row", "Modules holding the name as a string literal"],
            &rows
        )
    )
}

/// Whether a string literal names `tool`, alone or qualified by a server as `server:tool`.
fn names_tool(literal: &str, tool: &str) -> bool {
    literal == tool
        || literal
            .strip_suffix(tool)
            .is_some_and(|rest| rest.ends_with(':'))
}

/// The single-line string literals of one source line, without their quotes. A literal that is
/// not closed on its line ends the scan of that line.
fn string_literals(line: &str) -> Vec<&str> {
    let bytes = line.as_bytes();
    let mut literals = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        let quote = bytes[at];
        if quote != b'"' && quote != b'\'' {
            at += 1;
            continue;
        }
        let start = at + 1;
        let mut end = start;
        while end < bytes.len() && bytes[end] != quote {
            end += if bytes[end] == b'\\' { 2 } else { 1 };
        }
        if end >= bytes.len() {
            break;
        }
        literals.push(&line[start..end]);
        at = end + 1;
    }
    literals
}
