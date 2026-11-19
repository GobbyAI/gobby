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
