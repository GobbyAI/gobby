//! Unit tests for the start rule, over an in-memory `TaskSource`. The daemon wire
//! format is covered by `tests/start.rs`; these pin the matching and refusal logic.

use std::collections::HashMap;
use std::path::Path;

use anyhow::Context;
use serde_json::json;

use super::{Brief, Task, TaskSource, Tree, check, locate};
use crate::PORT_EPIC;
use crate::map::Map;

const EPIC_ID: &str = "epic-id";

struct Fixture {
    epic: Task,
    children: Vec<Brief>,
    tasks: HashMap<String, Task>,
}

impl Fixture {
    fn new(children: Vec<(Brief, Task)>) -> Self {
        let epic =
            serde_json::from_value(json!({"id": EPIC_ID, "ref": PORT_EPIC})).expect("epic record");
        let mut rows = Vec::new();
        let mut tasks = HashMap::new();
        for (brief, task) in children {
            rows.push(brief);
            tasks.insert(task.id.clone(), task);
        }
        Self {
            epic,
            children: rows,
            tasks,
        }
    }
}

impl TaskSource for Fixture {
    fn task_by_ref(&self, reference: &str) -> anyhow::Result<Option<Task>> {
        Ok((reference == PORT_EPIC).then(|| self.epic.clone()))
    }

    fn children(&self, parent_id: &str) -> anyhow::Result<Vec<Brief>> {
        assert_eq!(parent_id, EPIC_ID, "children are read from the epic");
        Ok(self.children.clone())
    }

    fn task(&self, id: &str) -> anyhow::Result<Task> {
        self.tasks
            .get(id)
            .cloned()
            .with_context(|| format!("no fixture task {id}"))
    }
}

/// A child row and its full record, with the given title, description and closed state.
fn child(id: &str, title: &str, description: &str, closed: Option<(&str, &str)>) -> (Brief, Task) {
    let (is_closed, reason, commit) = match closed {
        Some((reason, commit)) => (true, Some(reason), Some(commit)),
        None => (false, None, None),
    };
    let task = serde_json::from_value(json!({
        "id": id,
        "ref": format!("#{id}"),
        "description": description,
        "closed_reason": reason,
        "closed_commit_sha": commit,
        "state": {
            "is_closed": is_closed,
            "closed_reason": reason,
            "closed_commit_sha": commit,
        },
    }))
    .expect("task record");
    let brief = Brief {
        id: id.to_owned(),
        title: title.to_owned(),
    };
    (brief, task)
}

fn two_chunk_map() -> Map {
    Map::from_bytes(
        br#"{"chunks": [
            {"id": "FX-TARGET", "title": "Target chunk", "kind": "engine", "depends_on": ["FX-ALPHA"]},
            {"id": "FX-ALPHA", "title": "Alpha chunk", "kind": "engine", "depends_on": []}
        ]}"#,
    )
    .expect("fixture map")
}

#[test]
fn title_match_wins_over_untitled_heading() {
    let fixture = Fixture::new(vec![
        child("t-titled", "Target chunk", "# FX-TARGET: first", None),
        child("t-other", "Something else", "# FX-TARGET: second", None),
    ]);
    let map = two_chunk_map();
    let chunk = map.chunk("FX-TARGET").expect("chunk");
    let mut tree = Tree {
        source: &fixture,
        children: fixture.children.clone(),
        records: HashMap::new(),
    };
    let found = locate(&mut tree, chunk)
        .expect("lookup runs")
        .expect("the titled child is the one match");
    assert_eq!(found.id, "t-titled");
}

#[test]
fn heading_found_among_all_children_when_no_title_matches() {
    let fixture = Fixture::new(vec![child(
        "t-retitled",
        "Renamed work",
        "# FX-TARGET: body",
        None,
    )]);
    let map = two_chunk_map();
    let chunk = map.chunk("FX-TARGET").expect("chunk");
    let mut tree = Tree {
        source: &fixture,
        children: fixture.children.clone(),
        records: HashMap::new(),
    };
    let found = locate(&mut tree, chunk)
        .expect("lookup runs")
        .expect("the untitled child is found by heading");
    assert_eq!(found.id, "t-retitled");
}

#[test]
fn duplicate_headings_refuse_naming_both_tasks() {
    let fixture = Fixture::new(vec![
        child("d-one", "Target chunk", "# FX-TARGET: one", None),
        child("d-two", "Target chunk", "# FX-TARGET: two", None),
    ]);
    let map = two_chunk_map();
    let mut out = Vec::new();
    let passed = check(
        Path::new("/nonexistent"),
        &map,
        "FX-TARGET",
        &fixture,
        &mut out,
    )
    .expect("a refusal is not an error");
    let text = String::from_utf8(out).expect("utf-8 output");
    assert!(!passed, "two tasks for one chunk refuse");
    assert!(text.contains("REFUSE FX-TARGET"), "{text}");
    assert!(text.contains("#d-one") && text.contains("#d-two"), "{text}");
    assert!(!text.contains("launch base"), "{text}");
}

#[test]
fn open_dependency_refuses_without_reading_git() {
    let fixture = Fixture::new(vec![
        child("t-target", "Target chunk", "# FX-TARGET: body", None),
        child("t-alpha", "Alpha chunk", "# FX-ALPHA: body", None),
    ]);
    let map = two_chunk_map();
    let mut out = Vec::new();
    let passed = check(
        Path::new("/nonexistent"),
        &map,
        "FX-TARGET",
        &fixture,
        &mut out,
    )
    .expect("a refusal is not an error");
    let text = String::from_utf8(out).expect("utf-8 output");
    assert!(!passed);
    assert!(text.contains("REFUSE FX-ALPHA"), "{text}");
    assert!(!text.contains("launch base"), "{text}");
}

#[test]
fn dependency_missing_from_map_is_an_input_error() {
    let fixture = Fixture::new(vec![child(
        "t-target",
        "Target chunk",
        "# FX-TARGET: body",
        None,
    )]);
    let map = Map::from_bytes(
        br#"{"chunks": [
            {"id": "FX-TARGET", "title": "Target chunk", "kind": "engine", "depends_on": ["FX-NOPE"]}
        ]}"#,
    )
    .expect("fixture map");
    let mut out = Vec::new();
    let error = check(
        Path::new("/nonexistent"),
        &map,
        "FX-TARGET",
        &fixture,
        &mut out,
    )
    .expect_err("a dependency outside the map is a malformed map, not a refusal");
    assert!(error.to_string().contains("FX-NOPE"), "{error}");
}
