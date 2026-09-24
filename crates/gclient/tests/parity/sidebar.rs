//! herdr `src/ui/sidebar.rs` (30) and `src/ui/sidebar/tokens.rs` (6) keep-set
//! render tests.
//!
//! State mapping: a herdr workspace card is a gclient project card (one
//! line, `glyph name (branch ↑a ↓b)`, worktree rows under the one expanded
//! card); a herdr agent-panel entry is a sessions row (one terminal, two
//! lines); a herdr attention state is the row's attention entry. herdr's
//! configurable multi-row token layouts collapse to gclient's fixed `glyph
//! title` line (only a needs-you row carries a trailing word) over a
//! `provider · model · task · tab · machine` line, where the detected agent
//! kind becomes the provider token. Per-token style overrides are a dropped
//! surface; their fixtures map to the fixed rows and each adapted assertion
//! carries the herdr original in a comment. The sidebar has no draggable
//! section rules: `sidebar_layout` sizes the machines and projects sections
//! by their rows and the sessions section takes the rest.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::sidebar_model::{AgentEntry, ProjectEntry, SidebarModel, WorktreeEntry};
use gobby_client::app::{
    apply_rename, rename_project, route_modal_key, route_mouse, Backend, ContextMenuKind,
    ModalOutcome, MouseGesture, MouseOutcome, Pane, PaneId, Workspace, MOUSE_SCROLL_LINES,
    PROJECT_DRAG_THRESHOLD,
};
use gobby_client::daemon::{
    Attention, Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow,
};
use gobby_client::key_input::KeyInput;
use gobby_client::ui::chrome::{Chrome, Mode, RowState, WorkspaceView};
use gobby_client::ui::chrome_render::render_workspace;
use gobby_client::ui::dialogs::{Dialog, RenameKind};
use gobby_client::ui::hit::SidebarSection;
use gobby_client::ui::scrollbar::scrollbar_thumb_grab_offset;
use gobby_client::ui::settings::AgentSort;
use gobby_client::ui::sidebar::{
    next_machine_filter, project_list_metrics, render_sidebar, section_body_rect, section_metrics,
    section_rects, session_rows, sidebar_layout, SidebarHits, ALL_MACHINES,
};
use gobby_client::ui::sidebar_rows::{
    fitted_spans, project_rows, row_line, row_second_line, RowKind, SidebarRow,
};
use gobby_client::ui::status::{state_dot, state_label};
use gobby_client::ui::text::display_width;
use gobby_client::ui::Action;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use ratatui::Terminal;
use serde_json::json;

use super::fixtures::{cell, render, rows};
use super::token_map::{palette, theme};

/// herdr fixtures name the product; gclient's rows name gobby (rule 3).
const PRODUCT: &str = "gobby";
/// Pane backend for terminals whose herdr entry has no detected agent.
const DEFAULT_BACKEND: &str = "native";
/// The board's own machine id: where herdr's local agents run.
const LOCAL_MACHINE: &str = "local";

/// herdr `AppState` + `Workspace::test_new` stand-in: each herdr workspace
/// is one project card and one terminal attached to one pane, listed as an
/// agent row through its `agent:<name>` entry.
struct Board {
    roster: Vec<String>,
    attention: Vec<String>,
    panes: Vec<Pane>,
    sidebar: SidebarModel,
    focused: Option<String>,
}

impl Board {
    fn new(names: &[&str]) -> Self {
        let mut board = Board {
            roster: Vec::new(),
            attention: Vec::new(),
            panes: Vec::new(),
            sidebar: SidebarModel {
                local_machine: LOCAL_MACHINE.to_string(),
                machines: vec![LOCAL_MACHINE.to_string()],
                ..SidebarModel::default()
            },
            focused: None,
        };
        for name in names {
            board.add(name, DEFAULT_BACKEND);
        }
        board
    }

    /// Adds a workspace: a project card and a terminal whose `agent` is the
    /// herdr detected agent kind, listed as the row's provider.
    fn add(&mut self, name: &str, agent: &str) -> PaneId {
        self.add_on(name, name, agent, LOCAL_MACHINE)
    }

    /// herdr's remote agent: a terminal of `project` running on `machine`.
    fn add_remote(&mut self, project: &str, name: &str, machine: &str) -> PaneId {
        if !self.sidebar.machines.iter().any(|known| known == machine) {
            self.sidebar.machines.push(machine.to_string());
            self.sidebar.machines.sort();
        }
        self.add_on(project, name, DEFAULT_BACKEND, machine)
    }

    fn add_on(&mut self, project: &str, name: &str, agent: &str, machine: &str) -> PaneId {
        let id = PaneId(self.panes.len() as u32 + 1);
        // herdr names its rows; the board's `name` is that name, not a UUID,
        // and it reaches the row as the terminal id these panes are keyed by.
        let pane = Pane::new(id, name, Backend::parse(DEFAULT_BACKEND), "epoch");
        self.panes.push(pane);
        self.roster.push(name.to_string());
        self.attention.push(format!("agent:{name}"));
        if !self
            .sidebar
            .projects
            .iter()
            .any(|known| known.project_id == project)
        {
            self.sidebar.projects.push(ProjectEntry {
                project_id: project.to_string(),
                name: project.to_string(),
                ..ProjectEntry::default()
            });
        }
        self.sidebar.agents.push(AgentEntry {
            entry_id: format!("agent:{name}"),
            project_id: project.to_string(),
            machine_id: machine.to_string(),
            terminal_id: name.to_string(),
            backend: Backend::parse(DEFAULT_BACKEND),
            name: name.to_string(),
            provider: agent.to_string(),
            ..AgentEntry::default()
        });
        id
    }

    fn agent_mut(&mut self, name: &str) -> &mut AgentEntry {
        self.sidebar
            .agents
            .iter_mut()
            .find(|agent| agent.terminal_id == name)
            .expect("known agent")
    }

    fn project_mut(&mut self, name: &str) -> &mut ProjectEntry {
        self.sidebar
            .projects
            .iter_mut()
            .find(|project| project.project_id == name)
            .expect("known project")
    }

    /// herdr's worktree child under the `project` card; the row id is the
    /// branch without its `worktree/` prefix.
    fn add_worktree(&mut self, project: &str, branch: &str, task_ref: Option<&str>) {
        let id = branch.strip_prefix("worktree/").unwrap_or(branch);
        self.project_mut(project).worktrees.push(WorktreeEntry {
            worktree_id: id.to_string(),
            branch: Some(branch.to_string()),
            path: std::path::PathBuf::from(format!("/repos/{project}/.worktrees/{id}")),
            task_ref: task_ref.map(str::to_string),
            ..WorktreeEntry::default()
        });
    }

    /// herdr's cached git branch on the workspace card.
    fn set_branch(&mut self, name: &str, branch: &str) {
        self.project_mut(name).branch = Some(branch.to_string());
    }

    fn pane_id(&self, name: &str) -> PaneId {
        self.pane_for_terminal(name).expect("known terminal")
    }

    /// herdr `terminal.state = ...` for the pane behind `name`.
    fn set_state(&mut self, name: &str, state: RowState) {
        match state {
            RowState::Attention => {
                // The chrome reads blockedness from the agent row, as the
                // typed roster carries it; the entry id alone does not.
                let agent = self.agent_mut(name);
                agent.state = RowState::Attention;
                agent.attention = Some(Attention::default());
            }
            RowState::Working | RowState::Unseen => {
                let pane = self
                    .panes
                    .iter_mut()
                    .find(|pane| pane.terminal_id == name)
                    .expect("known terminal");
                pane.new_output = true;
                pane.live = state == RowState::Working;
                // A working herdr agent is a running gobby run.
                self.agent_mut(name).lifecycle_status =
                    (state == RowState::Working).then(|| "running".to_string());
            }
            RowState::Idle => {}
            RowState::Paused => {
                self.agent_mut(name).lifecycle_status = Some("awaiting_input".to_string());
            }
            RowState::Orphaned => {
                let agent = self.agent_mut(name);
                agent.state = RowState::Orphaned;
                agent.terminal_state = Some("orphaned".to_string());
            }
            RowState::Unknown => self.panes.retain(|pane| pane.terminal_id != name),
        }
    }
}

impl WorkspaceView for Board {
    fn gobby_home(&self) -> Option<&std::path::Path> {
        None
    }

