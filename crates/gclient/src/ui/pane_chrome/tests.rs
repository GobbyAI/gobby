use super::*;
use crate::app::{Backend, PaneId, Workspace};
use crate::daemon::{SessionRow, SidebarRows};
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

fn meta(text: &str) -> PaneMetadata {
    PaneMetadata {
        text: text.to_owned(),
        tone: MetadataTone::Focused,
        actionable: false,
    }
}

#[test]
fn pane_title_prefers_session_then_label_then_terminal_name() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
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
            "terminal": {"terminal_id": "term-alpha", "backend": "native"}
        }]
    }));
    ws.select_project("proj-alpha");
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    let session_pane = ws.pane(ws.pane_for_terminal("term-alpha").unwrap());
    assert_eq!(pane_title(&ws, session_pane), "Ship the Unicode 修复");

    let mut fallback = Pane::new(PaneId(99), "term-fallback", Backend::Native, "epoch");
    fallback.label = Some("renamed pane".to_owned());
    assert_eq!(pane_title(&ws, &fallback), "renamed pane");
    fallback.label = None;
    fallback.command = Some("nvim".to_owned());
    assert_eq!(pane_title(&ws, &fallback), "nvim");
}

#[test]
fn pane_metadata_maps_focus_and_exception_states_per_backend() {
    for (backend, name) in [(Backend::Native, "gclient"), (Backend::Tmux, "tmux")] {
        let mut pane = Pane::new(PaneId(1), "term", backend, "epoch");
        let reads = |pane: &Pane, focused| {
            let meta = pane_metadata(pane, focused);
            (meta.text, meta.tone, meta.actionable)
        };
        assert_eq!(
            reads(&pane, false),
            (name.to_owned(), MetadataTone::Ordinary, false)
        );
        assert_eq!(
            reads(&pane, true),
            (format!("{name} · Focused"), MetadataTone::Focused, false)
        );
        pane.control = ControlState::Held;
        assert_eq!(
            reads(&pane, true),
            (format!("{name} · Focused"), MetadataTone::Focused, false)
        );
        pane.control = ControlState::LeaseLost;
        assert_eq!(
            reads(&pane, true),
            (format!("{name} · Read-only"), MetadataTone::Warning, true)
        );
        // An unfocused pane still names its exception.
        assert_eq!(
            reads(&pane, false),
            (format!("{name} · Read-only"), MetadataTone::Warning, true)
        );
        pane.control = ControlState::UncertainReadOnly;
        assert_eq!(
            reads(&pane, true),
            (format!("{name} · Uncertain"), MetadataTone::Warning, true)
        );
        // A refused host write or grant leaves the pane observing with
        // take-back offered: the same Read-only, in the same tone.
        pane.control = ControlState::Observe;
        pane.take_back = true;
        assert_eq!(
            reads(&pane, true),
            (format!("{name} · Read-only"), MetadataTone::Warning, true)
        );
    }
}

#[test]
fn metadata_sits_bottom_right_one_cell_short_of_the_corner() {
    let rect = Rect::new(10, 2, 30, 8);
    let text = meta("gclient · Focused");
    let at = metadata_rect(&info(rect, Borders::ALL, true), &text).unwrap();
    assert_eq!(at, Rect::new(20, 9, 19, 1));
    assert_eq!(at.right(), rect.right() - 1);
    assert_eq!(top_reserve(&info(rect, Borders::ALL, true), &text), 0);

    // Too narrow for the padded text: nothing is drawn rather than a cut word.
    let narrow = info(Rect::new(0, 0, 12, 8), Borders::ALL, true);
    assert_eq!(metadata_rect(&narrow, &text), None);
    // No border at all: the status line carries it instead.
    assert_eq!(metadata_rect(&info(rect, Borders::NONE, true), &text), None);
}

#[test]
fn pane_without_its_own_bottom_edge_keeps_metadata_top_right() {
    let text = meta("tmux · Focused");
    let upper = info(
        Rect::new(0, 0, 40, 10),
        Borders::TOP | Borders::LEFT | Borders::RIGHT,
        true,
    );
    let reserve = top_reserve(&upper, &text);
    assert_eq!(reserve, display_width("tmux · Focused") + 3);
    assert_eq!(metadata_rect(&upper, &text), Some(Rect::new(23, 0, 16, 1)));
    // The title keeps a readable window left of it, a rule cell apart.
    let budget = title_budget(40, true, reserve);
    assert_eq!(budget, 40 - 4 - 2 - reserve);
    assert!(1 + budget + 4 < 23, "title runs into the metadata");

    // Where the title would drop under a readable window, it wins.
    let cramped = info(Rect::new(0, 0, 24, 10), upper.borders, true);
    assert_eq!(top_reserve(&cramped, &text), 0);
    assert_eq!(metadata_rect(&cramped, &text), None);
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
