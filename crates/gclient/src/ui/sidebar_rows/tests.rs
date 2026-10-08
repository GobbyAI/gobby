use super::*;
use crate::app::sidebar_model::AgentEntry;
use crate::app::Workspace;
use crate::daemon::{Checkout, ProjectRow, SessionRow, SidebarRows, SourceStatus, WorktreeRow};
use crate::theme::{Theme, ThemeKind};
use crate::ui::sidebar::agent_rows;
use ratatui::style::Color;
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
    let mut ws = scripted_workspace();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    ws.pane_mut(beta).cwd = Some("/srv/app".into());
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
    assert_eq!(terminals[0].detail, "/srv/app");
    assert_eq!(terminals[0].address, "gclient");
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
                    source: Some("codex".to_string()),
                    model: Some("gpt-5".to_string()),
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
    assert_eq!(rows[0].model_slug, "gpt-5-high");
    let third = row_third_line(&rows[0], 34, &Chrome::dark());
    assert_eq!(line_text(&third), "   gpt-5-high");
    assert!(third
        .spans
        .iter()
        .all(|span| !span.style.add_modifier.contains(Modifier::DIM)));
    // The model takes the theme's model colour in every palette, grays
    // included, and never a state hue (#23416's model-line board).
    let mut mono = Chrome::dark();
    mono.prefs.monochrome = true;
    mono.set_theme(ThemeKind::Dark);
    let mut light = Chrome::dark();
    light.set_theme(ThemeKind::Light);
    for chrome in [Chrome::dark(), light, mono] {
        let p = &chrome.palette;
        let model = row_third_line(&rows[0], 34, &chrome).spans[1].style.fg;
        assert_eq!(model, Some(p.model));
        assert!(model != Some(p.text) && model != Some(p.overlay1));
    }
    assert_eq!(rows[1].model_slug, "gpt-5");
    let fable = SidebarRow {
        model_slug: "claude-fable-5.1-xhigh".into(),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    assert_eq!(
        line_text(&row_third_line(&fable, 34, &Chrome::dark())),
        "   claude-fable-5.1-xhigh",
        "the whole model shows while the width holds it"
    );

    let long = SidebarRow {
        definition: "An extraordinarily long agent name".into(),
        reference: "#77".into(),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    // With title scrolling off a long name truncates; scrolling is covered by
    // an_agent_name_too_long_for_its_row_scrolls_on_the_shared_clock.
    let mut still = Chrome::dark();
    still.prefs.title_scrolling = TitleScrolling::Off;
    let line = line_text(&row_line(&long, 34, &still, 0));
    assert!(line.ends_with(" #77: An extraordinarily long a…"), "{line}");

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
fn model_slug_reads_provider_model_effort_and_skips_absent_segments() {
    let slug = |model: Option<&str>, display: Option<&str>, effort: Option<&str>| {
        AgentEntry {
            model: model.map(str::to_owned),
            model_display_name: display.map(str::to_owned),
            effort: effort.map(str::to_owned),
            ..AgentEntry::default()
        }
        .model_slug()
    };
    assert_eq!(
        slug(Some("gpt-6-sol"), None, Some("xhigh")),
        "gpt-6-sol-xhigh"
    );
    assert_eq!(
        slug(Some("claude-fable-5-1"), Some("Fable 5.1"), Some("high")),
        "claude-fable-5.1-high"
    );
    assert_eq!(slug(Some("gpt-6-sol"), None, None), "gpt-6-sol");
    assert_eq!(
        slug(None, None, Some("xhigh")),
        "xhigh",
        "a run spawned with an effort before its model is known"
    );
    assert_eq!(slug(None, None, None), "");
}

#[test]
fn machine_detail_keeps_its_color_without_dimming_text() {
    let row = SidebarRow {
        kind: RowKind::Machine,
        label: "mbp".into(),
        detail: "local".into(),
        ..SidebarRow::default()
    };
    let line = row_line(&row, 34, &Chrome::dark(), 0);
    assert!(line_text(&line).contains(" · local"));
    for span in line.spans {
        if span.content.trim().is_empty() {
            continue;
        }
        assert!(
            !span.style.add_modifier.contains(Modifier::DIM),
            "visible span {:?} is dimmed",
            span.content
        );
    }
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
    // With no agent bound the worktree has no state: a blank holds the
    // glyph's cell so the branch stays under the card's name.
    let unbound = SidebarRow {
        state: RowState::Unknown,
        ..worktree.clone()
    };
    assert_eq!(
        line_text(&row_line(&unbound, 30, &chrome, 0)),
        "▸  └─   feature · #123"
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
    assert_eq!(wide, " ⍾ #123: Backend");
    assert_eq!(
        line_text(&row_second_line(&row, 60, &chrome)),
        "   Working task 123 Review auth"
    );
    assert_eq!(line_text(&row_third_line(&row, 60, &chrome)), "   gpt-5");
    let idle = SidebarRow {
        state: RowState::Idle,
        ..row.clone()
    };
    assert_eq!(
        line_text(&row_line(&idle, 60, &chrome, 0)),
        " ○ #123: Backend"
    );
    let nested = SidebarRow {
        nested: true,
        ..row.clone()
    };
    assert_eq!(
        line_text(&row_line(&nested, 60, &chrome, 0)),
        " ├─ ⍾ #123: Backend"
    );
    assert_eq!(
        line_text(&row_second_line(&nested, 60, &chrome)),
        "      Working task 123 Review auth"
    );
    let bare = SidebarRow { task: None, ..row };
    assert_eq!(
        line_text(&row_second_line(&bare, 60, &chrome)),
        "   No assigned task"
    );
}

#[test]
fn marquee_shares_one_period_and_rests_shorter_titles_at_the_start() {
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
    // Beside a ten-cell overrun it rests back at the start once its own
    // pass ends, and both restart together (D7).
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
    assert_eq!(beside(2 * TICKER_PAUSE + 3), "efghij");
    assert_eq!(beside(2 * TICKER_PAUSE + 4), "abcdef");
    assert_eq!(beside(2 * TICKER_PAUSE + 10), "abcdef");
    assert_eq!(beside(3 * TICKER_PAUSE + 12), "cdefgh");
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
    assert_eq!(row_travel(&row, 24), 4);
    assert_eq!(row_travel(&row, 60), 0);
    // A plain row scrolls like the active one.
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   rking task 1 abcdefgh"
    );
    // Beside a longer title it reads the same clock, then parks at its
    // end while the longer one walks on.
    chrome.view.title_travel = 10;
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   rking task 1 abcdefgh"
    );
    chrome.ticker = (TICKER_PAUSE + 8) * TICKER_STEP;
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   ing task 1 abcdefghij"
    );
    // Off truncates like any other title.
    chrome.prefs.title_scrolling = TitleScrolling::Off;
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   Working task 1 abcde…"
    );

    // Right uses the same clock in the opposite direction.
    chrome.prefs.title_scrolling = TitleScrolling::Right;
    chrome.ticker = 0;
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   ing task 1 abcdefghij"
    );
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    assert_eq!(
        line_text(&row_second_line(&row, 24, &chrome)),
        "   rking task 1 abcdefgh"
    );
}

