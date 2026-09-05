//! Crossterm capture forwarded to the daemon as `terminal_input`.

use crossterm::event::{Event, KeyEvent};
use gobby_terminal::input::{encode_key, KeyboardProtocol};

pub fn key_to_bytes(event: KeyEvent) -> Option<Vec<u8>> {
    key_to_bytes_with_protocol(event, KeyboardProtocol::Legacy)
}

pub fn key_to_bytes_with_protocol(
    event: KeyEvent,
    protocol: KeyboardProtocol,
) -> Option<Vec<u8>> {
    let bytes = encode_key(event, protocol);
    (!bytes.is_empty()).then_some(bytes)
}

pub fn event_to_bytes(event: Event) -> Option<Vec<u8>> {
    match event {
        Event::Key(key) => key_to_bytes(key),
        Event::Paste(text) => Some(text.into_bytes()),
        _ => None,
    }
}