    fn workspace_model(&self) -> Option<&gobby_client::app::WorkspaceModel> {
        None
    }

    fn project_id(&self) -> Option<&str> {
        None
    }

    fn focused_project(&self) -> Option<&str> {
        self.focused.as_deref()
    }

    fn sidebar(&self) -> &SidebarModel {
        &self.sidebar
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

/// herdr's agent panel spans every workspace; gclient's agents section
/// lists the focused project's local agents until the filter is `all`.
/// herdr's sidebar is always on screen, so gclient's is pinned.
fn chrome() -> Chrome {
    let mut chrome = Chrome::new(theme());
    chrome.sidebar.pinned = true;
    chrome.sidebar.machine_filter = Some(ALL_MACHINES.to_string());
    chrome
}

/// herdr `app.active = Some(ws_idx)`: focus the workspace's project card
/// and its terminal's pane.
fn focus(chrome: &mut Chrome, board: &mut Board, name: &str) {
    chrome.open_tab(board.pane_id(name), name);
    board.focused = Some(name.to_string());
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

/// The list body of `section` (herdr's workspace-list or agent-panel body):
/// the section's rect under its band, as `sidebar_layout` sizes it from the
/// board's rows.
fn section_body<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    area: Rect,
    section: SidebarSection,
) -> Rect {
    section_body_rect(
        section_rects(ws, chrome, area)[section.index()],
        section,
        false,
    )
}

fn spans_text(spans: &[Span<'_>]) -> String {
    spans.iter().map(|span| span.content.as_ref()).collect()
}

fn line_text(row: &SidebarRow, width: u16, chrome: &Chrome) -> String {
    spans_text(&row_line(row, width, chrome, 0).spans)
}

fn second_text(row: &SidebarRow, width: u16, chrome: &Chrome) -> String {
    spans_text(&row_second_line(row, width, chrome).spans)
}

fn plain_row(label: &str, state: RowState, tokens: &[&str]) -> SidebarRow {
    SidebarRow {
        id: label.to_string(),
        label: label.to_string(),
        kind: RowKind::Agent,
        state,
        tokens: tokens.iter().map(|token| token.to_string()).collect(),
        ..SidebarRow::default()
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
            focus(&mut chrome, &mut board, "one");
            let area = Rect::new(0, 0, 26, 24);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = section_body(&board, &chrome, area, SidebarSection::Sessions);
            let p = palette();

            let first = row_str(&terminal, body.y, 25);
            let second = row_str(&terminal, body.y + 1, 25);
            assert_eq!(first, format!(" {} one", dot(RowState::Working)));
            assert_eq!(second, "   pi");
            // herdr: `!contains("working")`; gclient's working glyph is the
            // whole indicator, so neither line carries the word.
            assert!(!first.contains("working"));
            assert!(!second.contains("working"));

            let workspace_x = find_symbol_x(&terminal, body.y, body.width, "o");
            let workspace_style = style_at(&terminal, workspace_x, body.y);
            assert_eq!(workspace_style.fg, Some(p.text));
            assert!(workspace_style.add_modifier.contains(Modifier::BOLD));
            assert!(!workspace_style.add_modifier.contains(Modifier::DIM));
            assert_eq!(workspace_style.bg, Some(p.surface_dim));

            let agent_x = find_symbol_x(&terminal, body.y + 1, body.width, "p");
            let agent_style = style_at(&terminal, agent_x, body.y + 1);
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
            let area = Rect::new(0, 0, 26, 24);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = section_body(&board, &chrome, area, SidebarSection::Sessions);
            let p = palette();
            let workspace = style_at(&terminal, find_symbol_x(&terminal, body.y, body.width, "o"), body.y);
            let agent = style_at(&terminal, find_symbol_x(&terminal, body.y + 1, body.width, "p"), body.y + 1);

            // herdr: `text`; an unfocused gclient title is `subtext0`.
            assert_eq!(workspace.fg, Some(p.subtext0));
            // herdr: `!BOLD` under `bold = false`; gclient names every
            // agent in bold (herdr's default workspace token).
            assert!(workspace.add_modifier.contains(Modifier::BOLD));
            assert_eq!(agent.fg, Some(p.overlay0));
            // herdr: `!DIM` under `dim = false`; gclient keeps the default dim.
            assert!(agent.add_modifier.contains(Modifier::DIM));
        }

        fn default_space_workspace_style_tracks_active_state() {
            let mut board = Board::new(&["one", "two"]);
            let mut chrome = chrome();
            focus(&mut chrome, &mut board, "one");
            chrome.mode = Mode::Terminal;
            let area = Rect::new(0, 0, 26, 20);
            let (terminal, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            // One-line cards: each card is its own row.
            assert!(hits.projects.iter().all(|(_, rect)| rect.height == 1));
            let first_row = hits.projects[0].1.y;
            let second_row = hits.projects[1].1.y;
            assert_eq!(second_row, first_row + 1);
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
            // gclient's styled occurrence is the active agent row's title in
            // `text` bold, with the ` · ` separator before the needs-you word
            // (the one trailing token a row carries) left in `overlay0`.
            let mut board = Board::new(&["HI"]);
            board.set_state("HI", RowState::Attention);
            let mut chrome = chrome();
            focus(&mut chrome, &mut board, "HI");
            chrome.mode = Mode::Terminal;
            let (terminal, hits) = draw_sidebar(&board, &chrome, 26, 24);
            let row = hits.agents[0].1.y;
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
            // fg; gclient's agent rows have no git tokens, so the composite
            // is two text tokens carrying one override colour (`mauve` for
            // `#123456`).
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
            let chrome = chrome();
            // herdr's 20x5 agent panel has a two-row body; gclient's two-line
            // rows need the four rows a ten-row sidebar leaves the sessions
            // section under the top half (the machines and projects) and its
            // own band.
            let area = Rect::new(0, 0, 20, 10);
            let body = section_body(&board, &chrome, area, SidebarSection::Sessions);
            assert_eq!(body.height, 4);
            let metrics = project_list_metrics(
                &[2, 2],
                body.height,
                chrome.sidebar.scroll(SidebarSection::Sessions),
            );
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 2);
            assert_eq!(metrics.max_offset_from_bottom, 0);
            // herdr: `" pi"` / `" claude"` (agent-only rows); gclient leads
            // with the state glyph and title on every other row.
            let first = row_str(&terminal, body.y, body.width);
            let second = row_str(&terminal, body.y + 2, body.width);
            assert!(first.contains(" pi"), "rendered row: {first:?}");
            assert!(second.contains(" claude"), "rendered row: {second:?}");
        }


        fn stripped_terminal_title_renders_with_unicode_width_truncation() {
            // herdr strips the `⠋` spinner from the terminal title; gclient's
            // row title is the terminal id, which never carries one.
            let mut board = Board::new(&[]);
            board.add("修复🙂标题很长", "claude");
            let chrome = chrome();
            let area = Rect::new(0, 0, 10, 24);
            let (terminal, _) = draw_sidebar(&board, &chrome, area.width, area.height);
            let body = section_body(&board, &chrome, area, SidebarSection::Sessions);
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
            // tokens) in a six-row panel; gclient agent rows are two lines
            // each, so three terminals in a four-row body carry the same
            // geometry. Nine rows leave the sessions that body: the cards
            // stop at the top half and the sessions take the rest.
            let board = Board::new(&["one", "two", "three"]);
            let mut chrome = chrome();
            let area = Rect::new(0, 0, 20, 9);
            let body = section_body(&board, &chrome, area, SidebarSection::Sessions);
            assert_eq!(body.height, 4);

            let metrics = project_list_metrics(&[2, 2, 2], body.height, 0);
            assert_eq!(metrics.max_offset_from_bottom, 1);
            // herdr: `agent_panel_scroll_for_target(&app, area, 0, 2) == 1`;
            // scrolling one row reveals the target row the packed layout hid.
            *chrome.sidebar.scroll_mut(SidebarSection::Sessions) = 1;
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let ids: Vec<&str> = hits.agents.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["agent:two", "agent:three"]);
        }

        fn oversized_space_layout_is_clipped_to_the_section_body() {
            // herdr's six-row space cards overflow a one-row body; gclient's
            // one-line cards overflow the one-row body an eleven-row sidebar
            // leaves the projects section: the top half holds the machines
            // band, one machine, its blank row, the projects band and one
            // card, whose blank row yields to the card.
            let board = Board::new(&["one", "two"]);
            let chrome = chrome();
            let area = Rect::new(0, 0, 20, 11);
            let body = section_body(&board, &chrome, area, SidebarSection::Projects);
            assert_eq!(body.height, 1);

            let metrics = project_list_metrics(
                &[1, 1],
                body.height,
                chrome.sidebar.scroll(SidebarSection::Projects),
            );
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(hits.projects.len(), 1);
            assert_eq!(hits.projects[0].0, "one");
            assert_eq!(hits.projects[0].1.height, body.height);
        }

        fn oversized_agent_override_is_clipped_to_the_panel_body() {
            // herdr overrides claude's rows with six agent tokens in a 20x5
            // panel; gclient's agent row is two lines in the same body: a
            // five-row sidebar leaves the sessions a three-row section (its
            // band and two rows) once the machines fill the top half.
            let mut board = Board::new(&[]);
            board.add("one", "claude");
            board.set_state("one", RowState::Attention);
            let chrome = chrome();
            let area = Rect::new(0, 0, 20, 5);
            let panel = section_rects(&board, &chrome, area)[SidebarSection::Sessions.index()];
            assert_eq!(panel.height, 3);
            let body = section_body_rect(panel, SidebarSection::Sessions, false);

            let metrics = project_list_metrics(
                &[2],
                body.height,
                chrome.sidebar.scroll(SidebarSection::Sessions),
            );
            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(metrics.max_offset_from_bottom, 0);
            let entry = hits.agents.last().expect("one agent row").1;
            // herdr: the clipped entry height equals the body height; a
            // two-line gclient row fills the two-row body exactly.
            assert_eq!(body.intersection(entry), entry);
            assert_eq!(entry.height, 2);
        }

        fn priority_agent_panel_sort_uses_attention_then_space_order() {
            // herdr: priority sort yields `["four", "two", "one", "three"]`
            // and grouped sort the space order.
            let mut board = Board::new(&["one", "two", "three", "four"]);
            board.set_state("one", RowState::Working);
            board.set_state("two", RowState::Unseen);
            board.set_state("three", RowState::Working);
            board.set_state("four", RowState::Attention);
            let mut chrome = chrome();
            // herdr's panel spans every workspace; gclient lists every
            // project's rows while no project is focused, and the open tab
            // leads the grouped order.
            chrome.open_tab(board.pane_id("one"), "one");

            let entries = session_rows(&board, &chrome);
            let labels: Vec<&str> = entries.iter().map(|entry| entry.label.as_str()).collect();
            assert_eq!(labels, ["one", "two", "three", "four"]);
            assert_eq!(entries[3].state, RowState::Attention);
            assert_eq!(entries[0].state, RowState::Working);

            chrome.prefs.agent_sort = AgentSort::Priority;
            let entries = session_rows(&board, &chrome);
            let labels: Vec<&str> = entries.iter().map(|entry| entry.label.as_str()).collect();
            assert_eq!(labels, ["four", "two", "one", "three"]);
        }

        fn all_workspaces_agent_panel_entries_use_live_root_runtime_cwd_for_workspace_label() {
            // herdr spawns a shell runtime and reads its live cwd; gclient
            // labels a project card with the daemon's project name, so the
            // live checkout name arrives as the name itself.
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
                    focus(&mut chrome, &mut board, &live_name);

                    tokio::task::yield_now().await;
                    let entries = project_rows(&board, &chrome);
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
            focus(&mut chrome, &mut board, "bridge");

            let entries = session_rows(&board, &chrome);
            assert_eq!(entries[0].label, "bridge");
            assert_eq!(entries[0].tokens[0], "planner");
        }

        fn expanded_sidebar_sections_handle_tiny_heights() {
            // herdr's 0.9 ratio of five rows is a four-row projects request;
            // gclient has no ratio: the top half of five rows (two) goes to
            // the machines band and its row, nothing is left for the
            // projects, and the sessions keep the other three.
            let layout = sidebar_layout(Rect::new(0, 0, 20, 5), 1, 4);

            assert_eq!(
                layout.sections,
                [
                    Rect::new(0, 0, 19, 2),
                    Rect::new(0, 2, 19, 0),
                    Rect::new(0, 2, 19, 3),
                ]
            );
            // The drawn sidebar follows the layout: a board with one card
            // shows the bands and the sessions row, and no card.
            let board = Board::new(&["one"]);
            let chrome = chrome();
            let (_, hits) = draw_sidebar(&board, &chrome, 20, 5);
            assert!(hits.projects.is_empty(), "{:?}", hits.projects);
            assert_eq!(
                section_rects(&board, &chrome, Rect::new(0, 0, 20, 5)),
                layout.sections
            );
        }

        fn sidebar_section_divider_is_hidden_for_tiny_heights() {
            // herdr `sidebar_section_divider_rect(20x5, 0.5) == Rect::default()`:
            // gclient's sidebar draws bands instead of rules.
            let area = Rect::new(0, 0, 20, 5);
            let board = Board::new(&["one"]);
            let (terminal, _) = draw_sidebar(&board, &chrome(), area.width, area.height);
            assert!(rows(&terminal).iter().all(|row| !row.starts_with("──")));
        }

        fn grouped_child_label_keeps_custom_workspace_name() {
            // herdr `grouped_child_display_label("renamed issue", branch, true)`.
            let board = Board::new(&["renamed issue"]);
            assert_eq!(project_rows(&board, &chrome())[0].label, "renamed issue");
        }

        fn grouped_child_label_uses_short_branch_for_auto_named_workspace() {
            // herdr shortens an auto-named worktree child to `issue-137`;
            // gclient's worktree row is its branch without `worktree/`, and
            // it lists once its card is expanded.
            let mut board = Board::new(&["main"]);
            board.add_worktree("main", "worktree/issue-137", None);
            let mut chrome = chrome();
            chrome.sidebar.toggle_group("main");
            let rows = project_rows(&board, &chrome);
            assert_eq!(rows[1].kind, RowKind::Worktree);
            assert_eq!(rows[1].label, "issue-137");
            assert!(rows[1].nested && rows[1].last_child);
        }

        fn workspace_list_truncates_cjk_branch_without_panic() {
            let mut board = Board::new(&["repo"]);
            // herdr's cached git branch is the card's parenthetical.
            board.set_branch("repo", "feature/中文-分支-644");
            let mut chrome = chrome();
            focus(&mut chrome, &mut board, "repo");
            chrome.mode = Mode::Terminal;

            // herdr draws the list straight into a 15x6 rect; gclient's
            // one-line card drops the parenthetical whole when it does not
            // fit beside the name, and shows it once the sidebar is wide.
            let (terminal, hits) = draw_sidebar(&board, &chrome, 15, 16);
            assert_eq!(hits.projects.len(), 1);
            let card = hits.projects[0].1;
            assert_eq!(card.height, 1);
            let narrow = row_str(&terminal, card.y, 14);
            assert!(narrow.contains("repo"), "{narrow:?}");
            assert!(!narrow.contains("feature"), "{narrow:?}");
            let (terminal, hits) = draw_sidebar(&board, &chrome, 40, 16);
            let card = hits.projects[0].1;
            // A wide glyph's second cell is blank in the buffer.
            let wide = row_str(&terminal, card.y, 39).replace(' ', "");
            assert!(wide.contains("repo(feature/中文-分支-644)"), "{wide:?}");
        }

        fn parent_workspace_row_stays_clickable_when_grouped() {
            let mut board = Board::new(&["main"]);
            board.add_worktree("main", "worktree/issue", None);
            let mut chrome = chrome();
            chrome.sidebar.toggle_group("main");
            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let main = hits.projects[0].clone();
            let issue = hits.worktrees[0].clone();

            assert_eq!(main.0, "main");
            assert_eq!(issue.0, "issue");
            // herdr: `cards[1].indented`; gclient indents the worktree row's
            // text under its card and keeps both hit rects at one column.
            assert_eq!(issue.1.x, main.1.x);
            assert_eq!(issue.1.y, main.1.y + main.1.height);
        }

        fn compact_space_group_scroll_clamps_when_all_entries_fit() {
            let board = Board::new(&["main", "one", "two"]);
            let mut chrome = chrome();
            let area = Rect::new(0, 0, 30, 30);
            *chrome.sidebar.scroll_mut(SidebarSection::Projects) = 2;
            let body = section_body(&board, &chrome, area, SidebarSection::Projects);

            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let cards = &hits.projects;

            assert_eq!(cards[0].1.y, body.y);
            assert_eq!(cards.len(), 3);
            assert_eq!(cards[2].0, "two");
        }

        fn workspace_scroll_metrics_count_display_entries_not_raw_workspaces() {
            // herdr collapses `issue` under `main`, leaving two display
            // entries (one-line cards) in a one-entry viewport.
            let metrics = project_list_metrics(&[1, 1], 1, 0);

            assert_eq!(metrics.viewport_rows, 1);
            assert_eq!(metrics.max_offset_from_bottom, 1);
            assert_eq!(metrics.offset_from_bottom, 1);
        }

        fn workspace_scroll_offset_applies_to_group_children() {
            // herdr hides the collapsed child `issue`; the display entries
            // are `main` and `notes` in the one-card body an eleven-row
            // sidebar leaves the projects.
            let board = Board::new(&["main", "notes"]);
            let mut chrome = chrome();
            chrome.mode = Mode::Terminal;
            *chrome.sidebar.scroll_mut(SidebarSection::Projects) = 1;
            let area = Rect::new(0, 0, 30, 11);
            assert_eq!(
                section_body(&board, &chrome, area, SidebarSection::Projects).height,
                1
            );

            let (_, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
            let cards = &hits.projects;

            assert_eq!(cards.len(), 1);
            assert_eq!(cards[0].0, "notes");
        }

        fn workspace_list_entries_do_not_group_normal_git_workspaces() {
            let board = Board::new(&["one", "two"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let entries: Vec<(&str, u16)> = hits
                .projects
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("one", 0), ("two", 0)]);
            assert!(hits.worktrees.is_empty());
        }

        fn workspace_list_entries_do_not_auto_attach_normal_git_workspace_to_group() {
            // herdr: `[main, issue (indented), scratch]`; a project that is
            // no worktree of `main` stays its own card in list order.
            let board = Board::new(&["main", "scratch", "issue"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 24);
            let entries: Vec<(&str, u16)> = hits
                .projects
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("main", 0), ("scratch", 0), ("issue", 0)]);
            assert!(hits.worktrees.is_empty());
        }

        fn workspace_list_entries_leave_single_git_and_non_git_workspaces_flat() {
            let board = Board::new(&["one", "notes"]);
            let (_, hits) = draw_sidebar(&board, &chrome(), 30, 20);
            let entries: Vec<(&str, u16)> = hits
                .projects
                .iter()
                .map(|(id, rect)| (id.as_str(), rect.x))
                .collect();

            assert_eq!(entries, [("one", 0), ("notes", 0)]);
        }

        fn collapsed_group_hides_inactive_children_but_keeps_active_visible() {
            let mut board = Board::new(&["main"]);
            board.add_worktree("main", "worktree/issue", None);
            let mut chrome = chrome();

            // herdr: `["main"]` once the child is inactive; gclient folds
            // every card until its group toggle expands it, one at a time.
            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.projects.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["main"]);
            assert!(hits.worktrees.is_empty());
            assert_eq!(hits.group_toggles.len(), 1, "{:?}", hits.group_toggles);

            chrome.sidebar.toggle_group("main");
            assert!(chrome.sidebar.is_expanded("main"));
            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.projects.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["main"]);
            let ids: Vec<&str> = hits.worktrees.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["issue"]);