#[test]
fn the_whole_task_line_tickers_as_one_string() {
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
    assert_eq!(first, "   Working task 13936 修");
    assert_eq!(later, "   rking task 13936 修复");
    assert!(row_travel(&row, 24) > 0);

    let unassigned = SidebarRow { task: None, ..row };
    assert_eq!(
        line_text(&row_second_line(&unassigned, 24, &chrome)),
        "   No assigned task"
    );
}

#[test]
fn a_worktree_name_too_long_for_its_row_scrolls_on_the_shared_clock() {
    let mut chrome = Chrome::dark();
    let worktree = SidebarRow {
        id: "wt-long".into(),
        label: "feature-with-a-long-name".into(),
        kind: RowKind::Worktree,
        detail: "#123".into(),
        nested: true,
        last_child: true,
        ..SidebarRow::default()
    };
    // The marker, indent, `└─ `, glyph and spacer leave the name 12 cells.
    assert_eq!(worktree_name_budget(&worktree, 20), 12);
    assert_eq!(
        row_travel(&worktree, 20),
        display_width(&worktree.label) - 12
    );
    assert_eq!(
        line_text(&row_line(&worktree, 20, &chrome, 0)),
        "   └─ ○ feature-with"
    );
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    assert_eq!(
        line_text(&row_line(&worktree, 20, &chrome, 0)),
        "   └─ ○ ature-with-a"
    );

    // A name that fits once its task drops stays still.
    let fits = SidebarRow {
        label: "feature-name".into(),
        ..worktree
    };
    assert_eq!(row_travel(&fits, 20), 0);
    assert_eq!(
        line_text(&row_line(&fits, 20, &chrome, 0)),
        "   └─ ○ feature-name"
    );
}

