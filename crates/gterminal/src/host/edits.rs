//! Ordered edit counts only; composer contents never leave the input path.

use serde::Serialize;

#[derive(Serialize)]
pub(crate) struct Edit {
    op: &'static str,
    count: usize,
}

pub(crate) fn summarize(mut bytes: &[u8]) -> Option<Vec<Edit>> {
    let mut edits: Vec<Edit> = Vec::new();
    while let Some(byte) = bytes.first() {
        let op = if *byte == 0x1b {
            let end = match bytes.get(1) {
                Some(b'O') if bytes.len() >= 3 => 3,
                Some(b'[') => bytes[2..].iter().position(|b| (0x40..=0x7e).contains(b))? + 3,
                _ => return None,
            };
            if !super::submit::escape_enter(&bytes[1..end]) {
                return None;
            }
            bytes = &bytes[end - 1..];
            "submit"
        } else {
            match byte {
                b' '..=b'~' => "insert",
                0x7f => "backspace",
                b'\r' => "submit",
                _ => return None,
            }
        };
        bytes = &bytes[1..];
        if let Some(last) = edits
            .last_mut()
            .filter(|last| last.op == op && op != "submit")
        {
            last.count += 1;
        } else {
            edits.push(Edit { op, count: 1 });
        }
        if edits.len() > 128 {
            return None;
        }
    }
    (!edits.is_empty()).then_some(edits)
}
