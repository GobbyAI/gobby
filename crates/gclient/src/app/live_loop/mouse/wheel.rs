//! Wheel notches: what a scroll over each chrome region means.

use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::sidebar::section_metrics;
use crate::ui::{Chrome, WorkspaceView};

use super::{focus_active_tab, MouseOutcome, MOUSE_SCROLL_LINES};

/// A wheel notch over `hit` at screen `row`; `up` is a notch away from the
/// user.
///
/// Over the tab bar a notch switches tabs, up = previous and down = next,
/// wrapping at either end (herdr's tab-bar wheel), and focuses the new tab's
/// pane the way a click would. Over the sidebar it scrolls the list under
/// the pointer by `MOUSE_SCROLL_LINES` rows (herdr `scroll_workspace_list`):
/// a row or scrollbar names its list, anything else goes by which side of
/// the section rule the pointer is on. A list that fits stays put, and the
/// collapsed rail has nothing to scroll. Panes wait for their section.
pub(super) fn wheel<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    hit: Hit,
    row: u16,
    up: bool,
) -> MouseOutcome {
    match hit {
        Hit::Tab(_) | Hit::TabScrollLeft | Hit::TabScrollRight | Hit::NewTab | Hit::TabBarEmpty => {
            let count = chrome.tabs.len();
            if count == 0 {
                return MouseOutcome::Handled;
            }
            let step = if up { count - 1 } else { 1 };
            chrome.active_tab = (chrome.active_tab + step) % count;
            chrome.tab_scroll_follow_active = true;
            focus_active_tab(chrome, false)
        }
        Hit::Roster(_)
        | Hit::Attention(_)
        | Hit::SidebarScrollbar { .. }
        | Hit::SidebarEmpty
        | Hit::SidebarToggle
        | Hit::SidebarDivider
        | Hit::SidebarSectionDivider => {
            if chrome.sidebar.collapsed {
                return MouseOutcome::Handled;
            }
            let section = match hit {
                Hit::Roster(_) => SidebarSection::Roster,
                Hit::Attention(_) => SidebarSection::Attention,
                Hit::SidebarScrollbar { section, .. } => section,
                _ => match chrome.view.sidebar_section_divider_y {
                    Some(rule) if row >= rule => SidebarSection::Attention,
                    _ => SidebarSection::Roster,
                },
            };
            let metrics = section_metrics(ws, chrome, section);
            let max = metrics.max_offset_from_bottom;
            if max == 0 {
                return MouseOutcome::Handled;
            }
            let current = max - metrics.offset_from_bottom;
            let next = if up {
                current.saturating_sub(MOUSE_SCROLL_LINES)
            } else {
                (current + MOUSE_SCROLL_LINES).min(max)
            };
            *chrome.sidebar.scroll_mut(section) = next;
            MouseOutcome::Handled
        }
        _ => MouseOutcome::Ignore,
    }
}
