// upstream: herdr v0.8.0 src/ui/panes.rs
//! Pane geometry on top of `gobby_terminal::layout`: gaps, border and
//! scrollbar lanes, scroll metrics. Never copies the BSP.

use crate::ui::chrome::Tab;
use crate::ui::scrollbar::should_show_scrollbar;
use crate::ui::settings::ClientPrefs;
pub use gobby_terminal::layout::{NavDirection, PaneId, PaneInfo, SplitBorder};
use gobby_terminal::layout::{ScrollMetrics, TileLayout};
use ratatui::layout::{Direction, Rect};
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

/// Terminal rect of a tab's only pane laid out in `area`. A lone pane keeps
/// every border (`apply_pane_chrome`), so this is the size its shell starts at.
pub fn lone_pane_inner_rect(area: Rect, pane_scrollbars: bool) -> Rect {
    stable_terminal_inner_rect(pane_inner_rect(area, Borders::ALL), pane_scrollbars)
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
    if tab.slots.is_empty() {
        return (Vec::new(), Vec::new());
    }
    if zoomed {
        let borders = Borders::ALL;
        let info = PaneInfo {
            id: focus,
            rect: area,
            inner_rect: lone_pane_inner_rect(area, prefs.pane_scrollbars),
            scrollbar_rect: None,
            borders,
            is_focused: true,
        };
        return (vec![info], Vec::new());
    }

    let pane_infos = terminal_panes(tab.layout.panes(area, focus), prefs);
    (pane_infos, tab.layout.splits(area))
}

/// Panes with their neighbour-aware borders and terminal rects set.
fn terminal_panes(panes: Vec<PaneInfo>, prefs: &ClientPrefs) -> Vec<PaneInfo> {
    let mut infos = apply_pane_chrome(panes, prefs.pane_gaps);
    for info in &mut infos {
        let pane_inner = pane_inner_rect(info.rect, info.borders);
        info.inner_rect = stable_terminal_inner_rect(pane_inner, prefs.pane_scrollbars);
        info.scrollbar_rect = None;
    }
    infos
}

/// The two rects an even split of `area` makes, as `split_rect` cuts them: the
/// first takes the half rounded away from zero.
fn even_halves(area: Rect, direction: Direction) -> (Rect, Rect) {
    match direction {
        Direction::Horizontal => {
            let first = (f32::from(area.width) * 0.5).round() as u16;
            let second = area.width.saturating_sub(first);
            let x = area.x + first;
            (
                Rect {
                    width: first,
                    ..area
                },
                Rect {
                    x,
                    width: second,
                    ..area
                },
            )
        }
        Direction::Vertical => {
            let first = (f32::from(area.height) * 0.5).round() as u16;
            let second = area.height.saturating_sub(first);
            let y = area.y + first;
            (
                Rect {
                    height: first,
                    ..area
                },
                Rect {
                    y,
                    height: second,
                    ..area
                },
            )
        }
    }
}

/// Terminal rect the pane that splitting `focus` along `direction` adds gets in
/// `area`: the size its shell starts at. A split only divides `focus`'s leaf,
/// so the new pane takes the second half of it and no other pane moves.
pub fn split_terminal_rect(
    layout: &TileLayout,
    focus: PaneId,
    direction: Direction,
    area: Rect,
    prefs: &ClientPrefs,
) -> Option<Rect> {
    let mut panes = layout.panes(area, focus);
    let kept = panes.iter().position(|info| info.id == focus)?;
    let (first, second) = even_halves(panes[kept].rect, direction);
    panes[kept].rect = first;
    let mut added = panes[kept].clone();
    added.rect = second;
    panes.push(added);
    terminal_panes(panes, prefs)
        .last()
        .map(|info| info.inner_rect)
}

