//! Mouse reports for panes whose app tracks the mouse: the bytes the app
//! asked for (herdr `encode_mouse_button`, `encode_mouse_motion`,
//! `encode_mouse_wheel`) and the routing that hands them to the pane under
//! the pointer or to the pane that captured the button.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_terminal::layout;
use gobby_terminal::protocol::{MouseTracking, PaneModes};

use crate::app::PaneId;
use crate::ui::{Chrome, WorkspaceView};

use super::select::inner_rect;
use super::{on_roster, MouseGesture, MouseOutcome};

/// The report an app that enabled `modes` gets for `kind` at pane cell
/// (`col`, `row`); `None` when its tracking level never asked for that event,
/// or the column does not fit the X10 encoding.
///
/// SGR (`mouse_sgr`) is `ESC [ < Cb ; Cx ; Cy M`, `m` for a release. X10 is
/// `ESC [ M` then `Cb + 32`, `Cx + 32`, `Cy + 32` as single bytes: a cell past
/// column 223 has no report, while `Cb` and `Cy` clamp to 255. `Cb` is the
/// button (0 left, 1 middle, 2
/// right; 3 for an X10 release or motion with no button) plus 4 shift, 8 alt,
/// 16 ctrl, 32 motion, and 64 to 67 for wheel up, down, left, right. `Cx` and
/// `Cy` are one-based.
pub fn encode_report(
    modes: &PaneModes,
    kind: MouseEventKind,
    modifiers: KeyModifiers,
    col: u16,
    row: u16,
) -> Option<Vec<u8>> {
    if !wanted(modes.mouse_tracking(), kind) {
        return None;
    }
    let (button, release) = match kind {
        MouseEventKind::Down(button) => (button_code(button), false),
        MouseEventKind::Up(button) => (button_code(button), true),
        MouseEventKind::Drag(button) => (button_code(button) + 32, false),
        MouseEventKind::Moved => (3 + 32, false),
        MouseEventKind::ScrollUp => (64, false),
        MouseEventKind::ScrollDown => (65, false),
        MouseEventKind::ScrollLeft => (66, false),
        MouseEventKind::ScrollRight => (67, false),
    };
    let mut cb: u32 = if release && !modes.mouse_sgr {
        3
    } else {
        button
    };
    if modifiers.contains(KeyModifiers::SHIFT) {
        cb += 4;
    }
    if modifiers.contains(KeyModifiers::ALT) {
        cb += 8;
    }
    if modifiers.contains(KeyModifiers::CONTROL) {
        cb += 16;
    }
    let (cx, cy) = (u32::from(col) + 1, u32::from(row) + 1);
    if modes.mouse_sgr {
        let terminator = if release { 'm' } else { 'M' };
        return Some(format!("\x1b[<{cb};{cx};{cy}{terminator}").into_bytes());
    }
    let clamped = |value: u32| u8::try_from(value + 32).unwrap_or(u8::MAX);
    let cx = u8::try_from(cx + 32).ok()?;
    Some(vec![0x1b, b'[', b'M', clamped(cb), cx, clamped(cy)])
}

/// Whether `tracking` reports `kind`: X10 asks for presses only, normal adds
/// releases and the wheel, button-event adds drags, any-event adds motion.
fn wanted(tracking: MouseTracking, kind: MouseEventKind) -> bool {
    match kind {
        MouseEventKind::Down(_) => tracking != MouseTracking::Off,
        MouseEventKind::Up(_)
        | MouseEventKind::ScrollUp
        | MouseEventKind::ScrollDown
        | MouseEventKind::ScrollLeft
        | MouseEventKind::ScrollRight => matches!(
            tracking,
            MouseTracking::Normal | MouseTracking::ButtonMotion | MouseTracking::AnyMotion
        ),
        MouseEventKind::Drag(_) => {
            matches!(
                tracking,
                MouseTracking::ButtonMotion | MouseTracking::AnyMotion
            )
        }
        MouseEventKind::Moved => tracking == MouseTracking::AnyMotion,
    }
}

fn button_code(button: MouseButton) -> u32 {
    match button {
        MouseButton::Left => 0,
        MouseButton::Middle => 1,
        MouseButton::Right => 2,
    }
}

/// The modes of `pane`'s latest frame while the app in it tracks the mouse.
fn reporting_modes<W: WorkspaceView>(ws: &W, pane: PaneId) -> Option<&PaneModes> {
    ws.pane(pane)
        .latest_frame()
        .map(|frame| &frame.modes)
        .filter(|modes| modes.mouse_tracking() != MouseTracking::Off)
}

/// The report `mouse` earns at pane cell `cell` of `pane`, with the
/// passthrough modifier `strip` hidden from the app; `None` when the pane does
/// not track the mouse or its level ignores this event.
pub(super) fn report<W: WorkspaceView>(
    ws: &W,
    pane: PaneId,
    mouse: &MouseEvent,
    strip: KeyModifiers,
    (col, row): (u16, u16),
) -> Option<MouseOutcome> {
    let modes = reporting_modes(ws, pane)?;
    let bytes = encode_report(
        modes,
        mouse.kind,
        mouse.modifiers.difference(strip),
        col,
        row,
    )?;
    Some(MouseOutcome::Write { pane, bytes })
}

