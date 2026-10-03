//! 4.2.1: committed screen goldens for the whole gclient chrome.
//!
//! Scripted workspace states render through the real `render_workspace`
//! into a 120x40 `TestBackend`, then serialise one line per row: the glyphs,
//! then the run-length-encoded style of every cell with each colour normalised
//! to its `theme::Palette` role name.
//!
//! Naming the role rather than the value is the same normalisation the parity
//! suite performs in `parity/token_map.rs`, for the same reason: a glyph,
//! alignment, or repaint change moves the capture, while retuning a theme token
//! moves the render and the expectation together and does not. Both sides read
//! the roles off `Palette::entries`, which is the single source of truth.
//!
//! `render_workspace` paints empty pane bodies, so a capture is chrome only —
//! terminal content never enters it and cannot make a golden flap.
//!
//! Regenerate with:
//!   `GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::startup_stages::{ConnectionView, StageState, StartupStages};
use gobby_client::app::{
    apply_local_menu_action, route_mouse, ContextMenuKind, ControlState, MouseOutcome, Submenu,
};
use gobby_client::daemon::{
    Checkout, ProjectRow, RunRow, SessionRow, SidebarRows, SourceStatus, WorktreeRow,
};
use gobby_client::theme::{Palette, ThemeKind, Token};
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::menu_bar::MenuBarMenu;
use gobby_client::ui::{render_workspace, Chrome};
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::Terminal;
use serde_json::json;
use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// Every capture is taken at one size, so a golden diff is never a reflow.
const WIDTH: u16 = 120;
const HEIGHT: u16 = 40;

const UPDATE_ENV: &str = "GOBBY_UPDATE_SCREENS";

/// Builds one scripted state: the workspace plus the chrome that frames it.
type ScriptedState = fn() -> (Workspace, Chrome);

/// The scripted states, in the order the plan names them.
const STATES: [(&str, ScriptedState); 18] = [
    ("empty_workspace", empty_workspace),
    ("splash_connecting", splash_connecting),
    ("splash_attach_running", splash_attach_running),
    ("agent_rows", agent_rows),
    ("projects_agents", projects_agents),
    ("split_live", split_live),
    ("daemon_unreachable", daemon_unreachable),
    ("help_dialog", help_dialog),
    ("label_ladder", label_ladder),
    ("unnamed_pane", unnamed_pane),
    ("pane_edges", pane_edges),
    ("menu_bar", menu_bar),
    ("arrange_window", arrange_window),
    ("arrange_tab", arrange_tab),
    ("arrange_pane_edge", arrange_pane_edge),
    ("sidebar_overlay", sidebar_overlay),
    ("status_segments", status_segments),
    ("monochrome", monochrome),
];

// ---------------------------------------------------------------- the states

/// Nothing open: the empty state, an empty roster, no tabs.
fn empty_workspace() -> (Workspace, Chrome) {
    (Workspace::scripted(), Chrome::dark())
}

fn splash_connecting() -> (Workspace, Chrome) {
    let (ws, mut chrome) = empty_workspace();
    let now = Instant::now();
    chrome.connection = ConnectionView {
        url: "http://127.0.0.1:60887".into(),
        machine: "local".into(),
        stages: Some(StartupStages::for_test(
            [
                StageState::Running {
                    since: now - Duration::from_millis(9800),
                },
                StageState::Pending,
                StageState::Pending,
                StageState::Pending,
            ],
            now,
        )),
        now,
        ..ConnectionView::default()
    };
    (ws, chrome)
}

fn splash_attach_running() -> (Workspace, Chrome) {
    let (ws, mut chrome) = empty_workspace();
    let now = Instant::now();
    chrome.connection = ConnectionView {
        url: "http://127.0.0.1:60887".into(),
        machine: "local".into(),
        daemon_version: Some("0.5.0".into()),
        stages: Some(StartupStages::for_test(
            [
                StageState::Done {
                    took: Duration::from_millis(300),
                },
                StageState::Running {
                    since: now - Duration::from_millis(2100),
                },
                StageState::Pending,
                StageState::Pending,
            ],
            now,
        )),
        now,
        ..ConnectionView::default()
    };
    (ws, chrome)
}

