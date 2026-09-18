//! Per-window presentation state over the daemon-owned layout (plan
//! gclient-workspaces 4.2). Two windows on one workspace share its tabs and
//! panes and keep their own focus, zoom, and active tab, so those live here,
//! keyed by daemon ids, and never on the shared `Tab`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};

use gobby_terminal::layout;

use super::project_tabs::first_slot;
use super::PaneId;
use crate::ui::chrome::Tab;

/// A daemon tab id, or a `local_tab_id` for a tab a scripted path opened.
pub type TabId = String;

/// Prefix of every `local_tab_id`.
pub const LOCAL_TAB_PREFIX: &str = "local-";

/// The id of a tab opened without the daemon (scripted and parity paths);
/// the daemon issues uuids, so the two never meet.
pub fn local_tab_id() -> TabId {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    format!("{LOCAL_TAB_PREFIX}{}", NEXT.fetch_add(1, Ordering::Relaxed))
}

/// Layout slots for daemon pane ids, minted with `layout::PaneId::alloc` on
/// first sight so an interned slot never collides with one the layout
/// allocated in this process; two windows are separate processes and never
/// need to agree on the number.
#[derive(Debug, Default)]
pub struct PaneInterner {
    by_daemon: HashMap<String, layout::PaneId>,
    by_slot: HashMap<layout::PaneId, String>,
}

impl PaneInterner {
    /// The slot for `daemon_pane_id`, minted on first sight.
    pub fn intern(&mut self, daemon_pane_id: &str) -> layout::PaneId {
        if let Some(slot) = self.by_daemon.get(daemon_pane_id) {
            return *slot;
        }
        let slot = layout::PaneId::alloc();
        self.by_daemon.insert(daemon_pane_id.to_string(), slot);
        self.by_slot.insert(slot, daemon_pane_id.to_string());
        slot
    }

    /// The slot already minted for `daemon_pane_id`.
    pub fn slot(&self, daemon_pane_id: &str) -> Option<layout::PaneId> {
        self.by_daemon.get(daemon_pane_id).copied()
    }

    /// The daemon pane id behind `slot`.
    pub fn daemon_id(&self, slot: layout::PaneId) -> Option<&str> {
        self.by_slot.get(&slot).map(String::as_str)
    }
}

/// What one window remembers about the layout it views.
#[derive(Debug, Default)]
pub struct ViewerState {
    /// The focused slot of each tab this window has visited.
    pub focus: BTreeMap<TabId, layout::PaneId>,
    /// Tabs this window shows zoomed to their focused slot.
    pub zoomed: BTreeSet<TabId>,
    /// The tab this window shows for each project.
    pub active_tab: BTreeMap<String, TabId>,
    pub panes: PaneInterner,
    /// The `(project, model generation)` last projected onto the chrome.
    pub applied: Option<(String, u64)>,
}

impl ViewerState {
    /// The slot this window focuses in `tab`: the remembered one, else the
    /// first in layout order.
    pub fn focus_of(&self, tab: &Tab) -> layout::PaneId {
        self.focus
            .get(&tab.id)
            .copied()
            .unwrap_or_else(|| first_slot(tab.layout.root()))
    }

    /// The pane in `tab`'s focused slot, when a terminal fills it.
    pub fn focused_pane(&self, tab: &Tab) -> Option<PaneId> {
        tab.slots.get(&self.focus_of(tab)).copied()
    }

    pub fn is_zoomed(&self, tab: &Tab) -> bool {
        self.zoomed.contains(&tab.id)
    }

    /// The index in `tabs` of the tab this window shows for `project`; the
    /// first tab when it remembers none.
    pub fn active_index(&self, project: &str, tabs: &[Tab]) -> usize {
        self.active_tab
            .get(project)
            .and_then(|id| tabs.iter().position(|tab| &tab.id == id))
            .unwrap_or(0)
    }
}