            chrome.sidebar.toggle_group("main");
            let (_, hits) = draw_sidebar(&board, &chrome, 30, 20);
            assert!(hits.worktrees.is_empty());
        }

        fn collapsed_group_keeps_selected_child_visible_in_navigate_mode() {
            let mut board = Board::new(&["main"]);
            board.add_worktree("main", "worktree/issue", None);
            let mut chrome = chrome();
            chrome.mode = Mode::Navigate;
            chrome.sidebar.toggle_group("main");
            chrome.sidebar.selected = 1;

            let (terminal, hits) = draw_sidebar(&board, &chrome, 30, 20);
            let ids: Vec<&str> = hits.worktrees.iter().map(|(id, _)| id.as_str()).collect();
            assert_eq!(ids, ["issue"]);
            assert!(row_str(&terminal, hits.worktrees[0].1.y, 29).starts_with("▸"));
        }
    }
    "src/ui/sidebar/tokens.rs" => {
        fn missing_custom_tokens_elide_rows_and_separators() {
            // herdr: rows `[state_icon, $missing]`, `[$missing]`, `[agent]`
            // resolve to two rows; gclient elides the empty second line and
            // every separator: a working row is its glyph and title.
            let chrome = chrome();
            let row = plain_row("pi", RowState::Working, &[]);

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} pi", dot(RowState::Working)));
            assert_eq!(text.matches('·').count(), 0);
            assert_eq!(second_text(&row, 60, &chrome), "");
        }

        fn state_text_and_arbitrary_values_are_independent_tokens() {
            // herdr: `[state_text, $summary]` resolve independently; the
            // state text (only a needs-you row carries one) stays on the
            // first line and the value is a second-line token.
            let chrome = chrome();
            let row = plain_row("repo", RowState::Attention, &["reviewing auth"]);

            let text = line_text(&row, 60, &chrome);
            assert_eq!(
                text,
                format!(
                    " {} repo · {}",
                    dot(RowState::Attention),
                    state_label(RowState::Attention)
                )
            );
            assert_eq!(second_text(&row, 60, &chrome), "   reviewing auth");
            let working = plain_row("repo", RowState::Working, &["reviewing auth"]);
            assert_eq!(
                line_text(&working, 60, &chrome),
                format!(" {} repo", dot(RowState::Working))
            );
            assert_eq!(second_text(&working, 60, &chrome), "   reviewing auth");
        }

        fn terminal_title_builtins_are_distinct_from_custom_tokens() {
            // herdr: raw title `⠋ raw title`, stripped `raw title`, custom
            // `custom title`; gclient's title is the row label and the custom
            // value is a second-line token.
            let chrome = chrome();
            let row = plain_row("raw title", RowState::Working, &["custom title"]);

            let text = line_text(&row, 60, &chrome);
            assert!(!text.contains('⠋'));
            assert_eq!(text, format!(" {} raw title", dot(RowState::Working)));
            assert_eq!(second_text(&row, 60, &chrome), "   custom title");
        }

        fn known_agent_override_replaces_default_rows() {
            // herdr: `rows_by_agent["pi"]` wins while `entry.agent` is `Pi`
            // and the default rows return once it is `None`; gclient's row
            // keeps the roster's provider token and name once its pane is
            // gone, and only the state changes.
            let mut board = Board::new(&[]);
            board.add("repo", "renamed pi");
            let chrome = chrome();

            let rows = session_rows(&board, &chrome);
            assert_eq!(rows[0].tokens, ["renamed pi"]);

            board.set_state("repo", RowState::Unknown);
            let rows = session_rows(&board, &chrome);
            assert_eq!(rows[0].tokens, ["renamed pi"]);
            assert_eq!(rows[0].label, "repo");
            assert!(!line_text(&rows[0], 60, &chrome).contains("detached"));
        }

        fn grouped_children_suppress_all_builtin_git_details() {
            // herdr: `[state_icon, workspace("feature")]` with the branch and
            // `↑2 ↓1` suppressed; gclient agent rows never carry git details.
            let chrome = chrome();
            let row = plain_row("feature", RowState::Idle, &[]);

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} feature", dot(RowState::Idle)));
            assert!(!text.contains("worktree/feature") && !text.contains('↑'));
            assert_eq!(second_text(&row, 60, &chrome), "");
        }

        fn workspace_custom_token_can_replace_git_specific_details() {
            // herdr: `[$jj_status]` resolves to `2 changes`.
            let chrome = chrome();
            let row = plain_row("repo", RowState::Idle, &["2 changes"]);

            let text = line_text(&row, 60, &chrome);
            assert_eq!(text, format!(" {} repo", dot(RowState::Idle)));
            let second = second_text(&row, 60, &chrome);
            assert_eq!(second, "   2 changes");
            assert!(!second.contains('↑') && !second.contains('↓'));
        }
    }
}