#[test]
fn an_agent_name_too_long_for_its_row_scrolls_on_the_shared_clock() {
    let mut chrome = Chrome::dark();
    let lane = SidebarRow {
        id: "session:lane".into(),
        definition: "Lane 2 developer gClient chrome".into(),
        reference: "#15398".into(),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    // The marker, glyph and spacer leave 21 of 24 cells and the pinned
    // `#15398: ` takes 8, so the 31-cell title scrolls through 13.
    assert_eq!(row_travel(&lane, 24), 31 - 13);
    assert_eq!(
        line_text(&row_line(&lane, 24, &chrome, 0)),
        " ○ #15398: Lane 2 develo"
    );
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    assert_eq!(
        line_text(&row_line(&lane, 24, &chrome, 0)),
        " ○ #15398: ne 2 develope"
    );
    chrome.ticker = (TICKER_PAUSE + 18) * TICKER_STEP;
    let line = row_line(&lane, 24, &chrome, 0);
    assert_eq!(line_text(&line), " ○ #15398: Client chrome");
    let reference = line
        .spans
        .iter()
        .find(|span| span.content == "#15398")
        .expect("the reference keeps its own span");
    assert_eq!(reference.style.fg, Some(chrome.palette.identifier));

    chrome.prefs.title_scrolling = TitleScrolling::Off;
    assert_eq!(
        line_text(&row_line(&lane, 24, &chrome, 0)),
        " ○ #15398: Lane 2 devel…"
    );

    // A row too narrow to pin the reference beside a title truncates.
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert_eq!(row_travel(&lane, 10), 0);
    assert_eq!(line_text(&row_line(&lane, 10, &chrome, 0)), " ○ #15398…");

    // A name that fits stays still.
    let fits = SidebarRow {
        definition: "Lane 2".into(),
        ..lane
    };
    chrome.prefs.title_scrolling = TitleScrolling::Left;
    assert_eq!(row_travel(&fits, 24), 0);
    assert_eq!(
        line_text(&row_line(&fits, 24, &chrome, 0)),
        " ○ #15398: Lane 2"
    );
}

#[test]
fn a_terminal_row_puts_its_address_at_the_right_edge_while_the_name_leaves_room() {
    let chrome = Chrome::dark();
    let row = SidebarRow {
        id: "terminal:t1".into(),
        label: "zsh".into(),
        kind: RowKind::Terminal,
        detail: "~/Projects/gobby".into(),
        address: "0:0:0:2".into(),
        ..SidebarRow::default()
    };
    assert_eq!(
        line_text(&row_line(&row, 20, &chrome, 0)),
        " ○ zsh       0:0:0:2"
    );
    assert_eq!(
        line_text(&row_second_line(&row, 20, &chrome)),
        "   ~/Projects/gobby"
    );

    // The name outranks the address.
    let long = SidebarRow {
        label: "cargo-nextest-run".into(),
        ..row
    };
    assert_eq!(
        line_text(&row_line(&long, 20, &chrome, 0)),
        " ○ cargo-nextest-run"
    );
}

#[test]
fn a_manual_session_title_takes_the_definition_slot_on_line_one() {
    let mut ws = Workspace::scripted();
    let session = |id: &str, reference: &str, title: &str, source: Option<&str>| SessionRow {
        id: id.to_string(),
        reference: Some(reference.to_string()),
        title: Some(title.to_string()),
        title_source: source.map(str::to_string),
        ..SessionRow::default()
    };
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
                session("sess-named", "#77", "Assistant", Some("manual")),
                session("sess-bare-ref", "#78", "alpha#78", Some("manual")),
                session("sess-auto", "#79", "alpha#79: Codex", Some("provisional")),
                session("sess-suffix", "#80", "Release #80", Some("manual")),
                session("sess-other", "#81", "other#81: Handoff", Some("manual")),
                session("sess-ref", "#82", "#82: Ship", Some("manual")),
            ],
        )]
        .into_iter()
        .collect(),
        ..SidebarRows::default()
    });
    let ids = [
        "sess-named",
        "sess-bare-ref",
        "sess-auto",
        "sess-suffix",
        "sess-other",
        "sess-ref",
    ];
    let seat = |id: &str| {
        json!({
            "entry_id": format!("session:{id}"),
            "session_id": id,
            "provider": "codex",
            "terminal": {"terminal_id": format!("term-{id}"), "backend": "native"}
        })
    };
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": ids.map(seat)
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    for id in ids {
        ws.open_terminal(&format!("term-{id}"), "native", "epoch")
            .unwrap();
    }

    let chrome = Chrome::dark();
    let rows = agent_rows(&ws, &chrome);
    let line_one = |id: &str| {
        let row = rows
            .iter()
            .find(|row| row.id == format!("session:{id}"))
            .expect("agent row");
        line_text(&row_line(row, 34, &chrome, 0))
    };

    assert_eq!(line_one("sess-named"), " ○ #77: Assistant");
    assert_eq!(
        line_one("sess-bare-ref"),
        " ○ #78: Codex",
        "a manual title that is only the ref names nothing"
    );
    assert_eq!(
        line_one("sess-auto"),
        " ○ #79: Codex",
        "an automatic title keeps the provider"
    );
    assert_eq!(
        line_one("sess-suffix"),
        " ○ #80: Release #80",
        "a ref inside the title is part of the name"
    );
    assert_eq!(
        line_one("sess-other"),
        " ○ #81: other#81: Handoff",
        "another project's prefix is not this session's ref"
    );
    assert_eq!(line_one("sess-ref"), " ○ #82: Ship");
}

