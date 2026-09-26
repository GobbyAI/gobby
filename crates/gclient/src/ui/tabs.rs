// upstream: herdr v0.8.0 src/ui/tabs.rs
//! Tab bar with scroll arrows, hit areas, and the new-tab button.

use crate::app::viewer_state::LOCAL_TAB_PREFIX;
use crate::app::MouseGesture;
use crate::ui::chrome::{Chrome, Tab, WorkspaceView};
use crate::ui::settings::SidebarSide;
use crate::ui::sidebar_rows::project_label;
use crate::ui::text::display_width_u16;
use crate::ui::widgets::panel_contrast_fg;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;
use std::collections::HashSet;

const MIN_TAB_WIDTH: u16 = 12;
const NEW_TAB_WIDTH: u16 = 3;
const TAB_SCROLL_BUTTON_WIDTH: u16 = 3;

#[derive(Debug, Clone, Default)]
pub struct TabBarHits {
    pub tabs: Vec<(usize, Rect)>,
    pub scroll_left: Option<Rect>,
    pub scroll_right: Option<Rect>,
    pub new_tab: Option<Rect>,
}

#[derive(Debug, Clone, Default)]
struct TabBarView {
    scroll: usize,
    tab_hit_areas: Vec<Rect>,
    scroll_left_hit_area: Rect,
    scroll_right_hit_area: Rect,
    new_tab_hit_area: Rect,
}

fn tab_is_auto_named(tab: &Tab) -> bool {
    tab.title.trim().is_empty()
}

/// herdr `tab_width`: the chrome label plus padding, never under `MIN_TAB_WIDTH`.
pub fn tab_width(labels: &[String], tab_idx: usize) -> u16 {
    labels
        .get(tab_idx)
        .map(|label| display_width_u16(label))
        .unwrap_or(0)
        .saturating_add(4)
        .max(MIN_TAB_WIDTH)
}

/// herdr `tab_display_name`: the custom title, or the 1-based position for an
/// auto-named tab. `None` past the end of the strip. The zoom marker is chrome
/// (see `tab_chrome_label`) and never part of the name.
pub fn tab_display_name(tabs: &[Tab], tab_idx: usize) -> Option<String> {
    let tab = tabs.get(tab_idx)?;
    Some(if tab_is_auto_named(tab) {
        (tab_idx + 1).to_string()
    } else {
        tab.title.trim().to_string()
    })
}

fn tab_chrome_label<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    tabs: &[Tab],
    tab_idx: usize,
) -> String {
    let Some(tab) = tabs.get(tab_idx) else {
        return String::new();
    };
    let name = if tab_is_auto_named(tab) {
        if tab.id.starts_with(LOCAL_TAB_PREFIX) {
            (tab_idx + 1).to_string()
        } else {
            tab.id.clone()
        }
    } else {
        tab.title.trim().to_string()
    };
    let project = ws
        .focused_project()
        .map(|id| project_label(ws, chrome, id).unwrap_or_else(|| id.to_string()));
    let mut label = match project {
        Some(project) => format!("{project}:{name}"),
        None => name,
    };
    if chrome.viewer.zoomed.contains(&tab.id) {
        label.push_str(" Z");
    }
    label
}

fn layout_tab_hit_areas(labels: &[String], area: Rect, scroll: usize) -> Vec<Rect> {
    let mut rects = vec![Rect::default(); labels.len()];
    if area.width == 0 || area.height == 0 {
        return rects;
    }

    let mut x = area.x;
    let right = area.x + area.width;
    for (idx, rect) in rects.iter_mut().enumerate().skip(scroll) {
        if x >= right {
            break;
        }
        let desired = tab_width(labels, idx);
        let remaining = right.saturating_sub(x);
        let width = desired.min(remaining).max(1);
        *rect = Rect::new(x, area.y, width, 1);
        x = x.saturating_add(width + 1);
    }
    rects
}

