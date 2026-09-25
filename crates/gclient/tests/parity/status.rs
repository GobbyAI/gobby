//! herdr `src/ui/status.rs` (4) keep-set render tests.

use std::collections::BTreeSet;

use gobby_client::app::ControlState;
use gobby_client::ui::chrome::RowState;
use gobby_client::ui::hit::Hit;
use gobby_client::ui::status::{
    control_indicator, copy_feedback_rect, render_status_line, state_dot, toast_cue_width,
    toast_notification_rect, Toast, ToastKind,
};
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
                (RowState::Paused, "‖", palette.yellow),
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

/// 2.5.2 (gclient-only, outside the keep-set): a focused pane with no border
/// keeps its metadata at the head of the status line, and only an exception
/// there is a button. Focus is a condition; Read-only and Uncertain take
/// control back on a press, the pointer resting on them underlines their
/// words, and the words, not the hue, tell the states apart.
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

    let mut indicator = None;
    let focused = render(80, 1, |frame| {
        indicator = render_status_line(frame, frame.area(), &ws, &chrome);
    });
    assert_eq!(indicator, None, "focus is not a button");
    assert_eq!(
        rect_rows(&focused, Rect::new(0, 0, 18, 1)),
        vec![" gclient · Focused".to_string()]
    );

    ws.pane_mut(pane).control = ControlState::LeaseLost;
    ws.pane_mut(pane).take_back = true;
    let lost = render(80, 1, |frame| {
        indicator = render_status_line(frame, frame.area(), &ws, &chrome);
    });
    let indicator = indicator.expect("an exception draws the indicator");
    let button = vec![" gclient · Read-only".to_string()];
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
        palette.yellow,
        "the colour is the warning token"
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
        rect_rows(&uncertain, Rect::new(0, 0, 20, 1))[0].starts_with(" gclient · Uncertain"),
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