// herdr `src/app/input/mouse.rs` sidebar rows: the mouse on the agent rows,
// project cards, toggle, edge, section rule and list scrollbars, routed
// through `route_mouse` against a drawn frame.

const LEFT_DOWN: MouseEventKind = MouseEventKind::Down(MouseButton::Left);
const LEFT_DRAG: MouseEventKind = MouseEventKind::Drag(MouseButton::Left);
const LEFT_UP: MouseEventKind = MouseEventKind::Up(MouseButton::Left);

/// `count` scripted roster terminals `term-0..`, no tab open yet.
fn sidebar_workspace(count: usize) -> Workspace {
    let mut ws = Workspace::scripted();
    for index in 0..count {
        ws.open_terminal(&format!("term-{index}"), "native", "epoch")
            .expect("open scripted terminal");
    }
    ws
}

/// A scripted daemon project row checked out on this machine.
fn scripted_project(id: &str, name: &str) -> ProjectRow {
    ProjectRow {
        id: id.to_string(),
        name: name.to_string(),
        display_name: name.to_string(),
        checkout: Some(Checkout {
            machine_id: "local".to_string(),
            root_path: format!("/repos/{name}"),
        }),
        ..ProjectRow::default()
    }
}

/// `count` blocked attention entries `run:term-0..`, each on its terminal.
fn set_attention(ws: &mut Workspace, count: usize) {
    let entries: Vec<_> = (0..count)
        .map(|index| {
            json!({
                "entry_id": format!("run:term-{index}"),
                "terminal": {"terminal_id": format!("term-{index}"), "backend": "native"},
                "attention": {"attention_id": format!("att-{index}"), "kind": "actionable"},
            })
        })
        .collect();
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": entries,
    }));
    ws.reconcile_subscribe_first().expect("attention roster");
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

fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}

fn route<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    kind: MouseEventKind,
    column: u16,
    row: u16,
) -> MouseOutcome {
    route_mouse(ws, chrome, &mouse(kind, column, row))
}

/// The pane `sidebar_workspace` opened for `term-{index}`.
fn pane(ws: &Workspace, index: usize) -> PaneId {
    ws.pane_for_terminal(&format!("term-{index}"))
        .expect("terminal pane")
}

#[test]
fn agent_row_click_routes_explicit_activation() {
    // The pointer layer identifies the roster row. End-to-end tab activation
    // and new-tab coverage lives in client_loop's async activation test
    // because the action layer owns those effects.
    let mut ws = sidebar_workspace(3);
    set_attention(&mut ws, 3);
    let mut chrome = chrome();
    chrome.open_tab(pane(&ws, 0), "0");
    chrome.open_tab(pane(&ws, 1), "1");
    chrome.activate_tab(0);
    let area = Rect::new(0, 0, 80, 40);
    draw_with_hits(&ws, &mut chrome, area);

    let (col, row) = row_cell(&chrome.view.agent_hit_areas, "run:term-1");
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, col, row),
        MouseOutcome::FocusAgent("run:term-1".to_string())
    );
    assert_eq!(
        chrome.active_index(),
        0,
        "activation is deferred to the async action layer"
    );
    assert_eq!(chrome.focused_pane(), Some(pane(&ws, 0)));
    assert_eq!(chrome.gesture, None, "an agent row starts no drag");
    route(&ws, &mut chrome, LEFT_UP, col, row);

    let (col, row) = row_cell(&chrome.view.agent_hit_areas, "run:term-2");
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, col, row),
        MouseOutcome::FocusAgent("run:term-2".to_string())
    );
    assert_eq!(
        chrome.tabs().tabs.len(),
        2,
        "the pointer layer does not open tabs"
    );
    assert_eq!(chrome.active_index(), 0);
    assert_eq!(chrome.focused_pane(), Some(pane(&ws, 0)));
}

/// 3.2 sessions section: the rows follow the focused project (or every
/// project under the `all` scope, grouped), the machine filter, and sort
/// grouped or by priority; the bands' filter, scope and sort labels are the
/// mouse's controls.
#[test]
fn agent_rows_follow_project_and_machine_filter() {
    const REMOTE: &str = "b2f7c0de-9a11-4c3e-8f2a-1d2e3f4a5b6c";
    let mut board = Board::new(&[]);
    board.add("alpha", "claude");
    board.add("beta", "codex");
    board.add_remote("alpha", "alpha-remote", REMOTE);
    let mut chrome = chrome();
    focus(&mut chrome, &mut board, "alpha");
    let labels = |board: &Board, chrome: &Chrome| -> Vec<String> {
        session_rows(board, chrome)
            .iter()
            .filter(|row| row.kind == RowKind::Agent)
            .map(|row| row.label.clone())
            .collect()
    };

    // local (the default) lists the focused project's agents on this
    // machine; a machine id lists the project's agents there; `all` lists
    // the project's agents everywhere and names each remote machine on its
    // second line.
    chrome.sidebar.machine_filter = None;
    assert_eq!(labels(&board, &chrome), ["alpha"]);
    chrome.sidebar.machine_filter = Some(REMOTE.to_string());
    assert_eq!(labels(&board, &chrome), ["alpha-remote"]);
    chrome.sidebar.machine_filter = Some(ALL_MACHINES.to_string());
    assert_eq!(labels(&board, &chrome), ["alpha", "alpha-remote"]);
    let rows = session_rows(&board, &chrome);
    assert_eq!(second_text(&rows[0], 40, &chrome), "   claude");
    assert_eq!(second_text(&rows[1], 40, &chrome), "   native · b2f7c0de");

    // The `all` scope adds the other projects' rows under dim group rows,
    // in project order; the group rows are no hits.
    chrome.sidebar.all_sessions = true;
    assert_eq!(labels(&board, &chrome), ["alpha", "alpha-remote", "beta"]);
    let ids: Vec<String> = session_rows(&board, &chrome)
        .iter()
        .map(|row| row.id.clone())
        .collect();
    assert_eq!(
        ids,
        [
            "group:alpha",
            "agent:alpha",
            "agent:alpha-remote",
            "group:beta",
            "agent:beta"
        ]
    );
    let (_, hits) = draw_sidebar(&board, &chrome, 34, 20);
    let drawn: Vec<&str> = hits.agents.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(drawn, ["agent:alpha", "agent:alpha-remote", "agent:beta"]);
    assert_eq!(
        hits.agents[1].1.y,
        hits.agents[0].1.y + 2,
        "rows of one group pack"
    );
    assert_eq!(
        hits.agents[2].1.y,
        hits.agents[1].1.y + 3,
        "the group row sits between the groups"
    );

    let model = board.sidebar();
    assert_eq!(
        next_machine_filter(model, None).as_deref(),
        Some(ALL_MACHINES)
    );
    assert_eq!(
        next_machine_filter(model, Some(ALL_MACHINES)).as_deref(),
        Some(REMOTE)
    );
    assert_eq!(next_machine_filter(model, Some(REMOTE)), None);

    // Priority order puts the blocked agent first in its group whatever its
    // tab.
    board.set_state("alpha-remote", RowState::Attention);
    chrome.prefs.agent_sort = AgentSort::Priority;
    assert_eq!(labels(&board, &chrome), ["alpha-remote", "alpha", "beta"]);
    chrome.sidebar.all_sessions = false;
    assert_eq!(labels(&board, &chrome), ["alpha-remote", "alpha"]);

    // The sessions band carries one control, `[view]`, and a click opens the
    // menu holding both axes: the value in force is marked and disabled, the
    // other choice of each pair carries the toggle. The projects band carries
    // the filter control, whose click is the action itself; the machines
    // section lists this machine first and the remote one nested under it,
    // and a click on a row sets the filter.
    let area = Rect::new(0, 0, 34, 20);
    let (terminal, hits) = draw_sidebar(&board, &chrome, area.width, area.height);
    let view_control = hits.sessions_view.expect("view control hit");
    let filter = hits.projects_filter.expect("filter control hit");
    assert_eq!(
        row_str(&terminal, view_control.y, 33),
        " Sessions                 [view]"
    );
    assert_eq!(
        row_str(&terminal, filter.y, 33),
        " Projects              [working]"
    );
    let machines: Vec<&str> = hits.machines.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(machines, [LOCAL_MACHINE, REMOTE]);
    let local_row = hits.machines[0].1;
    let remote_row = hits.machines[1].1;
    assert_eq!(remote_row.y, local_row.y + 1);
    assert!(
        row_str(&terminal, remote_row.y, 33).starts_with(" └─ "),
        "{:?}",
        row_str(&terminal, remote_row.y, 33)
    );
    chrome.view.sidebar_rect = area;
    chrome.view.sessions_view_hit_area = hits.sessions_view;
    chrome.view.projects_filter_hit_area = hits.projects_filter;
    chrome.view.machine_hit_areas = hits.machines.clone();
    assert_eq!(
        route(
            &board,
            &mut chrome,
            LEFT_DOWN,
            view_control.x,
            view_control.y
        ),
        MouseOutcome::Handled
    );
    let menu = chrome.menu.as_ref().expect("view menu open");
    assert_eq!(menu.kind, ContextMenuKind::SessionsView);
    let items: Vec<(&str, bool)> = menu
        .items
        .iter()
        .map(|item| (item.label, item.enabled))
        .collect();
    assert_eq!(
        items,
        [
            ("✓ this project", false),
            ("  all projects", true),
            ("  grouped", true),
            ("✓ priority", false),
        ]
    );
    chrome.menu = None;
    chrome.mode = Mode::Terminal;
    assert_eq!(
        route(&board, &mut chrome, LEFT_DOWN, filter.x, filter.y),
        MouseOutcome::Action(Action::ToggleProjectsFilter)
    );
    // A remote row filters to that machine and a second click returns to
    // local; the local row toggles `all`.
    assert_eq!(
        route(&board, &mut chrome, LEFT_DOWN, remote_row.x, remote_row.y),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.sidebar.machine_filter.as_deref(), Some(REMOTE));
    route(&board, &mut chrome, LEFT_DOWN, remote_row.x, remote_row.y);
    assert_eq!(chrome.sidebar.machine_filter, None);
    route(&board, &mut chrome, LEFT_DOWN, local_row.x, local_row.y);
    assert_eq!(chrome.sidebar.machine_filter.as_deref(), Some(ALL_MACHINES));
    route(&board, &mut chrome, LEFT_DOWN, local_row.x, local_row.y);
    assert_eq!(chrome.sidebar.machine_filter, None);

    // One known machine: the local row alone. The band's single control
    // fits beside the whole title at the default width too.
    let local = Board::new(&["alpha"]);
    let (_, hits) = draw_sidebar(&local, &chrome, area.width, area.height);
    assert_eq!(hits.machines.len(), 1);
    assert!(hits.sessions_view.is_some());
    let (_, hits) = draw_sidebar(&local, &chrome, 26, area.height);
    assert!(hits.sessions_view.is_some());
}

