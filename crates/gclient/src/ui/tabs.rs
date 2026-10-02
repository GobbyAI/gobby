// upstream: herdr v0.8.0 src/ui/tabs.rs
//! Tab bar with edge counts for hidden tabs, hit areas, and the new-tab button.

use crate::app::sidebar_model::rollup;
use crate::app::MouseGesture;
use crate::ui::chrome::{Chrome, RowState, Tab, WorkspaceView};
use crate::ui::settings::SidebarSide;
use crate::ui::sidebar::agents::agent_state;
use crate::ui::status::state_dot;
use crate::ui::text::display_width_u16;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

const MIN_TAB_WIDTH: u16 = 12;
const NEW_TAB_WIDTH: u16 = 3;

#[derive(Debug, Clone, Default)]
pub struct TabBarHits {
    pub tabs: Vec<(usize, Rect)>,
    pub scroll_left: Option<Rect>,
    pub scroll_right: Option<Rect>,
    pub new_tab: Option<Rect>,
}

/// One edge of an overflowing bar: how many tabs lie beyond it, and whether
/// one of them needs you.
#[derive(Debug, Clone, Default)]
struct EdgeMarker {
    rect: Rect,
    text: String,
    needs_you: bool,
}

#[derive(Debug, Clone, Default)]
struct TabBarView {
    overflow: bool,
    tab_hit_areas: Vec<Rect>,
    left: EdgeMarker,
    right: EdgeMarker,
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

/// A tab's name wherever the chrome shows it: the title alone, or until it
/// has one its number and `Untitled`. The number is the tab's segment of the
/// daemon address, or the position of a tab the daemon has not seen. The
/// project is never repeated here.
pub fn tab_label<W: WorkspaceView>(ws: &W, tabs: &[Tab], tab_idx: usize) -> String {
    let Some(tab) = tabs.get(tab_idx) else {
        return String::new();
    };
    if !tab_is_auto_named(tab) {
        return tab.title.trim().to_string();
    }
    let number = ws
        .workspace_model()
        .and_then(|model| model.tab(&tab.id))
        .map_or_else(
            || (tab_idx + 1).to_string(),
            |row| row.reference.to_string(),
        );
    format!("{number}: Untitled")
}

fn tab_chrome_label<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    tabs: &[Tab],
    tab_idx: usize,
) -> String {
    let mut label = tab_label(ws, tabs, tab_idx);
    if tabs
        .get(tab_idx)
        .is_some_and(|tab| chrome.viewer.zoomed.contains(&tab.id))
    {
        label.push_str(" Z");
    }
    label
}

fn layout_tab_hit_areas(labels: &[String], area: Rect, scroll: usize) -> Vec<Rect> {
    let mut rects = vec![Rect::default(); labels.len()];
    if area.width == 0 || area.height == 0 {
        return rects;
    }

    // A tab shows whole or not at all; only the first one clips, so a tab
    // wider than the bar still shows.
    let mut x = area.x;
    let right = area.x + area.width;
    for (idx, rect) in rects.iter_mut().enumerate().skip(scroll) {
        if x >= right {
            break;
        }
        let desired = tab_width(labels, idx);
        let remaining = right.saturating_sub(x);
        if desired > remaining && idx != scroll {
            break;
        }
        let width = desired.min(remaining).max(1);
        *rect = Rect::new(x, area.y, width, 1);
        x = x.saturating_add(width + 1);
    }
    rects
}

fn needs_you_prefix(needs_you: bool) -> &'static str {
    if needs_you {
        "⍾ "
    } else {
        ""
    }
}

