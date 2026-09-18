//! Per-project tab sets. The chrome shows the focused project's set and
//! parks the others; the daemon's workspace rows are the source of each.

use std::collections::BTreeMap;

use gobby_terminal::layout::{self, Node};

use crate::ui::chrome::Tab;

/// The tab bar of one project.
#[derive(Default)]
pub struct TabSet {
    pub tabs: Vec<Tab>,
}

pub(crate) fn first_slot(node: &Node) -> layout::PaneId {
    match node {
        Node::Pane(slot) => *slot,
        Node::Split { first, .. } => first_slot(first),
    }
}

/// Key of the set that exists before any project is focused: tabs opened
/// then belong to whichever project is focused first.
const ANONYMOUS: &str = "";

/// Every project's tab set by project id; `focused` names the one the chrome
/// shows.
pub struct ProjectTabs {
    pub sets: BTreeMap<String, TabSet>,
    pub focused: Option<String>,
}

impl Default for ProjectTabs {
    fn default() -> Self {
        Self {
            sets: BTreeMap::from([(ANONYMOUS.to_string(), TabSet::default())]),
            focused: None,
        }
    }
}

impl ProjectTabs {
    pub(crate) fn key(&self) -> &str {
        self.focused.as_deref().unwrap_or(ANONYMOUS)
    }

    /// Show `project_id`'s set, empty when it is new; the first focus of all
    /// adopts the anonymous set.
    pub fn focus(&mut self, project_id: &str) {
        if self.focused.as_deref() == Some(project_id) {
            return;
        }
        if self.focused.is_none() {
            let adopted = self.sets.remove(ANONYMOUS).unwrap_or_default();
            self.sets.insert(project_id.to_string(), adopted);
        }
        self.focused = Some(project_id.to_string());
        self.sets.entry(project_id.to_string()).or_default();
    }

    /// The focused project's set.
    pub fn set(&self) -> &TabSet {
        self.sets
            .get(self.key())
            .expect("the focused project has a tab set")
    }

    pub fn set_mut(&mut self) -> &mut TabSet {
        let key = self.key().to_string();
        self.sets
            .get_mut(&key)
            .expect("the focused project has a tab set")
    }
}
