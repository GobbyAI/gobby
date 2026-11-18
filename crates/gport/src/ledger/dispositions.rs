//! Open-node dispositions: scripts/port/check_dispositions.py. Every open node has exactly one
//! valid disposition, and each target names a chunk, a container, or a decision in the map.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use serde_json::Value;

use super::pyfmt::{
    json_counts, json_list, or_empty_stripped, py_repr_opt, py_str, py_str_opt, py_string_repr,
};

const KINDS: [&str; 6] = [
    "chunk",
    "superseded",
    "container",
    "start_gate",
    "post_1_0",
    "decision",
];
const CHUNK_TARGET: [&str; 2] = ["chunk", "superseded"];
/// Most lines of each kind printed before the rest are dropped, as the Python checker caps them.
const MISSING_CAP: usize = 80;
const UNKNOWN_CAP: usize = 40;
const DOUBLE_CAP: usize = 40;
const INVALID_CAP: usize = 80;

/// The disposition check's report and its verdict.
pub struct Dispositions {
    lines: Vec<String>,
    clean: bool,
}

/// One disposition object and the file that holds it.
struct Item<'a> {
    value: &'a Value,
    file: &'a str,
}

/// The dispositions of one stripped `ref`, in the order they were read.
type Group<'a> = (String, Vec<Item<'a>>);

impl Dispositions {
    /// Check `nodes` (the open-nodes document) against the map and the disposition files, given in
    /// the order they are read.
    pub fn check(nodes: &Value, map: &Value, files: &[(String, Value)]) -> Result<Self> {
        let nodes = nodes
            .get("nodes")
            .and_then(Value::as_array)
            .context("the open-nodes file has no nodes list")?;
        let mut expected: BTreeMap<String, &Value> = BTreeMap::new();
        for node in nodes {
            let reference = node
                .get("ref")
                .and_then(Value::as_str)
                .context("an open node has no string ref")?;
            expected.insert(reference.to_owned(), node);
        }
        let mut chunk_ids: BTreeSet<String> = BTreeSet::new();
        for chunk in list_of(map, "chunks")? {
            let id = chunk
                .get("id")
                .and_then(Value::as_str)
                .context("a map chunk has no string id")?;
            chunk_ids.insert(id.to_owned());
        }
        let mut decision_ids: BTreeSet<String> = BTreeSet::new();
        for decision in list_of(map, "decisions")? {
            match decision.as_object() {
                Some(object) => {
                    if let Some(id) = object.get("id").and_then(Value::as_str) {
                        decision_ids.insert(id.to_owned());
                    }
                }
                None => {
                    decision_ids.insert(py_str(decision));
                }
            }
        }

        let (seen, index) = group_by_reference(files)?;
        let missing: Vec<&str> = expected
            .keys()
            .map(String::as_str)
            .filter(|reference| !index.contains_key(*reference))
            .collect();
        let unknown: Vec<&str> = index
            .keys()
            .map(String::as_str)
            .filter(|reference| !expected.contains_key(*reference))
            .collect();
        let doubled: Vec<&Group<'_>> = seen.iter().filter(|(_, items)| items.len() > 1).collect();

        let mut invalid: Vec<String> = Vec::new();
        let mut per_kind: BTreeMap<String, usize> = BTreeMap::new();
        let mut per_chunk: BTreeMap<String, usize> = BTreeMap::new();
        for (reference, items) in &seen {
            for item in items {
                let kind = item.value.get("disposition");
                let target = or_empty_stripped(item.value.get("target"));
                *per_kind.entry(py_str_opt(kind)).or_default() += 1;
                let kind_name = kind
                    .and_then(Value::as_str)
                    .filter(|name| KINDS.contains(name));
                match kind_name {
                    None => invalid.push(format!(
                        "{reference}: unknown kind {} ({})",
                        py_repr_opt(kind),
                        item.file
                    )),
                    Some(name) => {
                        if CHUNK_TARGET.contains(&name) && !chunk_ids.contains(&target) {
                            invalid.push(format!(
                                "{reference}: {name} target {} is not a chunk id",
                                py_string_repr(&target)
                            ));
                        } else if name == "container"
                            && target != "map-root"
                            && !chunk_ids.contains(&target)
                        {
                            invalid.push(format!(
                                "{reference}: container target {} is not a chunk id",
                                py_string_repr(&target)
                            ));
                        } else if name == "decision" && !decision_ids.contains(&target) {
                            invalid.push(format!(
                                "{reference}: decision target {} is not in the map",
                                py_string_repr(&target)
                            ));
                        } else if target.is_empty() {
                            invalid.push(format!("{reference}: {name} has no target"));
                        }
                        if CHUNK_TARGET.contains(&name) {
                            *per_chunk.entry(target.clone()).or_default() += 1;
                        }
                    }
                }
                if or_empty_stripped(item.value.get("rationale")).is_empty() {
                    invalid.push(format!("{reference}: no rationale"));
                }
            }
        }

        let clean =
            missing.is_empty() && unknown.is_empty() && doubled.is_empty() && invalid.is_empty();
        let mut lines = vec![format!(
            "expected={} disposed={} missing={} unknown={} doubled={} invalid={}",
            expected.len(),
            seen.len(),
            missing.len(),
            unknown.len(),
            doubled.len(),
            invalid.len()
        )];
        for reference in missing.iter().take(MISSING_CAP) {
            let title = expected.get(*reference).and_then(|node| node.get("title"));
            lines.push(format!("MISSING {reference} {}", py_str_opt(title)));
        }
        lines.extend(
            unknown
                .iter()
                .take(UNKNOWN_CAP)
                .map(|reference| format!("UNKNOWN {reference}")),
        );
        for (reference, items) in doubled.iter().take(DOUBLE_CAP) {
            let tuples: Vec<String> = items
                .iter()
                .map(|item| {
                    format!(
                        "({}, {}, {})",
                        py_repr_opt(item.value.get("disposition")),
                        py_repr_opt(item.value.get("target")),
                        py_string_repr(item.file)
                    )
                })
                .collect();
            lines.push(format!("DOUBLE {reference} [{}]", tuples.join(", ")));
        }
        lines.extend(
            invalid
                .iter()
                .take(INVALID_CAP)
                .map(|message| format!("INVALID {message}")),
        );
        lines.push(format!(
            "PER_KIND {}",
            json_counts(per_kind.iter().map(|(kind, count)| (kind.as_str(), *count)))
        ));
        lines.push(format!(
            "PER_CHUNK {}",
            json_counts(per_chunk.iter().map(|(id, count)| (id.as_str(), *count)))
        ));
        let without_nodes: Vec<&str> = chunk_ids
            .iter()
            .map(String::as_str)
            .filter(|id| !per_chunk.contains_key(*id))
            .collect();
        lines.push(format!("CHUNKS_WITHOUT_NODES {}", json_list(without_nodes)));
        Ok(Self { lines, clean })
    }

