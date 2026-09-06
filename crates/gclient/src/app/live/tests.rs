//! Direct-attach eligibility and the direct locator carried on an attach reply.
//!
//! Both seams read the same identity contract from two different payload
//! shapes: `/api/terminals` ships a flat `asdict(AttachLocator)`, while
//! `terminal_attach_result` ships the nested `direct_block()`. A native
//! terminal is named by `host_terminal_id`; a tmux pane is named by its
//! physical locator.

use super::*;
use serde_json::json;

fn row(attach: Value) -> TerminalRow {
    serde_json::from_value(json!({
        "terminal_id": "terminal-1",
        "backend": "native",
        "state": "live",
        "attach": attach,
    }))
    .expect("a daemon row must decode")
}

/// The flat block `_row_json` emits for a live native terminal.
fn native_attach() -> Value {
    json!({
        "backend": "native",
        "frame_host_epoch": "host-epoch",
        "host_socket": "/tmp/gobby-terminal.sock",
        "host_terminal_id": "ht-1",
        "socket_path": null,
        "pane_id": null,
        "server_pid": null,
        "server_start_time": null,
    })
}

/// The flat block `_row_json` emits for a live tmux pane. `TerminalManager`
/// never fills `host_terminal_id` on this path, so a real row carries null.
fn tmux_attach() -> Value {
    json!({
        "backend": "tmux",
        "frame_host_epoch": "host-epoch",
        "host_socket": "/tmp/gobby-terminal.sock",
        "host_terminal_id": null,
        "socket_path": "/tmp/tmux-501/default",
        "pane_id": "%9",
        "server_pid": 4242,
        "server_start_time": 1717171717,
    })
}

#[test]
fn a_real_tmux_row_is_direct_eligible_without_a_host_terminal_id() {
    assert!(row_has_direct_locator(&row(tmux_attach())));
    assert!(row_has_direct_locator(&row(native_attach())));
}

#[test]
fn each_backend_requires_the_identity_that_names_it() {
    // A native row has nothing but the host id to name the terminal.
    let mut attach = native_attach();
    attach["host_terminal_id"] = Value::Null;
    assert!(!row_has_direct_locator(&row(attach.clone())));
    attach["host_terminal_id"] = json!("");
    assert!(!row_has_direct_locator(&row(attach)));

    // A tmux row needs the whole pane locator: without the server generation a
    // recycled pane id would resolve to the wrong pane.
    for field in ["socket_path", "pane_id", "server_pid", "server_start_time"] {
        let mut attach = tmux_attach();
        attach[field] = Value::Null;
        assert!(
            !row_has_direct_locator(&row(attach)),
            "a tmux row missing {field} must not claim direct"
        );
    }
}

#[test]
fn a_row_without_a_usable_frame_host_is_not_direct_eligible() {
    for field in ["frame_host_epoch", "host_socket"] {
        for absent in [Value::Null, json!("")] {
            let mut attach = tmux_attach();
            attach[field] = absent.clone();
            assert!(
                !row_has_direct_locator(&row(attach)),
                "tmux row with {field}={absent} must not claim direct"
            );
        }
    }
    let mut attach = tmux_attach();
    attach["backend"] = json!("ssh");
    assert!(!row_has_direct_locator(&row(attach)));
    assert!(!row_has_direct_locator(&row(Value::Null)));
}

#[test]
fn a_tmux_generation_must_be_an_integer() {
    for bogus in [json!(true), json!(4242.5), json!("4242")] {
        let mut attach = tmux_attach();
        attach["server_pid"] = bogus.clone();
        assert!(
            !row_has_direct_locator(&row(attach)),
            "server_pid={bogus} is not a server generation"
        );
    }
}

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
