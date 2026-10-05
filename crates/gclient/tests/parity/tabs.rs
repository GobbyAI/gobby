//! herdr `src/ui/tabs.rs` (5) keep-set render tests.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{
    route_mouse, MouseGesture, MouseOutcome, PaneId, Workspace, TAB_DRAG_THRESHOLD,
};
use gobby_client::daemon::{ProjectRow, SidebarRows};
use gobby_client::ui::chrome::Tab;
use gobby_client::ui::chrome_render::render_workspace;
use gobby_client::ui::tabs::{render_tab_bar, tab_display_name, tab_width, TabBarHits};
use gobby_client::ui::text::display_width_u16;
use gobby_client::ui::{Action, Chrome};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use ratatui::Terminal;
use serde_json::json;

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
    let mut ws = Workspace::scripted();
    ws.select_project("project-gobby");
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "project-gobby".into(),
            name: "gobby".into(),
            display_name: "gobby".into(),
            ..ProjectRow::default()
        }],
        ..SidebarRows::default()
    });
    ws.reconcile_subscribe_first().expect("project sidebar");
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
            chrome.tabs_mut().tabs[0].id = "0:0:1".into();
            zoom_tab(&mut chrome, 0);
            let custom_tab = add_tab(&mut chrome, Some("test"));
            zoom_tab(&mut chrome, custom_tab);

            let tab_bar_rect = Rect::new(0, 0, 50, 1);
            let (term, _) = draw_tab_bar(&chrome, tab_bar_rect);

            let row = buffer_row_text(&term, tab_bar_rect, 0);
            assert!(row.contains(" 1: Untitled Z"), "tab row: {row:?}");
            assert!(row.contains(" test Z"), "tab row: {row:?}");
            assert!(!row.contains("gobby"), "no project prefix: {row:?}");
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

            assert_eq!(style.bg, Some(palette().selection), "the selection fill");
            assert!(!style.add_modifier.contains(Modifier::DIM));
            assert!(style.add_modifier.contains(Modifier::BOLD));
        }

        fn zoom_marker_counts_toward_tab_width() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "abcdefgh");
            zoom_tab(&mut chrome, 0);

            let (_, hits) = draw_tab_bar(&chrome, Rect::new(0, 0, 50, 1));
            let expected = tab_width(&["abcdefgh Z".into()], 0);
            assert_eq!(expected, display_width_u16("abcdefgh Z") + 4);
            assert_eq!(hits.tabs[0].1.width, expected);
        }

        fn tab_width_uses_display_width_for_cjk_labels() {
            let mut chrome = chrome_with_one_tab();
            set_custom_name(&mut chrome, 0, "提交 herdr 的反馈");

            let (_, hits) = draw_tab_bar(&chrome, Rect::new(0, 0, 50, 1));
            let expected = tab_width(&["提交 herdr 的反馈".into()], 0);
            assert_eq!(expected, display_width_u16("提交 herdr 的反馈") + 4);
            assert_eq!(hits.tabs[0].1.width, expected);
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

#[test]
fn every_tab_carries_the_rolled_up_state_of_its_agents() {
    let mut ws = Workspace::scripted();
    let first = ws
        .open_terminal("term-first", "native", "epoch")
        .expect("first terminal");
    let second = ws
        .open_terminal("term-second", "native", "epoch")
        .expect("second terminal");
    let third = ws
        .open_terminal("term-third", "native", "epoch")
        .expect("third terminal");
    let mut chrome = Chrome::new(theme());
    chrome.open_tab(first, "");
    chrome.open_tab(second, "");
    chrome.open_tab(third, "");
    chrome.tabs_mut().tabs[0].id = "0:0:1".into();
    chrome.tabs_mut().tabs[1].id = "0:0:2".into();
    chrome.tabs_mut().tabs[2].id = "0:0:3".into();
    chrome.activate_tab(0);

    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "run:first",
                "terminal": {"terminal_id": "term-first", "backend": "native"},
                "lifecycle_status": "running"
            },
            {
                "entry_id": "run:third",
                "terminal": {"terminal_id": "term-third", "backend": "native"},
                "lifecycle_status": "awaiting_input"
            },
            {
                "entry_id": "run:second",
                "terminal": {"terminal_id": "term-second", "backend": "native"},
                "attention": {"attention_id": "att-second", "kind": "actionable"}
            }
        ],
    }));
    ws.reconcile_subscribe_first().expect("attention roster");

    let area = Rect::new(0, 0, 80, 1);
    let mut term = terminal(area.width, area.height);
    let mut hits = TabBarHits::default();
    draw(&mut term, |frame| {
        hits = render_tab_bar(frame, area, &ws, &chrome);
    });
    // Only the bell reaches a tab, the active one included: a working or
    // idle agent draws nothing there, and a thin rule parts adjacent tabs.
    let row = buffer_row_text(&term, area, 0);
    assert!(row.starts_with(" 1: Untitled"), "tab row: {row:?}");
    assert!(row.contains("⍾ 2: Untitled"), "tab row: {row:?}");
    assert!(row.contains(" 3: Untitled"), "tab row: {row:?}");
    assert!(
        !row.contains('▶') && !row.contains('‖') && !row.contains('○'),
        "tab row: {row:?}"
    );
    let rect = hits.tabs[1].1;
    assert_eq!(cell(&term, rect.x + 1, rect.y).symbol(), "⍾");
    assert_eq!(
        cell(&term, rect.x + 1, rect.y).style().fg,
        Some(palette().peach)
    );
    // The same rule parts the last tab from the `+` one column on.
    for idx in 0..3 {
        let rect = hits.tabs[idx].1;
        let rule = cell(&term, rect.right(), rect.y);
        assert_eq!(rule.symbol(), "│", "tab row: {row:?}");
        assert_eq!(rule.style().fg, Some(palette().line));
    }
    let new_tab = hits.new_tab.expect("+ drawn");
    assert_eq!(new_tab.x, hits.tabs[2].1.right() + 1);
    assert_eq!(cell(&term, new_tab.x + 1, new_tab.y).symbol(), "+");

    chrome.activate_tab(1);
    let mut term = terminal(area.width, area.height);
    draw(&mut term, |frame| {
        render_tab_bar(frame, area, &ws, &chrome);
    });
    let row = buffer_row_text(&term, area, 0);
    assert!(
        row.contains("⍾ 2: Untitled"),
        "the active tab keeps it: {row:?}"
    );
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
/// A tab shows whole or hides, and each edge counts the tabs beyond it. The
/// count takes the needs-you glyph and colour when one of them needs you.
#[test]
fn an_edge_count_turns_needs_you_when_a_hidden_tab_does() {
    let mut ws = Workspace::scripted();
    let mut chrome = Chrome::new(theme());
    for index in 0..5 {
        let pane = ws
            .open_terminal(&format!("term-{index}"), "native", "epoch")
            .expect("open scripted terminal");
        chrome.open_tab(pane, "");
    }
    chrome.activate_tab(0);
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:last",
            "terminal": {"terminal_id": "term-4", "backend": "native"},
            "attention": {"attention_id": "att-last", "kind": "actionable"}
        }],
    }));
    ws.reconcile_subscribe_first().expect("attention roster");

    let area = Rect::new(0, 0, 50, 1);
    let draw_bar = |chrome: &Chrome| {
        let mut term = terminal(area.width, area.height);
        let mut hits = TabBarHits::default();
        draw(&mut term, |frame| {
            hits = render_tab_bar(frame, area, &ws, chrome);
        });
        (term, hits)
    };

    let (term, hits) = draw_bar(&chrome);
    let row = buffer_row_text(&term, area, 0);
    let whole: Vec<(usize, Rect)> = vec![(0, Rect::new(0, 0, 15, 1)), (1, Rect::new(16, 0, 15, 1))];
    assert_eq!(hits.tabs, whole, "tab row: {row:?}");
    assert_eq!(hits.scroll_left, None);
    assert_eq!(hits.scroll_right, Some(Rect::new(39, 0, 7, 1)));
    assert!(row.ends_with(" ⍾ 3 › │ +"), "tab row: {row:?}");
    for x in [40, 42, 44] {
        assert_eq!(
            cell(&term, x, 0).style().fg,
            Some(palette().peach),
            "column {x}"
        );
    }

    // Following the needs-you tab to the end: the left edge counts the
    // three before it, none of which needs you.
    chrome.activate_tab(4);
    let (term, hits) = draw_bar(&chrome);
    let row = buffer_row_text(&term, area, 0);
    assert_eq!(hits.scroll_left, Some(Rect::new(0, 0, 5, 1)));
    assert_eq!(hits.scroll_right, None);
    assert!(row.starts_with(" ‹ 3 "), "tab row: {row:?}");
    assert_eq!(cell(&term, 1, 0).style().fg, Some(palette().overlay1));
}

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
fn tab_bar_clicks_activate_new_tab_and_scroll() {
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

    // The new-tab button requests an empty tab.
    let plus = chrome.view.new_tab_hit_area.expect("new-tab button drawn");
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, plus.x + 1, plus.y, KeyModifiers::NONE)
        ),
        MouseOutcome::Action(Action::NewTab)
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

    // An edge count pages the strip one screen and stops following the
    // active tab; the next tab click follows it again. Nothing hides before
    // the first tab, so that edge draws no count.
    let (ws, mut chrome, area) =
        tab_bar_chrome(54, &["one", "two", "three", "four", "five", "six"]);
    assert_eq!(chrome.view.tab_scroll_left_hit_area, None);
    let right = chrome
        .view
        .tab_scroll_right_hit_area
        .expect("tabs hidden on the right draw their count");
    assert!(chrome.tab_scroll_follow_active);
    assert_eq!(
        route_mouse(
            &ws,
            &mut chrome,
            &mouse(LEFT_DOWN, right.x + 1, right.y, KeyModifiers::NONE)
        ),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.tab_scroll, 3, "the first hidden tab leads the page");
    assert!(!chrome.tab_scroll_follow_active);
    draw_with_hits(&ws, &mut chrome, area);
    let shown: Vec<usize> = chrome
        .view
        .tab_hit_areas
        .iter()
        .map(|(index, _)| *index)
        .collect();
    assert_eq!(shown, [3, 4, 5], "the bar paged one screen");
    assert_eq!(
        chrome.view.tab_scroll_right_hit_area, None,
        "nothing hides on the right"
    );
    let left = chrome
        .view
        .tab_scroll_left_hit_area
        .expect("tabs hidden on the left draw their count");
    route_mouse(
        &ws,
        &mut chrome,
        &mouse(LEFT_DOWN, left.x + 1, left.y, KeyModifiers::NONE),
    );
    assert_eq!(chrome.tab_scroll, 0, "the left count pages back a screen");
    draw_with_hits(&ws, &mut chrome, area);
    assert_eq!(chrome.view.tab_scroll_left_hit_area, None);
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

    // Past the threshold the tab is dragged and drawn reversed on the header fill.
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
    assert_eq!(style.bg, Some(palette().band));
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

