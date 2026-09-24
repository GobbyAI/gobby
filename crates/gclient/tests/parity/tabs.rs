//! herdr `src/ui/tabs.rs` (5) keep-set render tests.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{
    route_mouse, MouseGesture, MouseOutcome, PaneId, Placement, Workspace, TAB_DRAG_THRESHOLD,
};
use gobby_client::ui::chrome::Tab;
use gobby_client::ui::chrome_render::render_workspace;
use gobby_client::ui::tabs::{render_tab_bar, tab_display_name, tab_width, TabBarHits};
use gobby_client::ui::text::display_width_u16;
use gobby_client::ui::Chrome;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use ratatui::Terminal;

use super::fixtures::{cell, draw, rect_rows, render, terminal};
use super::token_map::{palette, theme};

/// herdr `Workspace::test_new("test")`: one auto-named tab. gclient keeps tabs
/// on `Chrome`, with an empty title marking a tab as auto-named.
fn chrome_with_one_tab() -> Chrome {
    let mut chrome = Chrome::new(theme());
    chrome.tabs_mut().tabs.push(Tab::new("", PaneId(1)));
    chrome
}

/// Zoom tab `idx` the way `Action::Zoom` does for the active tab.
fn zoom_tab(chrome: &mut Chrome, idx: usize) {
    let id = chrome.tabs().tabs[idx].id.clone();
    chrome.viewer.zoomed.insert(id);
}

/// herdr `ws.test_add_tab(Some(name))`: returns the new tab's index.
fn add_tab(chrome: &mut Chrome, name: Option<&str>) -> usize {
    let idx = chrome.tabs().tabs.len();
    chrome
        .tabs_mut()
        .tabs
        .push(Tab::new(name.unwrap_or(""), PaneId(idx as u32 + 1)));
    idx
}

/// herdr `tabs[i].set_custom_name(name)`.
fn set_custom_name(chrome: &mut Chrome, idx: usize, name: &str) {
    chrome.tabs_mut().tabs[idx].title = name.to_string();
}

/// Draws the tab bar into `rect` and returns the terminal plus hit areas.
fn draw_tab_bar(chrome: &Chrome, rect: Rect) -> (Terminal<TestBackend>, TabBarHits) {
    let ws = Workspace::scripted();
    let mut term = terminal(rect.x + rect.width, rect.y + rect.height);
    let mut hits = TabBarHits::default();
    draw(&mut term, |frame| {
        hits = render_tab_bar(frame, rect, &ws, chrome);
    });
    (term, hits)
}

/// herdr `buffer_row_text`: the row inside `area`, trailing spaces trimmed.
fn buffer_row_text(term: &Terminal<TestBackend>, area: Rect, row: u16) -> String {
    let rows = rect_rows(term, Rect::new(area.x, row, area.width, 1));
    rows.into_iter()
        .next()
        .expect("row inside area")
        .trim_end()
        .to_string()
}