/// The sidebar rows behind `projects_agents`: `alpha` on `main`, two ahead
/// and one behind, with one worktree child bound to a task; `beta` bare.
fn project_rows() -> SidebarRows {
    let project = |id: &str, name: &str| ProjectRow {
        id: id.to_string(),
        name: name.to_string(),
        display_name: name.to_string(),
        checkout: Some(Checkout {
            machine_id: "local".to_string(),
            root_path: format!("/repos/{name}"),
        }),
        ..ProjectRow::default()
    };
    SidebarRows {
        projects: vec![project("proj-alpha", "alpha"), project("proj-beta", "beta")],
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
    }
}

/// Two projects with `alpha` focused, its worktree child listed under it,
/// and one agent waiting for attention on `term-alpha`. No pane is open in
/// chrome, which isolates the sidebar from the tab surface. The sidebar is
/// pinned, and `split_live` and `help_dialog` keep it pinned; the other
/// states draw the default frame with it hidden.
fn projects_agents() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.set_local_machine("local");
    // The attention agent runs in the worktree, so the worktree rolls it up.
    let mut rows = project_rows();
    rows.runs.insert(
        "proj-alpha".into(),
        vec![RunRow {
            run_id: "run-alpha".into(),
            worktree_id: Some("wt-1".into()),
            ..RunRow::default()
        }],
    );
    ws.daemon_mut().set_sidebar_rows(rows);
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "run_id": "run-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().expect("install roster");
    for terminal_id in ["term-alpha", "term-beta", "term-gamma"] {
        ws.open_terminal(terminal_id, "native", "epoch")
            .expect("open terminal");
    }
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    (ws, chrome)
}

/// The projects and agents state in grays: every state keeps its glyph and
/// lightness once the hue is gone.
fn monochrome() -> (Workspace, Chrome) {
    let (ws, mut chrome) = projects_agents();
    chrome.prefs.monochrome = true;
    chrome.set_theme(ThemeKind::Dark);
    chrome.sidebar.toggle_group("proj-alpha");
    (ws, chrome)
}

fn agent_rows() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.set_local_machine("local");
    let mut rows = project_rows();
    rows.runs.insert(
        "proj-alpha".into(),
        vec![RunRow {
            run_id: "run-long".into(),
            agent_name: Some("backend-developer-workflow-run".into()),
            provider: Some("codex".into()),
            model: Some("gpt-5".into()),
            terminal_id: Some("term-agent".into()),
            ..RunRow::default()
        }],
    );
    rows.sessions.insert(
        "proj-alpha".into(),
        vec![SessionRow {
            id: "sess-long".into(),
            reference: Some("#123".into()),
            title: Some("Agent row preview".into()),
            ..SessionRow::default()
        }],
    );
    ws.daemon_mut().set_sidebar_rows(rows);
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-agent",
            "run_id": "run-long",
            "session_id": "sess-long",
            "provider": "codex",
            "model": "gpt-5",
            "task": {"ref": "#123", "title": "Implement the full chrome layout with a long scrolling task title"},
            "terminal": {"terminal_id": "term-agent", "backend": "native"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().expect("install roster");
    ws.open_terminal("term-agent", "native", "epoch")
        .expect("open agent terminal");
    ws.open_terminal("term-bare", "native", "epoch")
        .expect("open bare terminal");
    let bare = ws.pane_for_terminal("term-bare").expect("bare pane");
    let pane = ws.pane_mut(bare);
    pane.label = None;
    pane.command = Some("nvim".into());
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    chrome.sidebar.width = 34;
    (ws, chrome)
}

/// Two live panes split in the first tab, with a second tab behind them.
///
/// `open_terminal` attaches direct and pushes the first frame, so both panes
/// are already live; `term-beta` is reattached through the proxy and focused,
/// which draws its focus marker and `Focused` metadata. The transport itself
/// is not drawn: the operator cannot act on it.
fn split_live() -> (Workspace, Chrome) {
    let (mut ws, mut chrome) = projects_agents();
    let alpha = ws.pane_for_terminal("term-alpha").expect("term-alpha pane");
    let beta = ws.pane_for_terminal("term-beta").expect("term-beta pane");
    ws.reattach_frames(beta).expect("reattach term-beta");

    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    chrome.open_tab(alpha, "second");
    chrome.activate_tab(0);
    assert!(chrome.focus_pane(beta), "focus term-beta");
    (ws, chrome)
}