/// The style of the one span in `line` whose text is exactly `text`.
fn span_style(line: &Line<'_>, text: &str) -> Style {
    let mut matches = line.spans.iter().filter(|span| span.content == text);
    let span = matches
        .next()
        .unwrap_or_else(|| panic!("no span {text:?} in {:?}", line.spans));
    assert!(
        matches.next().is_none(),
        "two spans {text:?} in {:?}",
        line.spans
    );
    span.style
}

fn assert_role(line: &Line<'_>, text: &str, fg: Color, bold: bool, case: &str) {
    let style = span_style(line, text);
    assert_eq!(style.fg, Some(fg), "{case}: {text:?} colour");
    assert_eq!(
        style.add_modifier.contains(Modifier::BOLD),
        bold,
        "{case}: {text:?} bold"
    );
}

/// Dark, light, and both in monochrome: the board's roles hold in each, with
/// the colours each chrome actually paints.
fn option_b_chromes() -> Vec<(&'static str, Chrome)> {
    let mut chromes = Vec::new();
    for (name, kind) in [("dark", ThemeKind::Dark), ("light", ThemeKind::Light)] {
        let chrome = Chrome::new(Theme::new(kind));
        let mut mono = Chrome::new(Theme::new(kind));
        mono.palette = Palette::monochrome(&mono.theme);
        chromes.push((name, chrome));
        chromes.push((
            if kind == ThemeKind::Dark {
                "dark mono"
            } else {
                "light mono"
            },
            mono,
        ));
    }
    chromes
}

