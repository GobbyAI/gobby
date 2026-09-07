//! Direct-attach eligibility read from a `/api/terminals` row.
//!
//! The row ships a flat `asdict(AttachLocator)`: a native terminal is named
//! by `host_terminal_id`; a tmux pane is named by its physical locator. The
//! nested `direct_block()` of an attach reply is covered in `live_attach`.

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
