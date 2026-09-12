//! herdr `src/ui.rs` (31), `src/ui/keybind_help.rs` (2), `src/ui/tab_surface.rs`
//! (2), and `src/ui/text.rs` (2) keep-set render tests.

use std::borrow::Cow;

use gobby_client::app::{PaneId, Workspace};
use gobby_client::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus};
use gobby_client::ui::chrome::{Chrome, Mode};
use gobby_client::ui::chrome_render::{
    copy_feedback_offset_for_toast, render_workspace, render_workspace_with,
};
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::hit::SidebarSection;
use gobby_client::ui::keybind_help::{filter_help_entries, help_lines};
use gobby_client::ui::keymap::{HelpEntry, Keymap, HERDR_PREFIX};
use gobby_client::ui::pane_layout;
use gobby_client::ui::scrollbar::{
    pane_scrollbar_rect, scrollbar_offset_from_drag_row, scrollbar_offset_from_row,
    scrollbar_thumb, scrollbar_thumb_grab_offset, should_show_scrollbar,
};
use gobby_client::ui::settings::{SettingsRow, SETTINGS_POPUP_HEIGHT, SETTINGS_POPUP_WIDTH};
use gobby_client::ui::sidebar::{collapsed_sections, section_body_rect};
use gobby_client::ui::status::{
    render_copy_feedback, render_status_line, render_toast_notification, toast_notification_rect,
    Toast, ToastKind,
};
use gobby_client::ui::tab_surface::render_tab_surface;
use gobby_client::ui::tabs::{render_tab_bar, tab_display_name, TabBarHits};
use gobby_client::ui::text::{display_width, middle_elide, truncate_end};
use gobby_client::ui::widgets::centered_popup_rect;
use gobby_terminal::layout::{self, PaneInfo, ScrollMetrics};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::widgets::{Borders, Paragraph};
use ratatui::{Frame, Terminal};
use serde_json::json;
use sha2::{Digest, Sha256};

use super::fixtures::{cell, rect_rows, render, screen};
use super::token_map::{palette, theme};

/// gclient reserves the bottom row of the content column for its status line
/// (control state, focused terminal, mode); herdr had no such row, so every
/// herdr terminal-area height below is one row taller than gclient's.
const STATUS_ROWS: u16 = 1;

/// gclient names the mode in its lowercase status line; herdr drew an
/// uppercase `PREFIX` badge in the tab-bar row or as a terminal overlay.
const PREFIX_INDICATOR: &str = "prefix";

/// gclient marks the selected roster row with `▸` (a non-colour cue); herdr's
/// selected workspace card left that column blank.
const SELECTED_MARK: &str = "▸";

/// gclient gives every roster state a distinct glyph: an attached, quiet
/// terminal shows the idle `○`; herdr's fresh workspace showed `·`.
const IDLE_DOT: &str = "○";

/// gclient joins multiple chords with `", "`; herdr's help used `" / "`.
const CHORD_SEPARATOR: &str = ", ";

/// herdr `PRODUCT_ANNOUNCEMENT_MODAL_SIZE`. Release notes are dropped, so the
/// announcement stands in as gclient's respond dialog (64x10 with no options).
const ANNOUNCEMENT_MODAL_SIZE: (u16, u16) = (64, 10);
/// The respond dialog's free text sits at inner row 2 (`popup.y + 3`); herdr's
/// announcement title sat at `popup.y + 1`.
const ANNOUNCEMENT_TITLE_ROW: u16 = 3;

/// herdr `ToastNotification { kind: Finished, title, context }`: gclient's
/// `Success` kind with the context as the body.
fn finished_toast(title: &str, context: &str) -> Toast {
    Toast {
        kind: ToastKind::Success,
        title: title.to_string(),
        body: Some(context.to_string()),
        target: None,
    }
}

/// herdr `Workspace::test_new(name)` per name: a roster terminal attached to
/// one pane each.
fn scripted(names: &[&str]) -> Workspace {
    let mut ws = Workspace::scripted();
    for name in names {
        ws.open_terminal(name, "native", "epoch")
            .expect("open scripted terminal");
    }
    ws
}

/// herdr `Workspace::test_new(name)` carried a git checkout: one project per
/// scripted terminal, named after it and checked out on `main`, the first
/// one focused.
fn with_projects(mut ws: Workspace) -> Workspace {
    let names = ws.roster_terminal_ids();
    let rows = SidebarRows {
        projects: names
            .iter()
            .map(|name| ProjectRow {
                id: format!("proj-{name}"),
                name: name.clone(),
                display_name: name.clone(),
                checkout: Some(Checkout {
                    machine_id: "local".to_string(),
                    root_path: format!("/repos/{name}"),
                }),
                ..ProjectRow::default()
            })
            .collect(),
        statuses: names
            .iter()
            .map(|name| {
                (
                    format!("proj-{name}"),
                    SourceStatus {
                        current_branch: Some("main".to_string()),
                        ..SourceStatus::default()
                    },
                )
            })
            .collect(),
        ..SidebarRows::default()
    };
    ws.daemon_mut().set_sidebar_rows(rows);
    if let Some(first) = names.first() {
        ws.select_project(format!("proj-{first}"));
    }
    ws.reconcile_subscribe_first().expect("install projects");
    ws
}

/// herdr `AppState::test_new()` with `active` pointing at `terminal`: one
/// auto-named tab showing that terminal's pane.
fn chrome_for(ws: &Workspace, terminal: &str) -> Chrome {
    let mut chrome = Chrome::new(theme());
    let pane = ws.pane_for_terminal(terminal).expect("terminal pane");
    chrome.open_tab(pane, "");
    chrome
}

/// herdr `Workspace::test_add_tab(Some(name))`: a new tab over the same pane.
fn add_tab(chrome: &mut Chrome, name: &str) -> usize {
    let pane = chrome.focused_pane().expect("focused pane");
    chrome.open_tab(pane, name);
    chrome.tabs().tabs.len() - 1
}

/// herdr `buffer_row_text`: one row of `area`, trailing spaces trimmed.
fn buffer_row_text(terminal: &Terminal<TestBackend>, area: Rect, row: u16) -> String {
    rect_rows(terminal, Rect::new(area.x, row, area.width, 1))
        .pop()
        .unwrap_or_default()
        .trim_end()
        .to_string()
}

/// Draw the whole frame the way the run loop does.
fn render_full(ws: &Workspace, chrome: &Chrome, area: Rect) -> Terminal<TestBackend> {
    render(area.width, area.height, |frame| {
        render_workspace(frame, ws, chrome);
    })
}

/// herdr read the mode badge from the tab-bar row or the last terminal row;
/// gclient shows the mode in the status line below the terminal area.
fn mode_row(chrome: &Chrome) -> Rect {
    chrome.view.status_rect
}

/// herdr anchored these toasts `TopLeft` and read `x`/`y`; gclient pins every
/// toast bottom-right, so the anchored corner's distance from the frame's
/// corner stands in for herdr's `x`/`y`.
fn toast_anchor_offset(hit: Rect, frame: Rect) -> (u16, u16) {
    (frame.right() - hit.right(), frame.bottom() - hit.bottom())
}