parity_tests! {
    "src/ui/tabs.rs" => {
        fn tab_bar_marks_zoomed_tabs_without_renaming_them() {
            let mut chrome = chrome_with_one_tab();
            zoom_tab(&mut chrome, 0);
            let custom_tab = add_tab(&mut chrome, Some("test"));
            zoom_tab(&mut chrome, custom_tab);

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, _) = draw_tab_bar(&chrome, tab_bar_rect);

            let row = buffer_row_text(&term, tab_bar_rect, 0);
            assert!(row.contains(" 1 Z"), "tab row: {row:?}");
            assert!(row.contains(" test Z"), "tab row: {row:?}");
            assert_eq!(tab_display_name(&chrome.tabs().tabs, 0).as_deref(), Some("1"));
            assert_eq!(
                tab_display_name(&chrome.tabs().tabs, custom_tab).as_deref(),
                Some("test")
            );
        }

        fn active_auto_named_tab_keeps_readable_weight() {
            let chrome = chrome_with_one_tab();

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, hits) = draw_tab_bar(&chrome, tab_bar_rect);

            let (_, tab_rect) = hits.tabs[0];
            let style = cell(&term, tab_rect.x + 1, tab_rect.y).style();

            assert_eq!(style.bg, Some(palette().accent));
            assert!(!style.add_modifier.contains(Modifier::DIM));
            assert!(!style.add_modifier.contains(Modifier::BOLD));
        }

        fn zoom_marker_counts_toward_tab_width() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "abcdefgh");
            zoom_tab(&mut chrome, 0);

            assert_eq!(tab_width(&chrome.tabs().tabs, 0, &chrome.viewer.zoomed), 14);
        }

        fn tab_width_uses_display_width_for_cjk_labels() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "提交 herdr 的反馈");

            assert_eq!(
                tab_width(&chrome.tabs().tabs, 0, &chrome.viewer.zoomed),
                display_width_u16("提交 herdr 的反馈") + 4
            );
        }

        fn tab_bar_renders_trailing_cjk_character() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "提交 herdr 的反馈");

            let tab_bar_rect = Rect::new(0, 0, 30, 1);
            let (term, _) = draw_tab_bar(&chrome, tab_bar_rect);

            let row = buffer_row_text(&term, tab_bar_rect, 0);
            assert!(row.contains('馈'), "tab row: {row:?}");
        }
    }
}

// Plan 2.2 tab bar mouse: gclient tests driving `route_mouse` over the drawn
// tab bar, kept outside the `parity_tests!` block so the herdr inventory
// stays exact.

const LEFT_DOWN: MouseEventKind = MouseEventKind::Down(MouseButton::Left);
const LEFT_DRAG: MouseEventKind = MouseEventKind::Drag(MouseButton::Left);
const LEFT_UP: MouseEventKind = MouseEventKind::Up(MouseButton::Left);

/// One roster terminal per title, each in its own tab, drawn once so the
/// tab-bar hit map is populated. Returns the workspace, the chrome and the
/// frame area.
fn tab_bar_chrome(width: u16, titles: &[&str]) -> (Workspace, Chrome, Rect) {
    let mut ws = Workspace::scripted();
    let mut chrome = Chrome::new(theme());
    for (index, title) in titles.iter().enumerate() {
        let pane = ws
            .open_terminal(&format!("term-{index}"), "native", "epoch")
            .expect("open scripted terminal");
        chrome.open_tab(pane, title);
    }
    chrome.activate_tab(0);
    let area = Rect::new(0, 0, width, 12);
    draw_with_hits(&ws, &mut chrome, area);
    (ws, chrome, area)
}

/// Draw the whole frame the way the run loop does and write the hits back.
fn draw_with_hits(ws: &Workspace, chrome: &mut Chrome, area: Rect) -> Terminal<TestBackend> {
    chrome.compute_view(ws, area);
    let mut hits = None;
    let term = render(area.width, area.height, |frame| {
        hits = Some(render_workspace(frame, ws, chrome));
    });
    chrome.view.apply_hits(hits.expect("frame drawn"));
    term
}

fn mouse(kind: MouseEventKind, column: u16, row: u16, modifiers: KeyModifiers) -> MouseEvent {
    MouseEvent {
        kind,
        column,
        row,
        modifiers,
    }
}

/// A cell inside tab `index` as last drawn.
fn tab_cell(chrome: &Chrome, index: usize) -> (u16, u16) {
    let (_, rect) = chrome
        .view
        .tab_hit_areas
        .iter()
        .find(|(drawn, _)| *drawn == index)
        .expect("tab drawn");
    (rect.x + 1, rect.y)
}

fn titles(chrome: &Chrome) -> Vec<&str> {
    chrome
        .tabs()
        .tabs
        .iter()
        .map(|tab| tab.title.as_str())
        .collect()
}

/// The pane `tab_bar_chrome` opened for the `index`th title.
fn pane(ws: &Workspace, index: usize) -> PaneId {
    ws.pane_for_terminal(&format!("term-{index}"))
        .expect("terminal pane")
}