#[test]
fn sidebar_drags_reorder_resize_and_scroll() {
    // herdr workspace-list drag reorder, `set_manual_sidebar_width`,
    // `scroll_workspace_list` and the list scrollbar's thumb drag and track
    // jump; herdr's `set_sidebar_section_split` has no gclient counterpart
    // since the sections size themselves.
    // Eighteen cards overflow the projects section (capped at the top
    // half of the sidebar) by more than a wheel notch; the ten sessions rows
    // (five roster entries and five bare terminals) overflow the sessions
    // list by less.
    let mut ws = sidebar_workspace(10);
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: (0..18)
            .map(|index| scripted_project(&format!("proj-{index}"), &format!("project-{index}")))
            .collect(),
        ..SidebarRows::default()
    });
    ws.select_project("proj-0");
    set_attention(&mut ws, 5);
    let mut chrome = chrome();
    chrome.sidebar.all_projects = true;
    chrome.open_pane(pane(&ws, 0), "0");
    let area = Rect::new(0, 0, 60, 40);
    draw_with_hits(&ws, &mut chrome, area);
    let sidebar = chrome.view.sidebar_rect;
    assert_eq!(session_rows(&ws, &chrome).len(), 10);

    // A card dragged onto another takes its place; the order is chrome
    // state the loop saves.
    let (col, row0) = row_cell(&chrome.view.project_hit_areas, "proj-0");
    let (_, row2) = row_cell(&chrome.view.project_hit_areas, "proj-2");
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, col, row0),
        MouseOutcome::FocusProject("proj-0".to_string())
    );
    assert_eq!(
        route(
            &ws,
            &mut chrome,
            LEFT_DRAG,
            col,
            row0 + PROJECT_DRAG_THRESHOLD
        ),
        MouseOutcome::Handled
    );
    assert!(matches!(
        chrome.gesture,
        Some(MouseGesture::ProjectDrag { moved: true, .. })
    ));
    let reordered: Vec<String> = [1, 2, 0]
        .into_iter()
        .chain(3..18)
        .map(|index| format!("proj-{index}"))
        .collect();
    assert_eq!(
        route(&ws, &mut chrome, LEFT_UP, col, row2),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.gesture, None);
    assert_eq!(chrome.sidebar.project_order, reordered);
    draw_with_hits(&ws, &mut chrome, area);
    let drawn = drawn_ids(&chrome.view.project_hit_areas);
    assert_eq!(
        drawn,
        reordered[..drawn.len()],
        "the cards follow the order"
    );
    let (col, row1) = row_cell(&chrome.view.project_hit_areas, "proj-1");
    let (_, row3) = row_cell(&chrome.view.project_hit_areas, "proj-3");
    route(&ws, &mut chrome, LEFT_DOWN, col, row1);
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DRAG, col, row1),
        MouseOutcome::Handled
    );
    assert_eq!(
        route(&ws, &mut chrome, LEFT_UP, col, row3),
        MouseOutcome::Handled,
        "a drag that never left its row is a click"
    );
    assert_eq!(chrome.sidebar.project_order, reordered);

    draw_with_hits(&ws, &mut chrome, area);

    // The edge follows the pointer within the width bounds, from the press
    // on; the release keeps the width as the preferred width.
    let divider_x = chrome.view.sidebar_divider_x.expect("edge drawn");
    assert_eq!(divider_x, sidebar.x + sidebar.width - 1);
    let row = sidebar.y + 1;
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, divider_x, row),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.gesture, Some(MouseGesture::SidebarDrag));
    assert_eq!(
        chrome.sidebar.width, 26,
        "the press keeps the width the edge sits at"
    );
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DRAG, divider_x + 4, row),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.sidebar.width, 30);
    route(&ws, &mut chrome, LEFT_DRAG, sidebar.x + 4, row);
    assert_eq!(
        chrome.sidebar.width, chrome.sidebar.min_width,
        "clamped to the minimum"
    );
    route(&ws, &mut chrome, LEFT_DRAG, area.width - 1, row);
    assert_eq!(
        chrome.sidebar.width, chrome.sidebar.max_width,
        "clamped to the maximum"
    );
    assert_ne!(chrome.prefs.sidebar_width, chrome.sidebar.max_width);
    assert_eq!(
        route(&ws, &mut chrome, LEFT_UP, area.width - 1, row),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.gesture, None);
    assert_eq!(
        chrome.prefs.sidebar_width, chrome.sidebar.max_width,
        "the release keeps the width reached"
    );
    chrome.sidebar.width = 26;
    draw_with_hits(&ws, &mut chrome, area);

    // The sections size themselves: the machines and projects sections stop
    // at the top half of the sidebar and the sessions take the rest, so a
    // press on a band's title starts no gesture. The band controls are the
    // mouse's filter and scope toggles.
    let sections = chrome.view.sidebar_section_rects;
    // Every row of the column goes to the sections.
    let rows = sidebar.height;
    assert_eq!(
        sections[0].height + sections[1].height,
        rows / 2,
        "the cards fill the top half"
    );
    assert_eq!(sections[2].height, rows - rows / 2);
    let projects_band = sections[SidebarSection::Projects.index()].y;
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, sidebar.x + 1, projects_band),
        MouseOutcome::Ignore
    );
    assert_eq!(chrome.gesture, None);
    let filter = chrome
        .view
        .projects_filter_hit_area
        .expect("projects filter drawn");
    assert_eq!(filter.y, projects_band);
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, filter.x, filter.y),
        MouseOutcome::Action(Action::ToggleProjectsFilter)
    );
    let view_control = chrome
        .view
        .sessions_view_hit_area
        .expect("sessions view drawn");
    assert_eq!(view_control.y, sections[SidebarSection::Sessions.index()].y);
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, view_control.x, view_control.y),
        MouseOutcome::Handled
    );
    assert_eq!(
        chrome.menu.as_ref().map(|menu| menu.kind.clone()),
        Some(ContextMenuKind::SessionsView)
    );
    chrome.menu = None;
    chrome.mode = Mode::Terminal;

    // A wheel notch moves the list under the pointer three entries, clamped.
    let projects_max =
        section_metrics(&ws, &chrome, SidebarSection::Projects).max_offset_from_bottom;
    assert!(
        projects_max > MOUSE_SCROLL_LINES && projects_max < 2 * MOUSE_SCROLL_LINES,
        "the cards overflow by more than a notch: {projects_max}"
    );
    let (col, row) = row_cell(&chrome.view.project_hit_areas, "proj-1");
    assert_eq!(
        route(&ws, &mut chrome, MouseEventKind::ScrollDown, col, row),
        MouseOutcome::Handled
    );
    let projects_scroll = |chrome: &Chrome| chrome.sidebar.scroll(SidebarSection::Projects);
    let sessions_scroll = |chrome: &Chrome| chrome.sidebar.scroll(SidebarSection::Sessions);
    assert_eq!(projects_scroll(&chrome), MOUSE_SCROLL_LINES);
    route(&ws, &mut chrome, MouseEventKind::ScrollDown, col, row);
    assert_eq!(projects_scroll(&chrome), projects_max, "clamped at the end");
    route(&ws, &mut chrome, MouseEventKind::ScrollUp, col, row);
    assert_eq!(projects_scroll(&chrome), projects_max - MOUSE_SCROLL_LINES);
    route(&ws, &mut chrome, MouseEventKind::ScrollUp, col, row);
    assert_eq!(projects_scroll(&chrome), 0, "clamped at the top");
    let sessions_max =
        section_metrics(&ws, &chrome, SidebarSection::Sessions).max_offset_from_bottom;
    assert!(
        sessions_max > 0 && sessions_max < MOUSE_SCROLL_LINES,
        "the sessions list overflows by less than a notch: {sessions_max}"
    );
    let (col, row) = row_cell(&chrome.view.agent_hit_areas, "run:term-0");
    route(&ws, &mut chrome, MouseEventKind::ScrollDown, col, row);
    assert_eq!(sessions_scroll(&chrome), sessions_max);
    assert_eq!(
        projects_scroll(&chrome),
        0,
        "the cards are not the list under the pointer"
    );
    route(&ws, &mut chrome, MouseEventKind::ScrollUp, col, row);
    assert_eq!(sessions_scroll(&chrome), 0);
    let sessions_band = sections[SidebarSection::Sessions.index()].y;
    route(
        &ws,
        &mut chrome,
        MouseEventKind::ScrollDown,
        sidebar.x + 1,
        sessions_band,
    );
    assert_eq!(
        sessions_scroll(&chrome),
        sessions_max,
        "a section band belongs to the list under it"
    );
    route(
        &ws,
        &mut chrome,
        MouseEventKind::ScrollDown,
        sidebar.x + 1,
        projects_band,
    );
    assert_eq!(
        projects_scroll(&chrome),
        MOUSE_SCROLL_LINES,
        "the projects band belongs to the cards"
    );
    chrome.sidebar.scrolls = [0; 3];
    draw_with_hits(&ws, &mut chrome, area);

    // The scrollbar track jumps the list; the thumb drags it.
    let track = chrome.view.sidebar_scrollbar_hit_areas[SidebarSection::Projects.index()]
        .expect("projects scrollbar drawn");
    let bottom = track.y + track.height - 1;
    let metrics = section_metrics(&ws, &chrome, SidebarSection::Projects);
    assert_eq!(
        scrollbar_thumb_grab_offset(metrics, track, bottom),
        None,
        "the thumb sits at the top of an unscrolled list"
    );
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, track.x, bottom),
        MouseOutcome::Handled
    );
    assert_eq!(
        projects_scroll(&chrome),
        projects_max,
        "a track click jumps there"
    );
    assert_eq!(chrome.gesture, None);
    draw_with_hits(&ws, &mut chrome, area);
    route(&ws, &mut chrome, LEFT_DOWN, track.x, track.y);
    assert_eq!(projects_scroll(&chrome), 0);
    draw_with_hits(&ws, &mut chrome, area);
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DOWN, track.x, track.y),
        MouseOutcome::Handled
    );
    assert_eq!(
        chrome.gesture,
        Some(MouseGesture::SidebarScrollbarDrag {
            section: SidebarSection::Projects,
            grab_offset: 0,
        }),
        "a thumb press starts a drag"
    );
    assert_eq!(projects_scroll(&chrome), 0, "a thumb press does not jump");
    assert_eq!(
        route(&ws, &mut chrome, LEFT_DRAG, track.x, bottom),
        MouseOutcome::Handled
    );
    assert_eq!(
        projects_scroll(&chrome),
        projects_max,
        "the thumb follows the pointer"
    );
    assert_eq!(
        route(&ws, &mut chrome, LEFT_UP, track.x, bottom),
        MouseOutcome::Handled
    );
    assert_eq!(chrome.gesture, None);
}