/// Toast hit area as the run loop stores it: the rect the renderer drew.
fn toast_hit_area(ws: &Workspace, chrome: &Chrome, area: Rect) -> Rect {
    let mut hit = None;
    render(area.width, area.height, |frame| {
        render_workspace(frame, ws, chrome);
        hit = render_toast_notification(frame, frame.area(), chrome);
    });
    hit.expect("toast drawn")
}

/// Bounding box of every cell drawn in `fg`: the copy-feedback box has a
/// `green` border, so this recovers herdr's `copy_feedback_rect`.
fn drawn_rect(terminal: &Terminal<TestBackend>, fg: Color) -> Rect {
    let buffer = terminal.backend().buffer();
    let mut bounds: Option<(u16, u16, u16, u16)> = None;
    for y in 0..buffer.area.height {
        for x in 0..buffer.area.width {
            if buffer[(x, y)].fg != fg {
                continue;
            }
            bounds = Some(match bounds {
                None => (x, y, x, y),
                Some((x0, y0, x1, y1)) => (x0.min(x), y0.min(y), x1.max(x), y1.max(y)),
            });
        }
    }
    let (x0, y0, x1, y1) = bounds.expect("cells drawn in the given colour");
    Rect::new(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
}

/// herdr `ViewState` tab geometry rebuilt from gclient's `TabBarHits`: gclient
/// stores only visible tabs, herdr kept one rect per tab (zero-width when
/// scrolled out) and an empty rect for each absent control.
struct TabView {
    tab_hit_areas: Vec<Rect>,
    tab_scroll_left_hit_area: Rect,
    tab_scroll_right_hit_area: Rect,
    new_tab_hit_area: Rect,
}

fn tab_view(ws: &Workspace, chrome: &Chrome, area: Rect) -> TabView {
    let Some(bar) = chrome.view.tab_bar_rect else {
        return TabView {
            tab_hit_areas: Vec::new(),
            tab_scroll_left_hit_area: Rect::default(),
            tab_scroll_right_hit_area: Rect::default(),
            new_tab_hit_area: Rect::default(),
        };
    };
    let mut hits = TabBarHits::default();
    render(area.width, area.height, |frame| {
        hits = render_tab_bar(frame, bar, ws, chrome);
    });
    let mut tab_hit_areas = vec![Rect::default(); chrome.tabs().tabs.len()];
    for (idx, rect) in &hits.tabs {
        tab_hit_areas[*idx] = *rect;
    }
    TabView {
        tab_hit_areas,
        tab_scroll_left_hit_area: hits.scroll_left.unwrap_or_default(),
        tab_scroll_right_hit_area: hits.scroll_right.unwrap_or_default(),
        new_tab_hit_area: hits.new_tab.unwrap_or_default(),
    }
}

/// herdr `TerminalRuntime::current_size()` for a background tab: the rows and
/// columns its pane would be given in this chrome's terminal area.
fn runtime_size(chrome: &Chrome, tab_idx: usize) -> (u16, u16) {
    let (infos, _) = pane_layout::pane_geometry(
        &chrome.tabs().tabs[tab_idx],
        chrome.view.terminal_area,
        &chrome.prefs,
    );
    let inner = infos.first().expect("pane info").inner_rect;
    (inner.height, inner.width)
}

/// herdr `frame_digest` over gclient's rendered cells: symbol, colours, and
/// modifiers of every cell, row-major.
fn frame_digest(terminal: &Terminal<TestBackend>) -> String {
    let mut hasher = Sha256::new();
    for cell in terminal.backend().buffer().content() {
        hasher.update(cell.symbol().as_bytes());
        hasher.update(format!("{:?}{:?}{:?}", cell.fg, cell.bg, cell.modifier).as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

/// herdr help label for a gclient action: herdr's workspace/agent nouns are
/// gclient's terminal/attention nouns (UPSTREAM.md keymap provenance).
fn herdr_help_label(name: &str) -> Option<&'static str> {
    Some(match name {
        "previous_terminal" => "previous workspace",
        "next_terminal" => "next workspace",
        "previous_attention" => "previous agent",
        "next_attention" => "next agent",
        "focus_attention" => "focus agent 1-9",
        "switch_project" => "switch workspace 1-9",
        "switch_tab" => "switch tab 1-9",
        "focus_pane_left" => "focus pane left",
        "focus_pane_down" => "focus pane down",
        "focus_pane_up" => "focus pane up",
        "focus_pane_right" => "focus pane right",
        _ => return None,
    })
}

type HelpRow = (String, Cow<'static, str>);

/// herdr `keybind_help_groups`: gclient's flat help table regrouped under
/// herdr's headings by action name. gclient has no custom commands
/// (`custom_command` is reserved and hidden), so that group is always empty.
fn keybind_help_groups(chrome: &Chrome) -> Vec<(&'static str, Vec<HelpRow>)> {
    const GROUPS: &[(&str, &[&str])] = &[
        (
            "workspaces / tabs",
            &[
                "new_terminal",
                "new_project",
                "rename_terminal",
                "close_terminal",
                "previous_terminal",
                "next_terminal",
                "previous_project",
                "next_project",
                "previous_attention",
                "next_attention",
                "focus_attention",
                "switch_project",
                "toggle_group",
                "new_tab",
                "rename_tab",
                "previous_tab",
                "next_tab",
                "switch_tab",
                "close_tab",
            ],
        ),
        (
            "panes",
            &[
                "focus_pane_left",
                "focus_pane_down",
                "focus_pane_up",
                "focus_pane_right",
                "split_vertical",
                "split_horizontal",
                "close_pane",
                "zoom",
            ],
        ),
        ("custom", &[]),
    ];
    let entries = chrome.keymap.help_entries();
    GROUPS
        .iter()
        .map(|(heading, names)| {
            let rows = entries
                .iter()
                .filter(|entry| names.contains(&entry.name))
                .map(|entry| {
                    let label = herdr_help_label(entry.name).unwrap_or(entry.description);
                    (entry.keys.clone(), Cow::Borrowed(label))
                })
                .collect();
            (*heading, rows)
        })
        .collect()
}

/// herdr `keybind_help_lines(&app)` flattened to text.
fn rendered_help_text(chrome: &Chrome) -> String {
    help_lines(chrome)
        .into_iter()
        .flat_map(|line| line.spans)
        .map(|span| span.content.into_owned())
        .collect::<Vec<_>>()
        .join("")
}

type HelpGroup = (&'static str, Vec<(&'static str, &'static str)>);

fn help_entry(key: &'static str, label: &'static str) -> (&'static str, &'static str) {
    (key, label)
}

/// herdr `filter_keybind_help_groups` over gclient's flat
/// `filter_help_entries`: each group's rows filter as help entries, empty
/// groups drop, and headings never match (gclient has none to match).
fn filter_keybind_help_groups(groups: Vec<HelpGroup>, query: &str) -> Vec<HelpGroup> {
    groups
        .into_iter()
        .filter_map(|(heading, rows)| {
            let entries = rows
                .iter()
                .map(|(key, label)| HelpEntry {
                    name: "",
                    description: label,
                    keys: key.to_string(),
                })
                .collect();
            let kept = filter_help_entries(entries, query);
            let rows: Vec<_> = rows
                .into_iter()
                .filter(|(key, label)| {
                    kept.iter()
                        .any(|entry| entry.keys == *key && entry.description == *label)
                })
                .collect();
            (!rows.is_empty()).then_some((heading, rows))
        })
        .collect()
}

parity_tests! {
    "src/ui.rs" => {
        fn copy_feedback_offset_only_increases_when_toast_rect_overlaps() {
            let area = Rect::new(0, 0, 80, 24);
            let feedback = "copied to clipboard";
            let toast = finished_toast("pi finished", "workspace · 1");
            let chrome = Chrome::new(theme());
            // gclient draws copy feedback bottom-centre only; the rect herdr
            // computed for the `TopCenter` / `BottomCenter` positions is read
            // back from the frame.
            let drawn = render(area.width, area.height, |frame| {
                render_copy_feedback(frame, area, &chrome, feedback);
            });
            let feedback_rect = drawn_rect(&drawn, palette().green);

            let bottom_right_toast = toast_notification_rect(area, &toast).expect("toast rect");
            assert_eq!(
                copy_feedback_offset_for_toast(feedback_rect, 0, bottom_right_toast),
                0
            );

            let bottom_center_toast = Rect::new(28, 21, 24, 3);
            assert_eq!(
                copy_feedback_offset_for_toast(feedback_rect, 0, bottom_center_toast),
                bottom_center_toast.height
            );
        }

        // herdr's `RenameWorkspace` with a pending create cwd is the
        // workspace-creation dialog; gclient's `Dialog::NewProject` (plan
        // 3.3) renders the same `new workspace` title over the path typed.
        fn workspace_creation_dialog_renders_new_workspace_title() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::ProjectDialog;
            chrome.dialog = Some(Dialog::NewProject {
                path: "project".into(),
                cursor: "project".len(),
                error: None,
            });

            let area = Rect::new(0, 0, 80, 20);
            chrome.compute_view(&ws, area);
            let terminal = render_full(&ws, &chrome, area);
            let screen = (0..area.height)
                .map(|row| buffer_row_text(&terminal, area, row))
                .collect::<Vec<_>>()
                .join("\n");

            assert!(screen.contains("new workspace"), "{screen}");
            assert!(screen.contains("project"), "{screen}");
        }

        fn focused_pane_cursor_wins_during_terminal_render() {
            let ws = scripted(&["test", "test-right"]);
            let mut chrome = chrome_for(&ws, "test");
            let first_pane = ws.pane_for_terminal("test").expect("first pane");
            let second_pane = ws.pane_for_terminal("test-right").expect("second pane");
            chrome.open_pane(second_pane, "");
            assert!(chrome.focus_pane(first_pane));
            chrome.mode = Mode::Terminal;

            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));
            let focused = chrome
                .view
                .pane_infos
                .iter()
                .find(|info| chrome.pane_for_slot(info.id) == Some(first_pane))
                .expect("focused pane info")
                .clone();

            // The app shell paints each pane's grid and places the cursor
            // for the focused pane only; herdr's runtimes did that per pane.
            let mut terminal = render(80, 20, |frame| {
                let mut content = |frame: &mut Frame, rect: Rect, id: PaneId| {
                    let text = if id == first_pane { "left" } else { "r\nb" };
                    frame.render_widget(Paragraph::new(text), rect);
                    if chrome.focused_pane() == Some(id) {
                        frame.set_cursor_position((rect.x + 4, rect.y));
                    }
                };
                render_workspace_with(frame, &ws, &chrome, &mut content);
            });

            terminal
                .backend_mut()
                .assert_cursor_position((focused.inner_rect.x + 4, focused.inner_rect.y));
        }

        fn desktop_toast_hit_area_uses_full_frame_not_terminal_area() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::Terminal;
            chrome.toast = Some(finished_toast("pi finished", "one"));

            let area = Rect::new(0, 0, 100, 20);
            chrome.compute_view(&ws, area);
            let hit = toast_hit_area(&ws, &chrome, area);
            let anchor = toast_anchor_offset(hit, area);

            assert!(chrome.view.terminal_area.x > 0);
            assert_eq!(anchor.0, 0);
            assert_eq!(anchor.1, 0);
        }

        fn hide_tab_bar_when_single_tab_toggles_geometry_with_tab_count() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.prefs.hide_tab_bar_when_single_tab = true;
            chrome.mode = Mode::Terminal;
            let area = Rect::new(0, 0, 80, 20);

            chrome.compute_view(&ws, area);
            let single_tab_terminal_area = chrome.view.terminal_area;
            let tabs = tab_view(&ws, &chrome, area);
            assert_eq!(chrome.view.tab_bar_rect, None);
            assert_eq!(
                single_tab_terminal_area,
                Rect::new(26, 0, 54, 20 - STATUS_ROWS)
            );
            assert!(tabs.tab_hit_areas.is_empty());
            assert_eq!(tabs.new_tab_hit_area, Rect::default());

            add_tab(&mut chrome, "logs");
            chrome.compute_view(&ws, area);
            let tabs = tab_view(&ws, &chrome, area);

            assert_eq!(chrome.view.tab_bar_rect, Some(Rect::new(26, 0, 54, 1)));
            assert_eq!(
                chrome.view.terminal_area,
                Rect::new(26, 1, 54, 19 - STATUS_ROWS)
            );
            assert_eq!(tabs.tab_hit_areas.len(), 2);
            assert!(tabs.tab_hit_areas.iter().all(|rect| rect.width > 0));
            assert!(tabs.new_tab_hit_area.width > 0);

            assert!(chrome.close_focused().is_some());
            chrome.compute_view(&ws, area);
            let tabs = tab_view(&ws, &chrome, area);

            assert_eq!(chrome.view.terminal_area, single_tab_terminal_area);
            assert_eq!(chrome.view.tab_bar_rect, None);
            assert!(tabs.tab_hit_areas.is_empty());
            assert_eq!(tabs.new_tab_hit_area, Rect::default());
        }

        fn bottom_tab_bar_still_hides_when_single_tab() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.prefs.hide_tab_bar_when_single_tab = true;
            // herdr also set `tab_bar_position = Bottom`; gclient has no
            // position and hides the single-tab bar regardless.
            chrome.mode = Mode::Prefix;

            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));
            assert_eq!(chrome.view.tab_bar_rect, None);
            assert_eq!(
                chrome.view.terminal_area,
                Rect::new(26, 0, 54, 20 - STATUS_ROWS)
            );

            let terminal = render_full(&ws, &chrome, Rect::new(0, 0, 80, 20));
            let row = mode_row(&chrome);
            let mode_row = buffer_row_text(&terminal, row, row.y + row.height - 1);
            assert!(mode_row.contains(PREFIX_INDICATOR), "{mode_row}");
        }

        fn hide_tab_bar_when_single_tab_resizes_background_tabs_per_workspace() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("runtime")
                .block_on(async {
                    // herdr resized every workspace's background runtimes;
                    // gclient sizes a pane from the geometry its own chrome
                    // computes, so each herdr workspace is one chrome here.
                    let one_ws = scripted(&["one"]);
                    let mut one_tab_workspace = chrome_for(&one_ws, "one");
                    one_tab_workspace.prefs.hide_tab_bar_when_single_tab = true;

                    let two_ws = scripted(&["two"]);
                    let mut two_tab_workspace = chrome_for(&two_ws, "two");
                    two_tab_workspace.prefs.hide_tab_bar_when_single_tab = true;
                    let background_tab = add_tab(&mut two_tab_workspace, "logs");
                    two_tab_workspace.tabs_mut().active_tab = 0;
                    two_tab_workspace.mode = Mode::Terminal;
                    one_tab_workspace.mode = Mode::Terminal;

                    one_tab_workspace.compute_view(&one_ws, Rect::new(0, 0, 80, 20));
                    two_tab_workspace.compute_view(&two_ws, Rect::new(0, 0, 80, 20));

                    let one_tab_size = runtime_size(&one_tab_workspace, 0);
                    let two_tab_size = runtime_size(&two_tab_workspace, background_tab);
                    assert_eq!(one_tab_size, (20 - STATUS_ROWS, 53));
                    assert_eq!(two_tab_size, (19 - STATUS_ROWS, 53));
                });
        }

        fn product_announcement_renders_above_config_diagnostic() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::Respond;
            // herdr's announcement modal (release notes, dropped) stands in
            // as the respond dialog carrying the announcement title.
            chrome.dialog = Some(Dialog::Respond {
                entry_id: "keybinding-v2".into(),
                prompt: "Keybinding syntax changed".into(),
                options: Vec::new(),
                selected: 0,
                text: String::new(),
            });
            chrome.status_message = Some(
                "unsafe direct keybinding: keys.new_workspace = \"n\"\nunsafe direct keybinding: keys.new_tab = \"c\""
                    .into(),
            );

            let area = Rect::new(0, 0, 44, 20);
            chrome.compute_view(&ws, area);
            let terminal = render_full(&ws, &chrome, area);

            let popup = centered_popup_rect(
                area,
                ANNOUNCEMENT_MODAL_SIZE.0,
                ANNOUNCEMENT_MODAL_SIZE.1,
            )
            .expect("announcement popup");
            let title_row = popup.y + ANNOUNCEMENT_TITLE_ROW;
            let row = buffer_row_text(&terminal, Rect::new(0, title_row, area.width, 1), title_row);

            assert!(row.contains("Keybinding syntax changed"));
            assert!(!row.contains("config warning"));
        }

        fn compute_view_clamps_sidebar_width_to_configured_max() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::Terminal;
            chrome.sidebar.max_width = 30;
            chrome.sidebar.width = 999;

            chrome.compute_view(&ws, Rect::new(0, 0, 100, 20));

            assert_eq!(chrome.view.sidebar_rect.width, 30);
        }

        fn compute_view_clamps_sidebar_width_to_configured_min() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::Terminal;
            chrome.sidebar.min_width = 22;
            chrome.sidebar.width = 5;

            chrome.compute_view(&ws, Rect::new(0, 0, 100, 20));

            assert_eq!(chrome.view.sidebar_rect.width, 22);
        }

        fn hidden_collapsed_sidebar_uses_full_width_terminal_area() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.sidebar.collapsed = true;
            chrome.sidebar.hide_when_collapsed = true;
            chrome.mode = Mode::Terminal;

            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));

            assert_eq!(chrome.view.sidebar_rect, Rect::new(0, 0, 0, 20));
            assert_eq!(chrome.view.tab_bar_rect, Some(Rect::new(0, 0, 80, 1)));
            assert_eq!(
                chrome.view.terminal_area,
                Rect::new(0, 1, 80, 19 - STATUS_ROWS)
            );
            assert!(chrome.view.project_hit_areas.is_empty());

            render_full(&ws, &chrome, Rect::new(0, 0, 80, 20));
        }

        fn collapsed_sidebar_keeps_active_workspace_highlight_in_terminal_mode() {
            // herdr's active workspace is gclient's focused project: the
            // rail highlights the second project card.
            let mut ws = with_projects(scripted(&["one", "two"]));
            ws.select_project("proj-two");
            let mut chrome = chrome_for(&ws, "two");
            chrome.sidebar.collapsed = true;
            // Neither project has a live entry: list them both.
            chrome.sidebar.all_projects = true;
            chrome.sidebar.selected = 0;
            chrome.mode = Mode::Terminal;

            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));
            let terminal = render_full(&ws, &chrome, Rect::new(0, 0, 80, 20));

            // herdr `collapsed_sidebar_sections`: the cards under the machine dot.
            let (rail, _) = collapsed_sections(chrome.view.sidebar_rect);
            let ws_area = rail[SidebarSection::Projects.index()];
            let active_row = ws_area.y + 1;
            let active_style = cell(&terminal, ws_area.x, active_row).style();

            assert_eq!(active_style.bg, Some(palette().surface_dim));
        }

        fn expanded_sidebar_workspace_rows_show_state_before_name_without_numbers() {
            // herdr's workspace carried a git checkout on `main`; gclient's
            // one-line project card shows the branch in its parenthetical.
            let ws = with_projects(scripted(&["one"]));
            let mut chrome = chrome_for(&ws, "one");
            chrome.sidebar.selected = 0;
            chrome.mode = Mode::Navigate;

            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));
            let terminal = render_full(&ws, &chrome, Rect::new(0, 0, 80, 20));

            // herdr `workspace_card_areas[0]`: the first card sits below the
            // projects band, left of the separator column.
            let sidebar = chrome.view.sidebar_rect;
            let projects = SidebarSection::Projects;
            let body =
                section_body_rect(chrome.view.sidebar_section_rects[projects.index()], false);
            let card = Rect::new(sidebar.x, body.y, sidebar.width - 1, 1);
            let line1 = buffer_row_text(&terminal, card, card.y);
            let line2 = buffer_row_text(&terminal, card, card.y + 1);

            assert!(
                line1.starts_with(&format!("{SELECTED_MARK}{IDLE_DOT} one (main)")),
                "{line1}"
            );
            assert!(!line1.contains("1 one"));
            // The card is one line: the sessions band follows it.
            assert!(line2.starts_with(" Sessions"), "{line2}");
            assert!(!line2.contains("main"));
        }

        fn tab_bar_dims_auto_named_tabs_and_emphasizes_custom_tabs() {
            let ws = scripted(&["test"]);
            let mut chrome = chrome_for(&ws, "test");
            add_tab(&mut chrome, "logs");
            chrome.mode = Mode::Terminal;

            let area = Rect::new(0, 0, 80, 20);
            chrome.compute_view(&ws, area);
            let terminal = render_full(&ws, &chrome, area);
            let tabs = tab_view(&ws, &chrome, area);

            let auto_rect = tabs.tab_hit_areas[0];
            let custom_rect = tabs.tab_hit_areas[1];
            let auto_style = cell(&terminal, auto_rect.x + 1, auto_rect.y).style();
            let custom_style = cell(&terminal, custom_rect.x + 1, custom_rect.y).style();

            assert_eq!(auto_style.fg, Some(palette().overlay0));
            assert!(auto_style.add_modifier.contains(Modifier::DIM));
            assert_eq!(custom_style.fg, Some(palette().panel_bg));
            assert!(custom_style.add_modifier.contains(Modifier::BOLD));
        }

        fn tab_bar_uses_surface_dim_when_panel_background_resets() {
            let ws = scripted(&["test"]);
            let mut chrome = chrome_for(&ws, "test");
            add_tab(&mut chrome, "logs");
            chrome.palette.panel_bg = Color::Reset;
            chrome.mode = Mode::Terminal;

            let area = Rect::new(0, 0, 80, 20);
            chrome.compute_view(&ws, area);
            let terminal = render_full(&ws, &chrome, area);
            let tabs = tab_view(&ws, &chrome, area);

            let custom_rect = tabs.tab_hit_areas[1];
            let custom_style = cell(&terminal, custom_rect.x + 1, custom_rect.y).style();

            assert_eq!(custom_style.bg, Some(palette().accent));
            assert_eq!(custom_style.fg, Some(palette().surface_dim));
            assert!(custom_style.add_modifier.contains(Modifier::BOLD));
        }

        fn new_tab_button_tracks_rightmost_tab_when_tabs_fit() {
            let ws = scripted(&["test"]);
            let mut chrome = chrome_for(&ws, "test");
            add_tab(&mut chrome, "logs");
            chrome.mode = Mode::Terminal;

            let area = Rect::new(0, 0, 80, 20);
            chrome.compute_view(&ws, area);
            let tabs = tab_view(&ws, &chrome, area);

            let last_visible = tabs
                .tab_hit_areas
                .iter()
                .rev()
                .find(|rect| rect.width > 0)
                .copied()
                .expect("last visible tab");

            assert_eq!(
                tabs.new_tab_hit_area.x,
                last_visible.x + last_visible.width
            );
        }

        fn tab_bar_shows_scroll_controls_when_tabs_overflow() {
            let ws = scripted(&["test"]);
            let mut chrome = chrome_for(&ws, "test");
            for name in ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"] {
                add_tab(&mut chrome, name);
            }
            chrome.tabs_mut().active_tab = 0;
            chrome.mode = Mode::Terminal;
            chrome.tab_scroll_follow_active = false;
            chrome.tab_scroll = 2;

            let area = Rect::new(0, 0, 65, 20);
            chrome.compute_view(&ws, area);
            let tabs = tab_view(&ws, &chrome, area);

            assert!(tabs.tab_scroll_left_hit_area.width > 0);
            assert!(tabs.tab_scroll_right_hit_area.width > 0);
            assert_eq!(tabs.tab_hit_areas[0].width, 0);
            assert_eq!(tabs.tab_hit_areas[1].width, 0);
            assert!(tabs.tab_hit_areas[2].width > 0);
            assert!(tabs.new_tab_hit_area.width > 0);

            let last_visible = tabs
                .tab_hit_areas
                .iter()
                .rev()
                .find(|rect| rect.width > 0)
                .copied()
                .expect("last visible tab");

            assert_eq!(
                tabs.tab_scroll_right_hit_area.x,
                last_visible.x + last_visible.width
            );
            assert_eq!(
                tabs.new_tab_hit_area.x,
                tabs.tab_scroll_right_hit_area.x + tabs.tab_scroll_right_hit_area.width
            );
        }

        fn tab_bar_clamps_manual_scroll_at_last_visible_tab() {
            let ws = scripted(&["test"]);
            let mut chrome = chrome_for(&ws, "test");
            for name in [
                "one", "two", "three", "four", "five", "six", "seven", "eight",
            ] {
                add_tab(&mut chrome, name);
            }
            chrome.tabs_mut().active_tab = 0;
            chrome.mode = Mode::Terminal;
            chrome.tab_scroll_follow_active = false;
            chrome.tab_scroll = usize::MAX;

            let area = Rect::new(0, 0, 65, 20);
            chrome.compute_view(&ws, area);
            let tabs = tab_view(&ws, &chrome, area);

            let last_idx = chrome.tabs().tabs.len() - 1;
            assert!(tabs.tab_hit_areas[last_idx].width > 0);
            let clamped_scroll = chrome.tab_scroll;

            // herdr `scroll_tabs_right`; gclient has no tab-scroll action yet,
            // so the saturating step stands in for it.
            chrome.tab_scroll = chrome.tab_scroll.saturating_add(1);
            let tabs = tab_view(&ws, &chrome, area);

            assert_eq!(chrome.tab_scroll, clamped_scroll);
            assert!(tabs.tab_hit_areas[last_idx].width > 0);
        }

        fn pane_scrollbar_rect_uses_reserved_rightmost_column() {
            let info = PaneInfo {
                id: layout::PaneId::from_raw(1),
                rect: Rect::new(0, 0, 12, 8),
                inner_rect: Rect::new(1, 1, 9, 6),
                scrollbar_rect: Some(Rect::new(10, 1, 1, 6)),
                borders: Borders::ALL,
                is_focused: true,
            };

            assert_eq!(pane_scrollbar_rect(&info), Some(Rect::new(10, 1, 1, 6)));
        }

        fn compute_view_reserves_terminal_column_when_pane_scrollbar_is_visible() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("runtime")
                .block_on(async {
                    let mut ws = scripted(&["test"]);
                    let pane_id = ws.pane_for_terminal("test").expect("pane");
                    // herdr fed five lines into a four-row runtime; gclient's
                    // daemon reports the resulting scrollback instead.
                    ws.apply_scroll_applied(pane_id, 0, 1);
                    let mut chrome = chrome_for(&ws, "test");

                    chrome.compute_view(&ws, Rect::new(0, 0, 40, 12));

                    let info = chrome.view.pane_infos.first().expect("pane info");
                    assert_eq!(info.inner_rect.width + 1, chrome.view.terminal_area.width);
                    assert_eq!(
                        info.scrollbar_rect,
                        Some(Rect::new(
                            info.inner_rect.x + info.inner_rect.width,
                            info.inner_rect.y,
                            1,
                            info.inner_rect.height,
                        ))
                    );
                });
        }

        fn scrollbar_stays_hidden_without_scrollback() {
            let metrics = ScrollMetrics {
                offset_from_bottom: 0,
                max_offset_from_bottom: 0,
                viewport_rows: 5,
            };

            assert!(!should_show_scrollbar(metrics));
        }

        fn scrollbar_shows_with_scrollback() {
            let metrics = ScrollMetrics {
                offset_from_bottom: 0,
                max_offset_from_bottom: 20,
                viewport_rows: 5,
            };

            assert!(should_show_scrollbar(metrics));
        }

        fn scrollbar_thumb_reaches_bottom_when_scrolled_to_bottom() {
            let metrics = ScrollMetrics {
                offset_from_bottom: 0,
                max_offset_from_bottom: 20,
                viewport_rows: 5,
            };
            let track = Rect::new(9, 4, 1, 5);

            let thumb = scrollbar_thumb(metrics, track).expect("thumb");
            assert_eq!(thumb.top + thumb.len, track.y + track.height);
        }

        fn scrollbar_offset_mapping_hits_top_middle_and_bottom() {
            let metrics = ScrollMetrics {
                offset_from_bottom: 0,
                max_offset_from_bottom: 20,
                viewport_rows: 5,
            };
            let track = Rect::new(9, 4, 1, 5);

            assert_eq!(scrollbar_offset_from_row(metrics, track, 4), 20);
            assert_eq!(scrollbar_offset_from_row(metrics, track, 6), 10);
            assert_eq!(scrollbar_offset_from_row(metrics, track, 8), 0);
        }

        fn dragging_from_current_thumb_row_preserves_offset() {
            let metrics = ScrollMetrics {
                offset_from_bottom: 7,
                max_offset_from_bottom: 20,
                viewport_rows: 5,
            };
            let track = Rect::new(9, 4, 1, 8);
            let thumb = scrollbar_thumb(metrics, track).expect("thumb");
            let row = thumb.top + thumb.len / 2;
            let grab = scrollbar_thumb_grab_offset(metrics, track, row).expect("grab");

            assert_eq!(scrollbar_offset_from_drag_row(metrics, track, row, grab), 7);
        }

        fn prefix_mode_renders_prefix_indicator() {
            let ws = scripted(&["one"]);
            let mut chrome = chrome_for(&ws, "one");
            chrome.mode = Mode::Prefix;
            // herdr `render_prefix_overlay` over the terminal area; gclient
            // shows the mode in the status line.
            let area = Rect::new(0, 0, 60, 4);
            let terminal = render(60, 4, |frame| {
                render_status_line(frame, area, &ws, &chrome);
            });

            let rendered = screen(&terminal);
            assert!(rendered.contains(PREFIX_INDICATOR));
        }

        fn keybind_help_shows_unset_for_optional_actions() {
            let chrome = Chrome::new(theme());
            let groups = keybind_help_groups(&chrome);

            let workspace_tab = groups
                .iter()
                .find(|(name, _)| *name == "workspaces / tabs")
                .expect("workspace tab group")
                .1
                .clone();
            let panes = groups
                .iter()
                .find(|(name, _)| *name == "panes")
                .expect("panes group")
                .1
                .clone();

            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "previous workspace"));
            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "next workspace"));
            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "previous agent"));
            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "next agent"));
            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "focus agent 1-9"));
            assert!(workspace_tab
                .iter()
                .any(|(key, label)| key == "unset" && label.as_ref() == "switch workspace 1-9"));
            assert!(panes
                .iter()
                .any(|(key, label)| key == "prefix+h" && label.as_ref() == "focus pane left"));
            assert!(panes
                .iter()
                .any(|(key, label)| key == "prefix+j" && label.as_ref() == "focus pane down"));
            assert!(panes
                .iter()
                .any(|(key, label)| key == "prefix+k" && label.as_ref() == "focus pane up"));
            assert!(panes
                .iter()
                .any(|(key, label)| key == "prefix+l" && label.as_ref() == "focus pane right"));
        }

        // TODO(#20201): herdr binds two custom commands here (`prefix+alt+g`
        // -> lazygit described "open lazygit", `prefix+alt+h` -> a shell
        // command with no description, labelled "custom command"). gclient
        // reserves `custom_command` and hides it from help until the
        // plugin-menu plan lands a way to bind them; the herdr expectations
        // below stay verbatim for that day.
        #[deferred = "TODO(#20201): custom_command help is hidden until the plugin-menu plan lands"]
        fn keybind_help_shows_custom_command_descriptions() {
            let chrome = Chrome::new(theme());

            let groups = keybind_help_groups(&chrome);
            let custom = groups
                .iter()
                .find(|(name, _)| *name == "custom")
                .expect("custom group")
                .1
                .clone();
            assert!(custom
                .iter()
                .any(|(key, label)| key == "prefix+alt+g" && label.as_ref() == "open lazygit"));
            assert!(custom
                .iter()
                .any(|(key, label)| key == "prefix+alt+h" && label.as_ref() == "custom command"));

            let rendered_help = rendered_help_text(&chrome);
            assert!(rendered_help.contains("open lazygit"));
            assert!(rendered_help.contains("custom command"));
        }

        fn keybind_help_compacts_multiple_indexed_ranges() {
            let keymap = Keymap::from_toml(
                r#"
[bindings]
switch_tab = ["prefix+1..9", "alt+1..9"]
switch_project = "ctrl+1..9"
"#,
                HERDR_PREFIX,
            )
            .expect("config parses");

            let mut chrome = Chrome::new(theme());
            chrome.keymap = keymap;

            let workspace_tab = keybind_help_groups(&chrome)
                .into_iter()
                .find(|(name, _)| *name == "workspaces / tabs")
                .expect("workspace tab group")
                .1;

            let switch_tab_key = workspace_tab
                .iter()
                .find(|(_, label)| label.as_ref() == "switch tab 1-9")
                .map(|(key, _)| key.as_str())
                .expect("switch tab help entry");
            let switch_workspace_key = workspace_tab
                .iter()
                .find(|(_, label)| label.as_ref() == "switch workspace 1-9")
                .map(|(key, _)| key.as_str())
                .expect("switch workspace help entry");

            assert_eq!(
                switch_tab_key,
                format!("prefix+1..9{CHORD_SEPARATOR}alt+1..9")
            );
            assert_eq!(switch_workspace_key, "ctrl+1..9");
        }
    }
    "src/ui/keybind_help.rs" => {
        fn keybind_help_filter_matches_labels_case_insensitively() {
            let filtered = filter_keybind_help_groups(groups(), "WoRk");

            assert_eq!(filtered.len(), 1);
            assert_eq!(filtered[0].0, "workspaces / tabs");
            assert_eq!(filtered[0].1.len(), 1);
            assert_eq!(filtered[0].1[0].1, "workspace navigation");
        }

        fn keybind_help_filter_matches_shortcuts_without_matching_group_headings() {
            let filtered = filter_keybind_help_groups(groups(), "x");

            assert_eq!(filtered.len(), 1);
            assert_eq!(filtered[0].0, "panes");
            assert_eq!(filtered[0].1.len(), 1);
            assert_eq!(filtered[0].1[0].1, "close pane");

            assert!(filter_keybind_help_groups(groups(), "panes").is_empty());
        }
    }
    "src/ui/tab_surface.rs" => {
        fn explicit_surface_layout_drives_render_cursor_and_hyperlinks() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("runtime")
                .block_on(async {
                    // herdr's workspace name lives on the sidebar; gclient's
                    // counterpart is the project id.
                    let mut ws = scripted(&["left", "right"]);
                    ws.select_project("shell-workspace");
                    let left = ws.pane_for_terminal("left").expect("left pane");
                    let right = ws.pane_for_terminal("right").expect("right pane");
                    let mut chrome = chrome_for(&ws, "left");
                    chrome.open_pane(right, "");
                    chrome.mode = Mode::Terminal;

                    let full_area = Rect::new(0, 0, 106, 20);
                    chrome.compute_view(&ws, full_area);
                    let area = chrome.view.terminal_area;
                    assert_eq!(area, Rect::new(26, 1, 80, 19 - STATUS_ROWS));
                    assert_eq!(chrome.view.pane_infos.len(), 2);
                    assert!(!chrome.view.split_borders.is_empty());

                    // gclient renders from `chrome.view` and has no separate
                    // surface parameter, hyperlink registry, or runtime cursor;
                    // the content hook paints the grids the runtimes held.
                    let surface = chrome
                        .view
                        .tab_bar_rect
                        .map_or(area, |tabs| tabs.union(area));
                    let terminal = render(full_area.width, full_area.height, |frame| {
                        let mut content = |frame: &mut Frame, rect: Rect, id: PaneId| {
                            let text = if id == left { "LEFT" } else { "RIGHT" };
                            frame.render_widget(Paragraph::new(text), rect);
                        };
                        render_tab_surface(frame, surface, &ws, &chrome, &mut content);
                    });

                    let rendered = screen(&terminal);
                    assert!(rendered.contains("LEFT"), "surface: {rendered:?}");
                    assert!(rendered.contains("RIGHT"), "surface: {rendered:?}");
                    assert!(!rendered.contains("shell-workspace"));
                });
        }

        fn desktop_full_app_semantic_frame_is_characterized() {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("runtime")
                .block_on(async {
                    let mut ws = scripted(&["left", "right"]);
                    ws.select_project("characterization");
                    let left = ws.pane_for_terminal("left").expect("left pane");
                    let right = ws.pane_for_terminal("right").expect("right pane");
                    let mut chrome = chrome_for(&ws, "left");
                    add_tab(&mut chrome, "logs");
                    chrome.tabs_mut().active_tab = 0;
                    chrome.open_pane(right, "");
                    chrome.mode = Mode::Terminal;

                    let area = Rect::new(0, 0, 106, 20);
                    chrome.compute_view(&ws, area);
                    let mut terminal = render(area.width, area.height, |frame| {
                        let mut content = |frame: &mut Frame, rect: Rect, id: PaneId| {
                            let text = if id == left { "LINK" } else { "RIGHT\nPANE" };
                            frame.render_widget(Paragraph::new(text), rect);
                            if chrome.focused_pane() == Some(id) {
                                frame.set_cursor_position((rect.x + 4, rect.y));
                            }
                        };
                        render_workspace_with(frame, &ws, &chrome, &mut content);
                    });

                    let frame = terminal.backend().buffer().area;
                    assert_eq!((frame.width, frame.height), (106, 20));
                    assert_eq!(chrome.view.sidebar_rect, Rect::new(0, 0, 26, 20));
                    assert_eq!(chrome.view.tab_bar_rect, Some(Rect::new(26, 0, 80, 1)));
                    assert_eq!(
                        chrome.view.terminal_area,
                        Rect::new(26, 1, 80, 19 - STATUS_ROWS)
                    );
                    assert_eq!(chrome.view.pane_infos.len(), 2);
                    assert!(!chrome.view.split_borders.is_empty());
                    let focused = chrome
                        .view
                        .pane_infos
                        .iter()
                        .find(|info| info.is_focused)
                        .expect("focused pane info")
                        .inner_rect;
                    terminal
                        .backend_mut()
                        .assert_cursor_position((focused.x + 4, focused.y));
                    // herdr pinned its own frame bytes; gclient's frame is
                    // characterized the same way against its own render.
                    // Rehashed when the status line gained the transport
                    // field, again when the control indicator became a
                    // bracketed button, and again when a frameless pane began
                    // naming its wait (2.3), again when the sidebar became
                    // project cards over agent rows (3.1), and again when the
                    // agents header gained its sort label over two-line rows
                    // (3.2), and again when the status line began naming the
                    // prefix outside tmux, again when the sidebar split
                    // into machines, projects, sessions and agents, and
                    // again when the sidebar became bands over one-line
                    // cards with the runs nested under their sessions and
                    // the hub row took the pinned test host name (#22203):
                    // 4.1.3 requires a glyph change to fail here, so this
                    // digest moves only alongside a deliberate render change.
                    assert_eq!(
                        frame_digest(&terminal),
                        "2225ad46d3645874e3301599ea97f362c526573b07ee3b8db21620c3660be874"
                    );
                });
        }
    }
    "src/ui/text.rs" => {
        fn truncate_end_uses_display_width() {
            let text = truncate_end("提交 herdr 的反馈", 16);

            assert_eq!(text, "提交 herdr 的反…");
            assert!(display_width(&text) <= 16);
        }

        fn middle_elide_uses_display_width() {
            let text = middle_elide("重构用户认证模块并迁移到统一登录服务", 12);

            assert!(text.contains('…'));
            assert!(display_width(&text) <= 12);
        }
    }
}

