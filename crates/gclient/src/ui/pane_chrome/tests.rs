use super::*;
use crate::app::{Backend, PaneId, Workspace};
use crate::daemon::{ProjectRow, SessionRow, SidebarRows, WorkspaceSnapshot};
use crate::ui::pane_layout;
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

fn footer(left: &str, right: &str) -> PaneFooter {
    PaneFooter {
        left: left.to_owned(),
        right: right.to_owned(),
        tone: MetadataTone::Focused,
        actionable: false,
    }
}

#[test]
fn pane_title_prefers_task_then_label_then_provisional() {
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
    let session_pane = ws.pane(ws.pane_for_terminal("term-alpha").unwrap());
    assert_eq!(
        pane_title(&ws, session_pane),
        "Task #42 - Finish pane chrome"
    );
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
    let session_pane = ws.pane(ws.pane_for_terminal("term-alpha").unwrap());
    let footer = pane_footer(&ws, session_pane, true);
    assert_eq!(footer.left, "Codex (gobby#42) · Focused");
    assert_eq!(footer.right, "gclient 0:0:1:2");
    let mut tmux_pane = Pane::new(PaneId(100), "term-alpha", Backend::Tmux, "epoch");
    tmux_pane.address = Some("%15".to_owned());
    assert_eq!(pane_footer(&ws, &tmux_pane, true).right, "tmux %15");

    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 2,
        "entries": [{
            "entry_id": "session:sess-alpha",
            "session_id": "sess-alpha",
            "provider": "codex",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"},
            "task": {"ref": "#42"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let pane_id = ws.pane_for_terminal("term-alpha").unwrap();
    assert_eq!(pane_title(&ws, ws.pane(pane_id)), "Task #42");

    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 3,
        "entries": [{
            "entry_id": "session:sess-alpha",
            "session_id": "sess-alpha",
            "provider": "codex",
            "terminal": {"terminal_id": "term-alpha", "backend": "native"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let pane_id = ws.pane_for_terminal("term-alpha").unwrap();
    ws.pane_mut(pane_id).label = Some("manual title".to_owned());
    assert_eq!(pane_title(&ws, ws.pane(pane_id)), "manual title");
    ws.pane_mut(pane_id).label = None;
    assert_eq!(pane_title(&ws, ws.pane(pane_id)), "Ship the Unicode 修复");

    ws.daemon_mut().set_sidebar_rows(SidebarRows::default());
    ws.reconcile_subscribe_first().unwrap();
    assert_eq!(pane_title(&ws, ws.pane(pane_id)), "Codex");

    let mut fallback = Pane::new(PaneId(99), "term-fallback", Backend::Native, "epoch");
    fallback.label = Some("renamed pane".to_owned());
    assert_eq!(pane_title(&ws, &fallback), "renamed pane");
    fallback.label = None;
    fallback.command = Some("nvim".to_owned());
    assert_eq!(pane_title(&ws, &fallback), "");
}

#[test]
fn agent_footer_omits_empty_project_reference() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1", "seq": 1,
        "entries": [{
            "entry_id": "run:orphan",
            "provider": "codex",
            "terminal": {"terminal_id": "term-orphan", "backend": "native"}
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    let pane = ws.open_terminal("term-orphan", "native", "epoch").unwrap();
    assert_eq!(
        pane_footer(&ws, ws.pane(pane), true).left,
        "Codex · Focused"
    );
}

#[test]
fn pane_metadata_maps_focus_and_exception_states_per_backend() {
    let ws = Workspace::scripted();
    for (backend, name) in [(Backend::Native, "gclient"), (Backend::Tmux, "tmux")] {
        let mut pane = Pane::new(PaneId(1), "term", backend, "epoch");
        pane.command = Some("zsh".to_owned());
        let reads = |pane: &Pane, focused| {
            let footer = pane_footer(&ws, pane, focused);
            (footer.left, footer.right, footer.tone, footer.actionable)
        };
        assert_eq!(
            reads(&pane, false),
            (
                "zsh".to_owned(),
                name.to_owned(),
                MetadataTone::Ordinary,
                false
            )
        );
        assert_eq!(
            reads(&pane, true),
            (
                "zsh · Focused".to_owned(),
                name.to_owned(),
                MetadataTone::Focused,
                false
            )
        );
        pane.control = ControlState::Held;
        assert_eq!(
            reads(&pane, true),
            (
                "zsh · Focused".to_owned(),
                name.to_owned(),
                MetadataTone::Focused,
                false
            )
        );
        pane.control = ControlState::LeaseLost;
        assert_eq!(
            reads(&pane, true),
            (
                "zsh · Read-only".to_owned(),
                name.to_owned(),
                MetadataTone::Warning,
                true
            )
        );
        // An unfocused pane still names its exception.
        assert_eq!(
            reads(&pane, false),
            (
                "zsh · Read-only".to_owned(),
                name.to_owned(),
                MetadataTone::Warning,
                true
            )
        );
        pane.control = ControlState::UncertainReadOnly;
        assert_eq!(
            reads(&pane, true),
            (
                "zsh · Uncertain".to_owned(),
                name.to_owned(),
                MetadataTone::Warning,
                true
            )
        );
        // A refused host write or grant leaves the pane observing with
        // take-back offered: the same Read-only, in the same tone.
        pane.control = ControlState::Observe;
        pane.take_back = true;
        assert_eq!(
            reads(&pane, true),
            (
                "zsh · Read-only".to_owned(),
                name.to_owned(),
                MetadataTone::Warning,
                true
            )
        );
    }
}

#[test]
fn footer_occupies_both_bottom_corners() {
    let rect = Rect::new(10, 2, 50, 8);
    let text = footer("zsh · Focused", "gclient 0:0:1:2");
    let (left, right) = footer_rects(&info(rect, Borders::ALL, true), &text).unwrap();
    assert_eq!(left.x, rect.x + 1);
    assert_eq!(right.right(), rect.right() - 1);
    assert_eq!(left.y, rect.bottom() - 1);
    assert!(left.right() < right.x);
    assert_eq!(top_reserve(&info(rect, Borders::ALL, true), &text), 0);

    // Too narrow for the padded text: nothing is drawn rather than a cut word.
    let narrow = info(Rect::new(0, 0, 12, 8), Borders::ALL, true);
    assert_eq!(footer_rects(&narrow, &text), None);
    // No border at all: the status line carries it instead.
    assert_eq!(footer_rects(&info(rect, Borders::NONE, true), &text), None);
}

#[test]
fn shared_divider_moves_upper_footer_beside_title() {
    let footer = footer("Codex (proj#42) · Read-only", "gclient 0:0:1:2");
    let upper = info(
        Rect::new(10, 2, 100, 8),
        Borders::TOP | Borders::LEFT | Borders::RIGHT,
        true,
    );
    let reserve = top_reserve(&upper, &footer);
    assert_eq!(
        reserve,
        display_width(&footer.left) + display_width(&footer.right) + 5
    );
    let (left, right) = footer_rects(&upper, &footer).unwrap();
    assert_eq!(left.y, upper.rect.y);
    assert_eq!(right.y, upper.rect.y);
    assert_eq!(right.right(), upper.rect.right() - 1);
    assert_eq!(left.right() + 1, right.x);
    let title_end = upper.rect.x + 2 + title_budget(upper.rect.width, true, reserve) as u16;
    assert!(title_end < left.x);

    let cramped = info(Rect::new(10, 2, 50, 8), upper.borders, true);
    assert_eq!(top_reserve(&cramped, &footer), 0);
    assert_eq!(footer_rects(&cramped, &footer), None);
}

#[test]
fn title_travel_measures_each_header_window() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut()
        .set_roster(json!({"epoch": "e1", "seq": 1, "entries": []}));
    ws.reconcile_subscribe_first().unwrap();
    let mut pane = Pane::new(PaneId(1), "term", Backend::Native, "epoch");
    pane.label = Some("a-title-of-thirty-cells-wide!!".to_owned());
    let bordered = info(Rect::new(0, 0, 20, 6), Borders::ALL, false);
    assert_eq!(title_travel(&ws, &pane, &bordered), 30 - 16);
    let focused = info(Rect::new(0, 0, 20, 6), Borders::ALL, true);
    assert_eq!(title_travel(&ws, &pane, &focused), 30 - 14);
    // No top edge, or a window under the readable minimum, never scrolls.
    let borderless = info(Rect::new(0, 0, 20, 6), Borders::NONE, true);
    assert_eq!(title_travel(&ws, &pane, &borderless), 0);
    let tiny = info(Rect::new(0, 0, 9, 6), Borders::ALL, true);
    assert_eq!(title_travel(&ws, &pane, &tiny), 0);
}
