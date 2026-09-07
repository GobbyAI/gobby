// upstream: herdr v0.8.0 src/ui/tabs.rs
//! Tab bar with scroll arrows, hit areas, and the new-tab button.

use crate::app::MouseGesture;
use crate::ui::chrome::{Chrome, Tab, WorkspaceView};
use crate::ui::text::display_width_u16;
use crate::ui::widgets::panel_contrast_fg;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

const MIN_TAB_WIDTH: u16 = 8;
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
pub fn tab_width(tabs: &[Tab], tab_idx: usize) -> u16 {
    display_width_u16(&tab_chrome_label(tabs, tab_idx))
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

fn tab_chrome_label(tabs: &[Tab], tab_idx: usize) -> String {
    let name = tab_display_name(tabs, tab_idx).unwrap_or_else(|| (tab_idx + 1).to_string());
    if tabs.get(tab_idx).is_some_and(|tab| tab.zoomed) {
        format!("{name} Z")
    } else {
        name
    }
}

fn layout_tab_hit_areas(tabs: &[Tab], area: Rect, scroll: usize) -> Vec<Rect> {
    let mut rects = vec![Rect::default(); tabs.len()];
    if area.width == 0 || area.height == 0 {
        return rects;
    }

    let mut x = area.x;
    let right = area.x + area.width;
    for (idx, rect) in rects.iter_mut().enumerate().skip(scroll) {
        if x >= right {
            break;
        }
        let desired = tab_width(tabs, idx);
        let remaining = right.saturating_sub(x);
        let width = desired.min(remaining).max(1);
        *rect = Rect::new(x, area.y, width, 1);
        x = x.saturating_add(width + 1);
    }
    rects
}

fn centered_tab_scroll(tabs: &[Tab], active_tab: usize, area: Rect) -> usize {
    let mut best_scroll = active_tab;
    let mut best_distance = u16::MAX;
    let viewport_center = area.x.saturating_mul(2).saturating_add(area.width);

    for scroll in 0..=active_tab {
        let rects = layout_tab_hit_areas(tabs, area, scroll);
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

fn max_tab_scroll(tabs: &[Tab], area: Rect) -> usize {
    (0..tabs.len())
        .find(|&scroll| {
            layout_tab_hit_areas(tabs, area, scroll)
                .last()
                .is_some_and(|rect| rect.width > 0)
        })
        .unwrap_or(0)
}

fn compute_tab_bar_view(
    tabs: &[Tab],
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
    let all_tabs = layout_tab_hit_areas(tabs, all_tabs_area, 0);
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

    let max_scroll = max_tab_scroll(tabs, tab_area);
    let scroll = if follow_active {
        centered_tab_scroll(tabs, active_tab, tab_area).min(max_scroll)
    } else {
        current_scroll.min(max_scroll)
    };
    let tab_hit_areas = layout_tab_hit_areas(tabs, tab_area, scroll);
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

fn nonempty(rect: Rect) -> Option<Rect> {
    (rect.width > 0).then_some(rect)
}

/// Draw the tab strip into `area` and return its hit areas.
pub fn render_tab_bar<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    _ws: &W,
    chrome: &Chrome,
) -> TabBarHits {
    if area.width == 0 || area.height == 0 {
        return TabBarHits::default();
    }
    let tabs = &chrome.tabs;
    let p = &chrome.palette;
    let view = compute_tab_bar_view(
        tabs,
        chrome.active_tab,
        area,
        chrome.tab_scroll,
        chrome.tab_scroll_follow_active,
    );

    frame.render_widget(
        Paragraph::new(" ".repeat(area.width as usize)).style(Style::default().bg(p.panel_bg)),
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
    for (idx, tab) in tabs.iter().enumerate() {
        let Some(rect) = view.tab_hit_areas.get(idx).copied() else {
            break;
        };
        if rect.width == 0 {
            continue;
        }
        let active = idx == chrome.active_tab;
        let style = if active {
            let base = Style::default().fg(panel_contrast_fg(p)).bg(p.accent);
            if tab_is_auto_named(tab) {
                base
            } else {
                base.add_modifier(Modifier::BOLD)
            }
        } else if tab_is_auto_named(tab) {
            Style::default()
                .fg(p.overlay0)
                .bg(p.surface0)
                .add_modifier(Modifier::DIM)
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
        let name = tab_chrome_label(tabs, idx);
        let text = format!(" {:width$}", name, width = width.saturating_sub(1));
        frame.render_widget(Paragraph::new(text).style(style), rect);
    }

    if view.new_tab_hit_area.width > 0 {
        frame.render_widget(
            Paragraph::new(" + ").style(Style::default().fg(p.overlay1)),
            view.new_tab_hit_area,
        );
    }

    if first_visible_idx.is_some_and(|idx| idx > 0) {
        let x = if view.scroll_left_hit_area.width > 0 {
            view.scroll_left_hit_area.x + view.scroll_left_hit_area.width
        } else {
            area.x
        };
        if x < area.x + area.width {
            frame.buffer_mut()[(x, area.y)]
                .set_symbol("…")
                .set_style(Style::default().fg(p.overlay0));
        }
    }
    if last_visible_idx.is_some_and(|idx| idx + 1 < tabs.len()) {
        let x = if view.scroll_right_hit_area.width > 0 {
            view.scroll_right_hit_area.x.saturating_sub(1)
        } else {
            area.x + area.width.saturating_sub(1)
        };
        if x >= area.x && x < area.x + area.width {
            frame.buffer_mut()[(x, area.y)]
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
            chrome.tabs.push(Tab::new(*title, PaneId(idx as u32 + 1)));
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
        let chrome = chrome_with_tabs(&["alpha", "second", ""]);
        let (hits, text) = draw(&chrome, 80);
        assert_eq!(hits.tabs.len(), 3);
        assert_eq!(hits.tabs[0], (0, Rect::new(0, 0, 9, 1)));
        assert_eq!(hits.tabs[1].1.x, 10);
        assert!(hits.scroll_left.is_none() && hits.scroll_right.is_none());
        assert_eq!(hits.new_tab, Some(Rect::new(29, 0, 3, 1)));
        assert!(text.contains(" alpha") && text.contains(" second") && text.contains(" 3 "));
        assert!(text.contains(" + "));
    }

    #[test]
    fn overflowing_tabs_get_scroll_arrows_and_follow_the_active_tab() {
        let mut chrome = chrome_with_tabs(&["one", "two", "three", "four", "five", "six"]);
        chrome.active_tab = 5;
        chrome.tabs[5].zoomed = true;
        let (hits, text) = draw(&chrome, 46);
        assert_eq!(hits.scroll_left, Some(Rect::new(0, 0, 3, 1)));
        assert!(hits.scroll_right.is_some() && hits.new_tab.is_some());
        assert!(hits.tabs.iter().any(|(idx, _)| *idx == 5));
        assert!(hits.tabs.iter().all(|(_, rect)| rect.width > 0));
        assert!(text.contains("six Z"), "{text}");
        assert!(text.contains(" < ") && text.contains(" > "));
    }
}
