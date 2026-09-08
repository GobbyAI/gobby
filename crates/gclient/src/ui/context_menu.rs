// upstream: none (adapted from herdr v0.9.0 src/client/shell/overlays.rs `render_context_menu`)
//! The right-click menu popup.
//!
//! [`render_context_menu`] draws `Chrome::menu` in the `Mode::ContextMenu`
//! arm of `render_workspace_with`, composited last and without dimming the
//! workspace: the menu is contextual, so what it acts on stays readable. The
//! popup takes [`menu_rect`]'s geometry at the anchor and flips left or up
//! when that would overflow the frame; the row rects it draws go back into
//! `ContextMenuState::item_rects` through `Chrome::apply_hits` for `menu_hit`.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{item_rects, menu_rect, ContextMenuState, MenuItem};
use crate::theme::Palette;
use crate::ui::widgets::render_panel_shell;
use crate::ui::Chrome;

/// Draw the open menu over the frame and hand back the row rect of each item
/// as drawn, in item order; empty when the frame cannot hold the panel.
pub fn render_context_menu(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Vec<Rect> {
    let Some(menu) = chrome.menu.as_ref() else {
        return Vec::new();
    };
    let palette = &chrome.palette;
    let popup = popup_rect(area, menu);
    if render_panel_shell(frame, popup, palette.surface0, palette.panel_bg).is_none() {
        return Vec::new();
    }
    let rows = item_rects(popup, menu.items.len());
    for (index, (item, row)) in menu.items.iter().zip(&rows).enumerate() {
        let style = row_style(palette, item, index == menu.selected);
        frame.render_widget(
            Paragraph::new(format!(" {}", item.label)).style(style),
            *row,
        );
    }
    rows
}

/// Where the popup for `menu` lands inside `area`: [`menu_rect`] at the
/// anchor, or with its right or bottom edge on the anchor instead when the
/// popup would overflow the frame on that side, then held inside `area`.
pub fn popup_rect(area: Rect, menu: &ContextMenuState) -> Rect {
    let wanted = menu_rect(menu.anchor, &menu.items);
    let width = wanted.width.min(area.width);
    let height = wanted.height.min(area.height);
    let (column, row) = menu.anchor;
    Rect::new(
        place(column, width, area.x, area.right()),
        place(row, height, area.y, area.bottom()),
        width,
        height,
    )
}

/// `start` unless `len` cells from it pass `end`, then the span that ends
/// on `start`; either way held within `[low, end)`.
fn place(start: u16, len: u16, low: u16, end: u16) -> u16 {
    let placed = if start.saturating_add(len) > end {
        start.saturating_add(1).saturating_sub(len)
    } else {
        start
    };
    placed.clamp(low, end.saturating_sub(len).max(low))
}

/// Rows carry their state by weight and reversal, never hue alone: the
/// selected row is bold and reversed in `accent`, a disabled row dim in
/// `overlay0`, and a disabled selected row reversed but still dim, so the
/// pointer's place shows without promising an action.
fn row_style(palette: &Palette, item: &MenuItem, selected: bool) -> Style {
    let mut style = if item.enabled {
        Style::new().fg(palette.text)
    } else {
        Style::new()
            .fg(palette.overlay0)
            .add_modifier(Modifier::DIM)
    };
    if selected {
        style = style.add_modifier(Modifier::REVERSED);
        if item.enabled {
            style = style.fg(palette.accent).add_modifier(Modifier::BOLD);
        }
    }
    style
}
