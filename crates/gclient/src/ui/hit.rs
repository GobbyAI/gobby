//! Pure hit test over the last drawn frame (herdr `handle_mouse` order).
//!
//! `ViewState` keeps the rects each renderer drew; `hit_test` maps a cell to
//! the chrome element under it without touching the workspace, so the input
//! loop classifies a click against the frame the user actually saw.

use crate::ui::chrome::ViewState;
use crate::ui::pane_layout::{self, PaneId};
use ratatui::layout::{Direction, Position, Rect};

/// Which sidebar list a scrollbar lane belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidebarSection {
    Roster,
    Attention,
}

/// The chrome element under a cell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Hit {
    Tab(usize),
    TabScrollLeft,
    TabScrollRight,
    NewTab,
    /// Tab bar row outside every tab and button.
    TabBarEmpty,
    /// Roster row, by terminal id.
    Roster(String),
    /// Attention row, by entry id.
    Attention(String),
    SidebarToggle,
    /// The `│` column between sidebar and content.
    SidebarDivider,
    /// The `─` row between the roster and attention sections.
    SidebarSectionDivider,
    SidebarEmpty,
    /// Scrollbar lane beside a sidebar list; `row` is the screen row.
    SidebarScrollbar {
        section: SidebarSection,
        row: u16,
    },
    /// Pane content; `col` and `row` are offsets inside the pane's inner rect.
    Pane {
        slot: PaneId,
        col: u16,
        row: u16,
    },
    /// A pane's own border or its hidden scrollbar gutter.
    PaneBorder(PaneId),
    /// Pane scrollbar lane; `row` is the screen row.
    PaneScrollbar {
        slot: PaneId,
        row: u16,
    },
    /// Split divider, as an index into `ViewState::split_borders`.
    SplitBorder(usize),
    /// Settings row, as an index into `SettingsRow::ALL`.
    SettingsRow(usize),
    /// Settings popup outside its rows.
    SettingsDialog,
    ControlIndicator,
    Status,
    Toast,
    Empty,
}

/// Classify the cell at (`column`, `row`) against the last drawn frame.
pub fn hit_test(view: &ViewState, column: u16, row: u16) -> Hit {
    let at = Position::new(column, row);
    if let Some(dialog) = view.settings_dialog_area {
        if let Some((index, _)) = find_at(&view.settings_row_hit_areas, at) {
            return Hit::SettingsRow(*index);
        }
        if dialog.contains(at) {
            return Hit::SettingsDialog;
        }
    }
    if view.toast_hit_area.is_some_and(|toast| toast.contains(at)) {
        return Hit::Toast;
    }
    if view.tab_bar_rect.is_some_and(|bar| bar.contains(at)) {
        return tab_bar_hit(view, at);
    }
    if view.sidebar_rect.contains(at) {
        return sidebar_hit(view, at);
    }
    if let Some(index) = split_border_at(view, at) {
        return Hit::SplitBorder(index);
    }
    if let Some(info) = view
        .pane_infos
        .iter()
        .find(|info| info.inner_rect.contains(at))
    {
        return Hit::Pane {
            slot: info.id,
            col: column - info.inner_rect.x,
            row: row - info.inner_rect.y,
        };
    }
    if let Some(info) = view
        .pane_infos
        .iter()
        .find(|info| info.scrollbar_rect.is_some_and(|lane| lane.contains(at)))
    {
        return Hit::PaneScrollbar { slot: info.id, row };
    }
    if let Some(info) = view.pane_infos.iter().find(|info| info.rect.contains(at)) {
        return Hit::PaneBorder(info.id);
    }
    if view.status_rect.contains(at) {
        let on_indicator = view
            .control_indicator_hit_area
            .is_some_and(|indicator| indicator.contains(at));
        return if on_indicator {
            Hit::ControlIndicator
        } else {
            Hit::Status
        };
    }
    Hit::Empty
}

fn find_at<K>(hits: &[(K, Rect)], at: Position) -> Option<&(K, Rect)> {
    hits.iter().find(|(_, rect)| rect.contains(at))
}

fn tab_bar_hit(view: &ViewState, at: Position) -> Hit {
    if let Some((index, _)) = find_at(&view.tab_hit_areas, at) {
        return Hit::Tab(*index);
    }
    if view
        .tab_scroll_left_hit_area
        .is_some_and(|rect| rect.contains(at))
    {
        return Hit::TabScrollLeft;
    }
    if view
        .tab_scroll_right_hit_area
        .is_some_and(|rect| rect.contains(at))
    {
        return Hit::TabScrollRight;
    }
    if view.new_tab_hit_area.is_some_and(|rect| rect.contains(at)) {
        return Hit::NewTab;
    }
    Hit::TabBarEmpty
}

fn sidebar_hit(view: &ViewState, at: Position) -> Hit {
    if view.sidebar_divider_x == Some(at.x) {
        return Hit::SidebarDivider;
    }
    if view.sidebar_section_divider_y == Some(at.y) {
        return Hit::SidebarSectionDivider;
    }
    if view
        .sidebar_toggle_hit_area
        .is_some_and(|rect| rect.contains(at))
    {
        return Hit::SidebarToggle;
    }
    let lanes = [
        (SidebarSection::Roster, view.roster_scrollbar_hit_area),
        (SidebarSection::Attention, view.attention_scrollbar_hit_area),
    ];
    if let Some((section, _)) = lanes
        .iter()
        .find(|(_, lane)| lane.is_some_and(|lane| lane.contains(at)))
    {
        return Hit::SidebarScrollbar {
            section: *section,
            row: at.y,
        };
    }
    if let Some((id, _)) = find_at(&view.roster_hit_areas, at) {
        return Hit::Roster(id.clone());
    }
    if let Some((id, _)) = find_at(&view.attention_hit_areas, at) {
        return Hit::Attention(id.clone());
    }
    Hit::SidebarEmpty
}

/// Split divider under `at`: the divider column (or row) and, in gapped
/// layouts, the gap cell before it, but never a cell a pane draws content or
/// its scrollbar gutter into (herdr `find_border_at`, derived from the pane
/// rects instead of the prefs).
fn split_border_at(view: &ViewState, at: Position) -> Option<usize> {
    let inside_pane = view
        .pane_infos
        .iter()
        .any(|info| pane_layout::pane_inner_rect(info.rect, info.borders).contains(at));
    if inside_pane {
        return None;
    }
    view.split_borders.iter().position(|border| {
        let along = match border.direction {
            Direction::Horizontal => at.x,
            Direction::Vertical => at.y,
        };
        border.area.contains(at) && (along == border.pos || along.saturating_add(1) == border.pos)
    })
}

#[cfg(test)]
#[path = "hit/tests.rs"]
mod tests;
