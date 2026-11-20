use super::*;

#[test]
fn quoted_escapes_non_ascii_and_del_like_json_dumps() {
    // json.dumps("é/\x7f"). Code point 92 is the backslash that begins each escape.
    let escape = char::from(92_u8);
    assert_eq!(
        quoted("é/\u{7f}"),
        format!("\"{escape}u00e9/{escape}u007f\"")
    );
    // json.dumps("plain") keeps ASCII as it is.
    assert_eq!(quoted("plain"), r#""plain""#);
}

#[test]
fn quoted_writes_an_astral_character_as_one_toml_scalar_escape() {
    // TOML 1.0 refuses the surrogate pair json.dumps writes for U+1F600, since a
    // surrogate is not a Unicode scalar value; its eight-digit escape is valid TOML.
    let escape = char::from(92_u8);
    assert_eq!(
        quoted("a\u{1f600}é"),
        format!("\"a{escape}U0001f600{escape}u00e9\"")
    );
}