/// A press at pane cell `cell` of a pane that tracks the mouse: `slot`
/// captures the button so the drags and the release go to this pane wherever
/// the pointer travels, and the press is forwarded, after focusing the pane
/// when it is not the focused one (herdr
/// `captured_left_press_focuses_target_before_forwarding`). `None` when the
/// pane does not track the mouse, so the caller gives the press its plain
/// meaning.
pub(super) fn press<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    pane: PaneId,
    slot: layout::PaneId,
    mouse: &MouseEvent,
    strip: KeyModifiers,
    cell: (u16, u16),
) -> Option<MouseOutcome> {
    let outcome = report(ws, pane, mouse, strip, cell)?;
    chrome.gesture = Some(MouseGesture::Forwarding { slot, strip });
    Some(match outcome {
        MouseOutcome::Write { pane, bytes } if chrome.focused_pane() != Some(pane) => {
            MouseOutcome::FocusWrite { pane, bytes }
        }
        outcome => outcome,
    })
}

/// A drag or release while `slot` holds a captured button: the report goes
/// to that pane relative to its content, wherever the pointer is now, and an
/// event its level does not report is consumed rather than handed to the
/// chrome under the pointer.
pub(super) fn captured<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    slot: layout::PaneId,
    strip: KeyModifiers,
    mouse: &MouseEvent,
) -> MouseOutcome {
    let Some(pane) = chrome
        .pane_for_slot(slot)
        .filter(|pane| on_roster(ws, *pane))
    else {
        return MouseOutcome::Handled;
    };
    let Some(inner) = inner_rect(chrome, slot) else {
        return MouseOutcome::Handled;
    };
    let cell = (
        mouse.column.saturating_sub(inner.x),
        mouse.row.saturating_sub(inner.y),
    );
    report(ws, pane, mouse, strip, cell).unwrap_or(MouseOutcome::Handled)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn modes_at(tracking: MouseTracking, sgr: bool) -> PaneModes {
        PaneModes {
            mouse_any: tracking == MouseTracking::X10,
            mouse_standard: tracking == MouseTracking::Normal,
            mouse_button: tracking == MouseTracking::ButtonMotion,
            mouse_all: tracking == MouseTracking::AnyMotion,
            mouse_sgr: sgr,
            ..PaneModes::default()
        }
    }

    #[test]
    fn report_encoding_follows_the_tracking_level() {
        use MouseEventKind::{
            Down, Drag, Moved, ScrollDown, ScrollLeft, ScrollRight, ScrollUp, Up,
        };
        let none = KeyModifiers::NONE;
        let sgr = modes_at(MouseTracking::AnyMotion, true);
        let cases: [(MouseEventKind, KeyModifiers, &[u8]); 11] = [
            (Down(MouseButton::Left), none, b"\x1b[<0;2;3M"),
            (Up(MouseButton::Left), none, b"\x1b[<0;2;3m"),
            (
                Down(MouseButton::Middle),
                KeyModifiers::SHIFT,
                b"\x1b[<5;2;3M",
            ),
            (
                Down(MouseButton::Right),
                KeyModifiers::ALT | KeyModifiers::CONTROL,
                b"\x1b[<26;2;3M",
            ),
            (Drag(MouseButton::Left), none, b"\x1b[<32;2;3M"),
            (
                Drag(MouseButton::Right),
                KeyModifiers::SHIFT,
                b"\x1b[<38;2;3M",
            ),
            (Moved, none, b"\x1b[<35;2;3M"),
            (ScrollUp, none, b"\x1b[<64;2;3M"),
            (ScrollDown, KeyModifiers::CONTROL, b"\x1b[<81;2;3M"),
            (ScrollLeft, none, b"\x1b[<66;2;3M"),
            (ScrollRight, none, b"\x1b[<67;2;3M"),
        ];
        for (kind, modifiers, expected) in cases {
            assert_eq!(
                encode_report(&sgr, kind, modifiers, 1, 2).as_deref(),
                Some(expected),
                "{kind:?} {modifiers:?}"
            );
        }

        // X10: Cb + 32, one-based Cx/Cy + 32, a release is button 3, a cell
        // past column 223 does not fit, and a row past 223 clamps to 255.
        let x10 = modes_at(MouseTracking::AnyMotion, false);
        assert_eq!(
            encode_report(&x10, Down(MouseButton::Left), none, 0, 300).as_deref(),
            Some(&[0x1b, b'[', b'M', 32, 33, 255][..])
        );
        assert_eq!(
            encode_report(&x10, Down(MouseButton::Right), KeyModifiers::CONTROL, 0, 0).as_deref(),
            Some(&[0x1b, b'[', b'M', 50, 33, 33][..])
        );
        assert_eq!(
            encode_report(&x10, Up(MouseButton::Left), none, 4, 9).as_deref(),
            Some(&[0x1b, b'[', b'M', 35, 37, 42][..])
        );
        assert_eq!(
            encode_report(&x10, Down(MouseButton::Left), none, 222, 0).as_deref(),
            Some(&[0x1b, b'[', b'M', 32, 255, 33][..])
        );
        assert_eq!(
            encode_report(&x10, Down(MouseButton::Left), none, 223, 0),
            None
        );

        // Each level reports only what it asked for: press, release, drag,
        // motion, wheel.
        let kinds = [
            Down(MouseButton::Left),
            Up(MouseButton::Left),
            Drag(MouseButton::Left),
            Moved,
            ScrollUp,
        ];
        let levels = [
            (MouseTracking::Off, [false, false, false, false, false]),
            (MouseTracking::X10, [true, false, false, false, false]),
            (MouseTracking::Normal, [true, true, false, false, true]),
            (MouseTracking::ButtonMotion, [true, true, true, false, true]),
            (MouseTracking::AnyMotion, [true, true, true, true, true]),
        ];
        for (tracking, wanted) in levels {
            let modes = modes_at(tracking, true);
            for (kind, wanted) in kinds.into_iter().zip(wanted) {
                assert_eq!(
                    encode_report(&modes, kind, none, 0, 0).is_some(),
                    wanted,
                    "{tracking:?} {kind:?}"
                );
            }
        }
    }
}