fn daemon_unreachable() -> (Workspace, Chrome) {
    let (ws, mut chrome) = split_live();
    let now = Instant::now();
    chrome.connection.now = now;
    chrome.connection.url = "http://127.0.0.1:60887".into();
    chrome.connection.retry_at = Some(now + Duration::from_secs(3));
    (ws, chrome)
}

/// Rungs 2, 3 and 4 of the label ladder, in the chrome that draws them.
///
/// Every other state names its panes by rung 1: a scripted terminal is opened
/// under a name and keeps it. This one takes those names away so the
/// rungs below get their turn — `term-alpha` hosts a `codex` session,
/// `term-beta` has `nvim` in its foreground, and `term-gamma` has nothing left
/// to say and falls to the literal. Focus sits on `term-gamma`, and no row
/// here can be an id.
///
/// The second reconcile is the roster refresh that follows an attach, and it
/// is the only point at which a provider can reach a pane: the first one ran
/// before any pane existed.
fn label_ladder() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(project_rows());
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "provider": "codex"
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().expect("install roster");
    for terminal_id in ["term-alpha", "term-beta", "term-gamma"] {
        ws.open_terminal(terminal_id, "native", "epoch")
            .expect("open terminal");
    }
    for (terminal_id, command) in [
        ("term-alpha", None),
        ("term-beta", Some("nvim")),
        ("term-gamma", None),
    ] {
        let id = ws.pane_for_terminal(terminal_id).expect("scripted pane");
        let pane = ws.pane_mut(id);
        pane.label = None;
        pane.command = command.map(str::to_string);
    }
    ws.reconcile_subscribe_first().expect("refresh roster");

    let mut chrome = Chrome::dark();
    for terminal_id in ["term-alpha", "term-beta", "term-gamma"] {
        let pane = ws.pane_for_terminal(terminal_id).expect("scripted pane");
        chrome.open_pane(pane, "alpha");
    }
    let gamma = ws.pane_for_terminal("term-gamma").expect("term-gamma pane");
    assert!(chrome.focus_pane(gamma), "focus term-gamma");
    (ws, chrome)
}

fn unnamed_pane() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut()
        .set_roster(json!({"epoch": "e1", "seq": 1, "entries": []}));
    ws.reconcile_subscribe_first().expect("install roster");
    let pane_id = ws
        .open_terminal("unnamed-terminal", "native", "epoch")
        .expect("open terminal");
    let pane = ws.pane_mut(pane_id);
    pane.label = None;
    pane.command = Some("zsh".to_owned());
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane_id, "alpha");
    (ws, chrome)
}

fn status_segments() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    let mut rows = SidebarRows::default();
    rows.runs.insert(
        "alpha".to_string(),
        vec![RunRow {
            run_id: "run-alpha".to_string(),
            effective_reasoning_effort: Some("xhigh".to_string()),
            ..RunRow::default()
        }],
    );
    ws.daemon_mut().set_sidebar_rows(rows);
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "run:term-alpha",
                "run_id": "run-alpha",
                "terminal": {"terminal_id": "term-alpha", "backend": "native"},
                "provider": "claude",
                "model": "claude-fable-5-1",
                "model_display_name": "Fable 5.1",
                "context_percent": 63,
                "tokens_used": 12345
            },
            {
                "entry_id": "run:term-beta",
                "terminal": {"terminal_id": "term-beta", "backend": "native"},
                "attention": {"attention_id": "att-1", "kind": "actionable"}
            }
        ]
    }));
    ws.reconcile_subscribe_first().expect("install roster");
    let alpha = ws
        .open_terminal("term-alpha", "native", "epoch")
        .expect("open focused agent");
    let beta = ws
        .open_terminal("term-beta", "native", "epoch")
        .expect("open attention agent");
    let mut chrome = Chrome::dark();
    chrome.open_tab(alpha, "alpha");
    chrome.open_tab(beta, "beta");
    chrome.activate_tab(0);
    // The sandbox, context and token segments are opt-in.
    chrome.prefs.status_left = vec!["focus".to_string(), "sandbox".to_string()];
    chrome.prefs.status_right = vec!["context".to_string(), "tokens".to_string()];
    (ws, chrome)
}

