use super::*;
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SessionRow, SidebarRows, SourceStatus, WorktreeRow};
use crate::ui::sidebar::session_rows;
use serde_json::json;

fn scripted_workspace() -> Workspace {
    let mut ws = Workspace::scripted();
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
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
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
    });
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
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    ws
}

fn line_text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

#[test]
fn project_rows_list_working_projects_and_expand_one_card() {
    let ws = scripted_workspace();
    let mut chrome = Chrome::dark();
    // `working`: beta has no live entry, so only the focused alpha lists,
    // folded, with its toggle.
    let rows = project_rows(&ws, &chrome);
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(ids, ["proj-alpha"]);
    assert_eq!(rows[0].kind, RowKind::Project);
    assert_eq!(rows[0].branch.as_deref(), Some("main"));
    assert_eq!((rows[0].ahead, rows[0].behind), (2, 1));
    assert_eq!(rows[0].group, Some(false));
    assert!(rows[0].active && rows[0].selected);

    chrome.sidebar.all_projects = true;
    chrome.sidebar.selected = 1;
    chrome.sidebar.toggle_group("proj-alpha");
    let rows = project_rows(&ws, &chrome);
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(ids, ["proj-alpha", "wt-1", "proj-beta"]);
    assert_eq!(rows[0].group, Some(true));
    assert_eq!(rows[1].kind, RowKind::Worktree);
    assert_eq!(rows[1].label, "feature");
    assert_eq!(rows[1].detail, "#123");
    assert!(rows[1].nested && rows[1].last_child && rows[1].selected);
    assert_eq!(rows[2].branch.as_deref(), Some("~"));
    assert_eq!(rows[2].group, None);
    assert!(!rows[2].active);

    // The saved order leads; a second toggle folds the card again.
    chrome.sidebar.project_order = vec!["proj-beta".to_string()];
    chrome.sidebar.toggle_group("proj-alpha");
    let rows = project_rows(&ws, &chrome);
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(ids, ["proj-beta", "proj-alpha"]);
    assert_eq!(rows[1].group, Some(false));
    assert_eq!(
        displayed_project_ids(&ws, &chrome),
        ["proj-beta", "proj-alpha"]
    );
}

#[test]
fn session_rows_point_at_their_terminal() {
    let ws = scripted_workspace();
    let chrome = Chrome::dark();
    let rows = session_rows(&ws, &chrome);
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    // The roster entry, then the bare terminal no entry names.
    assert_eq!(ids, ["run:term-alpha", "terminal:term-beta"]);
    assert_eq!(rows[0].label, "term-alpha");
    assert_eq!(rows[0].kind, RowKind::Agent);
    assert_eq!(rows[0].state, RowState::Attention);
    assert_eq!(rows[0].height(), 2);
    assert!(rows[0].tokens.is_empty(), "{:?}", rows[0].tokens);
    assert_eq!(rows[1].label, "term-beta");
    assert_eq!(rows[1].tokens, ["native"]);
    assert!(!rows[1].nested);
}

#[test]
fn session_rows_render_session_effort_without_a_stray_separator() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        sessions: [(
            "proj-alpha".to_string(),
            vec![
                SessionRow {
                    id: "sess-effort".to_string(),
                    title: Some("effort session".to_string()),
                    reasoning_effort: Some("high".to_string()),
                    ..SessionRow::default()
                },
                SessionRow {
                    id: "sess-bare".to_string(),
                    title: Some("bare session".to_string()),
                    ..SessionRow::default()
                },
            ],
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
                "entry_id": "session:sess-effort",
                "session_id": "sess-effort",
                "provider": "codex",
                "model": "gpt-5",
                "terminal": {"terminal_id": "term-effort", "backend": "native"}
            },
            {
                "entry_id": "session:sess-bare",
                "session_id": "sess-bare",
                "provider": "codex",
                "model": "gpt-5",
                "terminal": {"terminal_id": "term-bare", "backend": "native"}
            }
        ]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-effort", "native", "epoch").unwrap();
    ws.open_terminal("term-bare", "native", "epoch").unwrap();

    let rows = session_rows(&ws, &Chrome::dark());

    assert_eq!(rows[0].tokens, ["codex", "gpt-5-high"]);
    assert_eq!(rows[1].tokens, ["codex", "gpt-5"]);
}

