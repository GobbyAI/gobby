//! Roster row layout extracted from herdr `ui/sidebar.rs`.

use crate::app::Workspace;
use crate::ui::text::truncate_end;

#[derive(Debug, Clone)]
pub struct SidebarRow {
    pub label: String,
    pub kind: String,
}

pub fn roster_rows(ws: &Workspace) -> Vec<SidebarRow> {
    let mut rows = Vec::new();
    for id in ws.roster_terminal_ids() {
        rows.push(SidebarRow {
            label: truncate_end(&id, 24),
            kind: "live".into(),
        });
    }
    for id in ws.attention_entry_ids() {
        rows.push(SidebarRow {
            label: truncate_end(&id, 24),
            kind: "blocked".into(),
        });
    }
    rows
}
