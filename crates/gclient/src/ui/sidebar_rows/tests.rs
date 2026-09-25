use super::*;
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SessionRow, SidebarRows, SourceStatus, WorktreeRow};
use crate::ui::sidebar::agent_rows;
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
fn agent_and_terminal_rows_are_separate() {
    let ws = scripted_workspace();
    let chrome = Chrome::dark();
    let rows = agent_rows(&ws, &chrome);
    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(ids, ["run:term-alpha"]);
    assert_eq!(rows[0].label, "term-alpha");
    assert_eq!(rows[0].kind, RowKind::Agent);
    assert_eq!(rows[0].state, RowState::Attention);
    assert_eq!(rows[0].height(), 3);
    assert_eq!(rows[0].task, None);
    let terminals = crate::ui::sidebar::terminal_rows(&ws, &chrome);
    assert_eq!(terminals.len(), 1);
    assert_eq!(terminals[0].id, "terminal:term-beta");
    assert_eq!(terminals[0].label, "term-beta");
    assert_eq!(terminals[0].kind, RowKind::Terminal);
    assert_eq!(terminals[0].detail, "gclient");
    assert_eq!(terminals[0].height(), 2);
    assert!(!terminals[0].nested);
}

#[test]
fn agent_rows_render_three_lines_with_the_model_slug() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "proj-alpha".to_string(),
            name: "alpha".to_string(),
            display_name: "alpha".to_string(),
            ..ProjectRow::default()
        }],
        sessions: [(
            "proj-alpha".to_string(),
            vec![
                SessionRow {
                    id: "sess-effort".to_string(),
                    reference: Some("#77".to_string()),
                    title: Some("effort session".to_string()),
                    reasoning_effort: Some("high".to_string()),
                    ..SessionRow::default()
                },
                SessionRow {
                    id: "sess-bare".to_string(),
                    reference: Some("#78".to_string()),
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

    let rows = agent_rows(&ws, &Chrome::dark());

    assert_eq!(rows[0].height(), 3);
    assert_eq!(rows[0].definition, "Codex");
    assert_eq!(rows[0].reference, "#77");
    assert_eq!(rows[0].provider.as_deref(), Some("codex"));
    assert_eq!(rows[0].model_slug, "gpt-5-high");
    let third = row_third_line(&rows[0], 34, &Chrome::dark());
    assert_eq!(line_text(&third), "   gpt-5-high");
    assert!(third
        .spans
        .iter()
        .all(|span| !span.style.add_modifier.contains(Modifier::DIM)));
    assert_eq!(rows[1].model_slug, "gpt-5");

    let long = SidebarRow {
        definition: "An extraordinarily long agent name".into(),
        reference: "#77".into(),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    let line = line_text(&row_line(&long, 34, &Chrome::dark(), 0));
    assert!(line.contains("… (#77)"), "{line}");

    let mut all = Chrome::dark();
    all.sidebar.all_sessions = true;
    let rows = agent_rows(&ws, &all);
    let effort = rows
        .iter()
        .find(|row| row.id == "session:sess-effort")
        .expect("effort row");
    assert_eq!(effort.reference, "#77");
    all.prefs.agent_sort = crate::ui::settings::AgentSort::Priority;
    let rows = agent_rows(&ws, &all);
    let effort = rows
        .iter()
        .find(|row| row.id == "session:sess-effort")
        .expect("effort row");
    assert_eq!(effort.reference, "alpha#77");
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
        line_text(&row_line(&row, 24, &chrome, 0)),
        " ○ alpha (main ↑2 ↓1)  ▾"
    );
    // The parenthetical goes whole before the name loses a cell.
    assert_eq!(
        line_text(&row_line(&row, 16, &chrome, 0)),
        " ○ alpha       ▾"
    );
    assert_eq!(line_text(&row_line(&row, 7, &chrome, 0)), " ○ a… ▾");
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
        line_text(&row_line(&worktree, 30, &chrome, 0)),
        "▸  └─ ○ feature · #123"
    );
    let group = SidebarRow {
        id: "group:proj-alpha".into(),
        label: "alpha".into(),
        kind: RowKind::Group,
        ..SidebarRow::default()
    };
    assert_eq!(line_text(&row_line(&group, 12, &chrome, 0)), " alpha ─────");
    assert_eq!(group.height(), 1);
}

#[test]
fn agent_lines_carry_needs_you_and_nest_under_their_session() {
    let chrome = Chrome::dark();
    let row = SidebarRow {
        id: "run:term-alpha".into(),
        definition: "Backend".into(),
        reference: "#123".into(),
        model_slug: "gpt-5".into(),
        task: Some(("#123".into(), "Review auth".into())),
        kind: RowKind::Agent,
        state: RowState::Attention,
        ..SidebarRow::default()
    };
    let wide = line_text(&row_line(&row, 60, &chrome, 0));
    assert_eq!(wide, " ⍾ Backend (#123)");
    assert_eq!(
        line_text(&row_second_line(&row, 60, &chrome)),
        "   Task #123 - Review auth"
    );
    assert_eq!(line_text(&row_third_line(&row, 60, &chrome)), "   gpt-5");
    let idle = SidebarRow {
        state: RowState::Idle,
        ..row.clone()
    };
    assert_eq!(
        line_text(&row_line(&idle, 60, &chrome, 0)),
        " ○ Backend (#123)"
    );
    let nested = SidebarRow {
        nested: true,
        ..row.clone()
    };
    assert_eq!(
        line_text(&row_line(&nested, 60, &chrome, 0)),
        " ├─ ⍾ Backend (#123)"
    );
    assert_eq!(
        line_text(&row_second_line(&nested, 60, &chrome)),
        "      Task #123 - Review auth"
    );
    let bare = SidebarRow { task: None, ..row };
    assert_eq!(
        line_text(&row_second_line(&bare, 60, &chrome)),
        "   No assigned task"
    );
}

#[test]
fn marquee_shares_one_period_and_parks_shorter_titles() {
    assert_eq!(
        ticker_window("abcdefghij", 12, 500, 0, TitleScrolling::Left),
        "abcdefghij"
    );
    assert_eq!(
        ticker_window("abcdefghij", 3, 500, 0, TitleScrolling::Left),
        "ab…"
    );
    // Alone, a four-cell overrun rests, walks, parks and jumps home.
    let at =
        |step: u64| ticker_window("abcdefghij", 6, step * TICKER_STEP, 0, TitleScrolling::Left);
    assert_eq!(at(0), "abcdef");
    assert_eq!(at(TICKER_PAUSE - 1), "abcdef");
    assert_eq!(at(TICKER_PAUSE + 2), "cdefgh");
    assert_eq!(at(TICKER_PAUSE + 4), "efghij");
    assert_eq!(at(2 * TICKER_PAUSE + 3), "efghij");
    assert_eq!(at(2 * TICKER_PAUSE + 4), "abcdef");
    // Beside a ten-cell overrun it parks until that one has arrived, so
    // both restart together (D7).
    let beside = |step: u64| {
        ticker_window(
            "abcdefghij",
            6,
            step * TICKER_STEP,
            10,
            TitleScrolling::Left,
        )
    };
    assert_eq!(beside(TICKER_PAUSE + 4), "efghij");
    assert_eq!(beside(2 * TICKER_PAUSE + 9), "efghij");
    assert_eq!(beside(2 * TICKER_PAUSE + 10), "abcdef");
}

#[test]
fn every_overflowing_row_uses_the_selected_scroll_direction() {
    let mut chrome = Chrome::dark();
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    let row = SidebarRow {
        id: "session:one".into(),
        task: Some(("#1".into(), "abcdefghij".into())),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    assert_eq!(row_travel(&row, 19), 4);
    assert_eq!(row_travel(&row, 60), 0);
    // A plain row scrolls like the active one.
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - cdefgh"
    );
    // Beside a longer title it reads the same clock, then parks at its
    // end while the longer one walks on.
    chrome.view.title_travel = 10;
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - cdefgh"
    );
    chrome.ticker = (TICKER_PAUSE + 8) * TICKER_STEP;
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - efghij"
    );
    // Off truncates like any other title.
    chrome.prefs.title_scrolling = TitleScrolling::Off;
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - abcde…"
    );

    // Right uses the same clock in the opposite direction.
    chrome.prefs.title_scrolling = TitleScrolling::Right;
    chrome.ticker = 0;
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - efghij"
    );
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    assert_eq!(
        line_text(&row_second_line(&row, 19, &chrome)),
        "   Task #1 - cdefgh"
    );
}

#[test]
fn task_prefix_stays_fixed_while_the_title_scrolls() {
    let mut chrome = Chrome::dark();
    let row = SidebarRow {
        id: "session:one".into(),
        definition: "Codex".into(),
        reference: "#77".into(),
        task: Some(("#13936".into(), "修复 workspace chrome".into())),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    let first = line_text(&row_second_line(&row, 24, &chrome));
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    let later = line_text(&row_second_line(&row, 24, &chrome));
    assert!(first.contains("Task #13936 - "), "{first}");
    assert!(later.contains("Task #13936 - "), "{later}");
    assert_ne!(first, later);
    assert!(row_travel(&row, 24) > 0);

    let unassigned = SidebarRow { task: None, ..row };
    assert_eq!(
        line_text(&row_second_line(&unassigned, 24, &chrome)),
        "   No assigned task"
    );
}