/// The pane edges with pane gaps off, where they share lines: two panes
/// stacked in one tab.
///
/// The upper pane hosts a session whose Unicode title outranks the pane's
/// name. Its lease is lost, so its metadata reads `Read-only` in the warning
/// tone. It shares its bottom line with the pane below, so that metadata sits
/// top-right beside the title. The focused lower pane runs under tmux and
/// keeps `tmux · Focused` bottom-right. The Agents row puts the same title
/// behind its stationary `#1742:` prefix.
fn pane_edges() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    let mut rows = project_rows();
    rows.sessions.insert(
        "proj-alpha".to_string(),
        vec![SessionRow {
            id: "sess-alpha".to_string(),
            reference: Some("#1742".to_string()),
            title: Some("Ship the Unicode 修复 to pane headers".to_string()),
            status: "active".to_string(),
            ..SessionRow::default()
        }],
    );
    ws.daemon_mut().set_sidebar_rows(rows);
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "session:sess-alpha",
            "session_id": "sess-alpha",
            "provider": "codex",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().expect("install roster");
    ws.open_terminal("term-alpha", "native", "epoch")
        .expect("open term-alpha");
    ws.open_terminal("term-beta", "tmux", "epoch")
        .expect("open term-beta");
    let alpha = ws.pane_for_terminal("term-alpha").expect("term-alpha pane");
    let beta = ws.pane_for_terminal("term-beta").expect("term-beta pane");
    ws.pane_mut(alpha).label = None;
    ws.pane_mut(beta).label = None;
    ws.pane_mut(alpha).control = ControlState::LeaseLost;

    let mut chrome = Chrome::dark();
    chrome.prefs.pane_gaps = false;
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane_below(beta, "alpha");
    assert!(chrome.focus_pane(beta), "focus term-beta");
    (ws, chrome)
}

/// The split with its sidebar opened as the overlay rather than pinned: the
/// overlay lies over the panes' left 34 columns with an accent edge and holds
/// the keyboard, and the tab labels move to the bar's far end. Set after
/// `split_live` returns, since its `focus_pane` rolls an overlay up.
fn sidebar_overlay() -> (Workspace, Chrome) {
    let (ws, mut chrome) = split_live();
    chrome.sidebar.pinned = false;
    chrome.sidebar.overlay = true;
    chrome.mode = Mode::Navigate;
    (ws, chrome)
}

/// The keybind help dialog over the split. `render_workspace` dims the chrome
/// behind the mode overlay, so this capture also pins the dimmed background.
fn help_dialog() -> (Workspace, Chrome) {
    let (ws, mut chrome) = split_live();
    chrome.mode = Mode::KeybindHelp;
    (ws, chrome)
}

/// One tab with the View menu open, opened the way a click on its title
/// opens it: the open title reads reversed and the popup hangs under it.
fn menu_bar() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-alpha", "native", "epoch")
        .expect("open term-alpha");
    let mut chrome = Chrome::dark();
    chrome.open_tab(pane, "");
    let area = Rect::new(0, 0, WIDTH, HEIGHT);
    chrome.compute_view(&ws, area);
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, HEIGHT)).expect("test backend");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, &ws, &chrome)))
        .expect("draw frame");
    chrome.view.apply_hits(hits.expect("frame drawn"));
    let view = MenuBarMenu::ALL
        .iter()
        .position(|menu| *menu == MenuBarMenu::View)
        .and_then(|index| {
            chrome
                .view
                .menu_title_hit_areas
                .iter()
                .find(|(drawn, _)| *drawn == index)
        })
        .map(|(_, rect)| *rect)
        .expect("View title drawn");
    let press = MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: view.x + 1,
        row: view.y,
        modifiers: KeyModifiers::NONE,
    };
    route_mouse(&ws, &mut chrome, &press);
    assert_eq!(
        chrome.menu.as_ref().map(|menu| &menu.kind),
        Some(&ContextMenuKind::MenuBar(MenuBarMenu::View))
    );
    (ws, chrome)
}

