use super::*;
use crate::app::Workspace;
use crate::ui::hit::SidebarSection;
use crate::ui::settings::TitleScrolling;
use crate::ui::sidebar;
use crate::ui::sidebar_rows::{TICKER_PAUSE, TICKER_STEP};
use crate::ui::text::display_width;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::json;

fn scripted() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
    }));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    let mut chrome = Chrome::dark();
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    (ws, chrome)
}

fn screen(terminal: &Terminal<TestBackend>) -> String {
    terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect()
}

fn draw(ws: &Workspace, chrome: &Chrome, width: u16, height: u16) -> Terminal<TestBackend> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| render_panes(frame, ws, chrome, &mut |_, _, _| {}))
        .unwrap();
    terminal
}

/// The cells of row `y` from `x` up to (not including) `end`.
fn cells(terminal: &Terminal<TestBackend>, y: u16, x: u16, end: u16) -> String {
    let buffer = terminal.backend().buffer();
    (x..end).map(|x| buffer[(x, y)].symbol()).collect()
}

/// The laid-out info of the pane showing `terminal_id`.
fn info_of(ws: &Workspace, chrome: &Chrome, terminal_id: &str) -> PaneInfo {
    let pane = ws.pane_for_terminal(terminal_id).unwrap();
    let slot = chrome.active_tab().unwrap().slot_for(pane).unwrap();
    chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .unwrap()
        .clone()
}

#[test]
fn border_title_marks_focus_and_truncates_to_the_top_edge() {
    assert_eq!(pane_border_title("alpha", 12, false).unwrap(), " alpha ");
    assert_eq!(pane_border_title("alpha", 12, true).unwrap(), " ▸ alpha ");
    assert!(pane_border_title("  ", 12, true).is_none());
    assert!(pane_border_title("alpha", 4, false).is_none());
    let long = pane_border_title("a-very-long-terminal-title", 14, true).unwrap();
    assert!(display_width(&long) <= 14, "{long}");
    assert_eq!(long, " ▸ a-very-… ");
}

#[test]
fn render_panes_draws_titles_and_focus_marker() {
    let (ws, mut chrome) = scripted();
    let area = Rect::new(0, 0, 120, 40);
    chrome.compute_view(&ws, area);
    assert_eq!(chrome.view.pane_infos.len(), 2);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    let mut painted = Vec::new();
    terminal
        .draw(|frame| {
            render_panes(frame, &ws, &chrome, &mut |_, rect, id| {
                painted.push((id, rect))
            });
        })
        .unwrap();
    let text = screen(&terminal);
    assert_eq!(painted.len(), 2);
    assert!(painted
        .iter()
        .all(|(_, rect)| rect.width > 0 && rect.height > 0));
    for needle in [
        "term-alpha",
        "term-beta",
        "gclient",
        "gclient · Focused",
        "▸",
        "┌",
        "┐",
    ] {
        assert!(text.contains(needle), "frame lacks {needle:?}:\n{text}");
    }
    assert!(
        !text.contains("observe"),
        "pane-local control duplicated: {text}"
    );
    assert!(!text.contains('!'));
}

#[test]
fn bottom_metadata_renders_with_and_without_pane_gaps() {
    for pane_gaps in [true, false] {
        let (ws, mut chrome) = scripted();
        chrome.prefs.pane_gaps = pane_gaps;
        chrome.compute_view(&ws, Rect::new(0, 0, 120, 20));
        let terminal = draw(&ws, &chrome, 120, 20);
        for info in &chrome.view.pane_infos {
            let bottom = info.rect.bottom() - 1;
            let corner = info.rect.right() - 1;
            let edge = cells(&terminal, bottom, info.rect.x, corner);
            let expected = if info.is_focused {
                " gclient · Focused "
            } else {
                " gclient "
            };
            assert!(
                edge.ends_with(expected),
                "gaps={pane_gaps}: bottom edge {edge:?}"
            );
        }
    }
}

#[test]
fn stacked_panes_without_gaps_keep_metadata_on_their_own_title_row() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut()
        .set_roster(json!({"epoch": "e1", "seq": 1, "entries": []}));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "tmux", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    let mut chrome = Chrome::dark();
    chrome.open_pane(ws.pane_for_terminal("term-alpha").unwrap(), "alpha");
    chrome.open_pane_below(ws.pane_for_terminal("term-beta").unwrap(), "alpha");
    chrome.prefs.pane_gaps = false;
    chrome.compute_view(&ws, Rect::new(0, 0, 60, 20));
    let terminal = draw(&ws, &chrome, 60, 20);

    // The upper pane shares its bottom line with the lower pane's title, so
    // its own metadata moves up beside its title.
    let upper = info_of(&ws, &chrome, "term-alpha");
    assert!(
        !upper.borders.contains(Borders::BOTTOM),
        "{:?}",
        upper.borders
    );
    let top = cells(&terminal, upper.rect.y, upper.rect.x, upper.rect.right());
    assert!(top.contains("term-alpha"), "{top:?}");
    assert!(top.ends_with("─ tmux ┐"), "{top:?}");

    let lower = info_of(&ws, &chrome, "term-beta");
    let shared = cells(&terminal, lower.rect.y, lower.rect.x, lower.rect.right());
    assert!(!shared.contains("tmux"), "{shared:?}");
    let bottom = cells(
        &terminal,
        lower.rect.bottom() - 1,
        lower.rect.x,
        lower.rect.right(),
    );
    assert!(bottom.ends_with("─ gclient · Focused ┘"), "{bottom:?}");
}

