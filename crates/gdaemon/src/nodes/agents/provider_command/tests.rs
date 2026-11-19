use super::*;

#[test]
fn quoted_escapes_non_ascii_and_del_like_json_dumps() {
    // json.dumps("é/\x7f")
    assert_eq!(quoted("é/\u{7f}"), r#""é/\u007f""#);
    // json.dumps("plain") keeps ASCII as it is.
    assert_eq!(quoted("plain"), r#""plain""#);
}
