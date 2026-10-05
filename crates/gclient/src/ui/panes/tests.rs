use super::*;
use crate::app::Workspace;
use crate::ui::keymap::Keymap;
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
fn border_title_pads_and_truncates_to_the_top_edge() {
    assert_eq!(pane_border_title("alpha", 12).unwrap(), " alpha ");
    assert!(pane_border_title("  ", 12).is_none());
    assert!(pane_border_title("alpha", 4).is_none());
    let long = pane_border_title("a-very-long-terminal-title", 14).unwrap();
    assert!(display_width(&long) <= 14, "{long}");
    assert_eq!(long, " a-very-lo… ");
}

#[test]
fn render_panes_draws_titles_and_focus_word() {
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
        "○ term-beta · Focused",
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
fn corners_render_with_and_without_pane_gaps() {
    for pane_gaps in [true, false] {
        let (mut ws, mut chrome) = scripted();
        for terminal_id in ["term-alpha", "term-beta"] {
            let pane_id = ws.pane_for_terminal(terminal_id).unwrap();
            let pane = ws.pane_mut(pane_id);
            pane.label = None;
            pane.command = Some("zsh".to_owned());
            pane.address = Some("0:0:1:2".to_owned());
        }
        chrome.prefs.pane_gaps = pane_gaps;
        chrome.compute_view(&ws, Rect::new(0, 0, 120, 20));
        let terminal = draw(&ws, &chrome, 120, 20);
        for info in &chrome.view.pane_infos {
            let corner = info.rect.right() - 1;
            let top = cells(&terminal, info.rect.y, info.rect.x + 1, corner);
            let title = if info.is_focused {
                " ○ zsh · Focused "
            } else {
                " ○ zsh "
            };
            assert!(top.starts_with(title), "gaps={pane_gaps}: top edge {top:?}");
            // The bottom-left corner stays empty: rule up to the address.
            let bottom = info.rect.bottom() - 1;
            let edge = cells(&terminal, bottom, info.rect.x + 1, corner);
            assert!(
                edge.starts_with('─') && !edge.contains("zsh"),
                "gaps={pane_gaps}: bottom edge {edge:?}"
            );
            assert!(
                edge.ends_with(" 0:0:1:2 "),
                "gaps={pane_gaps}: bottom edge {edge:?}"
            );
        }
    }
}

#[test]
fn stacked_panes_without_gaps_keep_the_upper_address_beside_its_title() {
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

    // The upper pane has no bottom border: its address shares its title edge
    // without taking over the lower pane's top edge.
    let upper = info_of(&ws, &chrome, "term-alpha");
    assert!(
        !upper.borders.contains(Borders::BOTTOM),
        "{:?}",
        upper.borders
    );
    let top = cells(&terminal, upper.rect.y, upper.rect.x, upper.rect.right());
    assert!(top.contains(" ○ term-alpha "), "{top:?}");
    assert!(top.ends_with(" tmux ┐"), "{top:?}");

    let lower = info_of(&ws, &chrome, "term-beta");
    let shared = cells(&terminal, lower.rect.y, lower.rect.x, lower.rect.right());
    assert!(!shared.contains("tmux"), "{shared:?}");
    assert!(shared.contains(" ○ term-beta · Focused "), "{shared:?}");
    let bottom = cells(
        &terminal,
        lower.rect.bottom() - 1,
        lower.rect.x,
        lower.rect.right(),
    );
    assert!(!bottom.contains("term-beta"), "{bottom:?}");
    assert!(bottom.ends_with(" gclient ┘"), "{bottom:?}");
}

#[test]
fn header_titles_scroll_left_right_or_truncate_on_the_ticker() {
    let label = "0123456789abcdefghijklmnopqrstuvwxyz";
    let (mut ws, mut chrome) = scripted();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    ws.pane_mut(beta).label = Some(label.to_owned());
    chrome.compute_view(&ws, Rect::new(0, 0, 60, 8));
    let info = info_of(&ws, &chrome, "term-beta");
    let corners = pane_corners(&ws, &chrome, ws.pane(beta), info.is_focused);
    let title: Vec<char> = corners.title.chars().collect();
    let slice = |range: std::ops::Range<usize>| title[range].iter().collect::<String>();
    let budget = title_budget(info.rect.width, 0);
    assert!(budget >= 4 && budget + 3 < title.len(), "budget {budget}");
    chrome.ticker = (TICKER_PAUSE + 3) * TICKER_STEP;

    let header = |chrome: &Chrome| {
        let terminal = draw(&ws, chrome, 60, 8);
        cells(&terminal, info.rect.y, info.rect.x, info.rect.right())
    };
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert!(header(&chrome).contains(&slice(3..3 + budget)));
    chrome.prefs.title_scrolling = TitleScrolling::Right;
    let end = title.len() - 3;
    assert!(header(&chrome).contains(&slice(end - budget..end)));
    chrome.prefs.title_scrolling = TitleScrolling::Off;
    let off = header(&chrome);
    assert!(
        off.contains(&format!("{}…", slice(0..budget - 1))),
        "{off:?}"
    );

    // Too narrow for a readable window: the header truncates in any mode.
    let mut narrow = info.clone();
    narrow.rect.width = 7;
    narrow.is_focused = true;
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert_eq!(
        header_title(&corners.title, &narrow, &corners, &chrome, 0).as_deref(),
        Some(" ○ … ")
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
    // The agent task title scrolls in the narrower sidebar, so its overrun
    // is longer than the header's and sets the shared period.
    let label = "a-long-pane-title-that-overruns-the-sidebar-and-the-header-both";
    let (mut ws, mut chrome) = scripted();
    chrome.sidebar.pinned = true;
    ws.daemon_mut().set_roster(json!({
        "epoch": "e2",
        "seq": 2,
        "entries": [{
            "entry_id": "run:term-alpha",
            "name": label,
            "task": {"ref": "#1", "title": label},
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "kind": "blocked"
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    chrome.sidebar.machine_filter = Some(sidebar::ALL_MACHINES.to_owned());
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    ws.pane_mut(alpha).label = Some(label.to_owned());
    ws.reconcile_subscribe_first().unwrap();
    let area = Rect::new(0, 0, 100, 20);
    chrome.compute_view(&ws, area);
    let info = info_of(&ws, &chrome, "term-alpha");
    let own = pane_chrome::title_travel(&ws, &chrome, ws.pane(alpha), &info);
    let side = sidebar::title_travel(&ws, &chrome, &chrome.view.sidebar_section_rects);
    assert!(own > 0 && side > own, "header {own}, sidebar {side}");
    assert_eq!(chrome.view.title_travel, side);

    let title: Vec<char> = pane_corners(&ws, &chrome, ws.pane(alpha), info.is_focused)
        .title
        .chars()
        .collect();
    let slice = |range: std::ops::Range<usize>| title[range].iter().collect::<String>();
    let budget = title_budget(info.rect.width, 0);
    let header = |chrome: &Chrome| {
        let terminal = draw(&ws, chrome, 100, 20);
        cells(&terminal, info.rect.y, info.rect.x, info.rect.right())
    };
    // The header rests at its tail, then back at its start while the
    // sidebar row finishes; both set out again together.
    chrome.ticker = (2 * TICKER_PAUSE + own as u64 - 1) * TICKER_STEP;
    assert!(header(&chrome).contains(&slice(title.len() - budget..title.len())));
    chrome.ticker = (2 * TICKER_PAUSE + own as u64) * TICKER_STEP;
    assert!(header(&chrome).contains(&slice(0..budget)));
    let period = (2 * TICKER_PAUSE + side as u64) * TICKER_STEP;
    chrome.ticker = period + (TICKER_PAUSE + 1) * TICKER_STEP;
    assert!(header(&chrome).contains(&slice(1..budget + 1)));
}

#[test]
fn hovered_read_only_title_underlines_only_while_actionable() {
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
        let corners = pane_corners(ws, chrome, ws.pane(focused), true);
        let rect = pane_chrome::title_rect(&info, &corners).unwrap();
        let terminal = draw(ws, chrome, 100, 12);
        let buffer = terminal.backend().buffer();
        let modifier = |x| buffer[(x, rect.y)].modifier;
        (
            corners.title,
            modifier(rect.x).contains(Modifier::UNDERLINED),
            modifier(rect.x + 1).contains(Modifier::UNDERLINED),
        )
    };
    assert_eq!(
        underlined(&ws, &chrome),
        ("○ term-beta · Focused".to_owned(), false, false)
    );
    ws.pane_mut(focused).take_back = true;
    assert_eq!(
        underlined(&ws, &chrome),
        ("○ term-beta · Read-only".to_owned(), false, true)
    );
}

#[test]
fn empty_state_names_the_next_step_without_exclamation() {
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = false;
    let mut terminal = Terminal::new(TestBackend::new(60, 10)).unwrap();
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();
    let text = screen(&terminal);
    assert!(cells(&terminal, 3, 0, 60).contains("No pane open."));
    assert!(cells(&terminal, 4, 0, 60).contains("ctrl+b w  attach a terminal"));
    assert!(cells(&terminal, 5, 0, 60).contains("File › New terminal  start one"));
    assert!(cells(&terminal, 6, 0, 60).contains("ctrl+b b  open the sidebar"));
    assert!((0..3).all(|y| cells(&terminal, y, 0, 60).trim().is_empty()));
    assert!((7..10).all(|y| cells(&terminal, y, 0, 60).trim().is_empty()));
    assert!(!text.contains("select a terminal in the sidebar"));
    assert!(!text.contains('!'));

    // A pinned column is what the same chord takes away.
    chrome.sidebar.pinned = true;
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();
    assert!(cells(&terminal, 6, 0, 60).contains("ctrl+b b  hide the sidebar"));

    // An open overlay is what it rolls up.
    chrome.sidebar.pinned = false;
    chrome.sidebar.overlay = true;
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();
    assert!(cells(&terminal, 6, 0, 60).contains("ctrl+b b  close the sidebar"));
}

#[test]
fn empty_state_chords_follow_the_live_keymap() {
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::from_toml(
        "prefix = \"ctrl+]\"\n[bindings]\nterminal_picker = \"prefix+shift+u\"\ntoggle_sidebar = []\n",
        "ctrl+b",
    )
    .unwrap();
    let mut terminal = Terminal::new(TestBackend::new(80, 10)).unwrap();
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();
    let text = screen(&terminal);
    assert!(text.contains("ctrl+] shift+u  attach a terminal"));
    assert!(text.contains("unset  open the sidebar"));
    assert!(!text.contains("ctrl+b w"));
}

#[test]
fn empty_state_truncates_all_rows_in_a_narrow_area() {
    let chrome = Chrome::dark();
    let mut terminal = Terminal::new(TestBackend::new(8, 10)).unwrap();
    terminal
        .draw(|frame| render_empty(frame, frame.area(), &chrome))
        .unwrap();

    assert_eq!(cells(&terminal, 3, 0, 8), "No pane…");
    assert_eq!(cells(&terminal, 4, 0, 8), "ctrl+b …");
    assert_eq!(cells(&terminal, 5, 0, 8), "File › …");
    assert_eq!(cells(&terminal, 6, 0, 8), "ctrl+b …");
}

/// #23096: the SRT pane's lock draws in the destructive (red-family, hue
/// 350) token in both themes while its address keeps the corner's tone; an
/// unrestricted pane's bottom edge carries no padlock of either shape.
#[test]
fn srt_lock_draws_red_and_unrestricted_addresses_draw_no_mark() {
    use crate::daemon::{ProjectRow, RunRow, RunSandbox, SessionRow, SidebarRows};
    use crate::theme::{Theme, ThemeKind};

    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_sidebar_rows(SidebarRows {
            projects: vec![ProjectRow {
                id: "proj-alpha".to_owned(),
                name: "gobby".to_owned(),
                display_name: "gobby".to_owned(),
                ..ProjectRow::default()
            }],
            sessions: [(
                "proj-alpha".to_owned(),
                vec![
                    SessionRow {
                        id: "sess-run".to_owned(),
                        sandbox_enabled: Some(true),
                        ..SessionRow::default()
                    },
                    SessionRow {
                        id: "sess-seat".to_owned(),
                        ..SessionRow::default()
                    },
                ],
            )]
            .into_iter()
            .collect(),
            runs: [(
                "proj-alpha".to_owned(),
                vec![RunRow {
                    run_id: "run-srt".to_owned(),
                    sandbox: Some(RunSandbox {
                        enforced: Some(true),
                    }),
                    ..RunRow::default()
                }],
            )]
            .into_iter()
            .collect(),
            ..SidebarRows::default()
        });
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [
                {
                    "entry_id": "run:run-srt",
                    "run_id": "run-srt",
                    "session_id": "sess-run",
                    "terminal": {"terminal_id": "term-srt", "backend": "native"}
                },
                {
                    "entry_id": "session:sess-seat",
                    "session_id": "sess-seat",
                    "terminal": {"terminal_id": "term-seat", "backend": "native"}
                }
            ]
        }));
        ws.select_project("proj-alpha");
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-srt", "native", "epoch").unwrap();
        ws.open_terminal("term-seat", "native", "epoch").unwrap();
        let mut chrome = Chrome::new(Theme::new(kind));
        for terminal in ["term-srt", "term-seat"] {
            let pane_id = ws.pane_for_terminal(terminal).unwrap();
            chrome.open_pane(pane_id, "alpha");
            ws.pane_mut(pane_id).address = Some("0:0:1:2".to_owned());
        }
        chrome.compute_view(&ws, Rect::new(0, 0, 120, 20));
        let terminal = draw(&ws, &chrome, 120, 20);
        let buffer = terminal.backend().buffer();

        let srt = info_of(&ws, &chrome, "term-srt");
        let seat = info_of(&ws, &chrome, "term-seat");
        let edge = |info: &PaneInfo| {
            cells(
                &terminal,
                info.rect.bottom() - 1,
                info.rect.x + 1,
                info.rect.right() - 1,
            )
        };
        assert!(
            edge(&srt).ends_with(" \u{f023} · 0:0:1:2 "),
            "{kind:?}: SRT edge {:?}",
            edge(&srt)
        );
        let seat_edge = edge(&seat);
        assert!(
            seat_edge.ends_with("─ 0:0:1:2 ")
                && !seat_edge.contains('\u{f023}')
                && !seat_edge.contains('\u{f09c}'),
            "{kind:?}: unrestricted edge {seat_edge:?}"
        );

        let y = srt.rect.bottom() - 1;
        let lock_x = (srt.rect.x..srt.rect.right())
            .find(|&x| buffer[(x, y)].symbol() == "\u{f023}")
            .unwrap();
        assert_eq!(buffer[(lock_x, y)].fg, chrome.palette.red, "{kind:?} lock");
        let digit = buffer[(srt.rect.right() - 3, y)].clone();
        assert_eq!(digit.symbol(), "2");
        assert_ne!(
            digit.fg, chrome.palette.red,
            "{kind:?}: the address keeps its tone"
        );
    }
}

/// Dark and Light set an unfocused pane on its fill, over the ground and
/// unpainted cells alike, and leave the cells it painted itself; the
/// focused pane, System and a lone pane draw no fill (#23416).
#[test]
fn unfocused_panes_take_the_fill_in_dark_and_light_only() {
    use crate::theme::{Theme, ThemeKind, ThemeName};
    const OWN: Color = Color::Rgb(0x40, 0x10, 0x10);

    // Each pane paints its first cell in its own colour and its second on
    // the ground; the rest it leaves.
    let draw_painted = |ws: &Workspace, chrome: &Chrome| {
        let ground = chrome.palette.panel_bg;
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| {
                render_panes(frame, ws, chrome, &mut |frame, rect, _| {
                    let buffer = frame.buffer_mut();
                    buffer[(rect.x, rect.y)].set_bg(OWN);
                    buffer[(rect.x + 1, rect.y)].set_bg(ground);
                })
            })
            .unwrap();
        terminal.backend().buffer().clone()
    };
    let cells = |buffer: &ratatui::buffer::Buffer, info: &PaneInfo| {
        let rect = info.inner_rect;
        [
            (rect.x, rect.y),
            (rect.x + 1, rect.y),
            (rect.x + 2, rect.y + 1),
        ]
        .map(|at| buffer[at].bg)
    };

    let (ws, mut chrome) = scripted();
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let host = Some((0x1e, 0x1e, 0x2e));
    for theme in [
        Theme::named(ThemeName::Restored, ThemeKind::Dark),
        Theme::named(ThemeName::Moss, ThemeKind::Light),
        Theme::hosted(ThemeName::Restored, ThemeKind::Dark, host),
    ] {
        let case = format!("{:?} hosted={}", theme.kind, theme.hosted);
        chrome.palette = theme.palette();
        chrome.theme = theme;
        let buffer = draw_painted(&ws, &chrome);
        let ground = chrome.palette.panel_bg;
        let focused = info_of(&ws, &chrome, "term-beta");
        let unfocused = info_of(&ws, &chrome, "term-alpha");
        assert!(focused.is_focused && !unfocused.is_focused, "{case}");
        assert_eq!(
            cells(&buffer, &focused),
            [OWN, ground, Color::Reset],
            "{case}: the focused pane"
        );
        let expected = match chrome.palette.unfocused {
            Some(fill) => [OWN, fill, fill],
            None => [OWN, ground, Color::Reset],
        };
        assert_eq!(
            chrome.palette.unfocused.is_none(),
            chrome.theme.hosted,
            "{case}"
        );
        assert_eq!(
            cells(&buffer, &unfocused),
            expected,
            "{case}: the unfocused pane"
        );
        // The fill runs under the scrollbar lane, so no seam of ground.
        if let Some(fill) = chrome.palette.unfocused {
            let body = pane_layout::pane_inner_rect(unfocused.rect, unfocused.borders);
            assert!(
                body.right() > unfocused.inner_rect.right(),
                "{case}: no lane"
            );
            for y in body.top()..body.bottom() {
                let lane = &buffer[(body.right() - 1, y)];
                assert_eq!(lane.bg, fill, "{case}: lane row {y}");
            }
        }
    }

    // A lone pane is never unfocused.
    let mut ws = Workspace::scripted();
    ws.daemon_mut()
        .set_roster(json!({"epoch": "e1", "seq": 1, "entries": []}));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    let mut chrome = Chrome::dark();
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    chrome.open_pane(alpha, "alpha");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let buffer = draw_painted(&ws, &chrome);
    let lone = info_of(&ws, &chrome, "term-alpha");
    assert_eq!(
        cells(&buffer, &lone),
        [OWN, chrome.palette.panel_bg, Color::Reset],
        "a lone pane"
    );
}