#[test]
fn header_titles_scroll_left_right_or_truncate_on_the_ticker() {
    let label = "0123456789abcdefghijklmnopqrstuvwxyz";
    let (mut ws, mut chrome) = scripted();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    ws.pane_mut(beta).label = Some(label.to_owned());
    chrome.compute_view(&ws, Rect::new(0, 0, 60, 8));
    let info = info_of(&ws, &chrome, "term-beta");
    let budget = title_budget(info.rect.width, info.is_focused, 0);
    assert!(budget >= 4 && budget + 3 < label.len(), "budget {budget}");
    chrome.ticker = (TICKER_PAUSE + 3) * TICKER_STEP;

    let header = |chrome: &Chrome| {
        let terminal = draw(&ws, chrome, 60, 8);
        cells(&terminal, info.rect.y, info.rect.x, info.rect.right())
    };
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert!(header(&chrome).contains(&label[3..3 + budget]));
    chrome.prefs.title_scrolling = TitleScrolling::Right;
    let end = label.len() - 3;
    assert!(header(&chrome).contains(&label[end - budget..end]));
    chrome.prefs.title_scrolling = TitleScrolling::Off;
    let off = header(&chrome);
    assert!(
        off.contains(&format!("{}…", &label[..budget - 1])),
        "{off:?}"
    );

    // Too narrow for a readable window: the header truncates in any mode.
    let meta = pane_metadata(ws.pane(beta), true);
    let mut narrow = info.clone();
    narrow.rect.width = 9;
    narrow.is_focused = true;
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert_eq!(
        header_title(label, &narrow, &meta, &chrome, 0).as_deref(),
        Some(" ▸ 01… ")
    );
}

#[test]
fn wide_header_titles_fill_the_edge_exactly_while_scrolling() {
    let (mut ws, mut chrome) = scripted();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    ws.pane_mut(beta).label = Some("模块组织已定模块组织已定模块组织已定".to_owned());
    chrome.compute_view(&ws, Rect::new(0, 0, 70, 8));
    let info = info_of(&ws, &chrome, "term-beta");
    let corner = info.rect.right() - 1;
    for step in 0..2 * TICKER_PAUSE + 40 {
        chrome.ticker = step * TICKER_STEP;
        let terminal = draw(&ws, &chrome, 70, 8);
        let buffer = terminal.backend().buffer();
        // A wide character cut by either window edge leaves a blank cell, so
        // the closing pad always meets the corner and no rule cell flickers.
        assert_eq!(buffer[(corner, info.rect.y)].symbol(), "┐", "step {step}");
        assert_eq!(
            buffer[(corner - 1, info.rect.y)].symbol(),
            " ",
            "step {step}"
        );
    }
}

#[test]
fn header_titles_share_the_sidebar_period() {
    // The bare-terminal row repeats this label in the narrower sidebar, so
    // its overrun is longer than the header's and sets the shared period.
    let label = "a-long-pane-title-that-overruns-the-sidebar-and-the-header-both";
    let (mut ws, mut chrome) = scripted();
    chrome.sidebar.pinned = true;
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    ws.pane_mut(beta).label = Some(label.to_owned());
    let area = Rect::new(0, 0, 100, 20);
    chrome.compute_view(&ws, area);
    let info = info_of(&ws, &chrome, "term-beta");
    let own = pane_chrome::title_travel(&ws, ws.pane(beta), &info);
    let sessions = chrome.view.sidebar_section_rects[SidebarSection::Sessions.index()];
    let side = sidebar::sessions_title_travel(&ws, &chrome, sessions);
    assert!(own > 0 && side > own, "header {own}, sidebar {side}");
    assert_eq!(chrome.view.title_travel, side);

    let budget = title_budget(info.rect.width, info.is_focused, 0);
    let header = |chrome: &Chrome| {
        let terminal = draw(&ws, chrome, 100, 20);
        cells(&terminal, info.rect.y, info.rect.x, info.rect.right())
    };
    // Past its own period the header is still parked at its tail, waiting
    // for the sidebar row; both jump home together.
    chrome.ticker = (2 * TICKER_PAUSE + own as u64) * TICKER_STEP;
    assert!(header(&chrome).contains(&label[label.len() - budget..]));
    chrome.ticker = (2 * TICKER_PAUSE + side as u64) * TICKER_STEP;
    assert!(header(&chrome).contains(&label[..budget]));
}

#[test]
fn hovered_read_only_metadata_underlines_only_while_actionable() {
    let (mut ws, mut chrome) = scripted();
    chrome.hover = Some(Hit::ControlIndicator);
    chrome.compute_view(&ws, Rect::new(0, 0, 100, 12));
    let info = chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.is_focused)
        .unwrap()
        .clone();
    let focused = chrome.focused_pane().unwrap();
    let underlined = |ws: &Workspace, chrome: &Chrome| {
        let meta = pane_metadata(ws.pane(focused), true);
        let rect = metadata_rect(&info, &meta).unwrap();
        let terminal = draw(ws, chrome, 100, 12);
        let buffer = terminal.backend().buffer();
        let modifier = |x| buffer[(x, rect.y)].modifier;
        (
            meta.text,
            modifier(rect.x).contains(Modifier::UNDERLINED),
            modifier(rect.x + 1).contains(Modifier::UNDERLINED),
        )
    };
    assert_eq!(
        underlined(&ws, &chrome),
        ("gclient · Focused".to_owned(), false, false)
    );
    ws.pane_mut(focused).take_back = true;
    assert_eq!(
        underlined(&ws, &chrome),
        ("gclient · Read-only".to_owned(), false, true)
    );
}

#[test]
fn empty_state_names_the_next_step_without_exclamation() {
    let chrome = Chrome::dark();
    let mut terminal = Terminal::new(TestBackend::new(60, 10)).unwrap();
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();
    let text = screen(&terminal);
    assert!(text.contains("No pane open."));
    assert!(!text.contains('!'));
}
