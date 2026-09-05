//! herdr `src/ui/panes.rs` (17) keep-set render tests.

use gobby_client::app::{PaneId as AppPaneId, Workspace};
use gobby_client::theme::relative_luminance;
use gobby_client::ui::chrome::{Chrome, Tab};
use gobby_client::ui::pane_layout::{
    apply_pane_chrome, metrics_for, pane_geometry, pane_inner_rect, scrollbar_gutter, PaneId,
    PaneInfo, SplitBorder,
};
use gobby_client::ui::panes::{self, highlight_selection, render_pane_borders, selection_style};
use gobby_client::ui::text::{display_width, display_width_u16};
use gobby_terminal::selection::Selection;
use ratatui::layout::{Direction, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::Borders;
use std::future::Future;

use super::fixtures::{cell, render};
use super::token_map::{palette, theme};

/// herdr `#[tokio::test]` bodies run on a current-thread runtime.
fn block_on(future: impl Future<Output = ()>) {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio runtime")
        .block_on(future);
}

/// herdr `AppState::test_new()`: shared dividers (`pane_gaps: false`),
/// borders and scrollbars on. gclient's default prefs gap panes, so the
/// fixture pins herdr's default; a test that sets `pane_gaps` keeps its own
/// line.
fn chrome() -> Chrome {
    let mut chrome = Chrome::new(theme());
    chrome.prefs.pane_gaps = false;
    chrome
}

/// herdr's border title ignores focus; gclient's focus ring (`FocusRing`,
/// `.impeccable.md`) prefixes the focused pane's title with a marker so
/// focus never rides on colour alone. The fixture reserves the marker's
/// width and strips it, so herdr's title text stays verbatim.
fn pane_border_title(label: &str, pane_width: u16, focused: bool) -> Option<String> {
    if !focused {
        return panes::pane_border_title(label, pane_width, false);
    }
    let marker = format!("{} ", theme().focus_ring().marker);
    let reserved = display_width_u16(&marker);
    let title = panes::pane_border_title(label, pane_width.saturating_add(reserved), true)?;
    Some(title.replacen(&marker, "", 1))
}

/// herdr `Workspace::test_new("test")` as a gclient tab: one slot showing
/// workspace pane `first`. Returns the tab and its root slot.
fn test_tab(first: AppPaneId) -> (Tab, PaneId) {
    let tab = Tab::new("test", first);
    let root = tab.layout.focused();
    (tab, root)
}

/// herdr `Workspace::test_split(direction)`: split the focused slot; the
/// new slot shows `pane` and takes focus, as upstream.
fn test_split(tab: &mut Tab, direction: Direction, pane: AppPaneId) -> PaneId {
    let slot = tab.layout.split_focused(direction);
    tab.slots.insert(slot, pane);
    slot
}

/// herdr `AppState` with one active `Workspace::test_new("test")`: a
/// scripted gclient workspace whose first terminal fills the chrome's only
/// tab. Returns the chrome, the workspace, and the root slot.
fn app_with_workspace() -> (Chrome, Workspace, PaneId) {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("test", "native", "epoch")
        .expect("open terminal");
    let mut chrome = chrome();
    chrome.open_tab(pane, "test");
    let root = chrome.active_tab().expect("tab").layout.focused();
    (chrome, ws, root)
}

/// herdr `TerminalRuntime::test_with_scrollback_bytes(_, rows, _, bytes)`
/// installed on a slot: gclient panes carry the daemon-reported scrollback
/// depth instead of a local grid, so the fixture reports the rows `bytes`
/// pushes above a `rows`-high viewport.
fn install_runtime(ws: &mut Workspace, chrome: &Chrome, slot: PaneId, rows: u16, bytes: &[u8]) {
    let pane = chrome.pane_for_slot(slot).expect("slot shows a pane");
    let lines = bytes.iter().filter(|byte| **byte == b'\n').count() as u32 + 1;
    ws.apply_scroll_applied(pane, 0, lines.saturating_sub(u32::from(rows)));
}

/// herdr `compute_pane_infos(app, runtimes, area, false, cell_size)`:
/// gclient splits it into `pane_geometry` (rects, borders, stable gutter)
/// and the renderer's per-pane scrollbar resolution from live metrics;
/// this glue is the same three calls `render_panes` makes.
fn compute_pane_infos(chrome: &Chrome, ws: &Workspace, area: Rect) -> Vec<PaneInfo> {
    let tab = chrome.active_tab().expect("active tab");
    let (mut infos, _) = pane_geometry(tab, area, &chrome.prefs);
    for info in &mut infos {
        let pane = ws.pane(tab.slots[&info.id]);
        let metrics = metrics_for(pane.scroll_offset, pane.max_scroll, info.inner_rect.height);
        info.scrollbar_rect = scrollbar_gutter(
            pane_inner_rect(info.rect, info.borders),
            chrome.prefs.pane_scrollbars,
            metrics,
        );
    }
    infos
}

/// herdr `Selection::range(pane, row, start_col, end_col, None)`: the same
/// dragging selection through `gobby_terminal`'s public anchor + drag pair
/// (`range` is crate-private there).
fn selection_range(pane_id: PaneId, row: u16, start_col: u16, end_col: u16) -> Selection {
    let mut selection = Selection::anchor(pane_id, row, start_col, None);
    selection.drag(end_col, row, Rect::new(0, 0, end_col + 1, row + 1), None);
    assert!(selection.is_visible(), "a drag past the anchor is visible");
    selection
}

/// A pane info with only the fields herdr's border tests set.
fn pane_info(id: u32, rect: Rect, borders: Borders, is_focused: bool) -> PaneInfo {
    PaneInfo {
        id: PaneId::from_raw(id),
        rect,
        inner_rect: Rect::default(),
        scrollbar_rect: None,
        borders,
        is_focused,
    }
}

parity_tests! {
    "src/ui/panes.rs" => {
        fn pane_border_title_trims_and_truncates() {
            assert_eq!(
                pane_border_title(" claude ", 20, false).as_deref(),
                Some(" claude ")
            );
            assert_eq!(
                pane_border_title(" claude ", 20, true).as_deref(),
                Some(" claude ")
            );
            assert_eq!(pane_border_title("", 20, false), None);
            assert_eq!(
                pane_border_title("abcdef", 8, false).as_deref(),
                Some(" abc… ")
            );
            assert_eq!(
                pane_border_title("abcdef", 8, true).as_deref(),
                Some(" abc… ")
            );
            assert_eq!(pane_border_title("abcdef", 4, false), None);
        }

        fn pane_border_title_truncates_cjk_by_display_width() {
            let title = pane_border_title("1 模块组织（已定）", 12, false).unwrap();

            assert_eq!(title, " 1 模块… ");
            assert!(display_width(title.as_str()) <= 10);
        }

        fn pane_border_renderer_places_adjacent_cjk_by_display_width() {
            let chrome = chrome();
            let pane_infos = vec![pane_info(1, Rect::new(0, 0, 12, 3), Borders::ALL, false)];
            // herdr labels the terminal (`set_manual_label`); gclient's
            // renderer hands each pane's title in beside its info.
            let titles = vec![Some("1 模块组织（已定）".to_string())];

            let terminal = render(12, 3, |frame| {
                render_pane_borders(&chrome, &pane_infos, &[], &titles, frame)
            });

            assert_eq!(cell(&terminal, 4, 0).symbol(), "模");
            assert_eq!(cell(&terminal, 5, 0).symbol(), " ");
            assert_eq!(cell(&terminal, 6, 0).symbol(), "块");
        }

        fn default_horizontal_split_uses_one_shared_divider_column() {
            let (mut tab, root) = test_tab(AppPaneId(1));
            let right = test_split(&mut tab, Direction::Horizontal, AppPaneId(2));
            tab.layout.focus_pane(root);

            let infos = apply_pane_chrome(tab.layout.panes(Rect::new(0, 0, 100, 20)), true, false);
            let left = infos.iter().find(|info| info.id == root).unwrap();
            let right = infos.iter().find(|info| info.id == right).unwrap();

            assert_eq!(left.rect.x + left.rect.width, right.rect.x);
            assert!(!left.borders.contains(Borders::RIGHT));
            assert!(right.borders.contains(Borders::LEFT));
        }

        fn default_vertical_split_uses_one_shared_divider_row() {
            let (mut tab, root) = test_tab(AppPaneId(1));
            let bottom = test_split(&mut tab, Direction::Vertical, AppPaneId(2));
            tab.layout.focus_pane(root);

            let infos = apply_pane_chrome(tab.layout.panes(Rect::new(0, 0, 100, 20)), true, false);
            let top = infos.iter().find(|info| info.id == root).unwrap();
            let bottom = infos.iter().find(|info| info.id == bottom).unwrap();

            assert_eq!(top.rect.y + top.rect.height, bottom.rect.y);
            assert!(!top.borders.contains(Borders::BOTTOM));
            assert!(bottom.borders.contains(Borders::TOP));
        }

        fn pane_gaps_keep_independent_bordered_panes() {
            let (mut tab, root) = test_tab(AppPaneId(1));
            let right = test_split(&mut tab, Direction::Horizontal, AppPaneId(2));
            tab.layout.focus_pane(root);

            let infos = apply_pane_chrome(tab.layout.panes(Rect::new(0, 0, 100, 20)), true, true);
            let left = infos.iter().find(|info| info.id == root).unwrap();
            let right = infos.iter().find(|info| info.id == right).unwrap();

            assert_eq!(left.rect.x + left.rect.width, right.rect.x);
            assert_eq!(left.borders, Borders::ALL);
            assert_eq!(right.borders, Borders::ALL);
        }

        fn borderless_pane_gaps_add_one_empty_cell_between_panes() {
            let (mut tab, root) = test_tab(AppPaneId(1));
            let right = test_split(&mut tab, Direction::Horizontal, AppPaneId(2));
            tab.layout.focus_pane(root);

            let infos = apply_pane_chrome(tab.layout.panes(Rect::new(0, 0, 100, 20)), false, true);
            let left = infos.iter().find(|info| info.id == root).unwrap();
            let right = infos.iter().find(|info| info.id == right).unwrap();

            assert_eq!(left.rect, Rect::new(0, 0, 49, 20));
            assert_eq!(right.rect, Rect::new(50, 0, 50, 20));
            assert!(left.borders.is_empty());
            assert!(right.borders.is_empty());
        }

        fn disabled_pane_borders_make_inner_rect_equal_visual_rect() {
            let (mut tab, _root) = test_tab(AppPaneId(1));
            test_split(&mut tab, Direction::Horizontal, AppPaneId(2));

            let infos = apply_pane_chrome(tab.layout.panes(Rect::new(0, 0, 100, 20)), false, false);

            for info in infos {
                assert!(info.borders.is_empty());
                assert_eq!(pane_inner_rect(info.rect, info.borders), info.rect);
            }
        }

        fn global_pane_border_renderer_composes_junctions_and_focus_style() {
            let chrome = chrome();
            let pane_infos = vec![
                pane_info(1, Rect::new(0, 0, 2, 2), Borders::TOP | Borders::LEFT, true),
                pane_info(
                    2,
                    Rect::new(2, 0, 2, 2),
                    Borders::TOP | Borders::LEFT | Borders::RIGHT,
                    false,
                ),
                pane_info(
                    3,
                    Rect::new(0, 2, 2, 2),
                    Borders::TOP | Borders::LEFT | Borders::BOTTOM,
                    false,
                ),
                pane_info(4, Rect::new(2, 2, 2, 2), Borders::ALL, false),
            ];
            let split_borders = vec![
                SplitBorder {
                    pos: 2,
                    direction: Direction::Horizontal,
                    ratio: 0.5,
                    area: Rect::new(0, 0, 4, 4),
                    path: vec![],
                },
                SplitBorder {
                    pos: 2,
                    direction: Direction::Vertical,
                    ratio: 0.5,
                    area: Rect::new(0, 0, 4, 4),
                    path: vec![false],
                },
            ];
            let titles: Vec<Option<String>> = vec![None; 4];

            let terminal = render(4, 4, |frame| {
                render_pane_borders(&chrome, &pane_infos, &split_borders, &titles, frame)
            });

            assert_eq!(cell(&terminal, 2, 2).symbol(), "┼");
            assert_eq!(cell(&terminal, 2, 2).style().fg, Some(palette().accent));
            assert_eq!(cell(&terminal, 2, 1).symbol(), "│");
            assert_eq!(cell(&terminal, 2, 1).style().fg, Some(palette().accent));
        }

        fn gapped_pane_focus_does_not_color_neighbor_border() {
            let mut chrome = chrome();
            chrome.prefs.pane_gaps = true;
            let pane_infos = vec![
                pane_info(1, Rect::new(0, 0, 2, 3), Borders::ALL, true),
                pane_info(2, Rect::new(2, 0, 2, 3), Borders::ALL, false),
            ];
            let titles: Vec<Option<String>> = vec![None; 2];

            let terminal = render(4, 3, |frame| {
                render_pane_borders(&chrome, &pane_infos, &[], &titles, frame)
            });

            assert_eq!(cell(&terminal, 1, 1).style().fg, Some(palette().accent));
            assert_eq!(cell(&terminal, 2, 1).style().fg, Some(palette().overlay0));
        }

        fn pane_scrollbar_gutter_is_reserved_before_scrollback_exists() {
            block_on(async {
                let (chrome, mut ws, root_pane) = app_with_workspace();
                install_runtime(&mut ws, &chrome, root_pane, 8, b"ready\n");

                let area = Rect::new(10, 3, 40, 8);
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, None);
                assert_eq!(info.inner_rect, Rect::new(10, 3, 39, 8));
            });
        }

        fn zoomed_pane_scrollbar_gutter_is_reserved_before_scrollback_exists() {
            block_on(async {
                let (mut chrome, mut ws, root_pane) = app_with_workspace();
                chrome.active_tab_mut().expect("tab").zoomed = true;
                install_runtime(&mut ws, &chrome, root_pane, 8, b"ready\n");

                let area = Rect::new(10, 3, 40, 8);
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, None);
                assert_eq!(info.inner_rect, Rect::new(10, 3, 39, 8));
            });
        }

        fn zoomed_multi_pane_keeps_border_space() {
            block_on(async {
                let (mut chrome, mut ws, _root_pane) = app_with_workspace();
                let split_pane = ws
                    .open_terminal("split", "native", "epoch")
                    .expect("open terminal");
                let focused_pane = test_split(
                    chrome.active_tab_mut().expect("tab"),
                    Direction::Horizontal,
                    split_pane,
                );
                chrome.active_tab_mut().expect("tab").zoomed = true;
                install_runtime(&mut ws, &chrome, focused_pane, 8, b"ready\n");

                let area = Rect::new(10, 3, 40, 8);
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.id, focused_pane);
                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, None);
                assert_eq!(info.inner_rect, Rect::new(11, 4, 37, 6));
            });
        }

        fn tiny_pane_does_not_reserve_scrollbar_gutter() {
            block_on(async {
                let (chrome, mut ws, root_pane) = app_with_workspace();
                install_runtime(&mut ws, &chrome, root_pane, 8, b"ready\n");

                let area = Rect::new(10, 3, 4, 8);
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, None);
                assert_eq!(info.inner_rect, area);
            });
        }

        fn pane_scrollbar_setting_controls_reserved_column() {
            block_on(async {
                let (mut chrome, mut ws, root_pane) = app_with_workspace();
                install_runtime(
                    &mut ws,
                    &chrome,
                    root_pane,
                    8,
                    b"one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n",
                );

                let area = Rect::new(10, 3, 40, 8);
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, Some(Rect::new(49, 3, 1, 8)));
                assert_eq!(info.inner_rect, Rect::new(10, 3, 39, 8));

                chrome.prefs.pane_scrollbars = false;
                let infos = compute_pane_infos(&chrome, &ws, area);
                let info = &infos[0];

                assert_eq!(info.rect, area);
                assert_eq!(info.scrollbar_rect, None);
                assert_eq!(info.inner_rect, area);
            });
        }

        fn selection_highlight_uses_one_uniform_style() {
            let palette = palette();
            // herdr `automatic_selection_style(&palette, host_theme)` mixes
            // the probed host background in; gclient hosts terminals on its
            // own token map, so the palette alone fixes the style.
            let expected_style = selection_style(&palette);
            let selection = selection_range(PaneId::from_raw(1), 0, 0, 2);

            let terminal = render(4, 1, |frame| {
                let buf = frame.buffer_mut();
                buf[(0, 0)].set_style(
                    Style::default()
                        .fg(Color::Rgb(10, 220, 120))
                        .bg(Color::Black),
                );
                buf[(1, 0)].set_style(
                    Style::default()
                        .fg(Color::Rgb(220, 180, 40))
                        .bg(Color::DarkGray)
                        .add_modifier(Modifier::BOLD),
                );
                buf[(2, 0)].set_style(Style::default().fg(Color::Blue).bg(Color::Reset));
                highlight_selection(
                    frame,
                    &selection,
                    Rect::new(0, 0, 4, 1),
                    metrics_for(0, 0, 1),
                    &palette,
                );
            });

            let first = cell(&terminal, 0, 0).style();
            let second = cell(&terminal, 1, 0).style();
            let third = cell(&terminal, 2, 0).style();

            assert_eq!(first.fg, expected_style.fg);
            assert_eq!(second.fg, expected_style.fg);
            assert_eq!(third.fg, expected_style.fg);
            assert_eq!(first.bg, expected_style.bg);
            assert_eq!(second.bg, expected_style.bg);
            assert_eq!(third.bg, expected_style.bg);
            assert_eq!(first.add_modifier, expected_style.add_modifier);
            assert_eq!(second.add_modifier, expected_style.add_modifier);
            assert_eq!(third.add_modifier, expected_style.add_modifier);
            assert!(!second.add_modifier.contains(Modifier::BOLD));
        }

        fn automatic_selection_background_uses_host_background() {
            // herdr probes the host terminal (`Palette::terminal()` plus a
            // dark `TerminalTheme`); gclient's hosted terminals run on the
            // theme's own terminal theme, whose background is `panel_bg`.
            let host_theme = theme().terminal_theme();
            let host_background = host_theme
                .background
                .expect("gclient's terminal theme sets a background");
            let bg = selection_style(&palette())
                .bg
                .expect("selection style sets a background");

            let Color::Rgb(r, g, b) = bg else {
                panic!("selection background should resolve to rgb");
            };
            assert!(
                relative_luminance((r, g, b))
                    > relative_luminance((host_background.r, host_background.g, host_background.b))
            );
        }
    }
}