// ------------------------------------------------------- gclient hit map
//
// gclient-only: herdr wrote its hit rects back from `render`; gclient carries
// them through `ChromeHits` into `ViewState::apply_hits`. These pin the rects
// to the cells the renderers actually drew.

/// Draw the whole frame the way the run loop does and write the hits back.
fn render_with_hits(ws: &Workspace, chrome: &mut Chrome, area: Rect) -> Terminal<TestBackend> {
    chrome.compute_view(ws, area);
    let mut hits = None;
    let terminal = render(area.width, area.height, |frame| {
        hits = Some(render_workspace(frame, ws, chrome));
    });
    chrome.view.apply_hits(hits.expect("frame drawn"));
    terminal
}

/// Row `rect.y` of `rect`, trailing spaces trimmed.
fn hit_text(terminal: &Terminal<TestBackend>, rect: Rect) -> String {
    buffer_row_text(terminal, rect, rect.y)
}

#[test]
fn rendered_hits_match_drawn_cells() {
    // Twenty project cards overflow a 24-row screen's projects section, and
    // twelve tabs overflow a 74-column bar, so every scroll affordance is
    // drawn.
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable"}
        }]
    }));
    ws.open_terminal("term-alpha", "native", "epoch")
        .expect("open term-alpha");
    for n in 2..=20 {
        ws.open_terminal(&format!("t{n:02}"), "native", "epoch")
            .expect("open scripted terminal");
    }
    // One project card per terminal: twenty cards overflow the section once
    // every project lists (only `term-alpha`'s is working).
    let ws = with_projects(ws);
    let mut chrome = chrome_for(&ws, "term-alpha");
    chrome.sidebar.all_projects = true;
    // Wide enough for the active row's whole title beside its needs-you
    // word once the sessions scrollbar lane takes a column.
    chrome.sidebar.width = 28;
    for n in 2..=12 {
        add_tab(&mut chrome, &format!("tab-{n:02}"));
    }
    chrome.tabs_mut().active_tab = chrome.tabs().tabs.len() - 1;
    let terminal = render_with_hits(&ws, &mut chrome, Rect::new(0, 0, 100, 24));
    let view = &chrome.view;

    assert!(
        view.tab_hit_areas.len() < chrome.tabs().tabs.len(),
        "tabs overflow"
    );
    for (index, rect) in &view.tab_hit_areas {
        let name = tab_display_name(&chrome.tabs().tabs, *index).expect("tab name");
        let text = hit_text(&terminal, *rect);
        assert!(text.contains(&name), "tab {index} at {rect:?}: {text:?}");
    }
    let arrows = [
        (view.tab_scroll_left_hit_area, "<"),
        (view.tab_scroll_right_hit_area, ">"),
    ];
    assert!(
        arrows.iter().any(|(rect, _)| rect.is_some()),
        "scroll arrows"
    );
    for (rect, glyph) in arrows {
        if let Some(rect) = rect {
            assert_eq!(hit_text(&terminal, rect).trim(), glyph);
        }
    }
    if let Some(rect) = view.new_tab_hit_area {
        assert_eq!(hit_text(&terminal, rect).trim(), "+");
    }

    let divider_x = view.sidebar_divider_x.expect("sidebar divider");
    assert_eq!(
        cell(&terminal, divider_x, view.sidebar_rect.y).symbol(),
        "│"
    );
    // Each section opens with its titled band; the menu band sits above the
    // first and the footer band's toggle below the last.
    for section in SidebarSection::ALL {
        let rect = view.sidebar_section_rects[section.index()];
        assert!(rect.height > 0, "{section:?} drawn");
        let band = Rect::new(rect.x, rect.y, rect.width, 1);
        let text = hit_text(&terminal, band);
        assert!(
            text.starts_with(&format!(" {}", section.title())),
            "{section:?} band at {band:?}: {text:?}"
        );
    }
    let menu = view.projects_menu_hit_area.expect("menu control drawn");
    assert_eq!(menu.y, view.sidebar_rect.y);
    assert_eq!(hit_text(&terminal, menu), "[Menu]");
    let new = view.projects_new_hit_area.expect("new control drawn");
    assert_eq!(hit_text(&terminal, new), "[+]");
    let toggle = view.sidebar_toggle_hit_area.expect("toggle drawn");
    assert_eq!(toggle.y, view.sidebar_rect.bottom() - 1);
    assert_eq!(hit_text(&terminal, toggle), "[«]");
    assert!(!view.project_hit_areas.is_empty());
    assert!(view.project_hit_areas.len() < 20, "projects overflow");
    for (id, rect) in &view.project_hit_areas {
        let name = id.strip_prefix("proj-").expect("project id");
        let text = hit_text(&terminal, *rect);
        assert!(text.contains(name), "project {id} at {rect:?}: {text:?}");
    }
    let (entry, rect) = view.agent_hit_areas.first().expect("agent row");
    assert_eq!(entry, "run:term-alpha");
    let text = hit_text(&terminal, *rect);
    assert!(
        text.contains("term-alpha"),
        "agent row at {rect:?}: {text:?}"
    );
    // The cards and the twenty two-line sessions rows overflow their
    // sections; the one machine row does not.
    assert_eq!(
        view.sidebar_scrollbar_hit_areas[SidebarSection::Machines.index()],
        None
    );
    for section in [SidebarSection::Projects, SidebarSection::Sessions] {
        let lane = view.sidebar_scrollbar_hit_areas[section.index()]
            .unwrap_or_else(|| panic!("{section:?} scrollbar"));
        for y in lane.y..lane.bottom() {
            assert_eq!(cell(&terminal, lane.x, y).symbol(), "▕", "lane row {y}");
        }
    }

    let indicator = view.control_indicator_hit_area.expect("control indicator");
    assert_eq!(indicator.y, view.status_rect.y);
    assert_eq!(hit_text(&terminal, indicator), " [○ observe]");
    assert_eq!(usize::from(indicator.width), display_width(" [○ observe]"));
}

