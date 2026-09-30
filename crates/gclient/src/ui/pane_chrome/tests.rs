use super::*;
use crate::app::{Backend, PaneId, Workspace};
use crate::daemon::{ProjectRow, RunRow, RunSandbox, SessionRow, SidebarRows, WorkspaceSnapshot};
use crate::ui::pane_layout;
use crate::ui::settings::AgentSort;
use serde_json::json;

fn info(rect: Rect, borders: Borders, is_focused: bool) -> PaneInfo {
    PaneInfo {
        id: pane_layout::PaneId::from_raw(1),
        rect,
        inner_rect: Rect::default(),
        scrollbar_rect: None,
        borders,
        is_focused,
    }
}

fn corners(title: &str, address: &str) -> PaneCorners {
    PaneCorners {
        title: title.to_owned(),
        address: address.to_owned(),
        backend: None,
        sandbox: SandboxState::Sandboxed,
        sandbox_mark: sandbox_mark(SandboxState::Sandboxed, true),
        tone: MetadataTone::Focused,
        actionable: false,
    }
}

#[test]
fn corner_title_leads_with_glyph_ref_and_definition() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "proj-alpha".to_owned(),
            name: "gobby".to_owned(),
            display_name: "gobby".to_owned(),
            ..ProjectRow::default()
        }],
        sessions: [(
            "proj-alpha".to_owned(),
            vec![SessionRow {
                id: "sess-alpha".to_owned(),
                title: Some("Ship the Unicode 修复".to_owned()),
                reference: Some("gobby#1742".to_owned()),
                ..SessionRow::default()
            }],
        )]
        .into_iter()
        .collect(),
        ..SidebarRows::default()
    });
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "session:sess-alpha",
            "session_id": "sess-alpha",
            "provider": "codex",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "task": {"ref": "#42", "title": "Finish pane chrome"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    let mut snapshot: WorkspaceSnapshot = serde_json::from_str(include_str!(
        "../../../../../tests/fixtures/terminal_ws_golden/workspace_snapshot.json"
    ))
    .unwrap();
    snapshot.workspace.node_ref = Some(0);
    snapshot.workspace.reference = 0;
    snapshot.tabs[0].reference = 1;
    snapshot.panes[1].reference = 2;
    snapshot.panes[1].terminal_id = Some("term-alpha".to_owned());
    ws.apply_workspace_snapshot(snapshot);
    let mut chrome = Chrome::dark();
    let pane_id = ws.pane_for_terminal("term-alpha").unwrap();

    // The session ref, not the task: the task title lives on the Agents row.
    let focused = pane_corners(&ws, &chrome, ws.pane(pane_id), true);
    assert_eq!(focused.title, "○ #1742: Codex · Focused");
    assert_eq!(focused.address, "0:0:1:2");
    let unfocused = pane_corners(&ws, &chrome, ws.pane(pane_id), false);
    assert_eq!(unfocused.title, "○ #1742: Codex");

    // The project leads the ref only where every project's rows mix.
    chrome.sidebar.all_sessions = true;
    chrome.prefs.agent_sort = AgentSort::Priority;
    assert_eq!(
        pane_corners(&ws, &chrome, ws.pane(pane_id), false).title,
        "○ gobby#1742: Codex"
    );
    chrome.sidebar.all_sessions = false;

    // A tmux pane the workspace holds takes its gclient address over the
    // tmux id (#23120), and the tmux id stays on the pane untouched.
    let mut held = Pane::new(PaneId(99), "term-alpha", Backend::Tmux, "epoch");
    held.address = Some("%14".to_owned());
    assert_eq!(
        pane_corners(&ws, &chrome, &held, true).address_label(),
        "tmux · 0:0:1:2"
    );
    assert_eq!(held.address.as_deref(), Some("%14"));
    let mut tmux_pane = Pane::new(PaneId(100), "term-outside", Backend::Tmux, "epoch");
    tmux_pane.address = Some("%15".to_owned());
    // Josh's order (#23049): mark, then tmux for a tmux pane, then the address.
    // This seat has no SRT record, so no mark leads (#23096).
    let tmux = pane_corners(&ws, &chrome, &tmux_pane, true);
    assert_eq!(tmux.address_label(), "tmux · %15");
    tmux_pane.address = None;
    let unaddressed = pane_corners(&ws, &chrome, &tmux_pane, true);
    assert_eq!(unaddressed.address_label(), "tmux");

    // No session ref: the definition alone.
    ws.daemon_mut().set_sidebar_rows(SidebarRows::default());
    ws.reconcile_subscribe_first().unwrap();
    assert_eq!(
        pane_corners(&ws, &chrome, ws.pane(pane_id), false).title,
        "○ Codex"
    );

    let mut fallback = Pane::new(PaneId(99), "term-fallback", Backend::Native, "epoch");
    fallback.label = Some("renamed pane".to_owned());
    assert_eq!(
        pane_corners(&ws, &chrome, &fallback, false).title,
        "○ renamed pane"
    );
    fallback.label = None;
    fallback.command = Some("nvim".to_owned());
    assert_eq!(pane_corners(&ws, &chrome, &fallback, false).title, "○ nvim");
}