fn centered_tab_scroll(labels: &[String], active_tab: usize, area: Rect) -> usize {
    let mut best_scroll = active_tab;
    let mut best_distance = u16::MAX;
    let viewport_center = area.x.saturating_mul(2).saturating_add(area.width);

    for scroll in 0..=active_tab {
        let rects = layout_tab_hit_areas(labels, area, scroll);
        let Some(active_rect) = rects.get(active_tab).copied() else {
            continue;
        };
        if active_rect.width == 0 {
            continue;
        }

        let active_center = active_rect
            .x
            .saturating_mul(2)
            .saturating_add(active_rect.width);
        let distance = active_center.abs_diff(viewport_center);
        if distance <= best_distance {
            best_distance = distance;
            best_scroll = scroll;
        }
    }

    best_scroll
}

fn trailing_tab_controls_x(tab_hit_areas: &[Rect], fallback_x: u16) -> u16 {
    tab_hit_areas
        .iter()
        .rev()
        .find(|rect| rect.width > 0)
        .map(|rect| rect.x + rect.width)
        .unwrap_or(fallback_x)
}

fn max_tab_scroll(labels: &[String], area: Rect) -> usize {
    (0..labels.len())
        .find(|&scroll| {
            layout_tab_hit_areas(labels, area, scroll)
                .last()
                .is_some_and(|rect| rect.width > 0)
        })
        .unwrap_or(0)
}

fn compute_tab_bar_view(
    labels: &[String],
    active_tab: usize,
    area: Rect,
    current_scroll: usize,
    follow_active: bool,
) -> TabBarView {
    if area.width == 0 || area.height == 0 {
        return TabBarView::default();
    }

    let area_right = area.x + area.width;
    let all_tabs_area = Rect::new(
        area.x,
        area.y,
        area.width.saturating_sub(NEW_TAB_WIDTH),
        area.height,
    );
    let all_tabs = layout_tab_hit_areas(labels, all_tabs_area, 0);
    let overflow = all_tabs.iter().any(|rect| rect.width == 0);
    if !overflow {
        let new_tab_x = trailing_tab_controls_x(&all_tabs, area.x);
        let new_tab_hit_area = Rect::new(
            new_tab_x,
            area.y,
            area_right.saturating_sub(new_tab_x).min(NEW_TAB_WIDTH),
            1,
        );
        return TabBarView {
            scroll: 0,
            tab_hit_areas: all_tabs,
            scroll_left_hit_area: Rect::default(),
            scroll_right_hit_area: Rect::default(),
            new_tab_hit_area,
        };
    }

    let left_hit_area = Rect::new(area.x, area.y, TAB_SCROLL_BUTTON_WIDTH.min(area.width), 1);
    let tab_area_x = left_hit_area.x + left_hit_area.width;
    let reserved_trailing_width = NEW_TAB_WIDTH.saturating_add(TAB_SCROLL_BUTTON_WIDTH);
    let tab_area_right = area_right.saturating_sub(reserved_trailing_width);
    let tab_area = Rect::new(
        tab_area_x,
        area.y,
        tab_area_right.saturating_sub(tab_area_x),
        area.height,
    );

    let max_scroll = max_tab_scroll(labels, tab_area);
    let scroll = if follow_active {
        centered_tab_scroll(labels, active_tab, tab_area).min(max_scroll)
    } else {
        current_scroll.min(max_scroll)
    };
    let tab_hit_areas = layout_tab_hit_areas(labels, tab_area, scroll);
    let trailing_x = trailing_tab_controls_x(&tab_hit_areas, tab_area_x).min(tab_area_right);
    let right_hit_area = Rect::new(
        trailing_x,
        area.y,
        area_right
            .saturating_sub(trailing_x)
            .min(TAB_SCROLL_BUTTON_WIDTH),
        1,
    );
    let new_tab_x = right_hit_area.x + right_hit_area.width;
    let new_tab_hit_area = Rect::new(
        new_tab_x,
        area.y,
        area_right.saturating_sub(new_tab_x).min(NEW_TAB_WIDTH),
        1,
    );

    TabBarView {
        scroll,
        tab_hit_areas,
        scroll_left_hit_area: left_hit_area,
        scroll_right_hit_area: right_hit_area,
        new_tab_hit_area,
    }
}