#[test]
fn project_lines_carry_the_toggle_branch_and_counts() {
    let chrome = Chrome::dark();
    let row = SidebarRow {
        id: "proj-alpha".into(),
        label: "alpha".into(),
        branch: Some("main".into()),
        ahead: 2,
        behind: 1,
        group: Some(true),
        active: true,
        ..SidebarRow::default()
    };
    assert_eq!(
        line_text(&row_line(&row, 24, &chrome)),
        " ○ alpha (main ↑2 ↓1)  ▾"
    );
    // The parenthetical goes whole before the name loses a cell.
    assert_eq!(line_text(&row_line(&row, 16, &chrome)), " ○ alpha       ▾");
    assert_eq!(line_text(&row_line(&row, 7, &chrome)), " ○ a… ▾");
    assert_eq!(line_text(&row_second_line(&row, 24, &chrome)), "");
    let worktree = SidebarRow {
        id: "wt-1".into(),
        label: "feature".into(),
        kind: RowKind::Worktree,
        detail: "#123".into(),
        nested: true,
        last_child: true,
        selected: true,
        ..SidebarRow::default()
    };
    assert_eq!(
        line_text(&row_line(&worktree, 30, &chrome)),
        "▸  └─ ○ feature · #123"
    );
    let group = SidebarRow {
        id: "group:proj-alpha".into(),
        label: "alpha".into(),
        kind: RowKind::Group,
        ..SidebarRow::default()
    };
    assert_eq!(line_text(&row_line(&group, 12, &chrome)), " alpha ─────");
    assert_eq!(group.height(), 1);
}

#[test]
fn agent_lines_carry_needs_you_and_nest_under_their_session() {
    let chrome = Chrome::dark();
    let row = SidebarRow {
        id: "run:term-alpha".into(),
        label: "term-alpha".into(),
        kind: RowKind::Agent,
        state: RowState::Attention,
        tokens: vec!["codex".into(), "gpt-5".into(), "#123".into()],
        ..SidebarRow::default()
    };
    let wide = line_text(&row_line(&row, 60, &chrome));
    assert_eq!(wide, " ⍾ term-alpha · needs you");
    // The state word no longer fits beside the whole title, so it drops
    // before the title loses a cell.
    let narrow = line_text(&row_line(&row, 14, &chrome));
    assert_eq!(narrow, " ⍾ term-alpha");
    let tiny = line_text(&row_line(&row, 8, &chrome));
    assert_eq!(tiny, " ⍾ term…");
    // An idle row carries no word.
    let idle = SidebarRow {
        state: RowState::Idle,
        ..row.clone()
    };
    assert_eq!(line_text(&row_line(&idle, 60, &chrome)), " ○ term-alpha");
    // The tokens sit under the label and drop from the right.
    assert_eq!(
        line_text(&row_second_line(&row, 60, &chrome)),
        "   codex · gpt-5 · #123"
    );
    assert_eq!(
        line_text(&row_second_line(&row, 18, &chrome)),
        "   codex · gpt-5"
    );
    let nested = SidebarRow {
        nested: true,
        ..row.clone()
    };
    assert_eq!(
        line_text(&row_line(&nested, 60, &chrome)),
        " ├─ ⍾ term-alpha · needs you"
    );
    assert_eq!(
        line_text(&row_second_line(&nested, 60, &chrome)),
        "      codex · gpt-5 · #123"
    );
    let bare = SidebarRow {
        tokens: Vec::new(),
        ..row
    };
    assert_eq!(line_text(&row_second_line(&bare, 60, &chrome)), "");
}

#[test]
fn ticker_walks_the_active_label_and_rests_at_both_ends() {
    assert_eq!(ticker_window("abcdefghij", 12, 500), "abcdefghij");
    assert_eq!(ticker_window("abcdefghij", 3, 500), "abcdefghij");
    let at = |step: u64| ticker_window("abcdefghij", 6, step * TICKER_STEP);
    assert_eq!(at(0), "abcdef");
    assert_eq!(at(TICKER_PAUSE - 1), "abcdef");
    assert_eq!(at(TICKER_PAUSE + 2), "cdefgh");
    assert_eq!(at(TICKER_PAUSE + 4), "efghij");
    assert_eq!(at(2 * TICKER_PAUSE + 4), "efghij");
    assert_eq!(at(2 * TICKER_PAUSE + 6), "cdefgh");
    assert_eq!(at(2 * TICKER_PAUSE + 8), "abcdef");
    // Only the active row scrolls; a plain row keeps the ellipsis.
    let mut chrome = Chrome::dark();
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    let row = SidebarRow {
        id: "session:one".into(),
        label: "abcdefghij".into(),
        kind: RowKind::Agent,
        active: true,
        ..SidebarRow::default()
    };
    assert_eq!(line_text(&row_line(&row, 9, &chrome)), " ○ cdefgh");
    let plain = SidebarRow {
        active: false,
        ..row
    };
    assert_eq!(line_text(&row_line(&plain, 9, &chrome)), " ○ abcde…");
}
