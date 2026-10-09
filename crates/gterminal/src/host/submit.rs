//! Whether operator input submits a composer, for `input_activity`.

use crossterm::event::{KeyCode, KeyEventKind};

use crate::input::parse_terminal_key_sequence;

/// Whether `bytes` press Enter: a CR, or an escape sequence that parses as
/// Enter without modifiers and is not a release. A child that asks for every
/// key as an escape code (kitty flag 8) receives Enter as `ESC [ 13 ; 1 u`,
/// never as a CR. Shift-, Ctrl- and Alt-Enter insert a newline in the agent
/// CLIs, so they do not count.
pub(super) fn presses_enter(bytes: &[u8]) -> bool {
    bytes.contains(&b'\r') || bytes.split(|byte| *byte == 0x1b).skip(1).any(escape_enter)
}

/// One escape sequence after its ESC: a CSI through its final byte, or an SS3 key.
fn escape_enter(after_esc: &[u8]) -> bool {
    let len = match after_esc {
        [b'[', params @ ..] => match params.iter().position(|byte| (0x40..=0x7e).contains(byte)) {
            Some(end) => end + 2,
            None => return false,
        },
        [b'O', _, ..] => 2,
        _ => return false,
    };
    std::str::from_utf8(&after_esc[..len])
        .ok()
        .and_then(|sequence| parse_terminal_key_sequence(&format!("\x1b{sequence}")))
        .is_some_and(|key| {
            key.code == KeyCode::Enter
                && key.modifiers.is_empty()
                && key.kind != KeyEventKind::Release
        })
}

#[cfg(test)]
mod tests {
    use super::presses_enter;

    #[test]
    fn a_carriage_return_presses_enter_wherever_it_lands() {
        for bytes in [&b"\r"[..], b"abc\r", b"\x1b\r", b"one\rtwo"] {
            assert!(presses_enter(bytes), "{bytes:?}");
        }
    }

    #[test]
    fn keys_without_enter_do_not_press_it() {
        for bytes in [
            &b""[..],
            b"x",
            b"\n",
            b"\x1b",
            b"\x03",
            b"\x1b[A",
            b"\x1bOA",
            b"\x1b[13",
        ] {
            assert!(!presses_enter(bytes), "{bytes:?}");
        }
    }

    #[test]
    fn an_unmodified_enter_escape_presses_enter() {
        for bytes in [
            &b"\x1b[13u"[..],
            b"\x1b[13;1u",
            b"\x1b[13;1:1u",
            b"\x1b[13;1:2u",
            b"\x1b[13;1;13u",
            b"\x1b[57414u",
            b"\x1bOM",
            b"abc\x1b[13;1u",
            b"\x1b[A\x1b[13u",
        ] {
            assert!(presses_enter(bytes), "{bytes:?}");
        }
    }

    #[test]
    fn a_modified_or_released_enter_escape_does_not_press_enter() {
        for bytes in [
            &b"\x1b[13;2u"[..],
            b"\x1b[13;3u",
            b"\x1b[13;5u",
            b"\x1b[13;1:3u",
            b"\x1b[113;1u",
        ] {
            assert!(!presses_enter(bytes), "{bytes:?}");
        }
    }
}
