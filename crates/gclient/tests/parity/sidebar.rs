//! herdr `src/ui/sidebar.rs` (35) and `src/ui/sidebar/tokens.rs` (6) keep-set
//! render tests.
//!
//! State mapping: a herdr agent-panel entry or workspace card is a gclient
//! roster row (one terminal, one line); a herdr attention state is an
//! attention prompt entry. herdr's configurable multi-row token layouts
//! collapse to gclient's fixed `glyph title · state · detail` line, where the
//! detected agent kind becomes the pane backend shown in the detail token.
//! Worktree grouping, per-token style overrides, agent-panel sort modes, and
//! sidebar drag reorder are dropped surfaces; their fixtures map to the flat
//! roster and each adapted assertion carries the herdr original in a comment.

use gobby_client::app::{Pane, PaneId};
use gobby_client::ui::chrome::{Chrome, Mode, RowState, WorkspaceView};
use gobby_client::ui::sidebar::{
    attention_body_rect, collapsed_sections, expanded_sections, expanded_toggle_rect, list_metrics,
    render_collapsed_sidebar, render_sidebar, roster_body_rect, SidebarHits,
};
use gobby_client::ui::sidebar_rows::{
    attention_rows, fitted_spans, roster_rows, row_line, RowKind, SidebarRow,
};
use gobby_client::ui::status::state_dot;
use gobby_client::ui::text::display_width;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use ratatui::Terminal;

use super::fixtures::{cell, render, rows};
use super::token_map::{palette, theme};

/// herdr fixtures name the product; gclient's rows name gobby (rule 3).
const PRODUCT: &str = "gobby";
/// Pane backend for terminals whose herdr entry has no detected agent.
const DEFAULT_BACKEND: &str = "native";

/// herdr `AppState` + `Workspace::test_new` stand-in: a roster of terminals,
/// each attached to one pane, plus attention entries.
struct Board {
    roster: Vec<String>,
    attention: Vec<String>,
    panes: Vec<Pane>,
}

impl Board {
    fn new(names: &[&str]) -> Self {
        let mut board = Board {
            roster: Vec::new(),
            attention: Vec::new(),
            panes: Vec::new(),
        };
        for name in names {
            board.add(name, DEFAULT_BACKEND);
        }
        board
    }

    /// Adds a terminal; `backend` carries the herdr detected agent kind.
    fn add(&mut self, name: &str, backend: &str) -> PaneId {
        let id = PaneId(self.panes.len() as u32 + 1);
        self.panes.push(Pane::new(id, name, backend, "epoch"));
        self.roster.push(name.to_string());
        id
    }

    fn pane_id(&self, name: &str) -> PaneId {
        self.pane_for_terminal(name).expect("known terminal")
    }

    /// herdr `terminal.state = ...` for the pane behind `name`.
    fn set_state(&mut self, name: &str, state: RowState) {
        match state {
            RowState::Attention => self.attention.push(format!("blocked:{name}")),
            RowState::Working | RowState::Unseen => {
                let pane = self
                    .panes
                    .iter_mut()
                    .find(|pane| pane.terminal_id == name)
                    .expect("known terminal");
                pane.new_output = true;
                pane.live = state == RowState::Working;
            }
            RowState::Idle => {}
            RowState::Unknown => self.panes.retain(|pane| pane.terminal_id != name),
        }
    }
}

impl WorkspaceView for Board {
    fn project_id(&self) -> Option<&str> {
        None
    }

    fn roster_terminal_ids(&self) -> Vec<String> {
        self.roster.clone()
    }

    fn attention_entry_ids(&self) -> Vec<String> {
        self.attention.clone()
    }

    fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId> {
        self.panes
            .iter()
            .find(|pane| pane.terminal_id == terminal_id)
            .map(|pane| pane.id)
    }

    fn pane(&self, id: PaneId) -> &Pane {
        self.panes
            .iter()
            .find(|pane| pane.id == id)
            .expect("known pane")
    }

    fn daemon_ready(&self) -> bool {
        true
    }
}

fn chrome() -> Chrome {
    Chrome::new(theme())
}

/// herdr `app.active = Some(ws_idx)`: focus the terminal's pane.
fn focus(chrome: &mut Chrome, board: &Board, name: &str) {
    chrome.open_tab(board.pane_id(name), name);
}