#[test]
fn a_seat_without_a_definition_names_its_provider() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 1,
        "entries": [{
            "entry_id": "run:orphan",
            "provider": "claude_code",
            "terminal": {"terminal_id": "term-orphan", "backend": "native"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let pane = ws.open_terminal("term-orphan", "native", "epoch").unwrap();
    assert_eq!(
        pane_corners(&ws, &Chrome::dark(), ws.pane(pane), true).title,
        "○ Claude Code · Focused"
    );
}

/// A seat whose session carries `title` with `title_source`, joined to an
/// open pane on `term-seat`.
fn titled_seat(title: &str, title_source: &str) -> (Workspace, PaneId) {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "proj-alpha".to_owned(),
            name: "gobby".to_owned(),
            display_name: "gobby".to_owned(),
            ..ProjectRow::default()
        }],
        sessions: [(
            "proj-alpha".to_owned(),
            vec![SessionRow {
                id: "sess-seat".to_owned(),
                reference: Some("#14069".to_owned()),
                title: Some(title.to_owned()),
                title_source: Some(title_source.to_owned()),
                ..SessionRow::default()
            }],
        )]
        .into_iter()
        .collect(),
        ..SidebarRows::default()
    });
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 1,
        "entries": [{
            "entry_id": "session:sess-seat",
            "session_id": "sess-seat",
            "provider": "claude",
            "terminal": {"terminal_id": "term-seat", "backend": "native"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    let pane = ws.open_terminal("term-seat", "native", "epoch").unwrap();
    (ws, pane)
}

#[test]
fn a_manual_session_title_names_the_seat_in_place_of_its_provider() {
    let (ws, pane) = titled_seat("gobby#14069: Assistant", "manual");
    assert_eq!(
        pane_corners(&ws, &Chrome::dark(), ws.pane(pane), true).title,
        "○ #14069: Assistant · Focused",
        "the manual title drops the ref prefix the header already leads with"
    );

    let (ws, pane) = titled_seat("gobby#14069: Task #22974 - fix titles", "task");
    assert_eq!(
        pane_corners(&ws, &Chrome::dark(), ws.pane(pane), true).title,
        "○ #14069: Claude · Focused",
        "an automatic title keeps the provider"
    );
}

#[test]
fn bare_shell_title_uses_its_command_instead_of_unknown_agent_identity() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "session:shell",
            "session_id": "shell",
            "terminal": {"terminal_id": "term-shell", "backend": "native"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let mut pane = Pane::new(PaneId(101), "term-shell", Backend::Native, "epoch");
    pane.command = Some("zsh".to_owned());
    assert_eq!(ws.sidebar().agents.len(), 1);
    assert_eq!(
        pane_corners(&ws, &Chrome::dark(), &pane, true).title,
        "○ zsh · Focused"
    );
}

#[test]
fn a_seat_that_needs_you_reads_in_the_attention_tone() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 1,
        "entries": [{
            "entry_id": "run:asking",
            "provider": "codex",
            "terminal": {"terminal_id": "term-asking", "backend": "native"},
            "attention": {"attention_id": "att-1", "kind": "actionable"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let pane = ws.open_terminal("term-asking", "native", "epoch").unwrap();
    let chrome = Chrome::dark();
    let unfocused = pane_corners(&ws, &chrome, ws.pane(pane), false);
    assert_eq!(
        (unfocused.title.as_str(), unfocused.tone),
        ("⍾ Codex", MetadataTone::Attention)
    );
    // Focus outranks the ask in the hue; the glyph still says it.
    let focused = pane_corners(&ws, &chrome, ws.pane(pane), true);
    assert_eq!(
        (focused.title.as_str(), focused.tone),
        ("⍾ Codex · Focused", MetadataTone::Focused)
    );
}

#[test]
fn corners_map_focus_and_exception_states_per_backend() {
    let ws = Workspace::scripted();
    let chrome = Chrome::dark();
    for (backend, name) in [(Backend::Native, "gclient"), (Backend::Tmux, "tmux")] {
        let mut pane = Pane::new(PaneId(1), "term", backend, "epoch");
        pane.command = Some("zsh".to_owned());
        let reads = |pane: &Pane, focused| {
            let corners = pane_corners(&ws, &chrome, pane, focused);
            (
                corners.title,
                corners.address,
                corners.tone,
                corners.actionable,
            )
        };
        let expect =
            |title: &str, tone, actionable| (title.to_owned(), name.to_owned(), tone, actionable);
        assert_eq!(
            reads(&pane, false),
            expect("○ zsh", MetadataTone::Ordinary, false)
        );
        assert_eq!(
            reads(&pane, true),
            expect("○ zsh · Focused", MetadataTone::Focused, false)
        );
        pane.control = ControlState::Held;
        assert_eq!(
            reads(&pane, true),
            expect("○ zsh · Focused", MetadataTone::Focused, false)
        );
        pane.control = ControlState::LeaseLost;
        assert_eq!(
            reads(&pane, true),
            expect("○ zsh · Read-only", MetadataTone::Held, true)
        );
        // The focus word, exception included, is absent on unfocused panes.
        assert_eq!(
            reads(&pane, false),
            expect("○ zsh", MetadataTone::Ordinary, false)
        );
        pane.control = ControlState::UncertainReadOnly;
        assert_eq!(
            reads(&pane, true),
            expect("○ zsh · Uncertain", MetadataTone::Held, true)
        );
        // A refused host write or grant leaves the pane observing with
        // take-back offered: the same Read-only, in the same tone.
        pane.control = ControlState::Observe;
        pane.take_back = true;
        assert_eq!(
            reads(&pane, true),
            expect("○ zsh · Read-only", MetadataTone::Held, true)
        );
    }
}

#[test]
fn the_address_takes_the_bottom_right_corner_alone() {
    let rect = Rect::new(10, 2, 50, 8);
    let text = corners("○ zsh · Focused", "0:0:1:2");
    let bordered = info(rect, Borders::ALL, true);
    let address = address_rect(&bordered, &text).unwrap();
    assert_eq!(address.right(), rect.right() - 1);
    assert_eq!(address.y, rect.bottom() - 1);
    // " <lock> · 0:0:1:2 ": the mark is one cell and leads the address.
    assert_eq!(usize::from(address.width), 1 + 3 + "0:0:1:2".len() + 2);
    assert_eq!(top_reserve(&bordered, &text), 0);
    let title = title_rect(&bordered, &text).unwrap();
    assert_eq!((title.x, title.y), (rect.x + 1, rect.y));
    assert_eq!(usize::from(title.width), display_width(&text.title) + 2);

    // Too narrow for the padded address: nothing is drawn rather than a cut id.
    let narrow = info(Rect::new(0, 0, 10, 8), Borders::ALL, true);
    assert_eq!(address_rect(&narrow, &text), None);
    // No border at all: the status line carries the title instead.
    let bare = info(rect, Borders::NONE, true);
    assert_eq!(address_rect(&bare, &text), None);
    assert_eq!(title_rect(&bare, &text), None);
}

#[test]
fn shared_divider_moves_upper_address_beside_title() {
    let text = corners("○ #42: Codex · Read-only", "0:0:1:2");
    let upper = info(
        Rect::new(10, 2, 100, 8),
        Borders::TOP | Borders::LEFT | Borders::RIGHT,
        true,
    );
    let reserve = top_reserve(&upper, &text);
    // The lock, " · ", then the address, padded, plus one rule cell.
    assert_eq!(reserve, 1 + 3 + display_width(&text.address) + 3);
    let address = address_rect(&upper, &text).unwrap();
    assert_eq!(address.y, upper.rect.y);
    assert_eq!(address.right(), upper.rect.right() - 1);
    let title_end = upper.rect.x + 2 + title_budget(upper.rect.width, reserve) as u16;
    assert!(title_end < address.x);

    let cramped = info(Rect::new(10, 2, 17, 8), upper.borders, true);
    assert_eq!(top_reserve(&cramped, &text), 0);
    assert_eq!(address_rect(&cramped, &text), None);
}

#[test]
fn title_travel_measures_each_header_window() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut()
        .set_roster(json!({"epoch": "e1", "seq": 1, "entries": []}));
    ws.reconcile_subscribe_first().unwrap();
    let chrome = Chrome::dark();
    let mut pane = Pane::new(PaneId(1), "term", Backend::Native, "epoch");
    // "○ " plus 28 cells: a 30-cell title unfocused, 40 with " · Focused".
    pane.label = Some("a-title-of-twenty-eight-cell".to_owned());
    let bordered = info(Rect::new(0, 0, 20, 6), Borders::ALL, false);
    assert_eq!(title_travel(&ws, &chrome, &pane, &bordered), 30 - 16);
    let focused = info(Rect::new(0, 0, 20, 6), Borders::ALL, true);
    assert_eq!(title_travel(&ws, &chrome, &pane, &focused), 40 - 16);
    // No top edge, or a window under the readable minimum, never scrolls.
    let borderless = info(Rect::new(0, 0, 20, 6), Borders::NONE, true);
    assert_eq!(title_travel(&ws, &chrome, &pane, &borderless), 0);
    let tiny = info(Rect::new(0, 0, 7, 6), Borders::ALL, true);
    assert_eq!(title_travel(&ws, &chrome, &pane, &tiny), 0);
}

/// #23096: only an SRT pane draws a mark. The Nerd lock is one cell, the
/// text fallback widens the address corner rather than truncating it, and an
/// unrestricted pane draws no padlock of either shape.
#[test]
fn only_a_sandboxed_pane_leads_its_address_with_a_mark() {
    assert_eq!(
        sandbox_mark(SandboxState::Sandboxed, true),
        Some("\u{f023}")
    );
    assert_eq!(sandbox_mark(SandboxState::Sandboxed, false), Some("sbx"));
    assert_eq!(display_width("\u{f023}"), 1);
    for nerd in [true, false] {
        assert_eq!(
            sandbox_mark(SandboxState::Unrestricted, nerd),
            None,
            "an unrestricted pane draws no mark (nerd={nerd})"
        );
    }

    let mut text = corners("○ zsh", "0:0:1:2");
    assert_eq!(text.address_label(), "\u{f023} · 0:0:1:2");
    text.sandbox_mark = sandbox_mark(SandboxState::Sandboxed, false);
    text.backend = Some("tmux");
    assert_eq!(text.address_label(), "sbx · tmux · 0:0:1:2");
    let rect = Rect::new(10, 2, 50, 8);
    let address = address_rect(&info(rect, Borders::ALL, true), &text).unwrap();
    assert_eq!(
        usize::from(address.width),
        display_width("sbx · tmux · 0:0:1:2") + 2
    );
    assert_eq!(address.right(), rect.right() - 1);

    text.sandbox = SandboxState::Unrestricted;
    text.sandbox_mark = sandbox_mark(SandboxState::Unrestricted, true);
    assert_eq!(text.address_label(), "tmux · 0:0:1:2");
    text.backend = None;
    assert_eq!(text.address_label(), "0:0:1:2");
}

/// #23096: Josh's live roster held one SRT run among interactive seats; only
/// the run's pane is locked, and every seat's address stands alone.
#[test]
fn a_mixed_roster_locks_only_the_srt_pane() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: vec![ProjectRow {
            id: "proj-alpha".to_owned(),
            name: "gobby".to_owned(),
            display_name: "gobby".to_owned(),
            ..ProjectRow::default()
        }],
        sessions: [(
            "proj-alpha".to_owned(),
            vec![
                SessionRow {
                    id: "sess-run".to_owned(),
                    sandbox_enabled: Some(true),
                    ..SessionRow::default()
                },
                SessionRow {
                    id: "sess-seat".to_owned(),
                    sandbox_enabled: None,
                    ..SessionRow::default()
                },
                SessionRow {
                    id: "sess-off".to_owned(),
                    sandbox_enabled: Some(false),
                    ..SessionRow::default()
                },
            ],
        )]
        .into_iter()
        .collect(),
        runs: [(
            "proj-alpha".to_owned(),
            vec![RunRow {
                run_id: "run-srt".to_owned(),
                sandbox: Some(RunSandbox {
                    enforced: Some(true),
                }),
                ..RunRow::default()
            }],
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
                "entry_id": "run:run-srt",
                "run_id": "run-srt",
                "session_id": "sess-run",
                "provider": "codex",
                "terminal": {"terminal_id": "term-srt", "backend": "native"}
            },
            {
                "entry_id": "session:sess-seat",
                "session_id": "sess-seat",
                "provider": "claude",
                "terminal": {"terminal_id": "term-seat", "backend": "native"}
            },
            {
                "entry_id": "session:sess-off",
                "session_id": "sess-off",
                "provider": "claude",
                "terminal": {"terminal_id": "term-off", "backend": "native"}
            }
        ]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    for terminal in ["term-srt", "term-seat", "term-off"] {
        ws.open_terminal(terminal, "native", "epoch").unwrap();
    }
    let chrome = Chrome::dark();
    let corners_for = |terminal: &str| {
        let pane_id = ws.pane_for_terminal(terminal).unwrap();
        pane_corners(&ws, &chrome, ws.pane(pane_id), false)
    };

    let srt = corners_for("term-srt");
    assert_eq!(srt.sandbox, SandboxState::Sandboxed);
    assert_eq!(srt.address_label(), format!("\u{f023} · {}", srt.address));
    for terminal in ["term-seat", "term-off"] {
        let seat = corners_for(terminal);
        assert_eq!(seat.sandbox, SandboxState::Unrestricted, "{terminal}");
        assert_eq!(
            seat.address_label(),
            seat.address,
            "{terminal} draws no mark"
        );
    }
    let bare = Pane::new(PaneId(99), "term-bare", Backend::Native, "epoch");
    let bare = pane_corners(&ws, &chrome, &bare, false);
    assert_eq!(
        bare.address_label(),
        bare.address,
        "a bare shell draws no mark"
    );
}