#[test]
fn rendered_settings_hits_match_drawn_rows() {
    let ws = scripted(&["term-alpha"]);
    let mut chrome = chrome_for(&ws, "term-alpha");
    chrome.mode = Mode::Settings;
    let area = Rect::new(0, 0, 100, 30);
    let terminal = render_with_hits(&ws, &mut chrome, area);
    let view = &chrome.view;

    let popup = centered_popup_rect(area, SETTINGS_POPUP_WIDTH, SETTINGS_POPUP_HEIGHT);
    assert_eq!(view.settings_dialog_area, popup);
    assert_eq!(view.settings_row_hit_areas.len(), SettingsRow::ALL.len());
    let labels = [
        "theme",
        "mouse capture",
        "pane borders",
        "pane scrollbars",
        "pane gaps",
        "confirm close",
        "hide tab bar with one tab",
        "sidebar width",
        "right-click passthrough",
        "agent sort",
    ];
    for (index, rect) in &view.settings_row_hit_areas {
        let text = hit_text(&terminal, *rect);
        assert!(text.contains(labels[*index]), "row {index}: {text:?}");
        assert_eq!(text.contains('▸'), *index == chrome.settings.selected);
    }
}

/// herdr's `groups()` fixture for the keybind-help filter tests.
fn groups() -> Vec<HelpGroup> {
    vec![
        (
            "workspaces / tabs",
            vec![
                help_entry("w", "workspace navigation"),
                help_entry("c", "new tab"),
            ],
        ),
        (
            "panes",
            vec![
                help_entry("v", "split vertical"),
                help_entry("x", "close pane"),
            ],
        ),
    ]
}