/// The columns of `area` the overlay drawn at `overlay` leaves uncovered on
/// its far side; all of `area` when the overlay drew nothing.
fn beside_overlay(area: Rect, overlay: Rect, side: SidebarSide) -> Rect {
    if overlay.is_empty() {
        return area;
    }
    let (left, right) = match side {
        SidebarSide::Left => (overlay.right().clamp(area.x, area.right()), area.right()),
        SidebarSide::Right => (area.x, overlay.x.clamp(area.x, area.right())),
    };
    Rect::new(left, area.y, right - left, area.height)
}

fn nonempty(rect: Rect) -> Option<Rect> {
    (rect.width > 0).then_some(rect)
}

/// Draw the tab strip into `area` and return its hit areas.
pub fn render_tab_bar<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> TabBarHits {
    if area.width == 0 || area.height == 0 {
        return TabBarHits::default();
    }
    let tabs = &chrome.tabs().tabs;
    let attention_entries: HashSet<String> = ws.attention_entry_ids().into_iter().collect();
    let attention_panes: Vec<_> = ws
        .sidebar()
        .agents
        .iter()
        .filter(|agent| attention_entries.contains(&agent.entry_id))
        .filter_map(|agent| ws.pane_for_terminal(&agent.terminal_id))
        .collect();
    let labels: Vec<String> = (0..tabs.len())
        .map(|idx| {
            let label = tab_chrome_label(ws, chrome, tabs, idx);
            if idx != chrome.active_index()
                && attention_panes
                    .iter()
                    .any(|pane| tabs[idx].slot_for(*pane).is_some())
            {
                format!("⍾ {label}")
            } else {
                label
            }
        })
        .collect();
    let p = &chrome.palette;
    // An overlay covers one end of the bar, so the tabs fit, and scroll when
    // they do not, in the columns beside it.
    let beside = if chrome.sidebar.overlay {
        beside_overlay(area, chrome.view.sidebar_rect, chrome.sidebar.side)
    } else {
        area
    };
    let mut view = compute_tab_bar_view(
        &labels,
        chrome.active_index(),
        beside,
        chrome.tab_scroll,
        chrome.tab_scroll_follow_active,
    );
    // A left overlay covers the bar's start, so tabs that fit move to its
    // far side, hit areas with them; a right one leaves them at the start.
    let fits = view.scroll_left_hit_area.width == 0;
    if chrome.sidebar.overlay && chrome.sidebar.side == SidebarSide::Left && fits {
        let shift = area.right().saturating_sub(view.new_tab_hit_area.right());
        for rect in view
            .tab_hit_areas
            .iter_mut()
            .chain([&mut view.new_tab_hit_area])
        {
            rect.x += shift;
        }
    }

    frame.render_widget(
        Paragraph::new(" ".repeat(area.width as usize)).style(Style::default().bg(p.surface0)),
        area,
    );

    let first_visible_idx = view.tab_hit_areas.iter().position(|rect| rect.width > 0);
    let last_visible_idx = view.tab_hit_areas.iter().rposition(|rect| rect.width > 0);
    let can_scroll_left = view.scroll_left_hit_area.width > 0 && view.scroll > 0;
    let can_scroll_right = view.scroll_right_hit_area.width > 0
        && last_visible_idx.is_some_and(|idx| idx + 1 < tabs.len());

    let arrow_style = |enabled: bool| {
        if enabled {
            Style::default().fg(p.overlay1).bg(p.surface0)
        } else {
            Style::default()
                .fg(p.overlay0)
                .bg(p.surface0)
                .add_modifier(Modifier::DIM)
        }
    };
    if view.scroll_left_hit_area.width > 0 {
        frame.render_widget(
            Paragraph::new(" < ").style(arrow_style(can_scroll_left)),
            view.scroll_left_hit_area,
        );
    }
    if view.scroll_right_hit_area.width > 0 {
        frame.render_widget(
            Paragraph::new(" > ").style(arrow_style(can_scroll_right)),
            view.scroll_right_hit_area,
        );
    }

    let dragged = match chrome.gesture {
        Some(MouseGesture::TabDrag {
            index, moved: true, ..
        }) => Some(index),
        _ => None,
    };
    for (idx, name) in labels.iter().enumerate() {
        let Some(rect) = view.tab_hit_areas.get(idx).copied() else {
            break;
        };
        if rect.width == 0 {
            continue;
        }
        let active = idx == chrome.active_index();
        let style = if active {
            Style::default()
                .fg(p.text)
                .bg(panel_contrast_fg(p))
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.overlay1).bg(p.surface0)
        };
        // A dragged tab lifts off the bar: its own foreground on the bar's
        // surface, reversed, until the release drops it.
        let style = if dragged == Some(idx) {
            style.bg(p.surface0).add_modifier(Modifier::REVERSED)
        } else {
            style
        };
        let width = rect.width as usize;
        let text = format!(" {:width$}", name, width = width.saturating_sub(1));
        let line = if let Some(rest) = text.strip_prefix(" ⍾") {
            Line::from(vec![
                Span::raw(" "),
                Span::styled("⍾", Style::default().fg(p.yellow)),
                Span::raw(rest.to_string()),
            ])
        } else {
            Line::raw(text)
        };
        frame.render_widget(Paragraph::new(line).style(style), rect);
    }

    if view.new_tab_hit_area.width > 0 {
        frame.render_widget(
            Paragraph::new(" + ").style(Style::default().fg(p.overlay1).bg(p.surface0)),
            view.new_tab_hit_area,
        );
    }

    if first_visible_idx.is_some_and(|idx| idx > 0) {
        let x = if view.scroll_left_hit_area.width > 0 {
            view.scroll_left_hit_area.x + view.scroll_left_hit_area.width
        } else {
            beside.x
        };
        if x < beside.x + beside.width {
            frame.buffer_mut()[(x, beside.y)]
                .set_symbol("…")
                .set_style(Style::default().fg(p.overlay0));
        }
    }
    if last_visible_idx.is_some_and(|idx| idx + 1 < tabs.len()) {
        let x = if view.scroll_right_hit_area.width > 0 {
            view.scroll_right_hit_area.x.saturating_sub(1)
        } else {
            beside.x + beside.width.saturating_sub(1)
        };
        if x >= beside.x && x < beside.x + beside.width {
            frame.buffer_mut()[(x, beside.y)]
                .set_symbol("…")
                .set_style(Style::default().fg(p.overlay0));
        }
    }

    TabBarHits {
        tabs: view
            .tab_hit_areas
            .iter()
            .enumerate()
            .filter(|(_, rect)| rect.width > 0)
            .map(|(idx, rect)| (idx, *rect))
            .collect(),
        scroll_left: nonempty(view.scroll_left_hit_area),
        scroll_right: nonempty(view.scroll_right_hit_area),
        new_tab: nonempty(view.new_tab_hit_area),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::{PaneId, Workspace};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn chrome_with_tabs(titles: &[&str]) -> Chrome {
        let mut chrome = Chrome::dark();
        for (idx, title) in titles.iter().enumerate() {
            chrome
                .tabs_mut()
                .tabs
                .push(Tab::new(*title, PaneId(idx as u32 + 1)));
        }
        chrome
    }

    fn draw(chrome: &Chrome, width: u16) -> (TabBarHits, String) {
        let ws = Workspace::scripted();
        let mut terminal = Terminal::new(TestBackend::new(width, 1)).unwrap();
        let mut hits = TabBarHits::default();
        terminal
            .draw(|frame| hits = render_tab_bar(frame, frame.area(), &ws, chrome))
            .unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect();
        (hits, text)
    }

    #[test]
    fn fitting_tabs_expose_every_tab_and_the_new_tab_button() {
        assert!(MIN_TAB_WIDTH > display_width_u16("gobby:0:0:1"));
        let chrome = chrome_with_tabs(&["alpha", "second", ""]);
        let (hits, text) = draw(&chrome, 80);
        assert_eq!(hits.tabs.len(), 3);
        assert_eq!(hits.tabs[0], (0, Rect::new(0, 0, 12, 1)));
        assert_eq!(hits.tabs[1].1.x, 13);
        assert!(hits.scroll_left.is_none() && hits.scroll_right.is_none());
        assert_eq!(hits.new_tab, Some(Rect::new(38, 0, 3, 1)));
        assert!(text.contains(" alpha") && text.contains(" second") && text.contains(" 3 "));
        assert!(text.contains(" + "));
    }

    #[test]
    fn overlay_puts_the_tabs_on_its_far_side() {
        // A left overlay covers the bar's left end, so the tabs and the new
        // tab button right-align; a right one leaves them where they were.
        let mut chrome = chrome_with_tabs(&["alpha", "second", ""]);
        chrome.sidebar.overlay = true;
        chrome.view.sidebar_rect = Rect::new(0, 0, 34, 1);
        let (hits, text) = draw(&chrome, 80);
        assert_eq!(hits.tabs[0], (0, Rect::new(39, 0, 12, 1)));
        assert_eq!(hits.new_tab, Some(Rect::new(77, 0, 3, 1)));
        assert!(
            text.starts_with(&" ".repeat(39)) && text.ends_with(" + "),
            "{text}"
        );

        chrome.sidebar.side = SidebarSide::Right;
        chrome.view.sidebar_rect = Rect::new(46, 0, 34, 1);
        let (hits, _) = draw(&chrome, 80);
        assert_eq!(hits.tabs[0], (0, Rect::new(0, 0, 12, 1)));
        assert_eq!(hits.new_tab, Some(Rect::new(38, 0, 3, 1)));
    }

    #[test]
    fn overlay_fits_the_tabs_to_the_columns_it_leaves() {
        // Tabs that fit the whole bar but not the columns beside the overlay
        // scroll inside those columns; none lies under the overlay.
        let mut chrome = chrome_with_tabs(&["one", "two", "three", "four", "five", "six"]);
        chrome.activate_tab(5);
        let (hits, _) = draw(&chrome, 80);
        assert!(hits.scroll_left.is_none(), "the whole bar fits them");

        chrome.sidebar.overlay = true;
        for (side, overlay, beside) in [
            (SidebarSide::Left, Rect::new(0, 0, 34, 1), 34..80),
            (SidebarSide::Right, Rect::new(46, 0, 34, 1), 0..46),
        ] {
            chrome.sidebar.side = side;
            chrome.view.sidebar_rect = overlay;
            let (hits, text) = draw(&chrome, 80);
            assert!(
                hits.scroll_left.is_some() && hits.scroll_right.is_some(),
                "{side:?}: {text}"
            );
            let drawn = hits
                .tabs
                .iter()
                .map(|(_, rect)| *rect)
                .chain(
                    [hits.scroll_left, hits.scroll_right, hits.new_tab]
                        .into_iter()
                        .flatten(),
                )
                .filter(|rect| rect.width > 0);
            for rect in drawn {
                assert!(
                    beside.contains(&rect.x) && rect.right() <= beside.end,
                    "{side:?}: {rect:?} lies under the overlay"
                );
            }
            assert!(
                hits.tabs.iter().any(|(idx, _)| *idx == 5),
                "{side:?}: the active tab shows"
            );
        }
    }

    #[test]
    fn overflowing_tabs_get_scroll_arrows_and_follow_the_active_tab() {
        let mut chrome = chrome_with_tabs(&["one", "two", "three", "four", "five", "six"]);
        chrome.activate_tab(5);
        let sixth = chrome.tabs().tabs[5].id.clone();
        chrome.viewer.zoomed.insert(sixth);
        let (hits, text) = draw(&chrome, 46);
        assert_eq!(hits.scroll_left, Some(Rect::new(0, 0, 3, 1)));
        assert!(hits.scroll_right.is_some() && hits.new_tab.is_some());
        assert!(hits.tabs.iter().any(|(idx, _)| *idx == 5));
        assert!(hits.tabs.iter().all(|(_, rect)| rect.width > 0));
        assert!(text.contains("six Z"), "{text}");
        assert!(text.contains(" < ") && text.contains(" > "));
    }
}