/// `sidebar_workspace(count)` under two scripted projects: `alpha` on
/// `main` with one worktree bound to a task, and `beta` bare; `alpha` is
/// the focused project.
fn project_workspace(count: usize) -> Workspace {
    let mut ws = sidebar_workspace(count);
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![
            scripted_project("proj-alpha", "alpha"),
            scripted_project("proj-beta", "beta"),
        ],
        statuses: [(
            "proj-alpha".to_string(),
            SourceStatus {
                current_branch: Some("main".to_string()),
                ahead: Some(2),
                behind: Some(1),
                ..SourceStatus::default()
            },
        )]
        .into_iter()
        .collect(),
        worktrees: vec![WorktreeRow {
            id: "wt-1".to_string(),
            project_id: "proj-alpha".to_string(),
            task_id: Some("#123".to_string()),
            branch_name: Some("worktree/feature".to_string()),
            worktree_path: "/repos/alpha/.worktrees/feature".to_string(),
            status: "active".to_string(),
            workspace_role: "task".to_string(),
            ..WorktreeRow::default()
        }],
        ..SidebarRows::default()
    });
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().expect("install projects");
    ws
}

/// A cell inside the row last drawn for `id` in `areas`.
fn row_cell(areas: &[(String, Rect)], id: &str) -> (u16, u16) {
    let (_, rect) = areas
        .iter()
        .find(|(row_id, _)| row_id == id)
        .unwrap_or_else(|| panic!("row {id:?} drawn"));
    (rect.x + 1, rect.y)
}

fn drawn_ids(areas: &[(String, Rect)]) -> Vec<&str> {
    areas.iter().map(|(id, _)| id.as_str()).collect()
}

/// `project_workspace(2)` drawn once at `area` under a chrome focused on
/// `alpha` with one tab open there, every project listed (`beta` has no
/// live entry, so the working filter would hide it) and `alpha` expanded.
fn project_board() -> (Workspace, Chrome, Rect) {
    let ws = project_workspace(2);
    let mut chrome = chrome();
    chrome.sidebar.all_projects = true;
    chrome.sidebar.toggle_group("proj-alpha");
    chrome.focus_project("proj-alpha");
    chrome.open_tab(pane(&ws, 0), "0");
    let area = Rect::new(0, 0, 80, 40);
    draw_with_hits(&ws, &mut chrome, area);
    assert_eq!(
        drawn_ids(&chrome.view.project_hit_areas),
        ["proj-alpha", "proj-beta"]
    );
    assert_eq!(drawn_ids(&chrome.view.worktree_hit_areas), ["wt-1"]);
    (ws, chrome, area)
}

/// Plan 3.1.2: the project rows' pointer and navigate entry points, one
/// test per behaviour.
mod project_rows_focus_toggle_and_reorder {
    use super::*;

