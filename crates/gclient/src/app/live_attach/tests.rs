//! The direct locator carried on a `terminal_attach_result` reply.
//!
//! The reply ships the nested `direct_block()`: a native terminal is named
//! by `host_terminal_id`; a tmux pane is named by its physical locator.

use super::*;
use serde_json::json;

#[path = "../../../tests/mock_daemon/transport.rs"]
mod mock_daemon;

#[tokio::test]
async fn buffered_proxy_finalization_never_detaches_the_retired_attachment() {
    assert_buffered_finalization_has_no_cleanup(true).await;
}

#[tokio::test]
async fn buffered_public_proxy_finalization_never_detaches_the_retired_attachment() {
    assert_buffered_finalization_has_no_cleanup(false).await;
}

async fn assert_buffered_finalization_has_no_cleanup(live_request: bool) {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.finalize_next_proxy_attach_before_reply(
        "epoch-1",
        1,
        "attach_eof",
        "ended before attach result",
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect daemon");
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        if live_request {
            request_proxy_source(&daemon, "terminal-buffered-finalization", "retired-attach")
                .await
                .map(|_| ())
        } else {
            ProxyFrameSource::attach(daemon.clone(), "terminal-buffered-finalization")
                .await
                .map(|_| ())
        }
    })
    .await
    .expect("attach outcome completed");
    assert!(matches!(
        outcome,
        Err(FrameError::Finalized { code, reason })
            if code == "attach_eof" && reason == "ended before attach result"
    ));

    // Await the whole attach outcome, then a reply on the same socket. Every
    // cleanup request it issued must have reached the mock before this reply.
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        daemon.send(json!({
            "type": "subscribe",
            "events": [],
            "request_id": "retired-attach-wire-barrier",
        })),
    )
    .await
    .expect("wire barrier answered")
    .expect("wire barrier succeeded");
    let forbidden: Vec<_> = mock
        .requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body.get("attachment_id") == Some(&json!("attachment-1")))
        .filter(|body| {
            matches!(
                body.get("type").and_then(Value::as_str),
                Some("terminal_detach" | "terminal_set_viewport")
            )
        })
        .collect();
    assert!(
        forbidden.is_empty(),
        "retired attach requests: {forbidden:?}"
    );
    mock.shutdown().await;
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
