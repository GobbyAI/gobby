//! Bridges `gobby_terminal::raw_input` events to the chrome keymap and to
//! the bytes a focused pane receives.
//!
//! The run loop parses host stdin with `parse_raw_input_bytes`, turns each
//! `Key` into a [`KeyInput`] (the crossterm view for keymap matching plus the
//! pane encoding under its negotiated protocol), and asks [`resolve_chord`]
//! whether the chrome consumes the key. Text commits and bracketed pastes
//! bypass the keymap through [`text_bytes`].

use crossterm::event::KeyEvent;
use gobby_terminal::input::{encode_terminal_key, KeyboardProtocol};
use gobby_terminal::raw_input::RawInputEvent;

use crate::ui::keymap::{Action, Keymap};

/// One key press as the chrome sees it and as the focused pane receives it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyInput {
    /// Crossterm view used for keymap matching.
    pub key: KeyEvent,
    /// Bytes the pane receives under its keyboard protocol.
    pub bytes: Vec<u8>,
}

/// Key event plus pane bytes for `RawInputEvent::Key`; `None` for every
/// other event.
pub fn key_input(event: &RawInputEvent, protocol: KeyboardProtocol) -> Option<KeyInput> {
    match event {
        RawInputEvent::Key(key) => Some(KeyInput {
            key: key.as_key_event(),
            bytes: encode_terminal_key(key.clone(), protocol),
        }),
        _ => None,
    }
}

/// What the chrome keymap makes of one key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolution {
    /// The prefix chord: arm prefix mode and swallow the key.
    Prefix,
    /// A bound action.
    Action(Action),
    /// Nothing bound: the focused pane receives the key.
    Unbound,
}

/// Resolve `key` against `keymap`. Outside prefix mode the prefix chord arms
/// and direct chords match; inside it only `prefix+` chords match.
pub fn resolve_chord(keymap: &Keymap, key: &KeyEvent, prefix_armed: bool) -> Resolution {
    let action = if prefix_armed {
        keymap.lookup_prefix(key)
    } else if keymap.is_prefix(key) {
        return Resolution::Prefix;
    } else {
        keymap.lookup_direct(key)
    };
    action.map_or(Resolution::Unbound, Resolution::Action)
}

/// UTF-8 bytes of a text commit or bracketed paste; `None` for other events.
pub fn text_bytes(event: &RawInputEvent) -> Option<Vec<u8>> {
    match event {
        RawInputEvent::Text(commit) => Some(commit.as_str().as_bytes().to_vec()),
        RawInputEvent::Paste(text) => Some(text.as_bytes().to_vec()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{KeyCode, KeyModifiers, MouseEvent, MouseEventKind};
    use gobby_terminal::input::TerminalKey;

    fn key(code: KeyCode, modifiers: KeyModifiers) -> RawInputEvent {
        RawInputEvent::Key(TerminalKey::new(code, modifiers))
    }

    #[test]
    fn armed_prefix_mode_ignores_direct_and_prefix_chords_alike() {
        let keymap = Keymap::defaults();
        let up = key_input(
            &key(KeyCode::Up, KeyModifiers::NONE),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, &up.key, false),
            Resolution::Action(Action::NavigateUp)
        );
        assert_eq!(resolve_chord(&keymap, &up.key, true), Resolution::Unbound);
        let prefix = key_input(
            &key(KeyCode::Char('b'), KeyModifiers::CONTROL),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, &prefix.key, true),
            Resolution::Unbound
        );
        assert_eq!(prefix.bytes, vec![0x02]);
    }

    #[test]
    fn non_key_events_yield_neither_key_nor_text() {
        let mouse = RawInputEvent::Mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: 1,
            row: 1,
            modifiers: KeyModifiers::NONE,
        });
        assert!(key_input(&mouse, KeyboardProtocol::Legacy).is_none());
        assert!(text_bytes(&mouse).is_none());
        assert!(text_bytes(&key(KeyCode::Char('x'), KeyModifiers::NONE)).is_none());
    }
}
