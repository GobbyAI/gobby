//! Per-project tab sets. The chrome shows the focused project's set and
//! parks the others; a set round-trips through its project's snapshot with
//! the slots named by terminal id.

use std::collections::{BTreeMap, HashMap};

use gobby_terminal::layout::{self, Node, TileLayout};
use ratatui::layout::Direction;

use super::viewer_state::ViewerState;
use super::PaneId;
use crate::persist::{LayoutNode, SplitAxis, TabSnapshot, WorkspaceSnapshot};
use crate::ui::chrome::Tab;

/// The tab bar of one project.
#[derive(Default)]
pub struct TabSet {
    pub tabs: Vec<Tab>,
}

impl TabSet {
    /// The snapshot of this set; `terminal_of` names a pane's terminal, and
    /// a slot it cannot name saves as an empty leaf.
    pub fn snapshot(
        &self,
        project_id: &str,
        terminal_of: impl Fn(PaneId) -> Option<String>,
        viewer: &ViewerState,
    ) -> WorkspaceSnapshot {
        let tabs = self
            .tabs
            .iter()
            .map(|tab| TabSnapshot {
                title: tab.title.clone(),
                layout: save_node(tab.layout.root(), &tab.slots, &terminal_of),
                focused: viewer.focused_pane(tab).and_then(&terminal_of),
                worktree_id: tab.worktree_id.clone(),
            })
            .collect();
        let active_tab = viewer.active_index(project_id, &self.tabs);
        WorkspaceSnapshot {
            project_id: project_id.to_string(),
            focused_terminal_id: self
                .tabs
                .get(active_tab)
                .and_then(|tab| viewer.focused_pane(tab))
                .and_then(&terminal_of),
            tabs,
            active_tab,
        }
    }

    /// Rebuild a set from its snapshot; `resolve` names the pane of a
    /// terminal the workspace still has. A split with one vanished child
    /// collapses to the other, a tab with none is dropped, and the active
    /// tab stays the same tab when it survives.
    pub fn from_snapshot(
        snapshot: &WorkspaceSnapshot,
        resolve: impl Fn(&str) -> Option<PaneId>,
        viewer: &mut ViewerState,
        project: &str,
    ) -> Self {
        let mut set = Self::default();
        let mut active = 0;
        for (index, saved) in snapshot.tabs.iter().enumerate() {
            if index == snapshot.active_tab {
                active = set.tabs.len();
            }
            let mut slots = HashMap::new();
            let Some(root) = load_node(&saved.layout, &resolve, &mut slots) else {
                continue;
            };
            let focus = saved
                .focused
                .as_deref()
                .and_then(&resolve)
                .and_then(|pane| slots.iter().find(|(_, app)| **app == pane))
                .map_or_else(|| first_slot(&root), |(slot, _)| *slot);
            let mut tab = Tab::with_layout(&saved.title, TileLayout::from_saved(root), slots);
            tab.worktree_id = saved.worktree_id.clone();
            viewer.focus.insert(tab.id.clone(), focus);
            set.tabs.push(tab);
        }
        if let Some(tab) = set.tabs.get(active.min(set.tabs.len().saturating_sub(1))) {
            viewer
                .active_tab
                .insert(project.to_string(), tab.id.clone());
        }
        set
    }
}

fn save_node(
    node: &Node,
    slots: &HashMap<layout::PaneId, PaneId>,
    terminal_of: &impl Fn(PaneId) -> Option<String>,
) -> LayoutNode {
    match node {
        Node::Pane(slot) => slots
            .get(slot)
            .copied()
            .and_then(terminal_of)
            .map_or(LayoutNode::Empty, |terminal_id| LayoutNode::Pane {
                terminal_id,
            }),
        Node::Split {
            direction,
            ratio,
            first,
            second,
        } => LayoutNode::Split {
            axis: match direction {
                Direction::Horizontal => SplitAxis::Horizontal,
                Direction::Vertical => SplitAxis::Vertical,
            },
            ratio: *ratio,
            children: vec![
                save_node(first, slots, terminal_of),
                save_node(second, slots, terminal_of),
            ],
        },
    }
}

/// The tree for `node` over fresh slots, or none when no terminal in it
/// resolves.
fn load_node(
    node: &LayoutNode,
    resolve: &impl Fn(&str) -> Option<PaneId>,
    slots: &mut HashMap<layout::PaneId, PaneId>,
) -> Option<Node> {
    match node {
        LayoutNode::Empty => None,
        LayoutNode::Pane { terminal_id } => {
            let pane = resolve(terminal_id)?;
            let slot = layout::PaneId::alloc();
            slots.insert(slot, pane);
            Some(Node::Pane(slot))
        }
        LayoutNode::Split {
            axis,
            ratio,
            children,
        } => {
            let direction = match axis {
                SplitAxis::Horizontal => Direction::Horizontal,
                SplitAxis::Vertical => Direction::Vertical,
            };
            let mut kept = children
                .iter()
                .filter_map(|child| load_node(child, resolve, slots));
            let first = kept.next()?;
            Some(kept.fold(first, |first, second| Node::Split {
                direction,
                ratio: *ratio,
                first: Box::new(first),
                second: Box::new(second),
            }))
        }
    }
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