/// Plan 2.2: `open_pane_below` stacks the new slot under the focused one where
/// `open_pane` sets it beside; both open a first tab on an empty chrome.
#[test]
fn open_pane_below_stacks_the_new_slot_under_the_focused_one() {
    let ws = scripted(&["alpha", "beta", "gamma"]);
    let beta = ws.pane_for_terminal("beta").expect("beta pane");
    let gamma = ws.pane_for_terminal("gamma").expect("gamma pane");

    let mut chrome = chrome_for(&ws, "alpha");
    let beside = chrome.open_pane(beta, "beta");
    let below = chrome.open_pane_below(gamma, "gamma");
    assert_eq!(chrome.tabs().tabs.len(), 1);
    assert_eq!(
        chrome.focused_pane(),
        Some(gamma),
        "the new slot takes focus"
    );

    chrome.compute_view(&ws, Rect::new(0, 0, 100, 30));
    let rect_of = |slot: layout::PaneId| {
        chrome
            .view
            .pane_infos
            .iter()
            .find(|info| info.id == slot)
            .expect("slot drawn")
            .rect
    };
    let (beta_rect, gamma_rect) = (rect_of(beside), rect_of(below));
    assert_eq!(gamma_rect.x, beta_rect.x, "stacked slots share a column");
    assert_eq!(gamma_rect.width, beta_rect.width);
    assert!(
        gamma_rect.y >= beta_rect.y + beta_rect.height,
        "gamma sits under beta: {beta_rect:?} then {gamma_rect:?}"
    );

    let mut empty = Chrome::new(theme());
    empty.open_pane_below(beta, "beta");
    assert_eq!(empty.tabs().tabs.len(), 1);
    assert_eq!(empty.focused_pane(), Some(beta));
}
