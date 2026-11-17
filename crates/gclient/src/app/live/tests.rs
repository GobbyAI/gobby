//! What the client reads from a `/api/terminals` row: direct-attach
//! eligibility, and the working directory the daemon observed.
//!
//! The row ships a flat `asdict(AttachLocator)`: a native terminal is named
//! by `host_terminal_id`. The
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
    })
}

#[test]
fn a_native_row_with_a_host_terminal_id_is_direct_eligible() {
    assert!(row_has_direct_locator(&row(native_attach())));
}

#[test]
fn a_native_row_requires_the_identity_that_names_it() {
    // A native row has nothing but the host id to name the terminal.
    let mut attach = native_attach();
    attach["host_terminal_id"] = Value::Null;
    assert!(!row_has_direct_locator(&row(attach.clone())));
    attach["host_terminal_id"] = json!("");
    assert!(!row_has_direct_locator(&row(attach)));
}

#[test]
fn a_row_without_a_usable_frame_host_is_not_direct_eligible() {
    for field in ["frame_host_epoch", "host_socket"] {
        for absent in [Value::Null, json!("")] {
            let mut attach = native_attach();
            attach[field] = absent.clone();
            assert!(
                !row_has_direct_locator(&row(attach)),
                "native row with {field}={absent} must not claim direct"
            );
        }
    }
    let mut attach = native_attach();
    attach["backend"] = json!("ssh");
    assert!(!row_has_direct_locator(&row(attach)));
    assert!(!row_has_direct_locator(&row(Value::Null)));
}

#[test]
fn a_row_carries_the_working_directory_the_daemon_observed() {
    let with_cwd = |cwd: Value| -> TerminalRow {
        serde_json::from_value(json!({
            "terminal_id": "terminal-1",
            "backend": "native",
            "state": "live",
            "cwd": cwd,
        }))
        .expect("a daemon row must decode")
    };
    assert_eq!(row_cwd(&with_cwd(json!("/repo"))).as_deref(), Some("/repo"));
    assert_eq!(row_cwd(&with_cwd(json!(""))), None);
    assert_eq!(row_cwd(&with_cwd(Value::Null)), None);
    assert_eq!(row_cwd(&row(native_attach())), None);
}
