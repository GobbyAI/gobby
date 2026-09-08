//! 3.1.1 / 3.1.4: the carve matches `UPSTREAM.md` and renders scripted data.

use gobby_client::theme::{Theme, ThemeKind};
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::dialogs::{CloseScope, CloseTarget, Dialog};
use gobby_client::ui::{keybind_help, navigator, render_workspace, sidebar, status, Chrome};
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};

const FORBIDDEN: &[&str] = &[
    "agent_detection",
    "crate::detect",
    "crate::plugin",
    "crate::integration",
    "crate::persist",
    "plugin_command",
    "onboarding",
    "release_notes",
    "release notes",
    "is_mobile",
    "mobile_header",
    "ToastHerdr",
    "HERDR_AGENT",
];

const FORK_HEADER: &str = "// upstream: herdr v0.8.0 ";
/// Header of a `src/ui` module written for Gobby rather than carved from the
/// fork; a parenthetical after it may name what the module was modelled on.
const NATIVE_HEADER: &str = "// upstream: none";

fn crate_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn backticked(cell: &str) -> Vec<String> {
    cell.split('`')
        .skip(1)
        .step_by(2)
        .map(str::to_string)
        .collect()
}

/// Rows of the `UPSTREAM.md` accept/reject table: (upstream path, decision, gobby cell).
fn upstream_rows() -> Vec<(String, String, String)> {
    let text = fs::read_to_string(crate_root().join("UPSTREAM.md")).unwrap();
    text.lines()
        .filter(|line| line.starts_with("| `src/"))
        .map(|line| {
            let cells: Vec<&str> = line.split('|').map(str::trim).collect();
            let upstream = backticked(cells[1]).remove(0);
            (upstream, cells[2].to_string(), cells[3].to_string())
        })
        .collect()
}

fn gobby_path(token: &str) -> PathBuf {
    let rel = token.trim_start_matches("crates/gclient/");
    let rel = rel.strip_prefix("src/").unwrap_or(rel);
    crate_root().join("src").join(rel)
}

fn first_line(path: &Path) -> String {
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .next()
        .unwrap_or_default()
        .to_string()
}

fn scripted_workspace() -> Workspace {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:term-alpha",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    ws
}

fn chrome_for(ws: &Workspace, kind: ThemeKind) -> Chrome {
    let mut chrome = Chrome::new(Theme::new(kind));
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    chrome.open_tab(alpha, "second");
    chrome.tabs_mut().active_tab = 0;
    // Splitting focuses the new pane; the assertions below name term-alpha.
    assert!(chrome.focus_pane(alpha));
    chrome
}

