//! The direct locator carried on a `terminal_attach_result` reply.
//!
//! The reply ships the nested `direct_block()`: a native terminal is named
//! by `host_terminal_id`.

use super::*;
use serde_json::json;

use crate::mock_daemon;

#[tokio::test]
async fn buffered_proxy_finalization_never_detaches_the_retired_attachment() {
    let cleanup = buffered_finalization_cleanup(true).await;
    assert!(cleanup.is_empty(), "retired attach requests: {cleanup:?}");
}

#[tokio::test]
async fn buffered_public_proxy_finalization_never_detaches_the_retired_attachment() {
    let cleanup = buffered_finalization_cleanup(false).await;
    assert!(cleanup.is_empty(), "retired attach requests: {cleanup:?}");
}

async fn buffered_finalization_cleanup(live_request: bool) -> Vec<Value> {
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
    mock.shutdown().await;
    forbidden
}

/// A native direct locator names its host terminal.
fn native_reply() -> Value {
    json!({
        "backend": "native",
        "direct": {
            "host_epoch": "host-epoch",
            "frame_socket_path": "/tmp/gobby-terminal.sock",
            "host_terminal_id": "ht-1",
        },
    })
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

    let mut bare = reply;
    bare["direct"]["host_terminal_id"] = Value::Null;
    let error = direct_reply_locator(&bare).expect_err("a nameless native reply must fail");
    assert!(
        error.to_string().contains("host_terminal_id"),
        "error must name the missing field: {error}"
    );
}

#[test]
fn an_incomplete_native_locator_is_a_protocol_error() {
    for field in ["host_epoch", "frame_socket_path", "host_terminal_id"] {
        let mut reply = native_reply();
        reply["direct"][field] = Value::Null;
        let error = direct_reply_locator(&reply)
            .expect_err("an incomplete native locator must not be silently accepted");
        assert!(
            error.to_string().contains(field),
            "error must name {field}: {error}"
        );
    }
    let mut reply = native_reply();
    reply["direct"] = Value::Null;
    assert!(direct_reply_locator(&reply).is_err());
}