/// Josh's #22780 check: the ' + ' cells start at the last tab's right edge,
/// so a tab dragged into the gap before '+' (or onto it) moves last.
#[test]
fn tab_drop_past_the_last_tab_moves_it_last() {
    let (ws, mut chrome, area) = tab_bar_chrome(80, &["alpha", "beta", "gamma"]);
    let (from, row) = tab_cell(&chrome, 0);
    let plus = chrome.view.new_tab_hit_area.expect("new-tab button drawn");
    let at = |kind, col| mouse(kind, col, row, KeyModifiers::NONE);

    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, from));
    route_mouse(&ws, &mut chrome, &at(LEFT_DRAG, plus.x));
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_UP, plus.x)),
        MouseOutcome::Handled
    );
    assert_eq!(titles(&chrome), ["beta", "gamma", "alpha"]);
    assert_eq!(chrome.active_index(), 2);
    assert_eq!(chrome.focused_pane(), Some(pane(&ws, 0)));
    assert_eq!(chrome.gesture, None);

    // A daemon tab asks the daemon for the last tab's position instead of
    // reordering locally; the scripted workspace has no model, so the
    // position is the last tab's index.
    draw_with_hits(&ws, &mut chrome, area);
    let plus = chrome.view.new_tab_hit_area.expect("new-tab button drawn");
    for tab in &mut chrome.tabs_mut().tabs {
        tab.id = format!("tab-{}", tab.title);
    }
    let (from, _) = tab_cell(&chrome, 0);
    route_mouse(&ws, &mut chrome, &at(LEFT_DOWN, from));
    route_mouse(&ws, &mut chrome, &at(LEFT_DRAG, plus.x + 1));
    assert_eq!(
        route_mouse(&ws, &mut chrome, &at(LEFT_UP, plus.x + 1)),
        MouseOutcome::MoveTab {
            tab: "tab-beta".to_string(),
            position: 2
        }
    );
    assert_eq!(titles(&chrome), ["beta", "gamma", "alpha"]);
    assert_eq!(chrome.gesture, None);
}