#[test]
fn tab_bar_clicks_activate_spawn_and_scroll() {
    let (ws, mut chrome, _) = tab_bar_chrome(80, &["alpha", "beta", "gamma"]);
    let (col, row) = tab_cell(&chrome, 1);

    // 2.2.1: a click activates the tab and focuses its pane.
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, col, row, KeyModifiers::NONE)
        ),
        MouseOutcome::Focus {
            pane: pane(&ws, 1),
            observe_only: false
        }
    );
    assert_eq!(chrome.active_index(), 1);
    assert_eq!(
        chrome.gesture,
        Some(MouseGesture::TabDrag {
            index: 1,
            origin_col: col,
            moved: false
        })
    );
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_UP, col, row, KeyModifiers::NONE)
        ),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.gesture, None, "the release ends the click");
    assert_eq!(chrome.active_index(), 1, "a plain click keeps the tab");

    // Alt observes the tab's pane without taking control.
    let (col, _) = tab_cell(&chrome, 0);
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, col, row, KeyModifiers::ALT)
        ),
        MouseOutcome::Focus {
            pane: pane(&ws, 0),
            observe_only: true
        }
    );
    route_mouse(
        &ws,
        &mut chrome,
        &mouse(LEFT_UP, col, row, KeyModifiers::NONE),
    );

    // The new-tab button spawns into a fresh tab.
    let plus = chrome.view.new_tab_hit_area.expect("new-tab button drawn");
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, plus.x + 1, plus.y, KeyModifiers::NONE)
        ),
        MouseOutcome::Spawn {
            placement: Placement::Tab
        }
    );
    assert_eq!(chrome.gesture, None, "the button starts no drag");

    // herdr `wheel_over_tab_bar_switches_tabs`: down = next, up = previous,
    // wrapping at both ends.
    let bar = chrome.view.tab_bar_rect.expect("tab bar drawn");
    let wheel = |chrome: &mut Chrome, kind, col| {
        route_mouse(&ws, chrome, &mouse(kind, col, bar.y, KeyModifiers::NONE))
    };
    assert_eq!(
        wheel(&mut chrome, MouseEventKind::ScrollDown, bar.x + 1),
        MouseOutcome::Focus {
            pane: pane(&ws, 1),
            observe_only: false
        }
    );
    assert_eq!(chrome.active_index(), 1);
    wheel(&mut chrome, MouseEventKind::ScrollUp, bar.x + 1);
    assert_eq!(chrome.active_index(), 0);
    wheel(&mut chrome, MouseEventKind::ScrollUp, bar.x + 1);
    assert_eq!(
        chrome.active_index(),
        2,
        "up from the first tab wraps to the last"
    );
    wheel(
        &mut chrome,
        MouseEventKind::ScrollDown,
        bar.x + bar.width - 1,
    );
    assert_eq!(
        chrome.active_index(),
        0,
        "down from the last tab wraps to the first"
    );

    // Scroll arrows move `tab_scroll` by one and stop following the active
    // tab; the next tab click follows it again.
    let (ws, mut chrome, area) = tab_bar_chrome(
        54,
        &[
            "the first long title",
            "the second long title",
            "the third long title",
            "the fourth long title",
        ],
    );
    let right = chrome
        .view
        .tab_scroll_right_hit_area
        .expect("overflowing tabs draw the scroll-right arrow");
    let left = chrome
        .view
        .tab_scroll_left_hit_area
        .expect("overflowing tabs draw the scroll-left arrow");
    assert!(chrome.tab_scroll_follow_active);
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, right.x + 1, right.y, KeyModifiers::NONE)
        ),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.tab_scroll, 1);
    assert!(!chrome.tab_scroll_follow_active);
    draw_with_hits(&ws, &mut chrome, area);
    assert_eq!(
        chrome.view.tab_hit_areas.first().map(|(index, _)| *index),
        Some(1),
        "the bar scrolled one tab"
    );
    for _ in 0..2 {
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, left.x + 1, left.y, KeyModifiers::NONE),
        );
    }
    assert_eq!(
        chrome.tab_scroll, 0,
        "scrolling left stops at the first tab"
    );
    let (col, row) = tab_cell(&chrome, 1);
    route_mouse(
        &ws,
        &mut chrome,
        &mouse(LEFT_DOWN, col, row, KeyModifiers::NONE),
    );
    assert!(
        chrome.tab_scroll_follow_active,
        "a tab click follows the active tab again"
    );
}

