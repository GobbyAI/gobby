//! Crossterm capture forwarded to the daemon as `terminal_input`.

use crossterm::event::{Event, KeyCode, KeyEvent};

pub fn key_to_bytes(event: KeyEvent) -> Option<String> {
    match event.code {
        KeyCode::Char(ch) => Some(ch.to_string()),
        KeyCode::Enter => Some("\r".into()),
        KeyCode::Tab => Some("\t".into()),
        KeyCode::Backspace => Some("\u{7f}".into()),
        KeyCode::Esc => Some("\u{1b}".into()),
        _ => None,
    }
}

pub fn event_to_bytes(event: Event) -> Option<String> {
    match event {
        Event::Key(key) => key_to_bytes(key),
        Event::Paste(text) => Some(text),
        _ => None,
    }
}