/// Two tabs, the first split in two, with Arrange ▸ opened by a click from
/// the menu that a press at the button and cell `locate` picks opens.
fn arrange_from(locate: fn(&Chrome) -> (MouseButton, u16, u16)) -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    let [alpha, beta, gamma] = ["term-alpha", "term-beta", "term-gamma"].map(|id| {
        ws.open_terminal(id, "native", "epoch")
            .expect("open terminal")
    });
    let mut chrome = Chrome::dark();
    chrome.open_tab(alpha, "build");
    chrome.open_pane(beta, "tests");
    chrome.open_tab(gamma, "logs");
    chrome.activate_tab(0);
    chrome.compute_view(&ws, Rect::new(0, 0, WIDTH, HEIGHT));
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, HEIGHT)).expect("test backend");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, &ws, &chrome)))
        .expect("draw frame");
    chrome.view.apply_hits(hits.expect("frame drawn"));
    let press = |button, column, row| MouseEvent {
        kind: MouseEventKind::Down(button),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    };
    let (button, column, row) = locate(&chrome);
    route_mouse(&ws, &mut chrome, &press(button, column, row));
    let menu = chrome.menu.as_ref().expect("menu opened");
    let row = menu
        .items
        .iter()
        .position(|item| item.label == "Arrange ▸")
        .map(|index| menu.item_rects[index])
        .expect("Arrange ▸ row");
    let MouseOutcome::Menu { action, .. } =
        route_mouse(&ws, &mut chrome, &press(MouseButton::Left, row.x, row.y))
    else {
        panic!("a click on Arrange ▸ opens it");
    };
    apply_local_menu_action(&mut ws, &mut chrome, &action);
    assert!(matches!(
        chrome.menu.as_ref().map(|menu| &menu.kind),
        Some(ContextMenuKind::Submenu(Submenu::Arrange(_)))
    ));
    (ws, chrome)
}

/// Window › Arrange ▸, opened from the Window title.
fn arrange_window() -> (Workspace, Chrome) {
    arrange_from(|chrome| {
        let window = MenuBarMenu::ALL
            .iter()
            .position(|menu| *menu == MenuBarMenu::Window)
            .and_then(|index| {
                chrome
                    .view
                    .menu_title_hit_areas
                    .iter()
                    .find(|(drawn, _)| *drawn == index)
            })
            .map(|(_, rect)| *rect)
            .expect("Window title drawn");
        (MouseButton::Left, window.x + 1, window.y)
    })
}

/// Arrange ▸ from the right-click menu of the second, inactive tab.
fn arrange_tab() -> (Workspace, Chrome) {
    arrange_from(|chrome| {
        let tab = chrome
            .view
            .tab_hit_areas
            .iter()
            .find(|(index, _)| *index == 1)
            .map(|(_, rect)| *rect)
            .expect("second tab drawn");
        (MouseButton::Right, tab.x + 1, tab.y)
    })
}

/// Arrange ▸ from a pane's right-click menu at the screen's right edge,
/// where the submenu opens to the left of its menu.
fn arrange_pane_edge() -> (Workspace, Chrome) {
    arrange_from(|chrome| {
        let pane = chrome
            .view
            .pane_infos
            .iter()
            .map(|info| info.inner_rect)
            .max_by_key(|rect| rect.x)
            .expect("panes drawn");
        (MouseButton::Right, pane.right() - 3, pane.y + 1)
    })
}

// ------------------------------------------------------------- the capture

fn render(ws: &Workspace, chrome: &mut Chrome) -> Terminal<TestBackend> {
    let area = Rect::new(0, 0, WIDTH, HEIGHT);
    chrome.compute_view(ws, area);
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, HEIGHT)).expect("test backend");
    terminal
        .draw(|frame| {
            render_workspace(frame, ws, chrome);
        })
        .expect("draw frame");
    terminal
}

/// `Palette::entries` resolved to the colours the render actually paints
/// with, then the chrome's own fields no herdr name covers: the bar and its
/// inks, the line and the wordmark. Those come after the entries, so where
/// one repeats a token (`bar_open_ink` is the dark `surface_dim`) the
/// capture keeps naming the entry. A monochrome chrome paints each token at
/// its own lightness with no chroma, so its roles are those grays.
fn roles(chrome: &Chrome) -> Vec<(&'static str, Color)> {
    let palette = &chrome.palette;
    let monochrome = chrome.prefs.monochrome;
    Palette::entries(&chrome.theme)
        .into_iter()
        .map(|(name, token)| {
            let token = if monochrome {
                Token {
                    chroma: 0.0,
                    ..token
                }
            } else {
                token
            };
            (name, token.color())
        })
        .chain([
            ("bar", palette.bar),
            ("bar_ink", palette.bar_ink),
            ("bar_open_ink", palette.bar_open_ink),
            ("line", palette.line),
            ("wordmark", palette.wordmark),
        ])
        .collect()
}

