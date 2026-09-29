//! herdr `src/ui/status.rs` (4) keep-set render tests.

use std::collections::BTreeSet;

use gobby_client::app::ControlState;
use gobby_client::daemon::{DaemonError, Generation, RunRow, SidebarRows};
use gobby_client::ui::chrome::{Mode, RowState};
use gobby_client::ui::hit::Hit;
use gobby_client::ui::status::{
    control_indicator, copy_feedback_rect, render_status_line, state_dot, toast_cue_width,
    toast_notification_rect, Toast, ToastKind,
};
use gobby_client::ui::status_segments::{agent_counts, AgentCounts};
use gobby_client::ui::text::display_width_u16;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use ratatui::Terminal;
use serde_json::json;

use super::fixtures::{cell, rect_rows, render};
use super::token_map::palette;

// herdr `ToastKind::Finished` / `ToastKind::NeedsAttention` map onto gclient's
// generic kinds: a finished run is a success, a prompt waiting on input is a
// warning. The kind only picks the cue, never the geometry rule under test.
const FINISHED: ToastKind = ToastKind::Success;
const NEEDS_ATTENTION: ToastKind = ToastKind::Warning;

// herdr `CopyFeedback { message }` is a bare `&str` in gclient.
const COPY_FEEDBACK: &str = "copied to clipboard";

fn toast() -> Toast {
    Toast {
        kind: FINISHED,
        title: "done".to_string(),
        body: Some("workspace".to_string()),
        target: None,
    }
}

/// herdr sizes a toast as `max(title, context) + 6`: a 2-cell `● ` kind mark,
/// 2 cells of padding, 2 border cells. gclient's kind cue is the fixed
/// `<glyph> <label>  ` (rule 3) in place of the 2-cell mark, and the body row
/// is indented 2 cells under it, so the same padding and borders wrap the
/// wider of those two rows.
fn expected_toast_width(toast: &Toast) -> u16 {
    let body = toast.body.as_deref().unwrap_or("");
    display_width_u16(&toast.title)
        .saturating_add(toast_cue_width(toast.kind))
        .max(display_width_u16(body).saturating_add(2))
        + 4
}

parity_tests! {
    "src/ui/status.rs" => {
        fn state_dots_use_aligned_static_workspace_marks() {
            let palette = palette();
            // herdr (AgentState, seen) pairs map onto gclient RowState:
            // Blocked -> Attention, Working -> Working, Idle unseen -> Unseen,
            // Idle seen -> Idle, Unknown -> Unknown. herdr's `●` in three
            // colours is gclient's one distinct glyph per state: `⍾` needs
            // you (warning), `▶` working (accent), `◆` unseen (info), `○`
            // idle, `·` unknown, `◌` orphaned (destructive).
            for (state, symbol, color) in [
                (RowState::Attention, "⍾", palette.peach),
                (RowState::Working, "▶", palette.accent),
                (RowState::Unseen, "◆", palette.teal),
                (RowState::Idle, "○", palette.overlay0),
                (RowState::Unknown, "·", palette.overlay0),
                (RowState::Orphaned, "◌", palette.red),
                (RowState::Paused, "‖", palette.overlay1),
            ] {
                let (actual_symbol, actual_color) = state_dot(state, &palette);
                assert_eq!(actual_symbol, symbol);
                assert_eq!(actual_color, color);
            }
        }

        fn toast_rect_uses_configured_corner() {
            let area = Rect::new(10, 20, 100, 40);
            let toast = toast();

            // herdr iterates its four configurable corners; gclient has no
            // toast position setting and pins the top-right corner of the
            // pane area (D3), so that is the one corner asserted here.
            let top_right = toast_notification_rect(area, &toast).expect("toast rect");
            assert_eq!(top_right.x + top_right.width, area.x + area.width);
            assert_eq!(top_right.y, area.y);
        }

        fn toast_rect_uses_display_width_for_cjk_labels() {
            let area = Rect::new(0, 0, 100, 20);
            let toast = Toast {
                kind: NEEDS_ATTENTION,
                title: "重构用户认证模块".to_string(),
                body: Some("提交 herdr 的反馈".to_string()),
                target: None,
            };

            let rect = toast_notification_rect(area, &toast).expect("toast rect");

            let expected_content_width = expected_toast_width(&toast);
            assert_eq!(rect.width, expected_content_width);
            assert_eq!(rect.x + rect.width, area.x + area.width);
        }

        fn copy_feedback_rect_uses_configured_position() {
            let area = Rect::new(10, 20, 100, 40);
            let feedback = COPY_FEEDBACK;

            // herdr iterates its configurable clipboard positions; gclient has
            // no such setting and pins bottom-centre, so that is the one
            // position asserted here.
            let bottom_center = copy_feedback_rect(area, feedback);
            assert_eq!(bottom_center.y + bottom_center.height, area.y + area.height);
            assert_eq!(
                bottom_center.x,
                area.x + area.width.saturating_sub(bottom_center.width) / 2
            );
        }
    }
}

