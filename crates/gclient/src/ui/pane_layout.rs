// upstream: herdr v0.8.0 src/ui/panes.rs
//! Pane geometry on top of `gobby_terminal::layout`: gaps, border and
//! scrollbar lanes, scroll metrics. Never copies the BSP.

use crate::ui::chrome::Tab;
use crate::ui::scrollbar::should_show_scrollbar;
use crate::ui::settings::ClientPrefs;
use gobby_terminal::layout::ScrollMetrics;
pub use gobby_terminal::layout::{NavDirection, PaneId, PaneInfo, SplitBorder};
use ratatui::layout::Rect;
use ratatui::widgets::{Block, Borders};

/// Content rect inside a pane's borders.
pub fn content_inner(area: Rect) -> Rect {
    Rect {
        x: area.x.saturating_add(1),
        y: area.y.saturating_add(1),
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(2),
    }
}

/// Scroll metrics from a pane's daemon-reported offsets and its viewport.
pub fn metrics_for(offset: u32, max: u32, viewport_rows: u16) -> ScrollMetrics {
    ScrollMetrics {
        offset_from_bottom: offset as usize,
        max_offset_from_bottom: max as usize,
        viewport_rows: viewport_rows as usize,
    }
}

/// Inner rect of a pane drawn with `borders` (herdr `pane_inner_rect`).
pub fn pane_inner_rect(area: Rect, borders: Borders) -> Rect {
    if borders.is_empty() {
        area
    } else {
        Block::default().borders(borders).inner(area)
    }
}

/// Content rect after reserving the stable one-column scrollbar gutter, so
/// the terminal grid does not reflow when scrollback appears.
pub fn stable_terminal_inner_rect(pane_inner: Rect, pane_scrollbars: bool) -> Rect {
    if !pane_scrollbars || pane_inner.width <= 4 {
        return pane_inner;
    }

    Rect::new(
        pane_inner.x,
        pane_inner.y,
        pane_inner.width.saturating_sub(1),
        pane_inner.height,
    )
}

/// The reserved gutter as a visible scrollbar lane, only while scrollback
/// exists (herdr `stable_scrollbar_gutter`).
pub fn scrollbar_gutter(
    pane_inner: Rect,
    pane_scrollbars: bool,
    metrics: ScrollMetrics,
) -> Option<Rect> {
    let inner_rect = stable_terminal_inner_rect(pane_inner, pane_scrollbars);
    if inner_rect == pane_inner {
        return None;
    }
    let gutter = Rect::new(
        pane_inner.x + pane_inner.width.saturating_sub(1),
        pane_inner.y,
        1,
        pane_inner.height,
    );
    should_show_scrollbar(metrics).then_some(gutter)
}

fn ranges_overlap(a_start: u16, a_len: u16, b_start: u16, b_len: u16) -> bool {
    a_start < b_start.saturating_add(b_len) && b_start < a_start.saturating_add(a_len)
}

fn pane_to_right<'a>(info: &PaneInfo, panes: &'a [PaneInfo]) -> Option<&'a PaneInfo> {
    let right = info.rect.x.saturating_add(info.rect.width);
    panes.iter().find(|other| {
        other.id != info.id
            && other.rect.x == right
            && ranges_overlap(
                info.rect.y,
                info.rect.height,
                other.rect.y,
                other.rect.height,
            )
    })
}

fn pane_below<'a>(info: &PaneInfo, panes: &'a [PaneInfo]) -> Option<&'a PaneInfo> {
    let bottom = info.rect.y.saturating_add(info.rect.height);
    panes.iter().find(|other| {
        other.id != info.id
            && other.rect.y == bottom
            && ranges_overlap(info.rect.x, info.rect.width, other.rect.x, other.rect.width)
    })
}

/// Apply the border rule to raw BSP rects (herdr `apply_pane_chrome`): every
/// pane, a lone one included, draws all four edges; with gaps off a right or
/// below neighbour takes the shared edge, so each divider is drawn once.
pub fn apply_pane_chrome(panes: Vec<PaneInfo>, pane_gaps: bool) -> Vec<PaneInfo> {
    panes
        .iter()
        .cloned()
        .map(|mut info| {
            let mut borders = Borders::ALL;
            if !pane_gaps {
                if pane_to_right(&info, &panes).is_some() {
                    borders.remove(Borders::RIGHT);
                }
                if pane_below(&info, &panes).is_some() {
                    borders.remove(Borders::BOTTOM);
                }
            }
            info.borders = borders;
            info
        })
        .collect()
}