fn draw_sidebar(
    board: &Board,
    chrome: &Chrome,
    width: u16,
    height: u16,
) -> (Terminal<TestBackend>, SidebarHits) {
    let area = Rect::new(0, 0, width, height);
    let mut hits = SidebarHits::default();
    let terminal = render(width, height, |frame| {
        hits = render_sidebar(frame, area, board, chrome);
    });
    (terminal, hits)
}

fn draw_collapsed(
    board: &Board,
    chrome: &Chrome,
    width: u16,
    height: u16,
) -> (Terminal<TestBackend>, SidebarHits) {
    let area = Rect::new(0, 0, width, height);
    let mut hits = SidebarHits::default();
    let terminal = render(width, height, |frame| {
        hits = render_collapsed_sidebar(frame, area, board, chrome);
    });
    (terminal, hits)
}

/// herdr's `row_text`: the first `width` cells, trailing blanks trimmed.
fn row_str(terminal: &Terminal<TestBackend>, y: u16, width: u16) -> String {
    (0..width)
        .map(|x| cell(terminal, x, y).symbol())
        .collect::<String>()
        .trim_end()
        .to_string()
}

fn find_symbol_x(terminal: &Terminal<TestBackend>, y: u16, width: u16, symbol: &str) -> u16 {
    (0..width)
        .find(|x| cell(terminal, *x, y).symbol() == symbol)
        .unwrap_or_else(|| {
            panic!(
                "missing symbol {symbol:?} in row {}",
                row_str(terminal, y, width)
            )
        })
}

fn style_at(terminal: &Terminal<TestBackend>, x: u16, y: u16) -> Style {
    cell(terminal, x, y).style()
}

/// Roster body (herdr agent-panel / workspace-list body) of a sidebar area.
fn roster_body(area: Rect, split: Option<u16>) -> Rect {
    roster_body_rect(expanded_sections(area, split).0, false)
}

fn spans_text(spans: &[Span<'_>]) -> String {
    spans.iter().map(|span| span.content.as_ref()).collect()
}

fn line_text(row: &SidebarRow, width: u16, chrome: &Chrome) -> String {
    spans_text(&row_line(row, width, chrome).spans)
}

fn plain_row(label: &str, state: RowState, detail: &str) -> SidebarRow {
    SidebarRow {
        id: label.to_string(),
        label: label.to_string(),
        kind: RowKind::Terminal,
        state,
        detail: detail.to_string(),
        selected: false,
        active: false,
    }
}

/// gclient state glyph for a herdr `AgentState` (distinct glyph/label pairs).
fn dot(state: RowState) -> &'static str {
    state_dot(state, &palette()).0
}