fn screen(terminal: &Terminal<TestBackend>) -> String {
    let buffer = terminal.backend().buffer();
    let width = buffer.area.width as usize;
    let cells: Vec<String> = buffer
        .content()
        .iter()
        .map(|c| c.symbol().to_string())
        .collect();
    cells
        .chunks(width)
        .map(|row| row.concat())
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn carve_matches_upstream_map_and_renders_data() {
    let rows = upstream_rows();
    let accepted: Vec<_> = rows.iter().filter(|r| r.1.starts_with("accept")).collect();
    let rejected: Vec<_> = rows.iter().filter(|r| r.1.starts_with("reject")).collect();
    assert!(accepted.len() >= 14, "keep-set rows: {}", accepted.len());
    assert!(rejected.len() >= 4, "reject rows: {}", rejected.len());

    let mut seen = 0;
    for (upstream, _, gobby) in &accepted {
        for token in backticked(gobby).into_iter().filter(|t| t.ends_with(".rs")) {
            let path = gobby_path(&token);
            assert!(
                path.is_file(),
                "accepted {upstream} missing at {}",
                path.display()
            );
            let expected = format!("{FORK_HEADER}{upstream}");
            assert_eq!(first_line(&path), expected, "{}", path.display());
            seen += 1;
        }
    }
    assert!(seen >= 16, "accepted module files with headers: {seen}");
    for (upstream, _, _) in &rejected {
        if let Some(rel) = upstream.strip_prefix("src/") {
            let path = crate_root().join("src").join(rel);
            assert!(
                !path.exists(),
                "rejected {upstream} present at {}",
                path.display()
            );
        }
    }

    let ui = crate_root().join("src/ui");
    let mut hits = Vec::new();
    let mut headerless = Vec::new();
    let mut stack = vec![ui];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            let text = fs::read_to_string(&path).unwrap();
            let lower = text.to_lowercase();
            for needle in FORBIDDEN {
                if lower.contains(&needle.to_lowercase()) {
                    hits.push(format!("{}: {needle}", path.display()));
                }
            }
            let name = path.file_name().unwrap().to_string_lossy().to_string();
            let headed = text.starts_with(FORK_HEADER) || text.starts_with(NATIVE_HEADER);
            if name != "mod.rs" && name != "tests.rs" && !headed {
                headerless.push(path.display().to_string());
            }
        }
    }
    assert!(
        hits.is_empty(),
        "forbidden herdr concepts:\n{}",
        hits.join("\n")
    );
    assert!(
        headerless.is_empty(),
        "modules without upstream header:\n{}",
        headerless.join("\n")
    );

    let ws = scripted_workspace();
    let mut chrome = chrome_for(&ws, ThemeKind::Dark);
    let area = Rect::new(0, 0, 120, 40);
    chrome.compute_view(&ws, area);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();

    terminal
        .draw(|frame| {
            sidebar::render_sidebar(frame, frame.area(), &ws, &chrome);
        })
        .unwrap();
    let side = screen(&terminal);
    // The sidebar lists projects and agents; term-beta, a plain terminal
    // with no entry, shows in the tab bar instead.
    for needle in [" projects", " agents", "term-alpha", "blocked"] {
        assert!(side.contains(needle), "sidebar lacks {needle:?}:\n{side}");
    }

    chrome.mode = Mode::Navigator;
    terminal
        .draw(|frame| {
            navigator::render_navigator(frame, frame.area(), &ws, &chrome);
        })
        .unwrap();
    let nav = screen(&terminal);
    for needle in ["term-alpha", "term-beta", "blocked"] {
        assert!(nav.contains(needle), "navigator lacks {needle:?}:\n{nav}");
    }

    chrome.mode = Mode::Terminal;
    terminal
        .draw(|frame| {
            status::render_status_line(frame, Rect::new(0, 39, 120, 1), &ws, &chrome);
        })
        .unwrap();
    let st = screen(&terminal);
    assert!(
        st.contains("observe") && st.contains("term-alpha"),
        "status lacks control state and focused terminal:\n{st}"
    );
}

#[test]
fn render_workspace_composes_imported_chrome() {
    let ws = scripted_workspace();
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let mut chrome = chrome_for(&ws, kind);
        chrome.mode = Mode::ConfirmClose;
        chrome.dialog = Some(Dialog::ConfirmClose {
            target: CloseTarget::Pane,
            title: "term-beta".to_string(),
            scope: CloseScope::Panes(1),
        });
        let area = Rect::new(0, 0, 120, 40);
        chrome.compute_view(&ws, area);
        assert_eq!(chrome.view.pane_infos.len(), 2, "{kind:?} pane rects");
        assert_eq!(chrome.view.split_borders.len(), 1, "{kind:?} split borders");
        assert!(chrome.view.tab_bar_rect.is_some(), "{kind:?} tab bar");

        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| {
                render_workspace(frame, &ws, &chrome);
            })
            .unwrap();
        let text = screen(&terminal);
        for needle in [
            "term-alpha",
            "term-beta",
            "blocked",
            "second",
            "observe",
            "close",
        ] {
            assert!(
                text.contains(needle),
                "{kind:?} frame lacks {needle:?}:\n{text}"
            );
        }
        assert!(
            !text.contains('!'),
            "{kind:?} frame uses an exclamation point:\n{text}"
        );

        let entries = keybind_help::filtered_entries(&chrome.keymap, "");
        assert!(!entries.is_empty());
        assert!(
            entries.iter().all(|e| e.name != "custom_command"),
            "reserved binding listed in help: {entries:?}"
        );
        for binding in chrome.keymap.bindings.iter().filter(|b| b.reserved) {
            assert!(entries.iter().all(|e| e.name != binding.name));
        }
        chrome.mode = Mode::KeybindHelp;
        chrome.dialog = None;
        terminal
            .draw(|frame| {
                render_workspace(frame, &ws, &chrome);
            })
            .unwrap();
        let help = screen(&terminal);
        assert!(
            help.contains("split_vertical") || help.contains("Split side by side"),
            "{help}"
        );
        assert!(!help.contains("custom_command"), "{help}");
    }
}
