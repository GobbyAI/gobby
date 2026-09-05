//! herdr `src/ui/status.rs` (4) keep-set render tests.

use gobby_client::ui::chrome::RowState;
use gobby_client::ui::status::{
    copy_feedback_rect, state_dot, toast_cue_width, toast_notification_rect, Toast, ToastKind,
};
use gobby_client::ui::text::display_width_u16;
use ratatui::layout::Rect;

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
            // Idle seen -> Idle, Unknown -> Unknown.
            for (state, symbol, color) in [
                (RowState::Attention, "●", palette.red),
                (RowState::Working, "●", palette.yellow),
                (RowState::Unseen, "●", palette.teal),
                (RowState::Idle, "○", palette.green),
                (RowState::Unknown, "·", palette.overlay0),
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
            // toast position setting and pins the bottom-right corner, so
            // that is the one corner asserted here.
            let bottom_right = toast_notification_rect(area, &toast).expect("toast rect");
            assert_eq!(bottom_right.x + bottom_right.width, area.x + area.width);
            assert_eq!(bottom_right.y + bottom_right.height, area.y + area.height);
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
