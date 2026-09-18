//! A daemon workspace the mock owns (plan gclient-workspaces 4.2): the
//! `workspace_attach` reply serves its rows and every `workspace_op` moves
//! them and emits the matching `workspace_event`, the way
//! `src/gobby/terminals/workspace_ops.py` does. Ids are `mock-tab-N` and
//! `mock-pane-N`; refs are the lowest free number per scope.

use serde_json::{json, Value};

const FIXTURE: &str =
    include_str!("../../../../tests/fixtures/terminal_ws_golden/workspace_snapshot.json");
const TIMESTAMP: &str = "2026-01-01T00:00:00+00:00";

#[derive(Debug)]
pub struct WorkspaceSim {
    workspace: Value,
    tabs: Vec<Value>,
    panes: Vec<Value>,
    daemon_epoch: String,
    seq: u64,
    next_id: u64,
}

impl WorkspaceSim {
    /// The golden snapshot fixture: one tab of two panes for its project.
    pub fn from_fixture() -> Self {
        let fixture: Value = serde_json::from_str(FIXTURE).expect("workspace snapshot fixture");
        Self {
            workspace: fixture["workspace"].clone(),
            tabs: fixture["tabs"].as_array().cloned().unwrap_or_default(),
            panes: fixture["panes"].as_array().cloned().unwrap_or_default(),
            daemon_epoch: fixture["snapshot"]["daemon_epoch"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            seq: fixture["snapshot"]["seq"].as_u64().unwrap_or(0),
            next_id: 1,
        }
    }

    pub fn workspace_id(&self) -> String {
        self.workspace["id"]
            .as_str()
            .unwrap_or_default()
            .to_string()
    }

    /// Replace every tab with `project`'s: one tab per entry, its terminals
    /// laid out left to right, the named terminal focused. Returns each
    /// tab's id with its pane ids in layout order.
    pub fn seed(&mut self, project: &str, tabs: &[(&[&str], &str)]) -> Vec<(String, Vec<String>)> {
        self.tabs.clear();
        self.panes.clear();
        let mut seeded = Vec::new();
        for (position, (terminals, focused)) in tabs.iter().enumerate() {
            let tab_id = self.mint("mock-tab");
            let mut pane_ids: Vec<String> = Vec::new();
            let mut focused_pane = None;
            for terminal in terminals.iter() {
                let pane_id = self.mint("mock-pane");
                if terminal == focused {
                    focused_pane = Some(pane_id.clone());
                }
                let reference = pane_ids.len() as u64 + 1;
                self.panes
                    .push(pane_row(&pane_id, &tab_id, Some(terminal), reference));
                pane_ids.push(pane_id);
            }
            let layout = pane_ids
                .iter()
                .rev()
                .fold(Value::Null, |rest, pane_id| match rest {
                    Value::Null => leaf(pane_id),
                    rest => json!({
                        "kind": "split",
                        "axis": "horizontal",
                        "ratio": 0.5,
                        "children": [leaf(pane_id), rest],
                    }),
                });
            self.tabs.push(json!({
                "id": tab_id,
                "workspace_id": self.workspace_id(),
                "ref": position as u64 + 1,
                "title": null,
                "project_id": project,
                "worktree_id": null,
                "position": position as u64,
                "focused_pane_id": focused_pane.or_else(|| pane_ids.first().cloned()),
                "layout": layout,
                "created_at": TIMESTAMP,
                "updated_at": TIMESTAMP,
            }));
            seeded.push((tab_id, pane_ids));
        }
        self.workspace["focused_project_id"] = json!(project);
        self.workspace["focused_tab_id"] = json!(seeded.first().map(|(id, _)| id.clone()));
        seeded
    }

    /// The `workspace_snapshot` reply for an attach request.
    pub fn attach_reply(&self, request_id: Option<&Value>) -> Value {
        json!({
            "type": "workspace_snapshot",
            "request_id": request_id.cloned().unwrap_or(Value::Null),
            "workspace": self.workspace,
            "tabs": self.tabs,
            "panes": self.panes,
            "snapshot": {"daemon_epoch": self.daemon_epoch, "seq": self.seq},
        })
    }

    /// Apply one `workspace_op` envelope and return the events it publishes.
    pub fn apply(&mut self, op: &Value) -> Vec<Value> {
        let kind = op.get("op").and_then(Value::as_str).unwrap_or_default();
        let field = |name: &str| op.get(name).and_then(Value::as_str).map(str::to_string);
        match kind {
            "tab.create" => {
                let Some(project) = field("project_id") else {
                    return Vec::new();
                };
                let tab_id = self.mint("mock-tab");
                let pane_id = self.mint("mock-pane");
                let position = self.tabs.len() as u64;
                let reference =
                    lowest_free(self.tabs.iter().map(|tab| tab["ref"].as_u64().unwrap_or(0)));
                let pane = pane_row(&pane_id, &tab_id, field("terminal_id").as_deref(), 1);
                self.panes.push(pane.clone());
                let tab = json!({
                    "id": tab_id,
                    "workspace_id": self.workspace_id(),
                    "ref": reference,
                    "title": field("title"),
                    "project_id": project,
                    "worktree_id": field("worktree_id"),
                    "position": position,
                    "focused_pane_id": pane_id,
                    "layout": leaf(&pane_id),
                    "created_at": TIMESTAMP,
                    "updated_at": TIMESTAMP,
                });
                self.tabs.push(tab.clone());
                vec![self.event("tab.created", Some(&project), vec![tab], vec![pane])]
            }
            "pane.split" => {
                let Some(pane_id) = field("pane") else {
                    return Vec::new();
                };
                let Some(index) = self.tab_index_of(&pane_id) else {
                    return Vec::new();
                };
                let tab_id = self.tabs[index]["id"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string();
                let new_id = self.mint("mock-pane");
                let reference = lowest_free(
                    self.panes
                        .iter()
                        .filter(|pane| pane["tab_id"] == tab_id)
                        .map(|pane| pane["ref"].as_u64().unwrap_or(0)),
                );
                let pane = pane_row(&new_id, &tab_id, field("terminal_id").as_deref(), reference);
                self.panes.push(pane.clone());
                let axis = field("axis").unwrap_or_else(|| "horizontal".into());
                let split = json!({
                    "kind": "split",
                    "axis": axis,
                    "ratio": 0.5,
                    "children": [leaf(&pane_id), leaf(&new_id)],
                });
                replace_leaf(&mut self.tabs[index]["layout"], &pane_id, split);
                self.tabs[index]["focused_pane_id"] = json!(new_id);
                let tab = self.tabs[index].clone();
                let project = tab["project_id"].as_str().map(str::to_string);
                vec![self.event("pane.added", project.as_deref(), vec![tab], vec![pane])]
            }
            "pane.close" => {
                let Some(pane_id) = field("pane") else {
                    return Vec::new();
                };
                let Some(index) = self.tab_index_of(&pane_id) else {
                    return Vec::new();
                };
                let removed = self.take_pane(&pane_id);
                let project = self.tabs[index]["project_id"].as_str().map(str::to_string);
                match remove_leaf(&self.tabs[index]["layout"], &pane_id) {
                    Some(layout) => {
                        self.tabs[index]["layout"] = layout;
                        if self.tabs[index]["focused_pane_id"] == pane_id {
                            let first = leaves(&self.tabs[index]["layout"]).into_iter().next();
                            self.tabs[index]["focused_pane_id"] = json!(first);
                        }
                        let tab = self.tabs[index].clone();
                        vec![self.event("pane.removed", project.as_deref(), vec![tab], removed)]
                    }
                    None => {
                        let tab = self.tabs.remove(index);
                        vec![self.event("tab.closed", project.as_deref(), vec![tab], removed)]
                    }
                }
            }
            "tab.close" => {
                let Some(tab_id) = field("tab") else {
                    return Vec::new();
                };
                let Some(index) = self.tabs.iter().position(|tab| tab["id"] == tab_id) else {
                    return Vec::new();
                };
                let tab = self.tabs.remove(index);
                let (removed, kept): (Vec<Value>, Vec<Value>) = std::mem::take(&mut self.panes)
                    .into_iter()
                    .partition(|pane| pane["tab_id"] == tab_id);
                self.panes = kept;
                let project = tab["project_id"].as_str().map(str::to_string);
                vec![self.event("tab.closed", project.as_deref(), vec![tab], removed)]
            }
            "pane.swap" => {
                let (Some(pane_id), Some(other)) = (field("pane"), field("other")) else {
                    return Vec::new();
                };
                let Some(index) = self.tab_index_of(&pane_id) else {
                    return Vec::new();
                };
                swap_leaves(&mut self.tabs[index]["layout"], &pane_id, &other);
                let tab = self.tabs[index].clone();
                let project = tab["project_id"].as_str().map(str::to_string);
                vec![self.event("pane.swapped", project.as_deref(), vec![tab], Vec::new())]
            }
            "pane.resize" => {
                let Some(pane_id) = field("pane") else {
                    return Vec::new();
                };
                let ratio = op.get("ratio").and_then(Value::as_f64).unwrap_or(0.5);
                let Some(index) = self.tab_index_of(&pane_id) else {
                    return Vec::new();
                };
                set_parent_ratio(&mut self.tabs[index]["layout"], &pane_id, ratio);
                let tab = self.tabs[index].clone();
                let project = tab["project_id"].as_str().map(str::to_string);
                vec![self.event("pane.resized", project.as_deref(), vec![tab], Vec::new())]
            }
            "tab.rename" => {
                let Some(tab_id) = field("tab") else {
                    return Vec::new();
                };
                let Some(index) = self.tabs.iter().position(|tab| tab["id"] == tab_id) else {
                    return Vec::new();
                };
                self.tabs[index]["title"] = op.get("title").cloned().unwrap_or(Value::Null);
                let tab = self.tabs[index].clone();
                let project = tab["project_id"].as_str().map(str::to_string);
                vec![self.event("tab.renamed", project.as_deref(), vec![tab], Vec::new())]
            }
            "tab.move" => {
                let Some(tab_id) = field("tab") else {
                    return Vec::new();
                };
                let position = op.get("position").and_then(Value::as_u64).unwrap_or(0) as usize;
                let Some(index) = self.tabs.iter().position(|tab| tab["id"] == tab_id) else {
                    return Vec::new();
                };
                let tab = self.tabs.remove(index);
                let position = position.min(self.tabs.len());
                self.tabs.insert(position, tab);
                for (position, tab) in self.tabs.iter_mut().enumerate() {
                    tab["position"] = json!(position as u64);
                }
                let tabs = self.tabs.clone();
                let project = self.tabs[position]["project_id"]
                    .as_str()
                    .map(str::to_string);
                vec![self.event("tab.moved", project.as_deref(), tabs, Vec::new())]
            }
            "pane.rename" => {
                let Some(pane_id) = field("pane") else {
                    return Vec::new();
                };
                let label = op.get("label").cloned().unwrap_or(Value::Null);
                let Some(pane) = self.panes.iter_mut().find(|pane| pane["id"] == pane_id) else {
                    return Vec::new();
                };
                pane["label"] = label;
                let pane = pane.clone();
                let project = self
                    .tab_index_of(&pane_id)
                    .and_then(|index| self.tabs[index]["project_id"].as_str())
                    .map(str::to_string);
                vec![self.event("pane.renamed", project.as_deref(), Vec::new(), vec![pane])]
            }
            "workspace.set_focus_hints" => {
                let project = field("project_id");
                self.workspace["focused_project_id"] = json!(project);
                self.workspace["focused_tab_id"] = op.get("tab").cloned().unwrap_or(Value::Null);
                let mut tabs = Vec::new();
                if let (Some(tab_id), Some(pane)) = (field("tab"), op.get("pane")) {
                    if let Some(tab) = self.tabs.iter_mut().find(|tab| tab["id"] == tab_id) {
                        tab["focused_pane_id"] = pane.clone();
                        tabs.push(tab.clone());
                    }
                }
                let workspace = self.workspace.clone();
                let mut event = self.event("focus_hints", project.as_deref(), tabs, Vec::new());
                event["workspace"] = workspace;
                vec![event]
            }
            _ => Vec::new(),
        }
    }

    fn event(
        &mut self,
        kind: &str,
        project: Option<&str>,
        tabs: Vec<Value>,
        panes: Vec<Value>,
    ) -> Value {
        self.seq += 1;
        json!({
            "type": "workspace_event",
            "kind": kind,
            "workspace_id": self.workspace_id(),
            "project_id": project,
            "workspace": null,
            "tabs": tabs,
            "panes": panes,
            "daemon_epoch": self.daemon_epoch,
            "seq": self.seq,
            "timestamp": TIMESTAMP,
        })
    }

    fn mint(&mut self, prefix: &str) -> String {
        let id = format!("{prefix}-{}", self.next_id);
        self.next_id += 1;
        id
    }

    fn tab_index_of(&self, pane_id: &str) -> Option<usize> {
        let tab_id = self.panes.iter().find(|pane| pane["id"] == pane_id)?["tab_id"].clone();
        self.tabs.iter().position(|tab| tab["id"] == tab_id)
    }

    /// Whether `op` names rows this workspace has: a close or focus-hint
    /// op on an unknown id is refused `not_found`, as the daemon does.
    pub fn knows(&self, op: &Value) -> bool {
        let has_tab = |id: &Value| id.is_null() || self.tabs.iter().any(|tab| &tab["id"] == id);
        let has_pane = |id: &Value| id.is_null() || self.panes.iter().any(|pane| &pane["id"] == id);
        match op.get("op").and_then(Value::as_str) {
            Some("pane.close") => op.get("pane").is_none_or(has_pane),
            Some("tab.close") => op.get("tab").is_none_or(has_tab),
            Some("workspace.set_focus_hints") => {
                op.get("tab").is_none_or(has_tab) && op.get("pane").is_none_or(has_pane)
            }
            _ => true,
        }
    }

    /// The daemon's reaping: a killed terminal's panes leave their tabs,
    /// and an emptied tab closes. Returns the events, in order.
    pub fn reap_terminal(&mut self, terminal_id: &str) -> Vec<Value> {
        let panes: Vec<String> = self
            .panes
            .iter()
            .filter(|pane| pane["terminal_id"] == terminal_id)
            .filter_map(|pane| pane["id"].as_str().map(str::to_string))
            .collect();
        panes
            .into_iter()
            .flat_map(|pane| self.apply(&json!({"op": "pane.close", "pane": pane})))
            .collect()
    }

    fn take_pane(&mut self, pane_id: &str) -> Vec<Value> {
        let (removed, kept): (Vec<Value>, Vec<Value>) = std::mem::take(&mut self.panes)
            .into_iter()
            .partition(|pane| pane["id"] == pane_id);
        self.panes = kept;
        removed
    }
}

fn pane_row(id: &str, tab_id: &str, terminal_id: Option<&str>, reference: u64) -> Value {
    json!({
        "id": id,
        "tab_id": tab_id,
        "ref": reference,
        "terminal_id": terminal_id,
        "owns_terminal": true,
        "label": null,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    })
}

fn leaf(pane_id: &str) -> Value {
    json!({"kind": "pane", "pane_id": pane_id})
}

fn lowest_free(taken: impl Iterator<Item = u64>) -> u64 {
    let taken: Vec<u64> = taken.collect();
    (1..)
        .find(|candidate| !taken.contains(candidate))
        .unwrap_or(1)
}

fn leaves(node: &Value) -> Vec<String> {
    match node["kind"].as_str() {
        Some("pane") => node["pane_id"]
            .as_str()
            .map(str::to_string)
            .into_iter()
            .collect(),
        _ => node["children"]
            .as_array()
            .map(|children| children.iter().flat_map(leaves).collect())
            .unwrap_or_default(),
    }
}

fn replace_leaf(node: &mut Value, pane_id: &str, replacement: Value) -> bool {
    if node["kind"] == "pane" {
        if node["pane_id"] == pane_id {
            *node = replacement;
            return true;
        }
        return false;
    }
    if let Some(children) = node["children"].as_array_mut() {
        for child in children {
            if replace_leaf(child, pane_id, replacement.clone()) {
                return true;
            }
        }
    }
    false
}

/// The tree without `pane_id`'s leaf: its sibling takes the split's place.
/// `None` when the root is that leaf.
fn remove_leaf(node: &Value, pane_id: &str) -> Option<Value> {
    if node["kind"] == "pane" {
        return (node["pane_id"] != pane_id).then(|| node.clone());
    }
    let children = node["children"].as_array()?;
    let kept: Vec<Value> = children
        .iter()
        .filter_map(|child| remove_leaf(child, pane_id))
        .collect();
    match kept.len() {
        0 => None,
        1 => kept.into_iter().next(),
        _ => {
            let mut split = node.clone();
            split["children"] = Value::Array(kept);
            Some(split)
        }
    }
}

fn swap_leaves(node: &mut Value, first: &str, second: &str) {
    if node["kind"] == "pane" {
        if node["pane_id"] == first {
            node["pane_id"] = json!(second);
        } else if node["pane_id"] == second {
            node["pane_id"] = json!(first);
        }
        return;
    }
    if let Some(children) = node["children"].as_array_mut() {
        for child in children {
            swap_leaves(child, first, second);
        }
    }
}

/// Set the ratio of the split directly above `pane_id`'s leaf.
fn set_parent_ratio(node: &mut Value, pane_id: &str, ratio: f64) -> bool {
    if node["kind"] == "pane" {
        return false;
    }
    let direct = node["children"]
        .as_array()
        .is_some_and(|children| children.iter().any(|child| child["pane_id"] == pane_id));
    if direct {
        node["ratio"] = json!(ratio);
        return true;
    }
    if let Some(children) = node["children"].as_array_mut() {
        for child in children {
            if set_parent_ratio(child, pane_id, ratio) {
                return true;
            }
        }
    }
    false
}