    /// The report lines, in the Python checker's order.
    pub fn lines(&self) -> &[String] {
        &self.lines
    }

    /// Whether every node has exactly one valid disposition.
    pub fn clean(&self) -> bool {
        self.clean
    }
}

/// The items of a map list such as `chunks`; an absent list is empty.
fn list_of<'a>(map: &'a Value, key: &str) -> Result<&'a [Value]> {
    match map.get(key) {
        None => Ok(&[]),
        Some(Value::Array(items)) => Ok(items.as_slice()),
        Some(_) => bail!("the map's {key} is not a list"),
    }
}

/// Every disposition grouped by its stripped `ref`, in first-seen order, with each key's position.
fn group_by_reference(
    files: &[(String, Value)],
) -> Result<(Vec<Group<'_>>, BTreeMap<String, usize>)> {
    let mut seen: Vec<Group<'_>> = Vec::new();
    let mut index: BTreeMap<String, usize> = BTreeMap::new();
    for (file, document) in files {
        let items = document
            .get("dispositions")
            .and_then(Value::as_array)
            .with_context(|| format!("{file} has no dispositions list"))?;
        for item in items {
            let Some(object) = item.as_object() else {
                bail!("{file} holds a disposition that is not an object");
            };
            let reference = object
                .get("ref")
                .map_or_else(String::new, py_str)
                .trim()
                .to_owned();
            let position = match index.get(&reference) {
                Some(position) => *position,
                None => {
                    let position = seen.len();
                    index.insert(reference.clone(), position);
                    seen.push((reference, Vec::new()));
                    position
                }
            };
            if let Some((_, group)) = seen.get_mut(position) {
                group.push(Item {
                    value: item,
                    file: file.as_str(),
                });
            }
        }
    }
    Ok((seen, index))
}
