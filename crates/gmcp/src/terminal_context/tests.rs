use super::*;
use serde_json::json;

#[test]
fn capture_matches_python_wrapper_rules() {
    let environment = BTreeMap::from([
        ("TMUX_PANE".into(), "%12".into()),
        ("TMUX".into(), "/tmp/tmux,100,0".into()),
        ("TMUX_SESSION".into(), "fallback".into()),
        ("TTY".into(), "/dev/test".into()),
        ("TERM_PROGRAM".into(), "terminal".into()),
        ("TERM_SESSION_ID".into(), "native".into()),
        ("GOBBY_TERMINAL_ID".into(), "terminal-id".into()),
        ("GOBBY_PANE_REF".into(), "pane-ref".into()),
    ]);
    let mut calls = Vec::new();
    let context = capture_with(&environment, Some(99), |socket, pane, format| {
        calls.push((socket.to_owned(), pane.to_owned(), format.to_owned()));
        Some("123\t456\t@7\tactual\n".into())
    });
    assert_eq!(
        context,
        json!({
            "parent_pid":99,"tmux_pane":"%12","tmux_socket_path":"/tmp/tmux",
            "tmux_window_id":"@7","tmux_session":"actual","tmux_server_pid":123,
            "tmux_server_start_time":456,"tty":"/dev/test","term_program":"terminal",
            "term_session_id":"native","gobby_terminal_id":"terminal-id","gobby_pane_ref":"pane-ref"
        })
        .as_object()
        .unwrap()
        .clone()
    );
    assert_eq!(
        calls,
        [("/tmp/tmux".into(), "%12".into(), GENERATION_FORMAT.into())]
    );

    let mut calls = Vec::new();
    let fallback = capture_with(&environment, None, |_, _, format| {
        calls.push(format.to_owned());
        if format == GENERATION_FORMAT {
            Some("bad generation".into())
        } else {
            Some("@8\twindow-session\n".into())
        }
    });
    assert_eq!(calls, [GENERATION_FORMAT, IDENTITY_FORMAT]);
    assert_eq!(fallback["tmux_session"], "window-session");
    assert_eq!(fallback["tmux_window_id"], "@8");
    assert!(!fallback.contains_key("tmux_server_pid"));
    assert!(!fallback.contains_key("parent_pid"));

    let absent = capture_with(&environment, None, |_, _, _| None);
    assert_eq!(absent["tmux_session"], "fallback");
    assert!(!absent.contains_key("tmux_window_id"));
    let mut filtered = context;
    filtered.insert("unknown".into(), json!("drop"));
    filtered.insert("tty".into(), Value::Null);
    let serialized: Value = serde_json::from_str(&serialize(&filtered)).unwrap();
    assert_eq!(serialized.as_object().unwrap().len(), 11);
    assert!(
        WRAPPER_CONTRACT
            .terminal_context_keys
            .iter()
            .filter(|key| key.as_str() != "tty")
            .all(|key| serialized.get(key).is_some())
    );
    assert!(serialized.get("unknown").is_none());
}

#[test]
fn invalid_tmux_identity_is_not_trusted() {
    for output in [
        "x\t123\t@1\ts",
        "1\tx\t@1\ts",
        "1\t2\twindow\ts",
        "1\t2\t@1\t",
        "1\t2\t@1\ts\textra",
    ] {
        let environment = BTreeMap::from([
            ("TMUX_PANE".into(), "%2".into()),
            ("TMUX".into(), "/tmp/socket,1,0".into()),
        ]);
        let context = capture_with(&environment, None, |_, _, _| Some(output.into()));
        assert!(!context.contains_key("tmux_window_id"), "{output:?}");
        assert!(!context.contains_key("tmux_server_pid"));
    }
    let environment = BTreeMap::from([
        ("TMUX_PANE".into(), "invalid".into()),
        ("TMUX".into(), "/tmp/socket".into()),
    ]);
    let context = capture_with(&environment, None, |_, _, _| {
        panic!("invalid pane must not run tmux")
    });
    assert_eq!(context["tmux_pane"], "invalid");
}
