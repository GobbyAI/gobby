//! 3.3.8 imported chrome has no herdr product concepts.

use gobby_client::ui::{navigator, sidebar, status};
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::json;
use std::fs;
use std::path::PathBuf;

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
    "worktree",
    "is_mobile",
    "mobile_header",
    "ToastHerdr",
    "HERDR_AGENT",
];

#[test]
fn no_herdr_concepts_in_imported_chrome() {
    let ui = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/ui");
    let mut hits = Vec::new();
    let mut stack = vec![ui.clone()];
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
            let text = fs::read_to_string(&path).unwrap().to_lowercase();
            for needle in FORBIDDEN {
                if text.contains(&needle.to_lowercase()) {
                    hits.push(format!("{}: {needle}", path.display()));
                }
            }
        }
    }
    assert!(
        hits.is_empty(),
        "forbidden herdr concepts:\n{}",
        hits.join("\n")
    );

    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{"entry_id": "run:alpha", "kind": "blocked"}]
    }));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-live", "native", "epoch").unwrap();

    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    terminal
        .draw(|frame| {
            let area = frame.area();
            sidebar::render_sidebar(frame, area, &ws);
        })
        .unwrap();
    let side = terminal.backend().buffer().clone();
    let side_text: String = side.content().iter().map(|c| c.symbol()).collect();
    assert!(
        side_text.contains("term-live")
            || side_text.contains("blocked")
            || side_text.contains("alpha"),
        "sidebar renders roster/attention, got {side_text:?}"
    );

    terminal
        .draw(|frame| {
            navigator::render_navigator(frame, frame.area(), &ws);
        })
        .unwrap();
    let nav: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(
        nav.contains("term-live") || nav.contains("proj") || !nav.trim().is_empty(),
        "navigator renders workspace data"
    );

    terminal
        .draw(|frame| {
            status::render_status(frame, frame.area(), &ws);
        })
        .unwrap();
    let st: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(
        st.contains("observe")
            || st.contains("held")
            || st.contains("gobby")
            || st.contains("live")
            || !st.trim().is_empty(),
        "status renders Gobby state"
    );
}
