// upstream: none (Gobby daemon workspace projection)
//! Projection of daemon workspace tabs into this window's viewer slots.

use super::*;

impl Chrome {
    /// Rebuild `project_id`'s tab set from the daemon workspace through this
    /// window's viewer state. Returns the terminal ids of slots no roster pane
    /// backs yet; they render empty until the roster delivers them.
    pub fn project_workspace<W: WorkspaceView>(&mut self, ws: &W, project_id: &str) -> Vec<String> {
        let Some(model) = ws.workspace_model() else {
            return Vec::new();
        };
        let mut unresolved = Vec::new();
        let mut tabs = Vec::new();
        for row in model.tabs_for_project(project_id) {
            let mut slots = HashMap::new();
            let root = project_node(&row.layout, &mut |pane_id: &str| {
                let slot = self.viewer.panes.intern(pane_id);
                if let Some(terminal_id) = model
                    .pane(pane_id)
                    .and_then(|pane| pane.terminal_id.as_deref())
                {
                    match ws.pane_for_terminal(terminal_id) {
                        Some(pane) => {
                            slots.insert(slot, pane);
                        }
                        None => unresolved.push(terminal_id.to_string()),
                    }
                }
                slot
            });
            let layout = TileLayout::from_saved(root);
            let focus = self
                .viewer
                .focus
                .get(&row.id)
                .copied()
                .filter(|slot| layout.pane_ids().contains(slot))
                .or_else(|| {
                    row.focused_pane_id
                        .as_deref()
                        .and_then(|pane_id| self.viewer.panes.slot(pane_id))
                        .filter(|slot| layout.pane_ids().contains(slot))
                })
                .unwrap_or_else(|| first_slot(layout.root()));
            self.viewer.focus.insert(row.id.clone(), focus);
            // A renamed tab keeps its name; otherwise the tab is named for its
            // own address, which is stable where a borrowed pane name is not.
            let title = row.title.clone().unwrap_or_else(|| {
                model
                    .tab_ref(&row.id)
                    .map_or_else(String::new, |reference| format!("tab-{reference}"))
            });
            let mut tab = Tab::with_layout(title, layout, slots);
            tab.id = row.id.clone();
            tab.worktree_id = row.worktree_id.clone();
            tabs.push(tab);
        }
        // Tabs a scripted path opened never came from the model, so it
        // cannot end them: they stay after the daemon's rows.
        if let Some(set) = self.project_tabs.sets.get_mut(project_id) {
            tabs.extend(set.tabs.drain(..).filter(Tab::is_local));
        }
        let active = self
            .viewer
            .active_tab
            .get(project_id)
            .or(model.workspace.focused_tab_id.as_ref())
            .and_then(|id| tabs.iter().position(|tab| &tab.id == id))
            .unwrap_or(0);
        if let Some(tab) = tabs.get(active) {
            self.viewer
                .active_tab
                .insert(project_id.to_string(), tab.id.clone());
        }
        self.project_tabs
            .sets
            .insert(project_id.to_string(), TabSet { tabs });
        unresolved
    }
}

/// The layout tree of a daemon tab; every leaf is interned through `slot_of`.
fn project_node(node: &LayoutNode, slot_of: &mut impl FnMut(&str) -> layout::PaneId) -> Node {
    match node {
        LayoutNode::Pane { pane_id } => Node::Pane(slot_of(pane_id)),
        LayoutNode::Split {
            axis,
            ratio,
            children,
        } => Node::Split {
            direction: match axis {
                LayoutAxis::Horizontal => Direction::Horizontal,
                LayoutAxis::Vertical => Direction::Vertical,
            },
            ratio: *ratio as f32,
            first: Box::new(project_node(&children[0], slot_of)),
            second: Box::new(project_node(&children[1], slot_of)),
        },
    }
}
