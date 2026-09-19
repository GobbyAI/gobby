//! 4.2.1: committed screen goldens for the whole gclient chrome.
//!
//! Five scripted workspace states render through the real `render_workspace`
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

use gobby_client::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
use gobby_client::theme::{Palette, Theme, ThemeKind};
use gobby_client::ui::chrome::Mode;
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

/// Every capture is taken at one size, so a golden diff is never a reflow.
const WIDTH: u16 = 120;
const HEIGHT: u16 = 40;

const UPDATE_ENV: &str = "GOBBY_UPDATE_SCREENS";

/// Builds one scripted state: the workspace plus the chrome that frames it.
type ScriptedState = fn() -> (Workspace, Chrome);

/// The scripted states, in the order the plan names them.
const STATES: [(&str, ScriptedState); 5] = [
    ("empty_workspace", empty_workspace),
    ("projects_agents", projects_agents),
    ("split_live", split_live),
    ("help_dialog", help_dialog),
    ("label_ladder", label_ladder),
];

// ---------------------------------------------------------------- the states

/// Nothing open: the empty state, an empty roster, no tabs.
fn empty_workspace() -> (Workspace, Chrome) {
    (Workspace::scripted(), Chrome::dark())
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
/// chrome, which isolates the sidebar from the tab surface.
fn projects_agents() -> (Workspace, Chrome) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(project_rows());
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
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
    (ws, Chrome::dark())
}

/// Two live panes split in the first tab, with a second tab behind them.
///
/// `open_terminal` attaches direct and pushes the first frame, so both panes
/// are already live; reattaching `term-beta` moves it to the proxy transport
/// and focusing it puts that transport in the status line, which is the field
/// that distinguishes the two attach paths.
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

/// Rungs 2, 3 and 4 of the label ladder, in the chrome that draws them.
///
/// Every other state names its panes by rung 1: a scripted terminal is opened
/// under a name and keeps it. This one takes those names away so the
/// rungs below get their turn — `term-alpha` hosts a `codex` session,
/// `term-beta` has `nvim` in its foreground, and `term-gamma` has nothing left
/// to say and falls to the literal. Focus sits on `term-gamma` so the status
/// line carries that literal too, and no row here can be an id.
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

