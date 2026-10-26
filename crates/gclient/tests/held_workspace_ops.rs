//! Regression probes for held `workspace_op`s in the mock daemon. A held op
//! keeps the refusal its receipt ordinal names, and its events reach the wire
//! in the order the daemon stamped them.

mod mock_daemon;

use futures_util::{SinkExt, StreamExt};
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::AUTHORIZATION;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

const TOKEN: &str = "held-workspace-op-token";
const PROJECT: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ROUNDS: usize = 50;

type Socket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

async fn connect(mock: &MockDaemon) -> Socket {
    let url = format!("{}/ws", mock.url().replacen("http://", "ws://", 1));
    let mut request = url.into_client_request().expect("websocket request");
    request.headers_mut().insert(
        AUTHORIZATION,
        format!("Bearer {TOKEN}")
            .parse()
            .expect("authorization header"),
    );
    let (socket, _) = connect_async(request).await.expect("websocket connects");
    socket
}

async fn send(socket: &mut Socket, value: Value) {
    socket
        .send(Message::Text(value.to_string().into()))
        .await
        .expect("send frame");
}

async fn next_frame(socket: &mut Socket) -> Value {
    let message = tokio::time::timeout(Duration::from_secs(5), socket.next())
        .await
        .expect("frame arrives within 5s")
        .expect("socket stays open")
        .expect("frame reads");
    match message {
        Message::Text(text) => serde_json::from_str(text.as_str()).expect("json frame"),
        other => panic!("unexpected frame {other:?}"),
    }
}

/// Read frames until every `request_id` in `ids` has its workspace reply,
/// returning all frames in wire order.
async fn frames_until_replies(socket: &mut Socket, ids: &[&str]) -> Vec<Value> {
    let mut frames = Vec::new();
    let mut pending: Vec<&str> = ids.to_vec();
    while !pending.is_empty() {
        let frame = next_frame(socket).await;
        if frame["type"] == json!("workspace_op") || frame["type"] == json!("workspace_error") {
            pending.retain(|id| frame["request_id"] != json!(id));
        }
        frames.push(frame);
    }
    frames
}

async fn wait_for_receipts(mock: &MockDaemon, count: usize) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while mock.workspace_requests().len() < count {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("workspace ops reach the mock");
}

fn read_op(request_id: &str) -> Value {
    json!({
        "type": "workspace_op",
        "request_id": request_id,
        "op": "pane.read",
    })
}

fn create_tab(request_id: &str) -> Value {
    json!({
        "type": "workspace_op",
        "request_id": request_id,
        "op": "tab.create",
        "project_id": PROJECT,
    })
}

#[tokio::test]
async fn held_op_keeps_the_refusal_its_receipt_ordinal_names() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.refuse_workspace_op_at("pane.read", 1, "invalid_op", "first read refused");
    let release = mock.hold_ws("workspace_op", |request| {
        request["request_id"] == json!("first")
    });
    let mut socket = connect(&mock).await;

    send(&mut socket, read_op("first")).await;
    wait_for_receipts(&mock, 1).await;
    send(&mut socket, read_op("second")).await;
    let frames = frames_until_replies(&mut socket, &["second"]).await;
    release.notify_one();
    let replies = frames_until_replies(&mut socket, &["first"]).await;

    let second = frames.last().expect("second reply");
    assert_eq!(
        second["type"],
        json!("workspace_op"),
        "second read is accepted: {second}"
    );
    let first = replies.last().expect("first reply");
    assert_eq!(
        first["type"],
        json!("workspace_error"),
        "first read keeps its refusal: {first}"
    );
    assert_eq!(first["code"], json!("invalid_op"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn held_op_events_reach_the_wire_in_stamp_order() {
    for round in 0..ROUNDS {
        let mock = MockDaemon::start(TOKEN).await;
        mock.seed_workspace(PROJECT, &[(&["terminal-a"], "first")]);
        let release = mock.hold_ws("workspace_op", |request| {
            request["request_id"] == json!("held")
        });
        let mut socket = connect(&mock).await;

        send(&mut socket, create_tab("held")).await;
        wait_for_receipts(&mock, 1).await;
        release.notify_one();
        send(&mut socket, create_tab("newer")).await;
        let frames = frames_until_replies(&mut socket, &["held", "newer"]).await;

        let seqs: Vec<u64> = frames
            .iter()
            .filter(|frame| frame["type"] == json!("workspace_event"))
            .filter_map(|frame| frame["seq"].as_u64())
            .collect();
        assert_eq!(
            seqs.len(),
            2,
            "round {round}: one event per tab.create: {frames:?}"
        );
        assert!(
            seqs[0] < seqs[1],
            "round {round}: events reached the wire out of stamp order: {seqs:?}"
        );
    }
}