/// Terminal rects of a `rows` x `cols` grid laid out in `area`, indexed
/// `[column][row]`. The tree is built the way the grid job splits it: each
/// column splits off the one before it, then each column splits down. The
/// split that adds column or row `i` takes the ratio `1 / (count - i)`, which
/// is the ratio the job's resizes set. Each cell is the rect its pane ends at.
pub fn grid_terminal_rects(
    rows: usize,
    cols: usize,
    area: Rect,
    prefs: &ClientPrefs,
) -> Vec<Vec<Rect>> {
    let (mut layout, head) = TileLayout::new();
    let mut columns = vec![head];
    for index in 1..cols {
        let ratio = 1.0 / (cols - index + 1) as f32;
        let column =
            layout.split_focused_with_ratio(columns[index - 1], Direction::Horizontal, ratio);
        columns.push(column);
    }
    let mut stacks = Vec::with_capacity(cols);
    for column in columns {
        let mut stack = vec![column];
        for index in 1..rows {
            let ratio = 1.0 / (rows - index + 1) as f32;
            let below =
                layout.split_focused_with_ratio(stack[index - 1], Direction::Vertical, ratio);
            stack.push(below);
        }
        stacks.push(stack);
    }
    let infos = terminal_panes(layout.panes(area, head), prefs);
    stacks
        .into_iter()
        .map(|stack| {
            stack
                .into_iter()
                .map(|id| {
                    infos
                        .iter()
                        .find(|info| info.id == id)
                        .map_or(Rect::default(), |info| info.inner_rect)
                })
                .collect()
        })
        .collect()
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

    /// The viewport a split predicts is the rect a real split of the tree gives
    /// the new pane, for either focus, direction and gap setting. The odd width
    /// locks the rounding of the half, and the gapless case the dropped borders.
    #[test]
    fn a_split_viewport_is_the_rect_a_real_split_gives_the_new_pane() {
        let area = Rect::new(0, 0, 81, 25);
        for gaps in [true, false] {
            let prefs = ClientPrefs {
                pane_gaps: gaps,
                ..ClientPrefs::default()
            };
            for focus_left in [true, false] {
                for direction in [Direction::Horizontal, Direction::Vertical] {
                    let (mut layout, first) = TileLayout::new();
                    let second = layout.split_focused(first, Direction::Horizontal);
                    let focus = if focus_left { first } else { second };
                    let predicted = split_terminal_rect(&layout, focus, direction, area, &prefs);
                    let added = layout.split_focused(focus, direction);
                    let actual = terminal_panes(layout.panes(area, focus), &prefs)
                        .into_iter()
                        .find(|info| info.id == added)
                        .map(|info| info.inner_rect);
                    assert_eq!(
                        predicted, actual,
                        "gaps {gaps}, focus left {focus_left}, {direction:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn a_split_of_an_unknown_pane_has_no_viewport() {
        let (layout, _first) = TileLayout::new();
        let stranger = PaneId::from_raw(u32::MAX);
        let area = Rect::new(0, 0, 80, 24);
        let prefs = ClientPrefs::default();
        assert_eq!(
            split_terminal_rect(&layout, stranger, Direction::Horizontal, area, &prefs),
            None
        );
    }

    /// A grid of equal columns and rows gives every cell the same size; a
    /// wrong ratio in either direction shows as uneven cells.
    #[test]
    fn a_grid_of_equal_cells_gives_every_pane_the_same_viewport() {
        let prefs = ClientPrefs::default();
        let cells = grid_terminal_rects(2, 3, Rect::new(0, 0, 90, 30), &prefs);
        assert_eq!(cells.len(), 3);
        assert!(cells.iter().all(|column| column.len() == 2));
        let size = cells[0][0];
        assert!(
            cells
                .iter()
                .flatten()
                .all(|cell| cell.width == size.width && cell.height == size.height),
            "{cells:?}"
        );
    }

    #[test]
    fn a_one_cell_grid_is_the_lone_pane_viewport() {
        let prefs = ClientPrefs::default();
        let area = Rect::new(0, 0, 80, 24);
        assert_eq!(
            grid_terminal_rects(1, 1, area, &prefs),
            vec![vec![lone_pane_inner_rect(area, prefs.pane_scrollbars)]]
        );
    }
}
