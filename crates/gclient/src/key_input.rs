//! Bridges `gobby_terminal::raw_input` events to the chrome keymap and to
//! the bytes a focused pane receives.
//!
//! The run loop parses host stdin with `parse_raw_input_bytes`, turns each
//! `Key` into a [`KeyInput`] (the crossterm view for keymap matching plus the
//! pane encoding under its negotiated protocol), and asks [`resolve_chord`]
//! whether the chrome consumes the key. Text commits and bracketed pastes
//! bypass the keymap through [`text_bytes`].

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use gobby_terminal::input::{encode_terminal_key, KeyboardProtocol};
use gobby_terminal::raw_input::RawInputEvent;

use crate::ui::chrome::Mode;
use crate::ui::keymap::{key_event_matches_combo, Action, KeyCombo, Keymap};

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
        RawInputEvent::Key(key) if key.kind != KeyEventKind::Release => Some(KeyInput {
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
    /// Nothing bound: the focused pane receives the key. Inside prefix mode
    /// the prefix chord itself resolves here (herdr's `ctrl+b ctrl+b`), so a
    /// held pane can still be sent the literal prefix.
    Unbound,
}

/// The keyboard exit from a held pane: `ctrl+\` releases control in every
/// mode, armed or not, before any forwarding. It needs no prefix, so an outer
/// tmux that eats the prefix can never lock the keyboard into a pane.
pub const RELEASE_ESCAPE: KeyCombo = (KeyCode::Char('\\'), KeyModifiers::CONTROL);

/// Resolve `key` against `keymap`. Outside prefix mode the prefix chord arms
/// and direct chords match; inside it only `prefix+` chords match.
/// [`RELEASE_ESCAPE`] resolves first, whatever the mode.
///
/// `Mode::Terminal` is the exception, and it is the whole point of taking a
/// mode here: a focused terminal owns the keyboard, so only the prefix is
/// intercepted and every other key reaches the pane. Direct chords are bare
/// keys — `h`, `j`, `k`, `l`, `up`, `down` are all bound to `navigate_*`
/// actions — so resolving them while the user is typing swallowed those
/// characters before the shell ever saw them. They belong to the navigation
/// modes, which is where they still resolve.
pub fn resolve_chord(
    keymap: &Keymap,
    mode: Mode,
    key: &KeyEvent,
    prefix_armed: bool,
) -> Resolution {
    if key_event_matches_combo(key, RELEASE_ESCAPE) {
        return Resolution::Action(Action::ReleaseControl);
    }
    let action = if prefix_armed {
        keymap.lookup_prefix(key)
    } else if keymap.is_prefix(key) {
        return Resolution::Prefix;
    } else if mode == Mode::Terminal {
        None
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
    use crate::ui::keymap::HERDR_PREFIX;
    use crossterm::event::{KeyCode, KeyModifiers, MouseEvent, MouseEventKind};
    use gobby_terminal::input::TerminalKey;

    fn key(code: KeyCode, modifiers: KeyModifiers) -> RawInputEvent {
        RawInputEvent::Key(TerminalKey::new(code, modifiers))
    }

    #[test]
    fn ctrl_enter_encodes_csi_u_under_kitty_protocol() {
        let kitty = KeyboardProtocol::Kitty { flags: 1 };
        for (modifiers, expected) in [
            (KeyModifiers::NONE, b"\r".as_slice()),
            (KeyModifiers::SHIFT, b"\x1b[13;2u".as_slice()),
            (KeyModifiers::CONTROL, b"\x1b[13;5u".as_slice()),
        ] {
            let input = key_input(&key(KeyCode::Enter, modifiers), kitty).expect("key input");
            assert_eq!(input.bytes, expected);
        }

        for modifiers in [
            KeyModifiers::NONE,
            KeyModifiers::SHIFT,
            KeyModifiers::CONTROL,
        ] {
            let input = key_input(&key(KeyCode::Enter, modifiers), KeyboardProtocol::Legacy)
                .expect("key input");
            assert_eq!(input.bytes, b"\r");
        }
    }

    #[test]
    fn armed_prefix_mode_ignores_direct_and_prefix_chords_alike() {
        let keymap = Keymap::defaults(HERDR_PREFIX);
        let up = key_input(
            &key(KeyCode::Up, KeyModifiers::NONE),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, Mode::Navigate, &up.key, false),
            Resolution::Action(Action::NavigateUp)
        );
        assert_eq!(
            resolve_chord(&keymap, Mode::Navigate, &up.key, true),
            Resolution::Unbound
        );
        let prefix = key_input(
            &key(KeyCode::Char('b'), KeyModifiers::CONTROL),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, Mode::Navigate, &prefix.key, true),
            Resolution::Unbound
        );
        assert_eq!(prefix.bytes, vec![0x02]);
    }

    #[test]
    fn terminal_mode_leaves_every_direct_chord_to_the_pane() {
        let keymap = Keymap::defaults(HERDR_PREFIX);
        // Every bare key the default keymap binds. In a focused terminal each
        // one is an ordinary character or cursor key the shell must receive.
        for (code, action) in [
            (KeyCode::Char('h'), Action::NavigatePaneLeft),
            (KeyCode::Char('j'), Action::NavigatePaneDown),
            (KeyCode::Char('k'), Action::NavigatePaneUp),
            (KeyCode::Char('l'), Action::NavigatePaneRight),
            (KeyCode::Up, Action::NavigateUp),
            (KeyCode::Down, Action::NavigateDown),
        ] {
            let input =
                key_input(&key(code, KeyModifiers::NONE), KeyboardProtocol::Legacy).expect("key");
            assert_eq!(
                resolve_chord(&keymap, Mode::Terminal, &input.key, false),
                Resolution::Unbound,
                "{code:?} must reach the pane in terminal mode"
            );
            assert_eq!(
                resolve_chord(&keymap, Mode::Navigate, &input.key, false),
                Resolution::Action(action),
                "{code:?} must still navigate in navigate mode"
            );
        }
    }

    #[test]
    fn terminal_mode_still_arms_the_prefix() {
        let keymap = Keymap::defaults(HERDR_PREFIX);
        let prefix = key_input(
            &key(KeyCode::Char('b'), KeyModifiers::CONTROL),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, Mode::Terminal, &prefix.key, false),
            Resolution::Prefix,
            "the prefix is the one key a focused terminal does not own"
        );
        let help = key_input(
            &key(KeyCode::Char('?'), KeyModifiers::NONE),
            KeyboardProtocol::Legacy,
        )
        .expect("key");
        assert_eq!(
            resolve_chord(&keymap, Mode::Terminal, &help.key, true),
            Resolution::Action(Action::Help),
            "prefix chords still resolve once armed"
        );
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