/// The role a painted colour normalises to.
///
/// Several roles share one token — `mauve` is `subtext0`, `teal` is `blue`,
/// `peach` is `yellow` — so the reverse lookup is genuinely ambiguous. First
/// match in `Palette::entries` order wins, which is stable across runs and
/// makes the capture name the earlier role of each pair.
fn role_name(color: Color, roles: &[(&'static str, Color)]) -> String {
    if color == Color::Reset {
        return "-".to_string();
    }
    match roles.iter().find(|(_, value)| *value == color) {
        Some((name, _)) => (*name).to_string(),
        // A colour outside the contract is a finding, not a crash: name it by
        // value so the diff says exactly what leaked into the render.
        None => format!("{color:?}").to_lowercase(),
    }
}

/// Modifier bits as stable letters, in declaration order.
fn modifier_tag(modifier: Modifier) -> String {
    const BITS: [(Modifier, char); 9] = [
        (Modifier::BOLD, 'b'),
        (Modifier::DIM, 'd'),
        (Modifier::ITALIC, 'i'),
        (Modifier::UNDERLINED, 'u'),
        (Modifier::SLOW_BLINK, 's'),
        (Modifier::RAPID_BLINK, 'r'),
        (Modifier::REVERSED, 'v'),
        (Modifier::HIDDEN, 'h'),
        (Modifier::CROSSED_OUT, 'x'),
    ];
    BITS.iter()
        .filter(|(bit, _)| modifier.contains(*bit))
        .map(|(_, letter)| *letter)
        .collect()
}

/// Run-length encodes one row's styles. The counts are what catch a shift that
/// leaves every glyph and colour otherwise intact.
fn style_runs(styles: &[String]) -> String {
    let mut out = String::new();
    let mut styles = styles.iter();
    let Some(mut current) = styles.next() else {
        return out;
    };
    let mut count = 1usize;
    for style in styles {
        if style == current {
            count += 1;
            continue;
        }
        write!(out, "{current}*{count} ").expect("write style run");
        current = style;
        count = 1;
    }
    write!(out, "{current}*{count}").expect("write style run");
    out
}

/// Serialises the last drawn frame: a header, then a glyph line and a style
/// line per row. The file is compared byte for byte and never parsed, so
/// delimiters appearing inside rendered content are harmless.
fn capture(name: &str, terminal: &Terminal<TestBackend>, chrome: &Chrome) -> String {
    let roles = roles(chrome);
    let buffer = terminal.backend().buffer();
    let theme = match (chrome.theme.kind, chrome.prefs.monochrome) {
        (ThemeKind::Dark, false) => "dark theme",
        (ThemeKind::Dark, true) => "dark theme in grays",
        (ThemeKind::Light, false) => "light theme",
        (ThemeKind::Light, true) => "light theme in grays",
    };
    let mut out = format!(
        "# gclient screen golden: {name}\n\
         # {WIDTH}x{HEIGHT}, {theme}, colours normalised to theme::Palette roles\n"
    );
    for y in 0..buffer.area.height {
        let mut glyphs = String::new();
        let mut styles = Vec::with_capacity(usize::from(buffer.area.width));
        for x in 0..buffer.area.width {
            let cell = &buffer[(x, y)];
            glyphs.push_str(cell.symbol());
            let mut style = format!(
                "{}/{}",
                role_name(cell.fg, &roles),
                role_name(cell.bg, &roles)
            );
            let tag = modifier_tag(cell.modifier);
            if !tag.is_empty() {
                style.push('+');
                style.push_str(&tag);
            }
            styles.push(style);
        }
        writeln!(out, "{y:02} |{glyphs}|").expect("write glyph row");
        writeln!(out, "{y:02} : {}", style_runs(&styles)).expect("write style row");
    }
    out
}

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/screens")
        .join(format!("{name}.txt"))
}

/// Names the first line that moved; a 40-row `assert_eq!` diff is unreadable.
fn first_difference(rendered: &str, committed: &str) -> String {
    for (index, (a, b)) in rendered.lines().zip(committed.lines()).enumerate() {
        if a != b {
            return format!("line {index}\n  committed: {b}\n  rendered:  {a}");
        }
    }
    format!(
        "line count: committed {}, rendered {}",
        committed.lines().count(),
        rendered.lines().count()
    )
}

/// Captures a state twice and returns the bytes once both renders agree.
///
/// Determinism is a property of the capture, not of the file: a second render
/// of the same state must serialise identically before the bytes are worth
/// committing, and the byte comparison below then carries that guarantee
/// across runs.
fn deterministic_capture(name: &str, build: ScriptedState) -> String {
    let (ws, mut chrome) = build();
    let first = capture(name, &render(&ws, &mut chrome), &chrome);
    let second = capture(name, &render(&ws, &mut chrome), &chrome);
    assert!(
        first == second,
        "{name} capture is not deterministic\n{}",
        first_difference(&second, &first)
    );
    first
}

// ----------------------------------------------------------------- the tests

#[test]
fn screens_match_committed_captures() {
    let update = std::env::var_os(UPDATE_ENV).is_some_and(|value| value == "1");

    for (name, build) in STATES {
        let rendered = deterministic_capture(name, build);
        let path = fixture_path(name);

        if update {
            fs::create_dir_all(path.parent().expect("fixtures directory"))
                .expect("create fixtures directory");
            fs::write(&path, &rendered).expect("write capture");
            continue;
        }

        let committed = fs::read_to_string(&path).unwrap_or_else(|error| {
            panic!(
                "{}: {error} — regenerate with {UPDATE_ENV}=1",
                path.display()
            )
        });
        assert!(
            rendered == committed,
            "{name} no longer matches its committed capture; \
             regenerate with {UPDATE_ENV}=1 once the render change is deliberate\n{}",
            first_difference(&rendered, &committed)
        );
    }
}

/// The glyph column of every row of a capture, in row order.
fn glyph_rows(capture: &str) -> Vec<&str> {
    capture
        .lines()
        .filter_map(|line| line.split_once(" |"))
        .map(|(_, glyphs)| glyphs.strip_suffix('|').unwrap_or(glyphs))
        .collect()
}

#[test]
fn agent_rows_golden() {
    let rendered = deterministic_capture("agent_rows", agent_rows);
    let rows = glyph_rows(&rendered);
    assert!(
        rows[9].contains("#123: backend-developer-work"),
        "{:?}",
        rows[9]
    );
    assert!(
        rows[10].contains("Working task 123 Implement"),
        "{:?}",
        rows[10]
    );
    assert!(rows[11].contains("gpt-5"));
    assert!(
        rows[25].contains("○ nvim") && rows[25].contains("gclient"),
        "foreground app with its address: {:?}",
        rows[25]
    );
    assert!(
        !rows[26].contains("gclient"),
        "no directory reported: {:?}",
        rows[26]
    );
    assert!(!rendered.contains("term-bare"), "pane ID is not row copy");
    let slug_style = rendered
        .lines()
        .find(|line| line.starts_with("11 :"))
        .expect("model slug style");
    // The model is secondary text in subtext0, never dimmed (#23280 Option B).
    assert!(slug_style.contains("subtext0/panel_bg*5"), "{slug_style}");
    assert!(!slug_style.contains("subtext0/panel_bg+d"), "{slug_style}");
}

#[test]
fn status_segments_golden() {
    let rendered = deterministic_capture("status_segments", status_segments);
    let rows = glyph_rows(&rendered);
    let status = rows[usize::from(HEIGHT - 1)];
    assert!(status.contains("⍾ 1 needs you │ 1 idle"), "{status:?}");
    // #23049: the sandbox state in words; the model stays on the Agents row.
    assert!(status.contains("1 idle · unrestricted"), "{status:?}");
    assert!(!status.contains("claude-fable"), "{status:?}");
    assert!(status.contains("63% · 12,345"), "{status:?}");
    assert!(status.contains("prefix ctrl+b"), "{status:?}");
}

/// 3.1.1: the pinned sidebar stacks the machines, the projects and the
/// agents under the menu-bar row. A project is a one-line card (state
/// glyph, name, branch with the ahead/behind counts, fold marker) that lists
/// its worktrees only while expanded; the `working` filter hides a project
/// with nothing live; the attention entry lists under the agents band.
/// `screens_match_committed_captures` pins the exact layout; comparing it
/// here as well raced that test's rewrite under the update flag.
#[test]
fn projects_agents_golden() {
    let rendered = deterministic_capture("projects_agents", projects_agents);
    let rows = glyph_rows(&rendered);
    let row_containing = |needle: &str| {
        rows.iter()
            .position(|row| row.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle:?}\n{rendered}"))
    };

    // Rows 0 and 1 are the menu bar and its line; the machines band opens the
    // sidebar under them.
    assert_eq!(
        rows[0].trim_end(),
        " Gobby  File  Edit  View  Window  Agent  Help",
        "menu-bar row"
    );
    let machines = row_containing(" Machines");
    assert_eq!(machines, 2, "the machines band tops the sidebar");
    let projects = row_containing(" Projects");
    assert_eq!(
        rows[projects].trim_end(),
        " Projects",
        "the projects band is its title alone"
    );
    assert!(machines < projects, "the machines sit above the projects");
    // alpha carries the most urgent state of its bound agents: needs you.
    let alpha = row_containing("⍾ alpha");
    assert_eq!(alpha, projects + 1, "the card follows the one-row band");
    assert!(
        rows[alpha].starts_with(" ⍾ alpha (main ↑2 ↓1)") && rows[alpha].trim_end().ends_with('▸'),
        "folded card: {:?}",
        rows[alpha]
    );
    assert!(
        !rendered.contains("feature"),
        "a folded card lists no worktree\n{rendered}"
    );
    assert!(
        !rendered.contains("○ beta"),
        "the working filter hides a project with nothing live\n{rendered}"
    );
    let sessions = row_containing(" Agents");
    assert_eq!(
        sessions,
        alpha + 2,
        "a blank row separates the cards from the agents band"
    );
    assert_eq!(
        rows[sessions].trim_end(),
        " Agents",
        "the agents band is its title alone"
    );
    let entry = row_containing("Unknown");
    assert_eq!(
        entry,
        sessions + 1,
        "the attention entry lists under agents"
    );
    assert!(
        rows[entry].contains("⍾ Unknown"),
        "an attention row carries its state glyph: {:?}",
        rows[entry]
    );
    assert!(rows[entry + 1].contains("No assigned task"));
    assert!(
        !rendered.contains("[«]") && !rendered.contains("[Menu]"),
        "no footer or menu band remains\n{rendered}"
    );

    // Expanding the card unfolds its worktree under it and flips the marker.
    let (ws, mut chrome) = projects_agents();
    chrome.sidebar.toggle_group("proj-alpha");
    let expanded = capture("projects_agents", &render(&ws, &mut chrome), &chrome);
    let expanded_rows = glyph_rows(&expanded);
    assert!(
        expanded_rows[alpha].trim_end().ends_with('▾'),
        "expanded card: {:?}",
        expanded_rows[alpha]
    );
    assert!(
        expanded_rows[alpha + 1].starts_with("   └─ ⍾ feature · #123"),
        "worktree line: {:?}",
        expanded_rows[alpha + 1]
    );
}

/// 4.2.1's second half. The tab bar is chrome and sits nowhere near a pane
/// body, so renaming one tab by a single character must move the capture.
#[test]
fn monochrome_paints_only_the_gray_roles() {
    // A colour outside the roles is named by value (`rgb(…)`), so none may
    // appear: monochrome leaves no hue behind anywhere in the chrome.
    let rendered = deterministic_capture("monochrome", monochrome);
    assert!(
        !rendered.contains("rgb("),
        "a hue leaked into monochrome\n{rendered}"
    );
    assert!(rendered.contains("dark theme in grays"));
}

#[test]
fn a_chrome_change_outside_the_terminal_content_region_fails_the_capture() {
    let committed = deterministic_capture("split_live", split_live);

    let (ws, mut chrome) = split_live();
    chrome.tabs_mut().tabs[1].title = "secont".to_string();
    let moved = capture("split_live", &render(&ws, &mut chrome), &chrome);

    assert!(
        moved != committed,
        "a renamed tab left the capture unchanged, so chrome is escaping the golden"
    );
}