    #[test]
    fn project_card_click_focuses_it_and_swaps_the_tab_set() {
        // herdr `FocusWorkspace` from a workspace-card click, over gclient's
        // project cards: the press asks the loop to focus the project and swaps
        // the tab bar to that project's set at once; a release without movement
        // is the click.
        let (ws, mut chrome, _) = project_board();
        let (col, row) = row_cell(&chrome.view.project_hit_areas, "proj-beta");
        assert_eq!(
            route(&ws, &mut chrome, LEFT_DOWN, col, row),
            MouseOutcome::FocusProject("proj-beta".to_string())
        );
        assert_eq!(chrome.project_tabs.focused.as_deref(), Some("proj-beta"));
        assert!(chrome.tabs().tabs.is_empty(), "beta has no tabs yet");
        assert_eq!(
            chrome.gesture,
            Some(MouseGesture::ProjectDrag {
                project_id: "proj-beta".to_string(),
                origin_row: row,
                moved: false,
            })
        );
        assert_eq!(
            route(&ws, &mut chrome, LEFT_UP, col, row),
            MouseOutcome::Handled
        );
        assert_eq!(chrome.gesture, None);
        chrome.focus_project("proj-alpha");
        assert_eq!(chrome.tabs().tabs.len(), 1, "alpha's tab set survives");
    }

    #[test]
    fn worktree_row_click_opens_a_shell_there() {
        // A worktree row asks the loop for a shell in that worktree; the row
        // starts no drag.
        let (ws, mut chrome, _) = project_board();
        let (col, row) = row_cell(&chrome.view.worktree_hit_areas, "wt-1");
        assert_eq!(
            route(&ws, &mut chrome, LEFT_DOWN, col, row),
            MouseOutcome::OpenWorktree("wt-1".to_string())
        );
        assert_eq!(chrome.gesture, None);
        route(&ws, &mut chrome, LEFT_UP, col, row);
    }

    #[test]
    fn group_toggle_and_card_drag_persist_in_chrome_state() {
        // The group toggle folds and unfolds a card's worktree rows; a card
        // dragged onto another takes its place; the order is sidebar state
        // that `prefs.toml` keeps, the projects filter is per-window.
        let (ws, mut chrome, area) = project_board();
        let (toggle_id, toggle) = chrome.view.group_toggle_hit_areas[0].clone();
        assert_eq!(toggle_id, "proj-alpha");
        assert_eq!(
            route(&ws, &mut chrome, LEFT_DOWN, toggle.x, toggle.y),
            MouseOutcome::Handled
        );
        assert!(!chrome.sidebar.is_expanded("proj-alpha"));
        assert_eq!(chrome.sidebar.expanded_project, None);
        route(&ws, &mut chrome, LEFT_UP, toggle.x, toggle.y);
        draw_with_hits(&ws, &mut chrome, area);
        assert!(chrome.view.worktree_hit_areas.is_empty());
        assert_eq!(
            drawn_ids(&chrome.view.project_hit_areas),
            ["proj-alpha", "proj-beta"]
        );
        let (_, toggle) = chrome.view.group_toggle_hit_areas[0].clone();
        route(&ws, &mut chrome, LEFT_DOWN, toggle.x, toggle.y);
        route(&ws, &mut chrome, LEFT_UP, toggle.x, toggle.y);
        assert!(chrome.sidebar.is_expanded("proj-alpha"));
        draw_with_hits(&ws, &mut chrome, area);
        assert_eq!(drawn_ids(&chrome.view.worktree_hit_areas), ["wt-1"]);

        let (col, row_alpha) = row_cell(&chrome.view.project_hit_areas, "proj-alpha");
        let (_, row_beta) = row_cell(&chrome.view.project_hit_areas, "proj-beta");
        assert_eq!(
            route(&ws, &mut chrome, LEFT_DOWN, col, row_alpha),
            MouseOutcome::FocusProject("proj-alpha".to_string())
        );
        assert_eq!(
            route(
                &ws,
                &mut chrome,
                LEFT_DRAG,
                col,
                row_alpha + PROJECT_DRAG_THRESHOLD
            ),
            MouseOutcome::Handled
        );
        assert!(matches!(
            chrome.gesture,
            Some(MouseGesture::ProjectDrag { moved: true, .. })
        ));
        assert_eq!(
            route(&ws, &mut chrome, LEFT_UP, col, row_beta),
            MouseOutcome::Handled
        );
        assert_eq!(chrome.gesture, None);
        assert_eq!(chrome.sidebar.project_order, ["proj-beta", "proj-alpha"]);
        draw_with_hits(&ws, &mut chrome, area);
        assert_eq!(
            drawn_ids(&chrome.view.project_hit_areas),
            ["proj-beta", "proj-alpha"]
        );
        // The drop mirrors the order into the prefs, which seed the next
        // window; the projects filter is per-window and stays behind.
        assert_eq!(chrome.prefs.project_order, ["proj-beta", "proj-alpha"]);
        let mut restored = Chrome::dark();
        restored.apply_prefs(chrome.prefs.clone());
        assert_eq!(restored.sidebar.project_order, ["proj-beta", "proj-alpha"]);
        assert!(chrome.sidebar.all_projects);
        assert!(
            !restored.sidebar.all_projects,
            "the projects filter is per-window"
        );
        assert!(!restored.sidebar.all_sessions);
    }

    /// 3.3: renaming a project labels its card for this client only; the
    /// label rides `prefs.toml` with the rest of the sidebar preferences,
    /// and an empty commit clears it back to the daemon's name.
    #[test]
    fn project_rename_labels_the_card_and_survives_the_session() {
        let (ws, mut chrome, _) = project_board();
        rename_project(&mut chrome, "proj-alpha", "alpha");
        assert_eq!(chrome.mode, Mode::Rename);
        assert_eq!(
            chrome.dialog,
            Some(Dialog::Rename {
                kind: RenameKind::Project("proj-alpha".to_string()),
                value: "alpha".to_string(),
                cursor: "alpha".len(),
            })
        );
        for ch in " prime".chars() {
            assert_eq!(
                route_modal_key(&ws, &mut chrome, &key(KeyCode::Char(ch))),
                ModalOutcome::Consumed
            );
        }
        let outcome = route_modal_key(&ws, &mut chrome, &key(KeyCode::Enter));
        assert_eq!(
            outcome,
            ModalOutcome::Commit(
                RenameKind::Project("proj-alpha".to_string()),
                "alpha prime".to_string()
            )
        );
        let ModalOutcome::Commit(kind, value) = outcome else {
            unreachable!()
        };
        let mut ws = ws;
        apply_rename(&mut ws, &mut chrome, kind, value);
        assert_eq!(
            chrome
                .sidebar
                .project_labels
                .get("proj-alpha")
                .map(String::as_str),
            Some("alpha prime")
        );
        let rows = project_rows(&ws, &chrome);
        assert_eq!(rows[0].label, "alpha prime");
        assert_eq!(rows[2].label, "beta", "other cards keep the daemon name");

        // The rename mirrors the labels into the prefs, which seed the next
        // window.
        assert_eq!(chrome.prefs.project_labels, chrome.sidebar.project_labels);
        let mut restored = Chrome::dark();
        restored.apply_prefs(chrome.prefs.clone());
        assert_eq!(
            restored.sidebar.project_labels,
            chrome.sidebar.project_labels
        );

        apply_rename(
            &mut ws,
            &mut chrome,
            RenameKind::Project("proj-alpha".to_string()),
            String::new(),
        );
        assert!(chrome.sidebar.project_labels.is_empty());
        assert_eq!(project_rows(&ws, &chrome)[0].label, "alpha");
    }

    #[test]
    fn navigate_enter_focuses_a_project_or_opens_a_worktree() {
        // Navigate: down walks the project rows, worktrees included (alpha,
        // its worktree, beta), and enter opens the selected worktree or
        // focuses the selected project.
        let (ws, mut chrome, _) = project_board();
        chrome.mode = Mode::Navigate;
        chrome.sidebar.selected = 0;
        assert_eq!(
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Down)),
            ModalOutcome::Consumed
        );
        assert_eq!(chrome.sidebar.selected, 1);
        assert_eq!(
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Enter)),
            ModalOutcome::OpenWorktree("wt-1".to_string())
        );
        assert_eq!(chrome.mode, Mode::Terminal);
        chrome.mode = Mode::Navigate;
        chrome.sidebar.selected = 2;
        assert_eq!(
            route_modal_key(&ws, &mut chrome, &key(KeyCode::Enter)),
            ModalOutcome::FocusProject("proj-beta".to_string())
        );
        assert_eq!(chrome.mode, Mode::Terminal);
    }
}

/// One bare key as the modal routers see it.
fn key(code: KeyCode) -> KeyInput {
    KeyInput {
        key: KeyEvent::new(code, KeyModifiers::NONE),
        bytes: Vec::new(),
    }
}