/// The overflowing bar from `scroll`: the left count when tabs lie before
/// it, the tabs that fit whole, and the right count when tabs lie after
/// them. `area` leaves out the new-tab button.
fn layout_at(
    labels: &[String],
    attention: &[bool],
    area: Rect,
    scroll: usize,
) -> (Vec<Rect>, EdgeMarker, EdgeMarker) {
    let left = if scroll > 0 {
        let needs_you = attention.iter().take(scroll).any(|&a| a);
        let text = format!(" ‹ {}{scroll} ", needs_you_prefix(needs_you));
        let width = display_width_u16(&text).min(area.width);
        EdgeMarker {
            rect: Rect::new(area.x, area.y, width, 1),
            text,
            needs_you,
        }
    } else {
        EdgeMarker::default()
    };
    let tabs_x = area.x + left.rect.width;
    let tabs_area = Rect::new(
        tabs_x,
        area.y,
        area.right().saturating_sub(tabs_x),
        area.height,
    );
    let rects = layout_tab_hit_areas(labels, tabs_area, scroll);
    if rects.last().is_some_and(|rect| rect.width > 0) {
        return (rects, left, EdgeMarker::default());
    }

    // Room for the widest right count, so the tabs never shift under it.
    let reserve = display_width_u16(&format!(" ⍾ {} › ", labels.len()));
    let narrowed = Rect {
        width: tabs_area.width.saturating_sub(reserve),
        ..tabs_area
    };
    let rects = layout_tab_hit_areas(labels, narrowed, scroll);
    let shown = rects
        .iter()
        .rposition(|rect| rect.width > 0)
        .map_or(scroll, |idx| idx + 1);
    let needs_you = attention.iter().skip(shown).any(|&a| a);
    let text = format!(
        " {}{} › ",
        needs_you_prefix(needs_you),
        labels.len() - shown
    );
    let width = display_width_u16(&text).min(tabs_area.width);
    let right = EdgeMarker {
        rect: Rect::new(area.right() - width, area.y, width, 1),
        text,
        needs_you,
    };
    (rects, left, right)
}