/// The keybind help dialog over the split. `render_workspace` dims the chrome
/// behind the mode overlay, so this capture also pins the dimmed background.
fn help_dialog() -> (Workspace, Chrome) {
    let (ws, mut chrome) = split_live();
    chrome.mode = Mode::KeybindHelp;
    (ws, chrome)
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

/// `Palette::entries` resolved to the colours the render actually paints with.
fn roles(theme: &Theme) -> Vec<(&'static str, Color)> {
    Palette::entries(theme)
        .into_iter()
        .map(|(name, token)| (name, token.color()))
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
fn capture(name: &str, terminal: &Terminal<TestBackend>, theme: &Theme) -> String {
    let roles = roles(theme);
    let buffer = terminal.backend().buffer();
    let mut out = format!(
        "# gclient screen golden: {name}\n\
         # {WIDTH}x{HEIGHT}, dark theme, colours normalised to theme::Palette roles\n"
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
fn deterministic_capture(name: &str, build: ScriptedState, theme: &Theme) -> String {
    let (ws, mut chrome) = build();
    let first = capture(name, &render(&ws, &mut chrome), theme);
    let second = capture(name, &render(&ws, &mut chrome), theme);
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
    let theme = Theme::new(ThemeKind::Dark);
    let update = std::env::var_os(UPDATE_ENV).is_some_and(|value| value == "1");

    for (name, build) in STATES {
        let rendered = deterministic_capture(name, build, &theme);
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
        .map(|(_, glyphs)| glyphs)
        .collect()
}

/// 3.1.1: the sidebar stacks the menu band, the machines, the projects, the
/// sessions, and the footer band. A project is a one-line card (state
/// glyph, name, branch with the ahead/behind counts, fold marker) that lists
/// its worktrees only while expanded; the `working` filter hides a project
/// with nothing live; the attention entry lists under the sessions band with
/// its reason; the collapsed rail numbers the cards and the sessions. The
/// committed capture pins the exact layout.
#[test]
fn projects_agents_golden() {
    let theme = Theme::new(ThemeKind::Dark);
    let rendered = deterministic_capture("projects_agents", projects_agents, &theme);
    let rows = glyph_rows(&rendered);
    let row_containing = |needle: &str| {
        rows.iter()
            .position(|row| row.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle:?}\n{rendered}"))
    };

    assert!(
        rows[0].starts_with(" [Menu]") && rows[0].contains("[+] "),
        "menu band: {:?}",
        rows[0]
    );
    let machines = row_containing(" Machines");
    let projects = row_containing(" Projects");
    assert!(
        rows[projects].contains("[working]"),
        "projects band: {:?}",
        rows[projects]
    );
    assert!(machines < projects, "the machines sit above the projects");
    // alpha carries the most urgent state of its bound agents: needs you.
    let alpha = row_containing("⍾ alpha");
    assert_eq!(alpha, projects + 1, "the card follows the one-row band");
    assert!(
        rows[alpha].starts_with(" ⍾ alpha (main ↑2 ↓1)") && rows[alpha].contains("▸│"),
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
    let sessions = row_containing(" Sessions");
    assert_eq!(sessions, alpha + 1, "the sessions band follows the cards");
    assert!(
        rows[sessions].contains("[view]"),
        "sessions band: {:?}",
        rows[sessions]
    );
    let entry = row_containing("term-alpha");
    assert_eq!(
        entry,
        sessions + 1,
        "the attention entry lists under sessions"
    );
    assert!(
        rows[entry].contains("⍾ term-alpha · needs you"),
        "a needs-you row carries its reason: {:?}",
        rows[entry]
    );
    assert!(
        rows[rows.len() - 1].contains("[«] │"),
        "footer band: {:?}",
        rows[rows.len() - 1]
    );

    // Expanding the card unfolds its worktree under it and flips the marker.
    let (ws, mut chrome) = projects_agents();
    chrome.sidebar.toggle_group("proj-alpha");
    let expanded = capture("projects_agents", &render(&ws, &mut chrome), &theme);
    let expanded_rows = glyph_rows(&expanded);
    assert!(
        expanded_rows[alpha].contains("▾│"),
        "expanded card: {:?}",
        expanded_rows[alpha]
    );
    assert!(
        expanded_rows[alpha + 1].starts_with("   └─ ○ feature · #123"),
        "worktree line: {:?}",
        expanded_rows[alpha + 1]
    );

    let (ws, mut chrome) = projects_agents();
    chrome.sidebar.collapsed = true;
    let rail = capture("projects_agents", &render(&ws, &mut chrome), &theme);
    assert!(
        rail.lines().any(|line| line.contains("|1 ⍾")),
        "the rail numbers the first project\n{rail}"
    );
    assert!(
        rail.lines().any(|line| line.contains("|2 ○")),
        "the rail numbers the sessions\n{rail}"
    );
    assert!(
        rail.lines().any(|line| line.contains("| » ")),
        "the rail carries the expand toggle\n{rail}"
    );

    let committed = fs::read_to_string(fixture_path("projects_agents"))
        .unwrap_or_else(|error| panic!("projects_agents golden: {error}"));
    assert!(
        rendered == committed,
        "projects_agents drifted from its capture\n{}",
        first_difference(&rendered, &committed)
    );
}

/// 4.2.1's second half. The tab bar is chrome and sits nowhere near a pane
/// body, so renaming one tab by a single character must move the capture.
#[test]
fn a_chrome_change_outside_the_terminal_content_region_fails_the_capture() {
    let theme = Theme::new(ThemeKind::Dark);
    let committed = deterministic_capture("split_live", split_live, &theme);

    let (ws, mut chrome) = split_live();
    chrome.tabs_mut().tabs[1].title = "secont".to_string();
    let moved = capture("split_live", &render(&ws, &mut chrome), &theme);

    assert!(
        moved != committed,
        "a renamed tab left the capture unchanged, so chrome is escaping the golden"
    );
}