/// #23280 item 4, Josh's approved Option B board: project names are bold
/// accent, refs and branches take the identifier hue, names and the live
/// task are text, the model line takes the theme's model colour (#23416),
/// and everything else secondary is subtext0. No text is drawn in overlay0
/// or overlay1, which fail AA on the sidebar's grounds.
#[test]
fn sidebar_rows_paint_the_option_b_roles() {
    for (case, chrome) in option_b_chromes() {
        let p = chrome.palette;
        let project = SidebarRow {
            id: "proj-gobby".into(),
            label: "gobby".into(),
            branch: Some("0.5.0".into()),
            ahead: 3,
            behind: 1,
            group: Some(true),
            active: true,
            ..SidebarRow::default()
        };
        let line = row_line(&project, 30, &chrome, 0);
        assert_role(&line, "gobby", p.accent, true, case);
        assert_role(&line, " (", p.subtext0, false, case);
        assert_role(&line, "0.5.0", p.identifier, false, case);
        assert_role(&line, " ↑3", p.green, false, case);
        assert_role(&line, " ↓1", p.subtext0, false, case);
        assert_role(&line, ")", p.subtext0, false, case);

        // A selected project name is accent too; the sidebar re-inks it on
        // the selection fill, where accent may fall under AA
        // (`sidebar::tests::agent_quiet_lines_meet_aa_on_every_row_fill`).
        let selected = SidebarRow {
            id: "proj-site".into(),
            label: "gobby-site".into(),
            branch: Some("main".into()),
            selected: true,
            active: false,
            ..project.clone()
        };
        let line = row_line(&selected, 30, &chrome, 0);
        assert_role(&line, "gobby-site", p.accent, true, case);
        assert_role(&line, "main", p.identifier, false, case);

        let worktree = SidebarRow {
            id: "wt-chrome".into(),
            label: "chrome".into(),
            kind: RowKind::Worktree,
            detail: "#23280".into(),
            nested: true,
            ..SidebarRow::default()
        };
        let line = row_line(&worktree, 30, &chrome, 0);
        assert_role(&line, "chrome", p.identifier, false, case);
        assert_role(&line, " · ", p.subtext0, false, case);
        assert_role(&line, "#23280", p.identifier, false, case);

        let agent = SidebarRow {
            id: "session:one".into(),
            kind: RowKind::Agent,
            reference: "#12217".into(),
            definition: "Claude Code".into(),
            task: Some(("#23280".into(), "gclient".into())),
            model_slug: "claude-opus-5.5-high".into(),
            active: true,
            ..SidebarRow::default()
        };
        let line = row_line(&agent, 30, &chrome, 0);
        assert_role(&line, "#12217", p.identifier, false, case);
        assert_role(&line, ": ", p.subtext0, false, case);
        assert_role(&line, "Claude Code", p.text, true, case);
        let second = row_second_line(&agent, 30, &chrome);
        assert_role(&second, "Working task 23280", p.text, false, case);
        assert_role(&second, " gclient", p.subtext0, false, case);
        let third = row_third_line(&agent, 30, &chrome);
        assert_role(&third, "claude-opus-5.5-high", p.model, false, case);

        let idle = SidebarRow {
            definition: "Codex".into(),
            task: None,
            model_slug: "gpt-5-codex-high".into(),
            active: false,
            ..agent
        };
        assert_role(
            &row_line(&idle, 30, &chrome, 0),
            "Codex",
            p.text,
            false,
            case,
        );
        let second = row_second_line(&idle, 30, &chrome);
        assert_role(&second, "No assigned task", p.subtext0, false, case);
        let third = row_third_line(&idle, 30, &chrome);
        assert_role(&third, "gpt-5-codex-high", p.model, false, case);

        let machine = SidebarRow {
            kind: RowKind::Machine,
            label: "mbp".into(),
            detail: "local".into(),
            ..SidebarRow::default()
        };
        let line = row_line(&machine, 30, &chrome, 0);
        assert_role(&line, "mbp", p.text, false, case);
        assert_role(&line, "local", p.subtext0, false, case);

        let terminal = SidebarRow {
            kind: RowKind::Terminal,
            label: "zsh".into(),
            address: "tmux %16".into(),
            detail: "~/Projects/gobby".into(),
            ..SidebarRow::default()
        };
        let line = row_line(&terminal, 30, &chrome, 0);
        assert_role(&line, "zsh", p.text, false, case);
        assert_role(&line, "tmux %16", p.subtext0, false, case);
        let second = row_second_line(&terminal, 30, &chrome);
        assert_role(&second, "~/Projects/gobby", p.subtext0, false, case);

        let group = SidebarRow {
            id: "group:proj-gobby".into(),
            label: "gobby".into(),
            kind: RowKind::Group,
            ..SidebarRow::default()
        };
        let line = row_line(&group, 12, &chrome, 0);
        assert_role(&line, "gobby", p.accent, true, case);
        assert_eq!(
            line.spans[2].style.fg,
            Some(p.overlay0),
            "{case}: group rule"
        );

        // overlay0 and overlay1 are rules and idle glyphs, never text.
        for line in [
            row_line(&project, 30, &chrome, 0),
            row_line(&worktree, 30, &chrome, 0),
            row_line(&machine, 30, &chrome, 0),
            row_line(&terminal, 30, &chrome, 0),
            row_second_line(&idle, 30, &chrome),
            row_third_line(&idle, 30, &chrome),
        ] {
            for span in &line.spans {
                let text = span.content.trim();
                if text.is_empty() || text.chars().all(|ch| "○├└─▾▸".contains(ch)) {
                    continue;
                }
                assert!(
                    span.style.fg != Some(p.overlay0) && span.style.fg != Some(p.overlay1),
                    "{case}: text {:?} in an overlay",
                    span.content
                );
            }
        }
    }
}

/// The task line keeps its two roles while it scrolls: the cells from
/// `Working task N` stay text and the title's cells stay subtext0.
#[test]
fn a_scrolling_task_line_keeps_text_and_subtext_roles() {
    let mut chrome = Chrome::dark();
    let p = chrome.palette;
    let row = SidebarRow {
        id: "session:one".into(),
        definition: "Codex".into(),
        reference: "#77".into(),
        task: Some(("#13936".into(), "修复 workspace chrome".into())),
        kind: RowKind::Agent,
        ..SidebarRow::default()
    };
    chrome.ticker = (TICKER_PAUSE + 2) * TICKER_STEP;
    let line = row_second_line(&row, 24, &chrome);
    assert_eq!(line_text(&line), "   rking task 13936 修复");
    assert_role(&line, "rking task 13936", p.text, false, "scrolled");
    assert_role(&line, " 修复", p.subtext0, false, "scrolled");
}
