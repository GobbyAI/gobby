use super::*;
use serde_json::json;

#[test]
fn context_carries_only_native_fields() {
    let environment = BTreeMap::from([
        ("TTY".into(), "/dev/test".into()),
        ("TERM_PROGRAM".into(), "terminal".into()),
        ("TERM_SESSION_ID".into(), "native".into()),
        ("GOBBY_TERMINAL_ID".into(), "terminal-id".into()),
        ("GOBBY_PANE_REF".into(), "pane-ref".into()),
    ]);
    let context = capture_with(&environment, Some(99));
    let expected = json!({
        "parent_pid": 99, "tty": "/dev/test", "term_program": "terminal",
        "term_session_id": "native", "gobby_terminal_id": "terminal-id",
        "gobby_pane_ref": "pane-ref"
    });
    assert_eq!(serde_json::to_value(&context).unwrap(), expected);
    assert_eq!(
        WRAPPER_CONTRACT
            .terminal_context_keys
            .iter()
            .collect::<std::collections::BTreeSet<_>>(),
        context.keys().collect()
    );
    let absent = capture_with(&BTreeMap::new(), None);
    assert!(absent.is_empty());
    let mut filtered = context;
    filtered.insert("unknown".into(), json!("drop"));
    filtered.insert("tty".into(), Value::Null);
    let serialized: Value = serde_json::from_str(&serialize(&filtered)).unwrap();
    let mut expected = expected.as_object().unwrap().clone();
    expected.remove("tty");
    assert_eq!(serialized, Value::Object(expected));
}

#[cfg(unix)]
#[test]
fn non_utf8_environment_entries_do_not_panic() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    temp_env::with_vars(
        [
            ("GOBBY_TERMINAL_ID", Some(OsStr::from_bytes(b"\xff\xfe"))),
            ("TTY", Some(OsStr::new("/dev/ok"))),
        ],
        || {
            assert_eq!(capture().get("tty"), Some(&json!("/dev/ok")));
            assert!(!environment().contains_key("GOBBY_TERMINAL_ID"));
        },
    );
}

#[test]
fn serialize_escapes_non_ascii_like_python_json_dumps() {
    let mut context = Map::new();
    context.insert("tty".into(), json!("/dev/ttyé"));
    context.insert("term_program".into(), json!("🚀"));
    let serialized = serialize(&context);
    assert!(serialized.is_ascii(), "{serialized}");
    // Code point 92 is the backslash that begins each escape.
    let escape = char::from(92_u8);
    assert!(
        serialized.contains(&format!("{escape}u00e9")),
        "{serialized}"
    );
    assert!(
        serialized.contains(&format!("{escape}ud83d{escape}ude80")),
        "{serialized}"
    );
    let parsed: Value = serde_json::from_str(&serialized).unwrap();
    assert_eq!(parsed, json!({"tty": "/dev/ttyé", "term_program": "🚀"}));
}

#[test]
fn serialize_escapes_del_like_json_dumps() {
    let mut context = Map::new();
    context.insert("tty".into(), json!("/dev/tty\u{7f}x"));

    // json.dumps({"tty": "/dev/tty\x7fx"}, separators=(",", ":"))
    assert_eq!(serialize(&context), r#"{"tty":"/dev/tty\u007fx"}"#);
}