parity_tests! {
    "src/ui/sidebar.rs" => {
        fn default_agent_rows_remove_redundant_state_text() {
            let mut board = Board::new(&[]);
            board.add("one", "pi");
            board.set_state("one", RowState::Working);
            let mut chrome = chrome();
            focus(&mut chrome, &board, "one");
            // herdr renders 26 columns over two rows; gclient's single row
            // needs the width for the agent token to stay visible.
            let area = Rect::new(0, 0, 34, 20);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = roster_body(area, None);
            let p = palette();

            let first = row_str(&terminal, body.y, 33);
            let second = row_str(&terminal, body.y + 1, 33);
            assert!(first.contains("one"));
            // herdr: `assert_eq!(second, "   pi")`; gclient shows the agent
            // token on the same row and leaves the next row empty.
            assert!(first.contains(" pi "), "rendered row: {first:?}");
            assert_eq!(second, "");
            // herdr: `!contains("working")`; gclient's state indicator is a
            // (glyph, label) pair, so the label appears exactly once.
            assert_eq!(first.matches("working").count(), 1);

            let workspace_x = find_symbol_x(&terminal, body.y, body.width, "o");
            let workspace_style = style_at(&terminal, workspace_x, body.y);
            assert_eq!(workspace_style.fg, Some(p.text));
            assert!(workspace_style.add_modifier.contains(Modifier::BOLD));
            assert!(!workspace_style.add_modifier.contains(Modifier::DIM));
            assert_eq!(workspace_style.bg, Some(p.surface_dim));

            let agent_x = find_symbol_x(&terminal, body.y, body.width, "p");
            let agent_style = style_at(&terminal, agent_x, body.y);
            assert_eq!(agent_style.fg, Some(p.overlay0));
            assert!(agent_style.add_modifier.contains(Modifier::DIM));
            assert!(!agent_style.add_modifier.contains(Modifier::BOLD));
            assert_eq!(agent_style.bg, Some(p.surface_dim));
        }

        fn occurrence_false_removes_default_workspace_bold_and_agent_dim() {
            // herdr configures `{ token = "workspace", bold = false },
            // { token = "agent", dim = false }`; gclient has no per-token
            // style overrides, so the un-emphasised row is the unfocused one.
            let mut board = Board::new(&[]);
            board.add("one", "pi");
            let chrome = chrome();
            let area = Rect::new(0, 0, 34, 20);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = roster_body(area, None);
            let p = palette();
            let workspace = style_at(&terminal, find_symbol_x(&terminal, body.y, body.width, "o"), body.y);
            let agent = style_at(&terminal, find_symbol_x(&terminal, body.y, body.width, "p"), body.y);

            // herdr: `text`; an unfocused gclient title is `subtext0`.
            assert_eq!(workspace.fg, Some(p.subtext0));
            assert!(!workspace.add_modifier.contains(Modifier::BOLD));
            assert_eq!(agent.fg, Some(p.overlay0));
            // herdr: `!DIM` under `dim = false`; gclient keeps the default dim.
            assert!(agent.add_modifier.contains(Modifier::DIM));
        }

        fn default_space_workspace_style_tracks_active_state() {
            let board = Board::new(&["one", "two"]);
            let mut chrome = chrome();
            focus(&mut chrome, &board, "one");
            chrome.mode = Mode::Terminal;
            let area = Rect::new(0, 0, 26, 20);
            let (terminal, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let first_row = hits.roster[0].1.y;
            let second_row = hits.roster[1].1.y;
            let p = palette();

            let active = style_at(&terminal, find_symbol_x(&terminal, first_row, 25, "o"), first_row);
            assert_eq!(active.fg, Some(p.text));
            assert!(active.add_modifier.contains(Modifier::BOLD));
            assert!(!active.add_modifier.contains(Modifier::DIM));
            assert_eq!(active.bg, Some(p.surface_dim));

            let inactive = style_at(&terminal, find_symbol_x(&terminal, second_row, 25, "t"), second_row);
            assert_eq!(inactive.fg, Some(p.subtext0));
            assert!(!inactive
                .add_modifier
                .intersects(Modifier::BOLD | Modifier::DIM));
            // herdr: `Color::Reset`; gclient fills the sidebar with `panel_bg`.
            assert_eq!(inactive.bg, Some(p.panel_bg));
        }

        fn space_occurrence_style_applies_without_styling_separator() {
            // herdr styles a custom `$hype` token (`HI`, fg #abcdef, bold);
            // gclient's styled occurrence is the active title in `text` bold.
            let board = Board::new(&["HI"]);
            let mut chrome = chrome();
            focus(&mut chrome, &board, "HI");
            chrome.mode = Mode::Terminal;
            let (terminal, hits) = draw_sidebar(&board, &chrome, 26, 20);
            let row = hits.roster[0].1.y;
            let p = palette();
            let h = style_at(&terminal, find_symbol_x(&terminal, row, 25, "H"), row);
            let i = style_at(&terminal, find_symbol_x(&terminal, row, 25, "I"), row);
            let separator = style_at(&terminal, find_symbol_x(&terminal, row, 25, "·"), row);

            for style in [h, i] {
                // herdr: the configured `#abcdef`.
                assert_eq!(style.fg, Some(p.text));
                assert!(style.add_modifier.contains(Modifier::BOLD));
                assert!(!style.add_modifier.contains(Modifier::DIM));
                assert_eq!(style.bg, Some(p.surface_dim));
            }
            assert_eq!(separator.fg, Some(p.overlay0));
            assert!(separator.add_modifier.contains(Modifier::DIM));
            assert!(!separator.add_modifier.contains(Modifier::BOLD));
            assert_eq!(separator.bg, Some(p.surface_dim));
        }

        fn occurrence_foreground_flattens_composite_git_status_colors() {
            // herdr colours a `git_status` token (`↑2 ↓1`) with a configured
            // fg; gclient has no git tokens, so the composite is two text
            // tokens carrying one override colour (`mauve` for `#123456`).
            let p = palette();
            let override_style = Style::default().fg(p.mauve);
            let spans = fitted_spans(
                ("↑2", override_style),
                ("↓1", override_style),
                &[],
                &p,
                20,
            );

            assert_eq!(spans_text(&spans), "↑2 ↓1");
            // herdr: every span, separator included; gclient keeps its
            // separator style on the blank between the tokens.
            assert!(spans
                .iter()
                .filter(|span| span.content.as_ref() != " ")
                .all(|span| span.style.fg == Some(p.mauve)));
        }

        fn default_agent_row_gap_packs_rendering_and_scroll_geometry() {
            let mut board = Board::new(&[]);
            board.add("pi", "pi");
            board.add("claude", "claude");
            let mut chrome = chrome();
            // herdr's 20x5 agent panel has a two-row body; gclient's roster
            // body is two rows with a five-row roster section.
            chrome.sidebar.section_split = Some(5);
            let area = Rect::new(0, 0, 20, 8);
            let body = roster_body(area, chrome.sidebar.section_split);
            let metrics = list_metrics(2, body.height, chrome.sidebar.scroll);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 2);
            assert_eq!(metrics.max_offset_from_bottom, 0);
            // herdr: `" pi"` / `" claude"` (agent-only rows); gclient leads
            // with the state glyph and title on consecutive rows.
            let first = row_str(&terminal, body.y, body.width);
            let second = row_str(&terminal, body.y + 1, body.width);
            assert!(first.contains(" pi"), "rendered row: {first:?}");
            assert!(second.contains(" claude"), "rendered row: {second:?}");
        }


        fn stripped_terminal_title_renders_with_unicode_width_truncation() {
            // herdr strips the `⠋` spinner from the terminal title; gclient's
            // row title is the terminal id, which never carries one.
            let mut board = Board::new(&[]);
            board.add("修复🙂标题很长", "claude");
            let chrome = chrome();
            let area = Rect::new(0, 0, 10, 12);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = roster_body(area, None);
            let rendered = row_str(&terminal, body.y, 9);

            assert!(!rendered.contains('⠋'));
            assert!(rendered.contains('修') && rendered.contains('复'));

            let spans = fitted_spans(
                ("", Style::default()),
                ("修复🙂标题很长", Style::default()),
                &[],
                &palette(),
                8,
            );
            let text = spans_text(&spans);
            assert!(display_width(&text) <= 8, "resolved title: {text:?}");
        }

        fn variable_agent_heights_pack_the_bottom_and_reveal_targets() {
            // herdr's first agent spans three rows (agent + two custom
            // tokens) in a six-row panel; gclient rows are one line each, so
            // three terminals in a two-row body carry the same geometry.
            let board = Board::new(&["one", "two", "three"]);
            let mut chrome = chrome();
            chrome.sidebar.section_split = Some(5);
            let area = Rect::new(0, 0, 20, 8);
            let body = roster_body(area, chrome.sidebar.section_split);
            assert_eq!(body.height, 2);

            let metrics = list_metrics(3, body.height, 0);
            assert_eq!(metrics.max_offset_from_bottom, 1);
            // herdr: `agent_panel_scroll_for_target(&app, area, 0, 2) == 1`;
            // scrolling one row reveals the target row the packed layout hid.
            chrome.sidebar.scroll = 1;
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let ids: Vec<&str> = hits.roster.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["two", "three"]);
        }

        fn oversized_space_layout_is_clipped_to_the_section_body() {
            // herdr's six-row space cards overflow a one-row body; gclient's
            // one-line rows meet the same body via a four-row roster section.
            let board = Board::new(&["one", "two"]);
            let mut chrome = chrome();
            chrome.sidebar.section_split = Some(4);
            let area = Rect::new(0, 0, 20, 10);
            let body = roster_body(area, chrome.sidebar.section_split);

            let metrics = list_metrics(2, body.height, chrome.sidebar.scroll);
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(hits.roster.len(), 1);
            assert_eq!(hits.roster[0].0, "one");
            assert_eq!(hits.roster[0].1.height, body.height);
        }

        fn oversized_agent_override_is_clipped_to_the_panel_body() {
            // herdr overrides claude's rows with six agent tokens in a 20x5
            // panel; gclient's attention entry is one line in the same body.
            let mut board = Board::new(&[]);
            board.add("one", "claude");
            board.set_state("one", RowState::Attention);
            let mut chrome = chrome();
            chrome.sidebar.section_split = Some(5);
            let area = Rect::new(0, 0, 20, 10);
            let panel = expanded_sections(area, chrome.sidebar.section_split).1;
            assert_eq!(panel.height, 5);
            let body = attention_body_rect(panel, false);

            let metrics = list_metrics(1, body.height, chrome.sidebar.attention_scroll);
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(metrics.max_offset_from_bottom, 0);
            let entry = hits.attention.last().expect("one attention entry").1;
            // herdr: the clipped entry height equals the body height; a
            // one-line gclient entry sits inside it instead.
            assert_eq!(body.intersection(entry), entry);
            assert_eq!(entry.height, 1);
        }

        fn render_sidebar_toggle_draws_expanded_collapse_icon() {
            let board = Board::new(&[]);
            let chrome = chrome();
            let area = Rect::new(0, 0, 26, 20);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);

            let toggle = expanded_toggle_rect(area);
            assert_eq!(cell(&terminal, toggle.x, toggle.y).symbol(), "«");
        }

        fn expanded_sidebar_toggle_sits_inside_sidebar_content() {
            let area = Rect::new(0, 0, 26, 20);
            let toggle = expanded_toggle_rect(area);

            assert_eq!(toggle.x, area.x + area.width - 2);
            assert_eq!(toggle.y, area.y + area.height - 1);
        }

        fn agent_panel_tab_label_visibility_tracks_tab_identity() {
            // herdr: `[("auto", None), ("custom", Some("focus")),
            // ("multi", Some("1")), ("multi", Some("logs"))]`; gclient rows
            // carry no tab token, so every entry keeps its terminal label.
            let mut board = Board::new(&[]);
            board.add("auto", "pi");
            board.add("custom", "claude");
            board.add("multi", "codex");
            board.add("multi-logs", "pi");
            let chrome = chrome();

            let entries = roster_rows(&board, &chrome);
            let labels: Vec<_> = entries
                .iter()
                .map(|entry| (entry.label.as_str(), entry.kind))
                .collect();

            assert_eq!(
                labels,
                [
                    ("auto", RowKind::Terminal),
                    ("custom", RowKind::Terminal),
                    ("multi", RowKind::Terminal),
                    ("multi-logs", RowKind::Terminal),
                ]
            );
            for entry in &entries {
                let text = line_text(entry, 60, &chrome);
                assert!(!text.contains("focus") && !text.contains("· 1 ·"), "{text:?}");
            }
        }

        fn priority_agent_panel_sort_uses_attention_then_space_order() {
            // herdr: priority sort yields `["four", "two", "one", "three"]`;
            // gclient has no sort modes: attention entries lead in their own
            // section and the roster keeps space order.
            let mut board = Board::new(&["one", "two", "three", "four"]);
            board.set_state("one", RowState::Working);
            board.set_state("two", RowState::Unseen);
            board.set_state("three", RowState::Working);
            board.set_state("four", RowState::Attention);
            let mut chrome = chrome();
            focus(&mut chrome, &board, "one");

            let attention: Vec<String> = attention_rows(&board, &chrome)
                .into_iter()
                .map(|entry| entry.label)
                .collect();
            let labels: Vec<String> = roster_rows(&board, &chrome)
                .into_iter()
                .map(|entry| entry.label)
                .collect();

            assert_eq!(attention, ["four"]);
            assert_eq!(labels, ["one", "two", "three", "four"]);
        }

        fn collapsed_sidebar_numbers_grouped_agents_by_list_position() {
            let mut board = Board::new(&[]);
            board.add("one", "claude");
            board.add("two", "claude");
            let chrome = chrome();
            let area = Rect::new(0, 0, 4, 12);
            // herdr's agent section is gclient's terminal (roster) rail.
            let (detail_area, _, _) = collapsed_sections(area);
            let (terminal, _) = draw_collapsed(&board, &chrome, area.width, area.height);

            assert_eq!(cell(&terminal, detail_area.x, detail_area.y).symbol(), "1");
            assert_eq!(cell(&terminal, detail_area.x, detail_area.y + 1).symbol(), "2");
        }

        fn collapsed_sidebar_keeps_status_visible_for_two_digit_positions() {
            let mut board = Board::new(&[]);
            for idx in 1..=10 {
                board.add(&format!("workspace-{idx}"), "claude");
            }
            let chrome = chrome();
            let area = Rect::new(0, 0, 4, 25);
            let (detail_area, _, _) = collapsed_sections(area);
            let (terminal, _) = draw_collapsed(&board, &chrome, area.width, area.height);

            let tenth_row = detail_area.y + 9;
            assert_eq!(cell(&terminal, detail_area.x, tenth_row).symbol(), "1");
            assert_eq!(cell(&terminal, detail_area.x + 1, tenth_row).symbol(), "0");
            // herdr: `"·"`, its idle glyph; gclient's idle glyph via `state_dot`.
            assert_eq!(
                cell(&terminal, detail_area.x + 2, tenth_row).symbol(),
                dot(RowState::Idle)
            );
        }

        fn collapsed_sidebar_numbers_priority_agents_by_list_position() {
            // herdr splits workspace "two" into a second, blocked pane and
            // priority-sorts it first; gclient lists it as its own terminal
            // and the blocked state leads the attention rail instead.
            let mut board = Board::new(&[]);
            board.add("one", "claude");
            board.add("two", "claude");
            board.add("two-2", "claude");
            board.set_state("one", RowState::Working);
            board.set_state("two", RowState::Working);
            board.set_state("two-2", RowState::Attention);
            let chrome = chrome();

            assert_eq!(roster_rows(&board, &chrome)[2].label, "two-2");
            assert_eq!(attention_rows(&board, &chrome)[0].label, "two-2");

            let area = Rect::new(0, 0, 4, 16);
            let (detail_area, _, attention_area) = collapsed_sections(area);
            let (terminal, _) = draw_collapsed(&board, &chrome, area.width, area.height);

            assert_eq!(cell(&terminal, detail_area.x, detail_area.y).symbol(), "1");
            assert_eq!(cell(&terminal, detail_area.x, detail_area.y + 1).symbol(), "2");
            assert_eq!(cell(&terminal, detail_area.x, detail_area.y + 2).symbol(), "3");
            assert_eq!(cell(&terminal, attention_area.x + 2, attention_area.y).symbol(), "●");
            assert_eq!(
                style_at(&terminal, attention_area.x + 2, attention_area.y).fg,
                Some(palette().red)
            );
        }

        fn all_workspaces_agent_panel_entries_use_live_root_runtime_cwd_for_workspace_label() {
            // herdr spawns a shell runtime and reads its live cwd; gclient
            // labels a roster row with the daemon's terminal id, so the live
            // checkout name arrives as the id itself.
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("tokio runtime")
                .block_on(async {
                    let unique = format!(
                        "{PRODUCT}-agent-panel-runtime-cwd-{}-{}",
                        std::process::id(),
                        std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .expect("clock after epoch")
                            .as_nanos()
                    );
                    let root = std::env::temp_dir().join(unique);
                    let stale_cwd = root.join("issue-264-nix-support");
                    let live_cwd = root.join(PRODUCT);
                    std::fs::create_dir_all(stale_cwd.join(".git")).expect("stale checkout");
                    std::fs::create_dir_all(live_cwd.join(".git")).expect("live checkout");

                    let live_name = live_cwd
                        .file_name()
                        .and_then(|name| name.to_str())
                        .expect("utf-8 checkout name")
                        .to_string();
                    let mut board = Board::new(&[]);
                    board.add(&live_name, "pi");
                    let mut chrome = chrome();
                    focus(&mut chrome, &board, &live_name);

                    tokio::task::yield_now().await;
                    let entries = roster_rows(&board, &chrome);
                    let primary_label = entries[0].label.clone();

                    let _ = std::fs::remove_dir_all(root);

                    assert_eq!(primary_label, PRODUCT);
                });
        }

        fn all_workspaces_agent_panel_entries_prefer_agent_names_for_agent_identity() {
            let mut board = Board::new(&[]);
            // herdr `set_agent_name("planner")` on a pi agent.
            board.add("bridge", "planner");
            let mut chrome = chrome();
            focus(&mut chrome, &board, "bridge");

            let entries = roster_rows(&board, &chrome);
            assert_eq!(entries[0].label, "bridge");
            assert_eq!(entries[0].detail.split(' ').next(), Some("planner"));
        }

        fn expanded_sidebar_sections_handle_tiny_heights() {
            // herdr's 0.9 ratio of five rows is a four-row roster request.
            let (ws_area, detail_area) = expanded_sections(Rect::new(0, 0, 20, 5), Some(4));

            assert_eq!(ws_area, Rect::new(0, 0, 19, 3));
            assert_eq!(detail_area, Rect::new(0, 3, 19, 2));
        }

        fn sidebar_section_divider_is_hidden_for_tiny_heights() {
            // herdr `sidebar_section_divider_rect(20x5, 0.5) == Rect::default()`:
            // gclient draws the attention rule only with a three-row header,
            // and the collapsed rail drops its divider under seven rows.
            let area = Rect::new(0, 0, 20, 5);
            let (_, divider, _) = collapsed_sections(area);
            assert_eq!(divider, None);

            let board = Board::new(&["one"]);
            let (terminal, _) = draw_sidebar(&board, &chrome(), area.width, area.height);
            assert!(rows(&terminal).iter().all(|row| !row.starts_with("──")));
        }

        fn grouped_child_label_keeps_custom_workspace_name() {
            // herdr `grouped_child_display_label("renamed issue", branch, true)`.
            let board = Board::new(&["renamed issue"]);
            assert_eq!(roster_rows(&board, &chrome())[0].label, "renamed issue");
        }

        fn grouped_child_label_uses_short_branch_for_auto_named_workspace() {
            // herdr shortens an auto-named worktree child to `issue-137`;
            // gclient dropped worktree grouping, so the row keeps its name.
            let name = format!("{PRODUCT}-issue");
            let board = Board::new(&[name.as_str()]);
            assert_eq!(roster_rows(&board, &chrome())[0].label, name);
        }

        fn workspace_list_truncates_cjk_branch_without_panic() {
            let mut board = Board::new(&[]);
            // herdr's cached git branch becomes the row's detail token.
            board.add("repo", "feature/中文-分支-644");
            let mut chrome = chrome();
            focus(&mut chrome, &board, "repo");
            chrome.mode = Mode::Terminal;

            // herdr draws the list straight into a 15x6 rect; gclient's
            // section clamps need eight rows for a one-row roster body.
            let (terminal, hits) = draw_sidebar(&board, &chrome, 15, 8);
            assert_eq!(hits.roster.len(), 1);
            assert!(row_str(&terminal, hits.roster[0].1.y, 14).contains("repo"));
        }

        fn parent_workspace_row_stays_clickable_when_grouped() {
            let board = Board::new(&["main", "issue"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let cards = &hits.roster;

            assert_eq!(cards[0].0, "main");
            assert_eq!(cards[1].0, "issue");
            // herdr: `cards[1].indented`; gclient dropped worktree grouping,
            // so both rows start at the same column.
            assert_eq!(cards[1].1.x, cards[0].1.x);
            assert_eq!(cards[1].1.y, cards[0].1.y + cards[0].1.height);
        }

        fn compact_space_group_scroll_clamps_when_all_entries_fit() {
            let board = Board::new(&["main", "one", "two"]);
            let mut chrome = chrome();
            let area = Rect::new(0, 0, 30, 20);
            chrome.sidebar.scroll = 2;
            let body = roster_body(area, None);

            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let cards = &hits.roster;

            assert_eq!(cards[0].1.y, body.y);
            assert_eq!(cards.len(), 3);
            assert_eq!(cards[2].0, "two");
        }

        fn workspace_scroll_metrics_count_display_entries_not_raw_workspaces() {
            // herdr collapses `issue` under `main`, leaving two display
            // entries of two rows each in a one-entry viewport.
            let metrics = list_metrics(2, 1, 0);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(metrics.max_offset_from_bottom, 1);
            assert_eq!(metrics.offset_from_bottom, 1);
        }

        fn workspace_scroll_offset_applies_to_group_children() {
            // herdr hides the collapsed child `issue`; the display entries
            // are `main` and `notes` in a one-row body.
            let board = Board::new(&["main", "notes"]);
            let mut chrome = chrome();
            chrome.mode = Mode::Terminal;
            chrome.sidebar.section_split = Some(4);
            chrome.sidebar.scroll = 1;

            let (_, hits) = draw_sidebar(&board, &chrome, 30, 12);
            let cards = &hits.roster;

            assert_eq!(cards.len(), 1);
            assert_eq!(cards[0].0, "notes");
        }

        fn workspace_list_entries_do_not_group_normal_git_workspaces() {
            let board = Board::new(&["one", "two"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let entries: Vec<(&str, u16)> = hits
                .roster
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("one", 0), ("two", 0)]);
        }

        fn workspace_list_entries_do_not_auto_attach_normal_git_workspace_to_group() {
            // herdr: `[main, issue (indented), scratch]`; with grouping
            // dropped gclient keeps list order and no indentation.
            let board = Board::new(&["main", "scratch", "issue"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let entries: Vec<(&str, u16)> = hits
                .roster
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("main", 0), ("scratch", 0), ("issue", 0)]);
        }

        fn workspace_list_entries_leave_single_git_and_non_git_workspaces_flat() {
            let board = Board::new(&["one", "notes"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let entries: Vec<(&str, u16)> = hits
                .roster
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("one", 0), ("notes", 0)]);
        }

        fn collapsed_group_hides_inactive_children_but_keeps_active_visible() {
            let board = Board::new(&["main", "issue"]);
            let mut chrome = chrome();
            focus(&mut chrome, &board, "issue");
            chrome.mode = Mode::Terminal;

            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.roster.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["main", "issue"]);

            let chrome = self::chrome();
            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.roster.iter().map(|(id, _)| id.as_str()).collect();
            // herdr: `["main"]` once the child is inactive; gclient dropped
            // group collapsing, so the flat roster keeps both rows.
            assert_eq!(ids, ["main", "issue"]);
        }

        fn collapsed_group_keeps_selected_child_visible_in_navigate_mode() {
            let board = Board::new(&["main", "issue"]);
            let mut chrome = chrome();
            chrome.mode = Mode::Navigate;
            chrome.sidebar.selected = 1;
            focus(&mut chrome, &board, "issue");

            let (terminal, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.roster.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["main", "issue"]);
            assert!(row_str(&terminal, hits.roster[1].1.y, 29).starts_with("▸"));
        }
    }
    "src/ui/sidebar/tokens.rs" => {
        fn missing_custom_tokens_elide_rows_and_separators() {
            // herdr: rows `[state_icon, $missing]`, `[$missing]`, `[agent]`
            // resolve to two rows; gclient elides the empty detail token and
            // its separator from the single line.
            let chrome = chrome();
            let row = plain_row("pi", RowState::Working, "");

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} pi · working", dot(RowState::Working)));
            assert_eq!(text.matches('·').count(), 1);
        }

        fn state_text_and_arbitrary_values_are_independent_tokens() {
            // herdr: `[state_text, $summary]` resolve independently.
            let chrome = chrome();
            let row = plain_row("repo", RowState::Working, "reviewing auth");

            let text = line_text(&row, 60, &chrome);
            assert_eq!(
                text,
                format!(" {} repo · working · reviewing auth", dot(RowState::Working))
            );
        }

        fn terminal_title_builtins_are_distinct_from_custom_tokens() {
            // herdr: raw title `⠋ raw title`, stripped `raw title`, custom
            // `custom title`; gclient's title is the row label and the custom
            // value is the detail token.
            let chrome = chrome();
            let row = plain_row("raw title", RowState::Working, "custom title");

            let text = line_text(&row, 60, &chrome);
            assert!(!text.contains('⠋'));
            assert_eq!(
                text,
                format!(" {} raw title · working · custom title", dot(RowState::Working))
            );
        }

        fn known_agent_override_replaces_default_rows() {
            // herdr: `rows_by_agent["pi"]` wins while `entry.agent` is `Pi`
            // and the default rows return once it is `None`; gclient shows
            // the agent's detail while its pane is attached and the default
            // `detached` text once it is gone.
            let mut board = Board::new(&[]);
            board.add("repo", "renamed pi");
            let chrome = chrome();

            let detail = roster_rows(&board, &chrome)[0].detail.clone();
            assert!(detail.starts_with("renamed pi"), "{detail:?}");

            board.set_state("repo", RowState::Unknown);
            assert_eq!(roster_rows(&board, &chrome)[0].detail, "detached");
            assert_eq!(roster_rows(&board, &chrome)[0].label, "repo");
        }

        fn grouped_children_suppress_all_builtin_git_details() {
            // herdr: `[state_icon, workspace("feature")]` with the branch and
            // `↑2 ↓1` suppressed; gclient rows never carry git details.
            let chrome = chrome();
            let row = plain_row("feature", RowState::Idle, "");

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} feature · idle", dot(RowState::Idle)));
            assert!(!text.contains("worktree/feature") && !text.contains('↑'));
        }

        fn workspace_custom_token_can_replace_git_specific_details() {
            // herdr: `[$jj_status]` resolves to `2 changes`.
            let chrome = chrome();
            let row = plain_row("repo", RowState::Idle, "2 changes");

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} repo · idle · 2 changes", dot(RowState::Idle)));
            assert!(!text.contains('↑') && !text.contains('↓'));
        }
    }
}
