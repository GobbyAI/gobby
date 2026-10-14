//! Composer effects of accepted native input, without exposing its payload.

use std::sync::LazyLock;

use regex::bytes::Regex;
use serde::Serialize;

use super::submit::presses_enter;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum InputEffect {
    Control,
    Cursor,
    Edit,
}

static CONTROLS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?-u:\x1b\[M[\s\S]{3})|\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[[IO]|(?P<cursor>\x1b\[[\d;]*[ABCD]|\x1bO[ABCD])")
        .expect("constant input control pattern")
});

pub(super) fn classify_input(bytes: &[u8]) -> (InputEffect, bool) {
    let residue = CONTROLS.replace_all(bytes, &b""[..]);
    let effect = if !residue.is_empty() {
        InputEffect::Edit
    } else if CONTROLS
        .captures_iter(bytes)
        .any(|capture| capture.name("cursor").is_some())
    {
        InputEffect::Cursor
    } else {
        InputEffect::Control
    };
    // A bracketed paste is text, even when its body contains Enter.
    let paste = bytes.windows(6).any(|window| window == b"\x1b[200~");
    (effect, !paste && presses_enter(&residue))
}

#[cfg(test)]
#[path = "activity/tests.rs"]
mod tests;