/// 2.5.2 (gclient-only, outside the keep-set): a focused pane whose edge is
/// too narrow for its metadata keeps it at the head of the status line, and
/// only an exception there is a button. Focus is a condition; Read-only and
/// Uncertain take control back on a press, the pointer resting on them
/// underlines their words, and the words, not the hue, tell the states apart.
#[test]
fn control_indicator_is_a_button() {
    let palette = palette();
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": []
    }));
    ws.reconcile_subscribe_first().expect("install roster");
    ws.open_terminal("term-alpha", "native", "epoch")
        .expect("open terminal");
    let pane = ws.pane_for_terminal("term-alpha").expect("term-alpha pane");
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "alpha");
    chrome.compute_view(&ws, Rect::new(0, 0, 18, 10));
    // Only a pane too narrow for any title hands it to the status row.
    chrome
        .view
        .pane_infos
        .iter_mut()
        .find(|info| info.is_focused)
        .expect("focused pane info")
        .rect
        .width = 4;

    let mut indicator = None;
    let focused = render(80, 1, |frame| {
        indicator = render_status_line(frame, frame.area(), &ws, &chrome).control_indicator;
    });
    assert_eq!(indicator, None, "focus is not a button");
    assert_eq!(
        rect_rows(&focused, Rect::new(0, 0, 23, 1)),
        vec![" ○ term-alpha · Focused".to_string()]
    );

    ws.pane_mut(pane).control = ControlState::LeaseLost;
    ws.pane_mut(pane).take_back = true;
    let lost = render(80, 1, |frame| {
        indicator = render_status_line(frame, frame.area(), &ws, &chrome).control_indicator;
    });
    let indicator = indicator.expect("an exception draws the indicator");
    let button = vec![" ○ term-alpha · Read-only".to_string()];
    let words = || indicator.x + 1..indicator.right();
    let underlined = |terminal: &Terminal<TestBackend>| -> Vec<bool> {
        words()
            .map(|x| {
                cell(terminal, x, indicator.y)
                    .modifier
                    .contains(Modifier::UNDERLINED)
            })
            .collect()
    };
    assert_eq!(rect_rows(&lost, indicator), button);
    assert!(
        underlined(&lost).iter().all(|cell| !cell),
        "nothing is underlined until the pointer rests on the button"
    );
    assert_eq!(
        cell(&lost, indicator.x + 1, indicator.y).fg,
        palette.subtext0,
        "a held pane reads in the held tone"
    );

    chrome.hover = Some(Hit::ControlIndicator);
    let hovered = render(80, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    assert!(
        underlined(&hovered).iter().all(|cell| *cell),
        "hover underlines the whole label"
    );
    assert_eq!(
        rect_rows(&hovered, indicator),
        button,
        "hover changes no text"
    );

    ws.pane_mut(pane).take_back = false;
    ws.pane_mut(pane).control = ControlState::UncertainReadOnly;
    let uncertain = render(80, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    assert!(
        rect_rows(&uncertain, Rect::new(0, 0, 25, 1))[0].starts_with(" ○ term-alpha · Uncertain"),
        "Uncertain reads apart from Read-only"
    );

    // The sidebar's per-row control glyphs still read without hue.
    let states = [
        (ControlState::Observe, false),
        (ControlState::Held, false),
        (ControlState::LeaseLost, false),
        (ControlState::UncertainReadOnly, false),
        (ControlState::Held, true),
    ];
    let readings: BTreeSet<String> = states
        .iter()
        .map(|(control, take_back)| {
            let (glyph, label, _) = control_indicator(*control, *take_back, &palette);
            format!("{glyph} {label}")
        })
        .collect();
    assert_eq!(
        readings.len(),
        states.len(),
        "every state reads without hue: {readings:?}"
    );
}

#[test]
fn status_bar_orders_fixed_slots_and_configured_segments() {
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
                "provider": "codex",
                "model": "gpt-6-sol",
                "model_display_name": "GPT-6 Sol",
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
        .expect("alpha pane");
    let beta = ws
        .open_terminal("term-beta", "native", "epoch")
        .expect("beta pane");
    let mut chrome = Chrome::dark();
    chrome.open_tab(alpha, "alpha");
    chrome.open_tab(beta, "beta");
    chrome.activate_tab(0);
    chrome.mode = Mode::Navigate;
    chrome.compute_view(&ws, Rect::new(0, 0, 100, 20));

    let mut count = None;
    let healthy = render(100, 1, |frame| {
        count = render_status_line(frame, frame.area(), &ws, &chrome).count;
    });
    let line = &rect_rows(&healthy, Rect::new(0, 0, 100, 1))[0];
    // The defaults: this machine's agents by class, then the prefix and the
    // mode. The context, token and sandbox segments are opt-in.
    assert!(line.starts_with(" ⍾ 1 needs you │ 1 idle  "), "{line:?}");
    assert!(!line.contains("gpt"), "{line:?}");
    assert!(line.ends_with("  prefix ctrl+b │ navigate "), "{line:?}");
    let count = count.expect("attention count hit area");
    assert_eq!(
        (count.x, count.width),
        (1, 13),
        "the hit covers the glyph and its words"
    );
    chrome.hover = Some(Hit::StatusCount);
    let hovered = render(100, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    for x in count.x..count.right() {
        assert!(
            cell(&hovered, x, count.y)
                .modifier
                .contains(Modifier::UNDERLINED),
            "count cell {x} should show hover"
        );
    }
    chrome.hover = None;

    chrome.activate_tab(1);
    assert_eq!(ws.attention_entry_ids(), ["run:term-beta"]);
    assert!(
        chrome
            .active_tab()
            .expect("beta tab")
            .slot_for(beta)
            .is_some(),
        "attention pane is visible"
    );
    chrome.prefs.status_left = vec!["tokens".to_string()];
    chrome.prefs.status_right = vec!["context".to_string(), "tokens".to_string()];
    chrome.compute_view(&ws, Rect::new(0, 0, 100, 20));
    let mut hits = None;
    let visible = render(100, 1, |frame| {
        hits = Some(render_status_line(frame, frame.area(), &ws, &chrome));
    });
    let line = &rect_rows(&visible, Rect::new(0, 0, 100, 1))[0];
    // The count is machine-wide, so attention on the active tab stays in
    // it. Segments the focused pane has no value for draw nothing.
    assert!(line.starts_with(" ⍾ 1 needs you │ 1 idle  "), "{line:?}");
    assert!(!line.contains('—'), "no placeholder: {line:?}");
    assert!(
        line.ends_with("  prefix ctrl+b │ navigate "),
        "no right segment: {line:?}"
    );
    assert!(hits.expect("status hits").count.is_some());

    chrome.activate_tab(0);
    chrome.prefs.status_left = vec!["focus".to_string(), "tokens".to_string()];
    chrome.prefs.status_right = vec!["context".to_string(), "tokens".to_string()];
    chrome.compute_view(&ws, Rect::new(0, 0, 100, 20));

    ws.observe_daemon_disconnect(
        Generation(1),
        DaemonError::Unavailable { retry_after: None },
    );
    let disconnected = render(100, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    let line = &rect_rows(&disconnected, Rect::new(0, 0, 100, 1))[0];
    assert!(
        line.starts_with(" × Daemon unreachable · retrying │ ⍾ 1 needs you"),
        "{line:?}"
    );
    assert!(
        !line.contains("12,") || line.contains("12,345"),
        "the tokens draw whole or drop whole: {line:?}"
    );
    assert!(line.ends_with("prefix ctrl+b │ navigate "), "{line:?}");

    chrome.prefs.status_left = vec![
        "tokens".to_string(),
        "unknown".to_string(),
        "model".to_string(),
    ];
    chrome.prefs.status_right = vec!["context".to_string()];
    let configured = render(100, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    let line = &rect_rows(&configured, Rect::new(0, 0, 100, 1))[0];
    assert!(
        line.starts_with(" × Daemon unreachable · retrying │ ⍾ 1 needs you"),
        "{line:?}"
    );
    assert!(
        line.ends_with("63% │ prefix ctrl+b │ navigate "),
        "{line:?}"
    );
    // #23049: the model left the status line; a prefs file that still names
    // it draws nothing for it.
    assert!(!line.contains("gpt"), "no model segment: {line:?}");
    assert!(!line.contains("unknown"), "{line:?}");

    // #23049: the sandbox segment names the focused pane's mark in words;
    // a pane with no SRT launch record says unrestricted (option A).
    chrome.prefs.status_right = vec!["sandbox".to_string()];
    let sandbox = render(100, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    let line = &rect_rows(&sandbox, Rect::new(0, 0, 100, 1))[0];
    assert!(
        line.ends_with("unrestricted │ prefix ctrl+b │ navigate "),
        "{line:?}"
    );
}

/// Point 11: the status line counts every agent on this machine by legend
/// class, whatever the sidebar filter or the active tab shows, and the counts
/// sum to that agent total.
#[test]
fn status_counts_every_agent_on_this_machine_by_class() {
    let mut ws = Workspace::scripted();
    ws.set_local_machine("local");
    let mut rows = SidebarRows::default();
    rows.runs.insert(
        "alpha".to_string(),
        vec![RunRow {
            run_id: "run-remote".to_string(),
            machine_id: Some("remote".to_string()),
            ..RunRow::default()
        }],
    );
    ws.daemon_mut().set_sidebar_rows(rows);
    let terminal = |id: &str| json!({"terminal_id": id, "backend": "native"});
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "run:visible",
                "terminal": terminal("visible"),
                "attention": {"kind": "actionable"}
            },
            {
                "entry_id": "run:off-tab",
                "terminal": terminal("off-tab"),
                "attention": {"kind": "actionable"}
            },
            {"entry_id": "run:idle", "terminal": terminal("idle")},
            {"entry_id": "run:held", "terminal": terminal("held"), "lifecycle_status": "paused"},
            {
                "entry_id": "run:waiting",
                "terminal": terminal("waiting"),
                "lifecycle_status": "awaiting_input"
            },
            {
                "entry_id": "run:working",
                "terminal": terminal("working"),
                "lifecycle_status": "running"
            },
            {
                "entry_id": "run:gone",
                "terminal": {"terminal_id": "gone", "backend": "native", "state": "orphaned"}
            },
            {
                "entry_id": "run:remote",
                "run_id": "run-remote",
                "terminal": terminal("remote"),
                "lifecycle_status": "running"
            }
        ]
    }));
    ws.reconcile_subscribe_first().expect("install roster");
    let visible = ws
        .open_terminal("visible", "native", "epoch")
        .expect("visible pane");
    let mut chrome = Chrome::dark();
    chrome.open_pane(visible, "alpha");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 20));

    // Attention on the active tab counts too; held and awaiting agents count
    // as idle; the run on another machine is not counted.
    let counts = agent_counts(&ws);
    assert_eq!(
        counts,
        AgentCounts {
            need_you: 2,
            idle: 3,
            active: 1,
            gone: 1,
        }
    );
    let local = ws
        .sidebar()
        .agents
        .iter()
        .filter(|agent| agent.machine_id == "local")
        .count();
    assert_eq!(local, 7, "the remote run is still a sidebar agent");
    assert_eq!(
        counts.need_you + counts.idle + counts.active + counts.gone,
        local,
        "the counts sum to the agents on this machine"
    );

    let status = render(120, 1, |frame| {
        render_status_line(frame, frame.area(), &ws, &chrome);
    });
    let line = &rect_rows(&status, Rect::new(0, 0, 120, 1))[0];
    assert!(
        line.starts_with(" ⍾ 2 need you │ 3 idle │ 1 active │ ◌ 1 gone  "),
        "{line:?}"
    );
    let gone = line
        .find('◌')
        .map(|byte| line[..byte].chars().count())
        .expect("gone count");
    let gone = u16::try_from(gone).expect("gone column");
    for x in gone..gone + 8 {
        assert_eq!(
            cell(&status, x, 0).fg,
            chrome.palette.red,
            "gone count, x={x}"
        );
    }
    assert_eq!(
        cell(&status, 3, 0).fg,
        chrome.palette.subtext0,
        "count words"
    );
}
