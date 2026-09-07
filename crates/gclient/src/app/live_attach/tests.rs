//! The direct locator carried on a `terminal_attach_result` reply.
//!
//! The reply ships the nested `direct_block()`: a native terminal is named
//! by `host_terminal_id`; a tmux pane is named by its physical locator.

use super::*;
use serde_json::json;

/// `direct_block()` for a tmux pane: the pane object carries the identity and
/// `host_terminal_id` rides along as null.
fn tmux_reply() -> Value {
    json!({
        "backend": "tmux",
        "direct": {
            "host_epoch": "host-epoch",
            "frame_socket_path": "/tmp/gobby-terminal.sock",
            "host_terminal_id": null,
            "pane": {
                "socket_path": "/tmp/tmux-501/default",
                "pane_id": "%9",
                "server_pid": 4242,
                "server_start_time": 1717171717,
            },
        },
    })
}

#[test]
fn a_tmux_attach_reply_parses_without_a_host_terminal_id() {
    let locator = direct_reply_locator(&tmux_reply()).expect("tmux reply must parse");
    assert_eq!(locator.backend, "tmux");
    assert_eq!(locator.frame_host_epoch, "host-epoch");
    assert_eq!(locator.frame_socket_path, "/tmp/gobby-terminal.sock");
    // The frame host discards it when a pane is present; nothing downstream
    // keys off it, so an absent id stays absent rather than being invented.
    assert_eq!(locator.host_terminal_id, "");
    let pane = locator.pane.expect("tmux reply carries a pane locator");
    assert_eq!(pane.pane_id, "%9");
    assert_eq!(pane.socket_path, "/tmp/tmux-501/default");
    assert_eq!(pane.server_pid, 4242);
    assert_eq!(pane.server_start_time, 1_717_171_717);
}

#[test]
fn a_native_attach_reply_still_requires_its_host_terminal_id() {
    let reply = json!({
        "backend": "native",
        "direct": {
            "host_epoch": "host-epoch",
            "frame_socket_path": "/tmp/gobby-terminal.sock",
            "host_terminal_id": "ht-1",
            "pane": null,
        },
    });
    let locator = direct_reply_locator(&reply).expect("native reply must parse");
    assert_eq!(locator.host_terminal_id, "ht-1");
    assert!(locator.pane.is_none());

    let mut bare = reply;
    bare["direct"]["host_terminal_id"] = Value::Null;
    let error = direct_reply_locator(&bare).expect_err("a nameless native reply must fail");
    assert!(
        error.to_string().contains("host_terminal_id"),
        "error must name the missing field: {error}"
    );
}

#[test]
fn a_half_built_pane_locator_is_a_protocol_error_not_a_downgrade() {
    for field in ["socket_path", "pane_id", "server_pid", "server_start_time"] {
        let mut reply = tmux_reply();
        reply["direct"]["pane"][field] = Value::Null;
        let error = direct_reply_locator(&reply)
            .expect_err("an incomplete pane locator must not be silently accepted");
        assert!(
            error.to_string().contains(field),
            "error must name {field}: {error}"
        );
    }
    let mut reply = tmux_reply();
    reply["direct"] = Value::Null;
    assert!(direct_reply_locator(&reply).is_err());
}
