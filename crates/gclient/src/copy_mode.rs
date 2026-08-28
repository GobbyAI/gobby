//! Attachment-local copy-mode and lease-gated paste helpers.

pub const PASTE_MAX_BYTES: usize = 1024 * 1024;

/// Soft-wrap marker preserved in tmux `capture-pane -J` history payloads.
pub const SOFT_WRAP: char = '\u{23CE}';

/// Join visual wraps so a wide grapheme stays on its logical line.
pub fn extract_logical_line(text: &str, _wrap_cols: usize) -> String {
    text.split('\n')
        .next()
        .unwrap_or("")
        .replace(SOFT_WRAP, "")
        .to_string()
}

/// herdr 0.8.0 `paste_payload`: one bracketed unit when the child asked for it.
pub fn paste_payload(text: &str, bracketed: bool) -> String {
    if bracketed {
        format!("\u{1b}[200~{text}\u{1b}[201~")
    } else {
        text.to_string()
    }
}