fn centered_tab_scroll(
    labels: &[String],
    attention: &[bool],
    active_tab: usize,
    area: Rect,
) -> usize {
    let mut best_scroll = active_tab;
    let mut best_distance = u16::MAX;
    let viewport_center = area.x.saturating_mul(2).saturating_add(area.width);

    for scroll in 0..=active_tab {
        let (rects, _, _) = layout_at(labels, attention, area, scroll);
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

/// The `+` column: one gap past the last shown tab, for the rule that parts
/// them, or `fallback_x` with no tab.
fn trailing_tab_controls_x(tab_hit_areas: &[Rect], fallback_x: u16) -> u16 {
    tab_hit_areas
        .iter()
        .rev()
        .find(|rect| rect.width > 0)
        .map(|rect| rect.right() + 1)
        .unwrap_or(fallback_x)
}

fn max_tab_scroll(labels: &[String], attention: &[bool], area: Rect) -> usize {
    (0..labels.len())
        .find(|&scroll| {
            layout_at(labels, attention, area, scroll)
                .0
                .last()
                .is_some_and(|rect| rect.width > 0)
        })
        .unwrap_or(0)
}

fn compute_tab_bar_view(
    labels: &[String],
    attention: &[bool],
    active_tab: usize,
    area: Rect,
    current_scroll: usize,
    follow_active: bool,
) -> TabBarView {
    if area.width == 0 || area.height == 0 {
        return TabBarView::default();
    }

    let area_right = area.x + area.width;
    // The `+` and the one-column rule before it.
    let all_tabs_area = Rect::new(
        area.x,
        area.y,
        area.width.saturating_sub(NEW_TAB_WIDTH + 1),
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
            overflow: false,
            tab_hit_areas: all_tabs,
            left: EdgeMarker::default(),
            right: EdgeMarker::default(),
            new_tab_hit_area,
        };
    }

    // The new-tab button stays pinned at the right end while tabs scroll.
    let max_scroll = max_tab_scroll(labels, attention, all_tabs_area);
    let scroll = if follow_active {
        centered_tab_scroll(labels, attention, active_tab, all_tabs_area).min(max_scroll)
    } else {
        current_scroll.min(max_scroll)
    };
    let (tab_hit_areas, left, right) = layout_at(labels, attention, all_tabs_area, scroll);
    let new_tab_x = all_tabs_area.right() + 1;
    let new_tab_hit_area = Rect::new(
        new_tab_x,
        area.y,
        area_right.saturating_sub(new_tab_x).min(NEW_TAB_WIDTH),
        1,
    );

    TabBarView {
        overflow: true,
        tab_hit_areas,
        left,
        right,
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
    let p = &chrome.palette;
    // A tab takes the most urgent state of the agents on its panes, the
    // active tab included, and shows only the bell, while one of them needs
    // you; work and new output stay on the sidebar rows.
    let states: Vec<RowState> = tabs
        .iter()
        .map(|tab| {
            rollup(ws.sidebar().agents.iter().filter_map(|agent| {
                let pane = ws.pane_for_terminal(&agent.terminal_id)?;
                tab.slot_for(pane).is_some().then(|| agent_state(ws, agent))
            }))
        })
        .collect();
    let glyphs: Vec<Option<(&str, Color)>> = states
        .iter()
        .map(|&state| (state == RowState::Attention).then(|| state_dot(state, p)))
        .collect();
    let attention: Vec<bool> = states
        .iter()
        .map(|&state| state == RowState::Attention)
        .collect();
    let labels: Vec<String> = (0..tabs.len())
        .map(|idx| {
            let label = tab_chrome_label(ws, chrome, tabs, idx);
            match glyphs[idx] {
                Some((glyph, _)) => format!("{glyph} {label}"),
                None => label,
            }
        })
        .collect();
    // An overlay covers one end of the bar, so the tabs fit, and scroll when
    // they do not, in the columns beside it.
    let beside = if chrome.sidebar.overlay {
        beside_overlay(area, chrome.view.sidebar_rect, chrome.sidebar.side)
    } else {
        area
    };
    let mut view = compute_tab_bar_view(
        &labels,
        &attention,
        chrome.active_index(),
        beside,
        chrome.tab_scroll,
        chrome.tab_scroll_follow_active,
    );
    // A left overlay covers the bar's start, so tabs that fit move to its
    // far side, hit areas with them; a right one leaves them at the start.
    if chrome.sidebar.overlay && chrome.sidebar.side == SidebarSide::Left && !view.overflow {
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

    // Each edge counts the tabs beyond it, in the needs-you colour when one
    // of them needs you.
    for marker in [&view.left, &view.right] {
        if marker.rect.width > 0 {
            let fg = if marker.needs_you {
                state_dot(RowState::Attention, p).1
            } else {
                p.overlay1
            };
            frame.render_widget(
                Paragraph::new(marker.text.as_str()).style(Style::default().fg(fg).bg(p.surface0)),
                marker.rect,
            );
        }
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
        // The active tab rises off the bar on the raised surface in every
        // theme; the host's own ground would read as a hole under System.
        let style = if active {
            Style::default()
                .fg(p.text)
                .bg(p.surface1)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.subtext0).bg(p.surface0)
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
        let line = match glyphs[idx] {
            Some((glyph, color)) => Line::from(vec![
                Span::raw(" "),
                Span::styled(glyph, Style::default().fg(color)),
                Span::raw(text[1 + glyph.len()..].to_string()),
            ]),
            None => Line::raw(text),
        };
        frame.render_widget(Paragraph::new(line).style(style), rect);
        // A thin rule in the gap before the next tab tells the two apart.
        let next_shown = view
            .tab_hit_areas
            .get(idx + 1)
            .is_some_and(|next| next.width > 0 && next.x == rect.right() + 1);
        if next_shown {
            frame.render_widget(
                Paragraph::new("│").style(Style::default().fg(p.line).bg(p.surface0)),
                Rect::new(rect.right(), rect.y, 1, 1),
            );
        }
    }

    if view.new_tab_hit_area.width > 0 {
        frame.render_widget(
            Paragraph::new(" + ").style(Style::default().fg(p.overlay1).bg(p.surface0)),
            view.new_tab_hit_area,
        );
        // The same rule that parts two tabs parts the last one from `+`.
        if view.tab_hit_areas.iter().any(|rect| rect.width > 0) {
            frame.render_widget(
                Paragraph::new("│").style(Style::default().fg(p.line).bg(p.surface0)),
                Rect::new(view.new_tab_hit_area.x - 1, area.y, 1, 1),
            );
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
        scroll_left: nonempty(view.left.rect),
        scroll_right: nonempty(view.right.rect),
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
        let chrome = chrome_with_tabs(&["alpha", "second", ""]);
        let (hits, text) = draw(&chrome, 80);
        assert_eq!(hits.tabs.len(), 3);
        assert_eq!(hits.tabs[0], (0, Rect::new(0, 0, 12, 1)));
        assert_eq!(hits.tabs[1].1.x, 13);
        assert_eq!(hits.tabs[2].1.width, display_width_u16("3: Untitled") + 4);
        assert!(hits.scroll_left.is_none() && hits.scroll_right.is_none());
        assert_eq!(hits.new_tab, Some(Rect::new(42, 0, 3, 1)));
        assert!(text.contains(" alpha") && text.contains(" second"));
        assert!(text.contains(" 3: Untitled "), "{text}");
        assert!(
            text.contains("│ + "),
            "the rule parts the last tab from +: {text}"
        );
    }

    #[test]
    fn overlay_puts_the_tabs_on_its_far_side() {
        // A left overlay covers the bar's left end, so the tabs and the new
        // tab button right-align; a right one leaves them where they were.
        let mut chrome = chrome_with_tabs(&["alpha", "second", ""]);
        chrome.sidebar.overlay = true;
        chrome.view.sidebar_rect = Rect::new(0, 0, 34, 1);
        let (hits, text) = draw(&chrome, 80);
        assert_eq!(hits.tabs[0], (0, Rect::new(35, 0, 12, 1)));
        assert_eq!(hits.new_tab, Some(Rect::new(77, 0, 3, 1)));
        assert!(
            text.starts_with(&" ".repeat(35)) && text.ends_with(" + "),
            "{text}"
        );

        chrome.sidebar.side = SidebarSide::Right;
        chrome.view.sidebar_rect = Rect::new(46, 0, 34, 1);
        let (hits, _) = draw(&chrome, 80);
        assert_eq!(hits.tabs[0], (0, Rect::new(0, 0, 12, 1)));
        assert_eq!(hits.new_tab, Some(Rect::new(42, 0, 3, 1)));
    }

    #[test]
    fn overlay_fits_the_tabs_to_the_columns_it_leaves() {
        // Tabs that fit the whole bar but not the columns beside the overlay
        // scroll inside those columns; none lies under the overlay.
        let mut chrome = chrome_with_tabs(&["one", "two", "three", "four", "five"]);
        chrome.activate_tab(4);
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
            // The active tab is the last, so tabs hide only on the left.
            assert!(
                hits.scroll_left.is_some() && hits.scroll_right.is_none(),
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
                hits.tabs.iter().any(|(idx, _)| *idx == 4),
                "{side:?}: the active tab shows"
            );
        }
    }

    #[test]
    fn overflowing_tabs_show_whole_and_count_the_hidden_ones() {
        let mut chrome = chrome_with_tabs(&["one", "two", "three", "four", "five", "six"]);
        chrome.activate_tab(5);
        let sixth = chrome.tabs().tabs[5].id.clone();
        chrome.viewer.zoomed.insert(sixth);
        let (hits, text) = draw(&chrome, 47);
        // Following the active last tab, three tabs show whole after the
        // count of the three before them; none hides on the right, and the
        // new-tab button stays at the right end.
        let shown: Vec<(usize, Rect)> = [3, 4, 5]
            .into_iter()
            .zip([5, 18, 31])
            .map(|(idx, x)| (idx, Rect::new(x, 0, 12, 1)))
            .collect();
        assert_eq!(hits.tabs, shown);
        assert_eq!(hits.scroll_left, Some(Rect::new(0, 0, 5, 1)));
        assert_eq!(hits.scroll_right, None);
        assert_eq!(hits.new_tab, Some(Rect::new(44, 0, 3, 1)));
        assert!(text.starts_with(" ‹ 3 "), "{text}");
        assert!(text.contains("six Z") && !text.contains('…'), "{text}");
    }

    #[test]
    fn only_the_first_tab_clips() {
        // A tab wider than the bar still shows, clipped; the tab after it
        // hides whole behind the right count.
        let chrome = chrome_with_tabs(&["a very long title that will not fit", "b"]);
        let (hits, text) = draw(&chrome, 21);
        assert_eq!(hits.tabs, [(0, Rect::new(0, 0, 10, 1))]);
        assert_eq!(hits.scroll_left, None);
        assert_eq!(hits.scroll_right, Some(Rect::new(12, 0, 5, 1)));
        assert!(text.ends_with(" 1 › │ + "), "{text}");
    }
}
