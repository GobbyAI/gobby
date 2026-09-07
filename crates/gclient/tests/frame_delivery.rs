//! `--frame-delivery` decides which transports the client will negotiate.
//!
//! The daemon advertises a direct locator on every row whose frame host is
//! reachable, which on a single machine is always, so `auto` never reaches the
//! proxy transport locally no matter where `--daemon-url` points. These tests
//! pin the three selections against the recorded `terminal_attach` traffic.

mod mock_daemon;

use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::{FrameDelivery, Workspace};
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::Instant;

const PROJECT_ID: &str = "project-1";
const TERMINAL: &str = "00000000-0000-0000-0000-000000000001";

/// The flat locator `/api/terminals` ships for a live native row with a
/// reachable frame host. Its presence is what makes `auto` choose direct.
fn direct_row() -> Value {
    json!({
        "terminal_id": TERMINAL,
        "state": "live",
        "backend": "native",
        "attach": {
            "backend": "native",
            "frame_host_epoch": "host-epoch",
            "host_socket": "/tmp/gobby-terminal.sock",
            "host_terminal_id": "ht-1",
            "socket_path": null,
            "pane_id": null,
            "server_pid": null,
            "server_start_time": null,
        }
    })
}

/// The same row without a locator: direct attach is not on offer at all.
fn row_without_locator() -> Value {
    json!({"terminal_id": TERMINAL, "state": "live", "backend": "native"})
}

/// Reconcile one roster row under `frame_delivery`, then report the
/// `frame_delivery` field of every `terminal_attach` the client sent, in order,
/// alongside the pane's final status message.
async fn negotiate(row: Value, frame_delivery: FrameDelivery) -> (Vec<String>, Option<String>) {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [row],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 0, "entries": []}),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project(PROJECT_ID);
    workspace.set_frame_delivery(frame_delivery);
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("roster reconcile");

    let requested: Vec<String> = mock
        .requests()
        .iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body.clone())
        .filter(|body| body.get("type").and_then(Value::as_str) == Some("terminal_attach"))
        .filter_map(|body| {
            body.get("frame_delivery")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    let pane_id = workspace
        .pane_for_terminal(TERMINAL)
        .expect("the roster row owns a pane");
    let status = workspace.pane(pane_id).status_message().map(str::to_string);

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
    (requested, status)
}

/// Without the flag there is no way to exercise the proxy transport locally:
/// a reachable frame host wins every negotiation.
#[tokio::test]
async fn proxy_skips_the_direct_attach_even_when_a_locator_is_offered() {
    let (requested, _) = negotiate(direct_row(), FrameDelivery::Proxy).await;
    assert_eq!(
        requested,
        ["proxy"],
        "forcing proxy must not first negotiate direct"
    );
}

/// The default must keep today's behavior exactly: direct first, proxy only
/// once the direct locator turns out to be unusable.
#[tokio::test]
async fn auto_asks_for_direct_first_and_falls_back_to_proxy() {
    let (requested, _) = negotiate(direct_row(), FrameDelivery::Auto).await;
    assert_eq!(requested, ["direct", "proxy"]);
}

/// `direct` refuses instead of downgrading, so a broken direct path stays
/// visible rather than being masked by a working proxy.
#[tokio::test]
async fn direct_refuses_a_pane_it_cannot_reach_instead_of_falling_back() {
    let (requested, status) = negotiate(row_without_locator(), FrameDelivery::Direct).await;
    assert!(
        requested.is_empty(),
        "direct-only negotiated a transport anyway: {requested:?}"
    );
    let status = status.expect("a refused pane must explain itself");
    assert!(
        status.contains("frame_delivery_direct_only"),
        "refusal does not name the policy: {status}"
    );
}