/// Pane rects and split borders for `tab` inside `area`, honouring
/// `prefs.pane_gaps` / `pane_scrollbars` and the tab's zoom (herdr
/// `panes::compute_pane_infos`). `scrollbar_rect` stays `None`: the renderer
/// resolves it per pane from live scroll metrics.
pub fn pane_geometry(
    tab: &Tab,
    focus: PaneId,
    zoomed: bool,
    area: Rect,
    prefs: &ClientPrefs,
) -> (Vec<PaneInfo>, Vec<SplitBorder>) {
    if zoomed {
        let borders = Borders::ALL;
        let pane_inner = pane_inner_rect(area, borders);
        let info = PaneInfo {
            id: focus,
            rect: area,
            inner_rect: stable_terminal_inner_rect(pane_inner, prefs.pane_scrollbars),
            scrollbar_rect: None,
            borders,
            is_focused: true,
        };
        return (vec![info], Vec::new());
    }

    let mut pane_infos = apply_pane_chrome(tab.layout.panes(area, focus), prefs.pane_gaps);
    for info in &mut pane_infos {
        let pane_inner = pane_inner_rect(info.rect, info.borders);
        info.inner_rect = stable_terminal_inner_rect(pane_inner, prefs.pane_scrollbars);
        info.scrollbar_rect = None;
    }
    (pane_infos, tab.layout.splits(area))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::PaneId as AppPaneId;
    use ratatui::layout::Direction;

    /// A tab split in two; the focus is the second (new) slot.
    fn two_pane_tab() -> (Tab, PaneId) {
        let mut tab = Tab::new("t", AppPaneId(1));
        let slot = tab
            .layout
            .split_focused(tab.first_slot(), Direction::Horizontal);
        tab.slots.insert(slot, AppPaneId(2));
        (tab, slot)
    }

    #[test]
    fn gapped_split_keeps_independent_bordered_panes() {
        let (tab, focus) = two_pane_tab();
        let (infos, splits) = pane_geometry(
            &tab,
            focus,
            false,
            Rect::new(0, 0, 80, 24),
            &ClientPrefs::default(),
        );
        assert_eq!(infos.len(), 2);
        assert_eq!(splits.len(), 1);
        assert!(infos.iter().all(|info| info.borders == Borders::ALL));
        assert_eq!(infos[0].inner_rect, Rect::new(1, 1, 37, 22));
        assert!(infos[1].is_focused && !infos[0].is_focused);
    }

    #[test]
    fn shared_divider_without_gaps_drops_the_inner_border() {
        let (tab, focus) = two_pane_tab();
        let prefs = ClientPrefs {
            pane_gaps: false,
            ..ClientPrefs::default()
        };
        let (infos, _) = pane_geometry(&tab, focus, false, Rect::new(0, 0, 80, 24), &prefs);
        assert!(!infos[0].borders.contains(Borders::RIGHT));
        assert_eq!(infos[1].borders, Borders::ALL);
    }

    #[test]
    fn zoomed_tab_shows_only_the_focused_pane() {
        let (tab, focus) = two_pane_tab();
        let area = Rect::new(0, 0, 80, 24);
        let (infos, splits) = pane_geometry(&tab, focus, true, area, &ClientPrefs::default());
        assert_eq!(infos.len(), 1);
        assert!(splits.is_empty());
        assert_eq!(infos[0].rect, area);
        assert_eq!(infos[0].id, focus);
        let metrics = metrics_for(0, 5, infos[0].inner_rect.height);
        let gutter = scrollbar_gutter(content_inner(area), true, metrics).unwrap();
        assert_eq!(gutter, Rect::new(78, 1, 1, 22));
        assert!(scrollbar_gutter(content_inner(area), true, metrics_for(0, 0, 22)).is_none());
    }
}
