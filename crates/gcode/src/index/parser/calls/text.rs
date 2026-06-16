use unicode_xid::UnicodeXID;

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
    UnicodeXID::is_xid_start(ch) || matches!(ch, '_' | '$')
}

pub(super) fn is_identifier_continue(ch: char) -> bool {
    UnicodeXID::is_xid_continue(ch) || matches!(ch, '_' | '$')
}

pub(super) fn is_textual_call_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$' | b'!' | b'?')
}

pub(super) fn should_ignore_call_name(language: &str, name: &str) -> bool {
    // Keep these lists aligned with language-maintained keyword/special-form
    // references: Dart keywords (https://dart.dev/language/keywords), Elixir
    // v1.19.5 Kernel.SpecialForms (https://hexdocs.pm/elixir/Kernel.SpecialForms.html),
    // and Kotlin keywords (https://kotlinlang.org/docs/keyword-reference.html).
    match language {
        "dart" => matches!(
            name,
            "assert"
                | "async"
                | "await"
                | "break"
                | "case"
                | "catch"
                | "class"
                | "continue"
                | "default"
                | "do"
                | "else"
                | "extension"
                | "finally"
                | "for"
                | "if"
                | "mixin"
                | "new"
                | "return"
                | "super"
                | "switch"
                | "this"
                | "throw"
                | "while"
                | "yield"
        ),
        "elixir" => matches!(
            name,
            "alias"
                | "case"
                | "cond"
                | "def"
                | "defguard"
                | "defguardp"
                | "defimpl"
                | "defmacro"
                | "defmodule"
                | "defp"
                | "defprotocol"
                | "fn"
                | "for"
                | "if"
                | "import"
                | "receive"
                | "require"
                | "try"
                | "unless"
                | "use"
                | "with"
        ),
        "kotlin" => matches!(
            name,
            "as" | "break"
                | "catch"
                | "class"
                | "continue"
                | "do"
                | "else"
                | "finally"
                | "for"
                | "fun"
                | "if"
                | "in"
                | "is"
                | "object"
                | "return"
                | "super"
                | "this"
                | "throw"
                | "try"
                | "val"
                | "var"
                | "when"
                | "while"
        ),
        "bash" => matches!(name, "source" | "."),
        "lua" => name == "require",
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ignores_language_keywords_that_look_like_calls() {
        assert!(should_ignore_call_name("dart", "await"));
        assert!(should_ignore_call_name("kotlin", "object"));
        assert!(should_ignore_call_name("elixir", "with"));
        assert!(!should_ignore_call_name("dart", "fetchUser"));
    }

    #[test]
    fn identifier_helpers_accept_unicode_xid_characters() {
        assert!(is_identifier_start('λ'));
        assert!(is_identifier_continue('é'));
        assert!(is_identifier_continue('\u{0301}'));
        assert!(is_identifier_start('_'));
        assert!(is_identifier_continue('$'));
    }
}