#[test]
fn tab_drag_reorders_or_clicks() {
    let (ws, mut chrome, area) = tab_bar_chrome(80, &["alpha", "beta", "gamma"]);
    let (col, row) = tab_cell(&chrome, 0);
    let (target, _) = tab_cell(&chrome, 2);
    let at = |kind, col| mouse(kind, col, row, KeyModifiers::NONE);

    // 2.2.2: a drag whose release stays short of the threshold is a click.
    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, col));
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &at(LEFT_DRAG, col + TAB_DRAG_THRESHOLD - 1)
        ),
        MouseOutcome::Handled
    );
    assert_eq!(
        chrome.gesture,
        Some(MouseGesture::TabDrag {
            index: 0,
            origin_col: col,
            moved: false
        })
    );
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_UP, col + TAB_DRAG_THRESHOLD - 1)),
        MouseOutcome::Handled
    );
    assert_eq!(titles(&chrome), ["alpha", "beta", "gamma"]);
    assert_eq!(chrome.active_index(), 0);
    assert_eq!(chrome.gesture, None);

    // Past the threshold the tab is dragged and drawn reversed on surface0.
    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, col));
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_DRAG, col + TAB_DRAG_THRESHOLD)),
        MouseOutcome::Handled
    );
    assert_eq!(
        chrome.gesture,
        Some(MouseGesture::TabDrag {
            index: 0,
            origin_col: col,
            moved: true
        })
    );
    let term = draw_with_hits(&ws, &mut chrome, area);
    let style = cell(&term, col, row).style();
    assert_eq!(style.bg, Some(palette().surface0));
    assert!(style.add_modifier.contains(Modifier::REVERSED));
    let (other, _) = tab_cell(&chrome, 1);
    assert!(!cell(&term, other, row)
        .style()
        .add_modifier
        .contains(Modifier::REVERSED));

    // Dropped on another tab it takes that tab's place and stays active.
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_UP, target)),
        MouseOutcome::Handled
    );
    assert_eq!(titles(&chrome), ["beta", "gamma", "alpha"]);
    assert_eq!(chrome.active_index(), 2);
    assert_eq!(chrome.focused_pane(), Some(pane(&ws, 0)));
    assert_eq!(chrome.gesture, None);

    // And back: the last tab dropped on the first slides the others right.
    draw_with_hits(&ws, &mut chrome, area);
    let (first, _) = tab_cell(&chrome, 0);
    let (last, _) = tab_cell(&chrome, 2);
    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, last));
    route_mouse(&ws, &mut chrome, &at(LEFT_DRAG, first));
    route_mouse(&ws, &mut chrome, &at(LEFT_UP, first));
    assert_eq!(titles(&chrome), ["alpha", "beta", "gamma"]);
    assert_eq!(chrome.active_index(), 0);

    // A moved tab released off every tab stays where it was.
    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, first));
    route_mouse(&ws, &mut chrome, &at(LEFT_DRAG, last));
    let bar = chrome.view.tab_bar_rect.expect("tab bar drawn");
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_UP, bar.x + bar.width - 1)),
        MouseOutcome::Handled
    );
    assert_eq!(titles(&chrome), ["alpha", "beta", "gamma"]);
    assert_eq!(chrome.gesture, None);
}

#[test]
fn tab_drag_reorders_when_terminal_coalesces_motion_events() {
    let (ws, mut chrome, _) = tab_bar_chrome(80, &["alpha", "beta", "gamma"]);
    let (from, row) = tab_cell(&chrome, 0);
    let (to, _) = tab_cell(&chrome, 2);
    route_mouse(
        &ws,
        &mut chrome,
        &mouse(LEFT_DOWN, from, row, KeyModifiers::NONE),
    );
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_UP, to, row, KeyModifiers::NONE)
        ),
        MouseOutcome::Handled
    );
    assert_eq!(titles(&chrome), ["beta", "gamma", "alpha"]);
}
