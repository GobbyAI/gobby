#[cfg(test)]
pub(in crate::index::parser) fn line_terminator_len(
    text: &str,
    line_start_byte: usize,
    line_len: usize,
) -> usize {
    let terminator_start = line_start_byte + line_len;
    let Some(rest) = text.as_bytes().get(terminator_start..) else {
        return 0;
    };
    if rest.starts_with(b"\r\n") {
        2
    } else if rest.starts_with(b"\n") {
        1
    } else {
        0
    }
}

pub(super) fn utf16_column_at_byte(source: &[u8], byte_offset: usize) -> usize {
    let byte_offset = byte_offset.min(source.len());
    let line_start = source[..byte_offset]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|idx| idx + 1)
        .unwrap_or(0);
    lossy_utf16_units(&source[line_start..byte_offset])
}

fn lossy_utf16_units(mut bytes: &[u8]) -> usize {
    let mut units = 0;
    loop {
        match std::str::from_utf8(bytes) {
            Ok(valid) => return units + valid.chars().map(char::len_utf16).sum::<usize>(),
            Err(error) => {
                let valid_up_to = error.valid_up_to();
                let valid = std::str::from_utf8(&bytes[..valid_up_to]).unwrap_or("");
                units += valid.chars().map(char::len_utf16).sum::<usize>();
                units += char::REPLACEMENT_CHARACTER.len_utf16();
                let Some(error_len) = error.error_len() else {
                    return units;
                };
                bytes = &bytes[valid_up_to + error_len..];
            }
        }
    }
}

pub(super) fn trim_identifier_token(token: &str) -> &str {
    token.trim_matches(|ch: char| !is_identifier_continue(ch))
}

pub(super) fn is_identifier_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || matches!(ch, '_' | '$')
}

pub(super) fn is_identifier_continue(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '$')
}

pub(super) fn is_textual_call_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$' | b'!' | b'?')
}

pub(super) fn should_ignore_call_name(language: &str, name: &str) -> bool {
    match language {
        "dart" => matches!(
            name,
            "if" | "for" | "while" | "switch" | "catch" | "assert" | "return" | "throw"
        ),
        "elixir" => matches!(
            name,
            "def" | "defp" | "defmacro" | "defmodule" | "alias" | "import" | "use" | "require"
        ),
        "kotlin" => matches!(
            name,
            "if" | "for" | "while" | "when" | "catch" | "return" | "throw"
        ),
        _ => false,
    }
}
