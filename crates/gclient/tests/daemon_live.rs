mod mock_daemon;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use gobby_client::daemon::{
    encode_message, Answer, Daemon, DaemonError, DaemonEvent, KillOutcome, LiveDaemon,
    SpawnOutcome, SpawnRequest, CONTROL_REQUEST_DEADLINE, REQUEST_DEADLINE,
    TERMINAL_WS_SAFE_INTEGER_MAX,
};
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use serde_json::json;
use std::time::Duration;
use tokio::time::{timeout, Instant};

#[tokio::test]
async fn list_terminals_follows_cursor_and_pins_snapshot() {
    const PROJECT_ID: &str = "project-1";
    const TERMINAL_1: &str = "00000000-0000-0000-0000-000000000001";
    const TERMINAL_2: &str = "00000000-0000-0000-0000-000000000002";
    const TERMINAL_3: &str = "00000000-0000-0000-0000-000000000003";
    const STALE_TERMINAL: &str = "00000000-0000-0000-0000-000000000004";
    const CURSOR_1: &str = "2026-01-01T00:00:00+00:00|00000000-0000-0000-0000-000000000001";
    const BYTE_CAP_CURSOR: &str = "2026-01-02T00:00:00+00:00|00000000-0000-0000-0000-000000000002";

    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": TERMINAL_1, "state": "live", "backend": "native"}],
            "next_cursor": CURSOR_1,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 7}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": TERMINAL_2,
                "state": "pending",
                "backend": "native",
                "title": "x".repeat(4096)
            }],
            "next_cursor": BYTE_CAP_CURSOR
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": TERMINAL_3, "state": "live", "backend": "native"}],
            "next_cursor": null
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
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("three-page roster traversal");

    assert_eq!(
        workspace.roster_terminal_ids(),
        vec![
            TERMINAL_1.to_string(),
            TERMINAL_2.to_string(),
            TERMINAL_3.to_string(),
        ]
    );
    assert_eq!(workspace.pane_count(), 3);

    let (_, mut observed) = daemon.subscribe();
    mock.send_event_and_wait(json!({
        "type": "terminal_event",
        "event": "created",
        "terminal_id": STALE_TERMINAL,
        "daemon_epoch": "epoch-1",
        "seq": 7,
        "timestamp": "2026-01-03T00:00:00Z"
    }))
    .await;
    assert!(matches!(
        timeout(Duration::from_secs(1), observed.recv())
            .await
            .expect("stale event delivery")
            .expect("stale event"),
        DaemonEvent::Terminal { seq: 7, .. }
    ));
    workspace
        .drain_live_events()
        .await
        .expect("discard event at page-one snapshot watermark");
    assert!(!workspace
        .roster_terminal_ids()
        .iter()
        .any(|terminal_id| terminal_id == STALE_TERMINAL));

    let requests = mock.requests();
    let list_requests: Vec<_> = requests
        .iter()
        .filter(|request| request.method == "GET" && request.target.starts_with("/api/terminals"))
        .collect();
    assert_eq!(list_requests.len(), 3, "one paged request per mock page");
    let expected_cursors = [None, Some(CURSOR_1), Some(BYTE_CAP_CURSOR)];
    for (request, expected_cursor) in list_requests.iter().zip(expected_cursors) {
        let url = reqwest::Url::parse(&format!("http://mock{}", request.target))
            .expect("recorded terminal list URL");
        let query: std::collections::HashMap<_, _> = url.query_pairs().into_owned().collect();
        assert_eq!(url.path(), "/api/terminals");
        assert_eq!(
            query.get("project_id").map(String::as_str),
            Some(PROJECT_ID)
        );
        assert_eq!(
            query.get("states").map(String::as_str),
            Some("pending,live")
        );
        assert_eq!(
            query.get("cursor").map(String::as_str),
            expected_cursor,
            "the byte-capped page must resume from its last included row"
        );
    }
    assert!(requests.iter().all(|request| {
        !request.target.contains("history")
            && request
                .body
                .as_ref()
                .is_none_or(|body| !body.to_string().contains("history"))
    }));
    let attached_terminal_ids: Vec<_> = requests
        .iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body.as_ref())
        .filter(|body| {
            body.get("type").and_then(serde_json::Value::as_str) == Some("terminal_attach")
        })
        .filter_map(|body| body.get("terminal_id").and_then(serde_json::Value::as_str))
        .collect();
    assert_eq!(attached_terminal_ids, [TERMINAL_1, TERMINAL_2, TERMINAL_3]);

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn every_method_has_success_and_typed_failure() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-1", "state": "live"}],
            "next_cursor": "next-1",
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 7}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({"items": [], "next_cursor": null}),
    );
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-1",
        200,
        json!({"id": "terminal-1", "state": "live"}),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-1",
            "seq": 4,
            "entries": [{"entry_id": "run:1", "kind": "blocked"}]
        }),
    );
    mock.enqueue(
        "POST",
        "/api/attention/run:1/respond",
        200,
        json!({"ok": true}),
    );
    mock.enqueue(
        "POST",
        "/api/attention/run:1/seen",
        200,
        json!({"ok": true}),
    );
    mock.enqueue_retry_after("GET", "/api/terminals/retry", 503, 9);
    mock.enqueue(
        "GET",
        "/api/terminals/missing",
        404,
        json!({"detail": "missing"}),
    );
    mock.enqueue(
        "GET",
        "/api/terminals/malformed",
        200,
        json!("not a terminal object"),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [],
            "next_cursor": null,
            "snapshot": {
                "daemon_epoch": "epoch-1",
                "seq": TERMINAL_WS_SAFE_INTEGER_MAX + 1
            }
        }),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let first = daemon
        .list_terminals("project-1", None)
        .await
        .expect("first terminal page");
    assert_eq!(first.items[0].id(), "terminal-1");
    assert_eq!(first.snapshot.expect("first-page snapshot").seq, 7);
    let second = daemon
        .list_terminals("project-1", first.next_cursor.as_deref())
        .await
        .expect("second terminal page");
    assert!(second.snapshot.is_none());
    assert_eq!(
        daemon.terminal("terminal-1").await.expect("terminal").id(),
        "terminal-1"
    );
    assert_eq!(daemon.roster().await.expect("roster")[0].entry_id, "run:1");
    daemon
        .respond("run:1", "attention-1", &Answer::option("fingerprint-1", 1))
        .await
        .expect("respond");
    daemon
        .mark_seen("run:1", "attention-1")
        .await
        .expect("mark seen");
    assert_eq!(
        daemon.terminal("retry").await.expect_err("503 must fail"),
        DaemonError::Unavailable {
            retry_after: Some(Duration::from_secs(9))
        }
    );
    assert_eq!(
        daemon.terminal("missing").await.expect_err("404 must fail"),
        DaemonError::NotFound
    );
    assert!(matches!(
        daemon.terminal("malformed").await,
        Err(DaemonError::Protocol { .. })
    ));
    assert!(matches!(
        daemon.list_terminals("project-1", None).await,
        Err(DaemonError::Protocol { .. })
    ));
    mock.set_token("rotated-token");
    assert_eq!(
        daemon
            .list_terminals("project-1", None)
            .await
            .expect_err("stale bearer token must fail"),
        DaemonError::Unauthorized
    );

    let requests = mock.requests();
    assert!(requests
        .iter()
        .all(|request| { request.authorization.as_deref() == Some("Bearer local-token") }));
    let list_targets: Vec<_> = requests
        .iter()
        .filter(|request| request.target.starts_with("/api/terminals?"))
        .map(|request| request.target.as_str())
        .collect();
    assert!(list_targets[0].contains("states=pending%2Clive"));
    assert!(list_targets[1].contains("cursor=next-1"));
    let respond = requests
        .iter()
        .find(|request| request.target.ends_with("/respond"))
        .expect("respond request");
    assert_eq!(
        respond
            .body
            .as_ref()
            .and_then(|body| body.get("attention_id")),
        Some(&json!("attention-1"))
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn single_reader_routes_replies_and_events() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");

    assert!(matches!(
        daemon.spawn(SpawnRequest::default()).await.expect("spawn"),
        SpawnOutcome::Created { .. }
    ));
    mock.set_spawn_refusal("capacity");
    assert_eq!(
        daemon
            .spawn(SpawnRequest::default())
            .await
            .expect("refusal"),
        SpawnOutcome::Refused {
            reason: "capacity".into()
        }
    );
    let write = daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-1",
            "client_write_seq": 3,
            "data": "echo ready\n"
        }))
        .await
        .expect("write outcome");
    assert_eq!(write["client_write_seq"], 3);
    let control = daemon
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-1",
            "takeover": false
        }))
        .await
        .expect("control outcome");
    assert_eq!(control["granted"], true);
    daemon
        .notify(json!({
            "type": "terminal_set_viewport",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-1",
            "rows": 24,
            "cols": 80
        }))
        .await
        .expect("notification write");
    assert!(matches!(
        daemon.terminate("terminal-1").await.expect("terminate"),
        KillOutcome::Killed { .. }
    ));
    assert_eq!(daemon.pending_counts(), (0, 0, 0));

    let messages: Vec<_> = mock
        .requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .collect();
    assert!(messages.iter().all(|message| message.get("mode").is_none()));
    assert!(messages
        .iter()
        .any(|message| message["type"] == "terminal_set_viewport"));
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn disconnect_reconnects_once_and_fences_old_attachments() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let (snapshot, mut events) = daemon.subscribe();
    let attached = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "attach-1",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("attach");
    let attachment = attached["attachment_id"].as_str().expect("attachment id");

    mock.send_event(json!({
        "type": "terminal_event",
        "event": "created",
        "terminal_id": "terminal-2",
        "daemon_epoch": "epoch-1",
        "seq": 8,
        "timestamp": "2026-01-01T00:00:00Z"
    }));
    assert!(matches!(
        timeout(Duration::from_secs(1), events.recv())
            .await
            .expect("event deadline")
            .expect("event"),
        DaemonEvent::Terminal { seq: 8, .. }
    ));

    mock.drop_websockets();
    loop {
        let event = timeout(Duration::from_secs(1), events.recv())
            .await
            .expect("disconnect deadline")
            .expect("disconnect event");
        if matches!(event, DaemonEvent::Disconnected { .. }) {
            break;
        }
    }
    let generation = daemon
        .reconnect(snapshot.generation)
        .await
        .expect("reconnect");
    assert!(generation > snapshot.generation);
    assert_eq!(
        daemon
            .reconnect(snapshot.generation)
            .await
            .expect("already reconnected"),
        generation
    );
    let old_write = daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "terminal-1",
            "attachment_id": attachment,
            "client_write_seq": 4,
            "data": "stale"
        }))
        .await;
    assert!(matches!(old_write, Err(DaemonError::Protocol { .. })));

    daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "attach-2",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("fresh attach clears tombstone");
    daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "terminal-1",
            "attachment_id": attachment,
            "client_write_seq": 4,
            "data": "fresh"
        }))
        .await
        .expect("fresh attachment write");
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("first close");
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("idempotent close");
    mock.shutdown().await;
}

#[tokio::test]
async fn websocket_handshake_rejects_bad_bearer_token() {
    let mock = MockDaemon::start("local-token").await;
    let error = LiveDaemon::connect(mock.url(), "wrong-token")
        .await
        .expect_err("bad token must fail");
    assert_eq!(error, DaemonError::Unauthorized);
    mock.shutdown().await;
}

#[tokio::test]
async fn correlation_maps_drain_on_every_terminal_path() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("terminal_input");
    mock.suppress_ws("terminal_take_control");
    mock.suppress_ws("terminal_kill");
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    tokio::time::pause();
    let mut writes = Vec::new();
    for sequence in 0..50 {
        let daemon = daemon.clone();
        writes.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-deadline",
                    "client_write_seq": sequence,
                    "data": "x"
                }))
                .await
        }));
    }
    let request_timeout = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_kill",
                    "request_id": "reusable-request-id",
                    "terminal_id": "terminal-1"
                }))
                .await
        })
    };
    for _ in 0..10_000 {
        if daemon.pending_counts() == (1, 50, 0) {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(daemon.pending_counts(), (1, 50, 0));
    tokio::time::advance(REQUEST_DEADLINE - Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert!(writes.iter().all(|request| !request.is_finished()));
    assert!(!request_timeout.is_finished());
    assert_eq!(daemon.pending_counts(), (1, 50, 0));
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    for write in writes {
        assert_eq!(write.await.expect("write task"), Err(DaemonError::Timeout));
    }
    assert_eq!(
        request_timeout.await.expect("request task"),
        Err(DaemonError::Timeout)
    );
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    tokio::time::resume();

    let mut cancelled = Vec::new();
    for sequence in 100..150 {
        let daemon = daemon.clone();
        cancelled.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-cancelled",
                    "client_write_seq": sequence,
                    "data": "x"
                }))
                .await
        }));
    }
    timeout(Duration::from_secs(1), async {
        while daemon.pending_counts().1 < 50 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("cancelled writes registered");
    for request in cancelled {
        request.abort();
        assert!(request.await.expect_err("cancelled write").is_cancelled());
    }
    assert_eq!(daemon.pending_counts(), (0, 0, 0));

    mock.allow_ws("terminal_input");
    mock.allow_ws("terminal_kill");
    daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-deadline",
            "client_write_seq": 0,
            "data": "reused"
        }))
        .await
        .expect("write key reusable after timeout");
    daemon
        .send(json!({
            "type": "terminal_kill",
            "request_id": "reusable-request-id",
            "terminal_id": "terminal-1"
        }))
        .await
        .expect("request id reusable after timeout");

    tokio::time::pause();
    let timed_control = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "takeover": false
                }))
                .await
        })
    };
    for _ in 0..10_000 {
        if daemon.pending_counts().2 == 1
            && mock.requests().iter().any(|request| {
                request.body.as_ref().and_then(|body| body.get("type"))
                    == Some(&json!("terminal_take_control"))
            })
        {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(daemon.pending_counts().2, 1);
    tokio::time::advance(CONTROL_REQUEST_DEADLINE - Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert!(!timed_control.is_finished());
    assert_eq!(daemon.pending_counts().2, 1);
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert_eq!(
        timed_control.await.expect("timed control task"),
        Err(DaemonError::Timeout)
    );
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    assert_eq!(
        daemon
            .send(json!({
                "type": "terminal_release_control",
                "terminal_id": "terminal-1",
                "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            }))
            .await,
        Err(DaemonError::ControlScopeIndeterminate)
    );
    tokio::time::resume();
    daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "fresh-after-timeout",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("fresh attach clears timed-out control tombstone");

    let controls_before = mock
        .requests()
        .iter()
        .filter(|request| {
            request.body.as_ref().and_then(|body| body.get("type"))
                == Some(&json!("terminal_take_control"))
        })
        .count();
    let cancelled_daemon = daemon.clone();
    let cancelled = tokio::spawn(async move {
        cancelled_daemon
            .send(json!({
                "type": "terminal_take_control",
                "terminal_id": "terminal-1",
                "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "takeover": false
            }))
            .await
    });
    timeout(Duration::from_secs(1), async {
        loop {
            let written = mock
                .requests()
                .iter()
                .filter(|request| {
                    request.body.as_ref().and_then(|body| body.get("type"))
                        == Some(&json!("terminal_take_control"))
                })
                .count();
            if written > controls_before {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("control was written");
    cancelled.abort();
    assert!(cancelled.await.expect_err("cancelled task").is_cancelled());
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    assert_eq!(
        daemon
            .send(json!({
                "type": "terminal_take_control",
                "terminal_id": "terminal-1",
                "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "takeover": false
            }))
            .await,
        Err(DaemonError::ControlScopeIndeterminate)
    );

    daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "fresh-after-cancel",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("fresh attach clears control tombstone");
    mock.allow_ws("terminal_take_control");
    daemon
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": "terminal-1",
            "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "takeover": false
        }))
        .await
        .expect("control key reusable after fresh attach");
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn fragment_reassembly_bounds_hold_under_saturation() {
    async fn send_fragment(
        mock: &MockDaemon,
        attachment_id: &str,
        message_seq: u64,
        fragment_index: u64,
        more: bool,
        payload: &[u8],
    ) {
        mock.send_event_and_wait(json!({
            "type": "terminal_ws_fragment",
            "terminal_id": "terminal-1",
            "attachment_id": attachment_id,
            "event": "terminal_output",
            "message_seq": message_seq,
            "fragment_index": fragment_index,
            "more": more,
            "encoding": "utf8-b64",
            "payload": STANDARD.encode(payload)
        }))
        .await;
    }

    async fn expect_protocol_disconnect(
        events: &mut tokio::sync::broadcast::Receiver<DaemonEvent>,
    ) {
        timeout(Duration::from_secs(10), async {
            loop {
                if matches!(
                    events.recv().await.expect("daemon event"),
                    DaemonEvent::Disconnected {
                        error: DaemonError::Protocol { .. },
                        ..
                    }
                ) {
                    break;
                }
            }
        })
        .await
        .expect("protocol disconnect deadline");
    }

    async fn observe_reader_progress(
        mock: &MockDaemon,
        events: &mut tokio::sync::broadcast::Receiver<DaemonEvent>,
        seq: u64,
    ) {
        mock.send_event_and_wait(json!({
            "type": "terminal_event",
            "event": "updated",
            "terminal_id": "terminal-1",
            "daemon_epoch": "epoch-1",
            "seq": seq,
            "timestamp": "2026-01-01T00:00:00Z"
        }))
        .await;
        timeout(Duration::from_secs(10), async {
            loop {
                if matches!(
                    events.recv().await.expect("daemon event"),
                    DaemonEvent::Terminal {
                        seq: observed, ..
                    } if observed == seq
                ) {
                    break;
                }
            }
        })
        .await
        .expect("reader progress deadline");
    }

    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let (_, mut events) = daemon.subscribe();
    mock.wait_for_websocket().await;
    let inner = serde_json::to_vec(&json!({
        "type": "terminal_output",
        "terminal_id": "terminal-1",
        "attachment_id": "fragment-attachment",
        "data": "ready\n",
        "timestamp": "2026-01-01T00:00:00Z"
    }))
    .expect("serialize inner event");
    let midpoint = inner.len() / 2;
    for (index, payload) in [&inner[..midpoint], &inner[midpoint..]]
        .into_iter()
        .enumerate()
    {
        send_fragment(
            &mock,
            "fragment-attachment",
            1,
            index as u64,
            index == 0,
            payload,
        )
        .await;
        if index == 0 {
            send_fragment(&mock, "fragment-attachment", 1, 0, true, payload).await;
        }
    }
    let event = timeout(Duration::from_secs(1), events.recv())
        .await
        .expect("assembled event deadline")
        .expect("assembled event");
    assert!(matches!(event, DaemonEvent::Output(payload) if payload["data"] == "ready\n"));

    send_fragment(
        &mock,
        "conflicting-sequence",
        2,
        0,
        true,
        &inner[..midpoint],
    )
    .await;
    observe_reader_progress(&mock, &mut events, 2).await;
    send_fragment(
        &mock,
        "conflicting-sequence",
        3,
        0,
        true,
        &inner[..midpoint],
    )
    .await;
    expect_protocol_disconnect(&mut events).await;

    daemon
        .reconnect(daemon.generation())
        .await
        .expect("reconnect after sequence refusal");
    tokio::time::pause();
    let one_mibibyte = vec![b'x'; 1024 * 1024];
    for index in 0..16 {
        send_fragment(&mock, "too-large", 4, index, true, &one_mibibyte).await;
    }
    observe_reader_progress(&mock, &mut events, 4).await;
    send_fragment(&mock, "too-large", 4, 16, true, b"x").await;
    expect_protocol_disconnect(&mut events).await;

    tokio::time::resume();
    daemon
        .reconnect(daemon.generation())
        .await
        .expect("reconnect after assembly limit");
    tokio::time::pause();
    for attachment in 0..4 {
        for index in 0..16 {
            send_fragment(
                &mock,
                &format!("aggregate-{attachment}"),
                10 + attachment,
                index,
                true,
                &one_mibibyte,
            )
            .await;
        }
    }
    observe_reader_progress(&mock, &mut events, 20).await;
    send_fragment(&mock, "aggregate-overflow", 20, 0, true, b"x").await;
    expect_protocol_disconnect(&mut events).await;
    drop(one_mibibyte);
    tokio::time::resume();

    daemon
        .reconnect(daemon.generation())
        .await
        .expect("reconnect after aggregate limit");
    send_fragment(&mock, "expired-assembly", 30, 0, true, &inner[..midpoint]).await;
    observe_reader_progress(&mock, &mut events, 30).await;
    tokio::time::pause();
    tokio::time::advance(Duration::from_millis(4_999)).await;
    tokio::task::yield_now().await;
    assert!(daemon.ready(), "assembly remains valid before its deadline");
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    send_fragment(&mock, "expired-assembly", 31, 0, false, &inner).await;
    let event = timeout(Duration::from_secs(1), events.recv())
        .await
        .expect("fresh event deadline")
        .expect("fresh event");
    assert!(matches!(event, DaemonEvent::Output(payload) if payload["data"] == "ready\n"));

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn repeated_cursor_restarts_from_a_fresh_snapshot_once() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "stale-terminal"}],
            "next_cursor": "repeated",
            "snapshot": {"daemon_epoch": "epoch-stale", "seq": 3}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({"items": [], "next_cursor": "repeated"}),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "fresh-terminal"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-fresh", "seq": 4}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace.fetch_roster().await.expect("restart pagination");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["fresh-terminal".to_string()]
    );
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|request| request.target.starts_with("/api/terminals?"))
            .count(),
        3
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn notify_settles_without_a_reply() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    daemon
        .notify(json!({
            "type": "terminal_set_viewport",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-1",
            "rows": 40,
            "cols": 120
        }))
        .await
        .expect("one-way notification settles after write");
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    timeout(Duration::from_secs(1), async {
        while !mock.requests().iter().any(|request| {
            request.body.as_ref().and_then(|body| body.get("type"))
                == Some(&json!("terminal_set_viewport"))
        }) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("mock receives one-way notification");
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    assert_eq!(
        daemon
            .notify(json!({
                "type": "terminal_set_viewport",
                "terminal_id": "terminal-1",
                "attachment_id": "attachment-1",
                "rows": 24,
                "cols": 80
            }))
            .await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn close_is_idempotent_and_rejects_later_requests() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let observed = daemon.generation();
    mock.drop_websockets();
    timeout(Duration::from_secs(1), async {
        while daemon.ready() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("disconnect observed");

    let gate = mock.pause_next_websocket();
    let owner = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(observed).await })
    };
    timeout(Duration::from_secs(1), async {
        while mock.websocket_handshakes() < 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("paused reconnect reached handshake");
    let joiner = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(observed).await })
    };
    tokio::task::yield_now().await;
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close cancels and awaits reconnect");
    assert_eq!(
        owner.await.expect("reconnect owner task"),
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        joiner.await.expect("reconnect joiner task"),
        Err(DaemonError::Unavailable { retry_after: None })
    );
    gate.notify_one();
    let requests_before = mock.requests().len();
    assert_eq!(
        daemon
            .send(json!({"type": "terminal_kill", "request_id": "closed", "terminal_id": "terminal-1"}))
            .await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon
            .notify(json!({"type": "terminal_set_viewport", "terminal_id": "terminal-1", "attachment_id": "attachment-1", "rows": 24, "cols": 80}))
            .await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon.reconnect(observed).await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(mock.requests().len(), requests_before);
    assert_eq!(mock.websocket_handshakes(), 2);
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("idempotent close");
    mock.shutdown().await;
}

fn assert_fixture(actual: &serde_json::Value, raw: &str) {
    let expected: serde_json::Value = serde_json::from_str(raw).expect("fixture JSON");
    let mut normalized = actual.clone();
    if let Some(request_id) = expected.get("request_id") {
        normalized["request_id"] = request_id.clone();
    }
    assert_eq!(
        encode_message(&normalized).expect("encode actual"),
        encode_message(&expected).expect("encode fixture")
    );
}

#[tokio::test]
async fn outbound_messages_match_corpus() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "backend": "tmux",
                "state": "live"
            }],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    daemon
        .spawn(SpawnRequest {
            cwd: Some("/tmp".into()),
            ..SpawnRequest::default()
        })
        .await
        .expect("spawn");
    daemon
        .terminate("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        .await
        .expect("terminate");
    daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "client_write_seq": 1,
            "data": "ls\n"
        }))
        .await
        .expect("input");
    daemon
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "takeover": false
        }))
        .await
        .expect("take control");
    daemon
        .notify(json!({
            "type": "terminal_set_viewport",
            "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "attachment_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "rows": 24,
            "cols": 80
        }))
        .await
        .expect("viewport");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("workspace attach");

    let messages: Vec<_> = mock
        .requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .collect();
    for (kind, fixture) in [
        (
            "terminal_create",
            include_str!("../../../tests/fixtures/terminal_ws_golden/create.json"),
        ),
        (
            "terminal_kill",
            include_str!("../../../tests/fixtures/terminal_ws_golden/kill.json"),
        ),
        (
            "terminal_input",
            include_str!("../../../tests/fixtures/terminal_ws_golden/input.json"),
        ),
        (
            "terminal_take_control",
            include_str!("../../../tests/fixtures/terminal_ws_golden/take_control.json"),
        ),
        (
            "terminal_set_viewport",
            include_str!("../../../tests/fixtures/terminal_ws_golden/set_viewport.json"),
        ),
        (
            "terminal_attach",
            include_str!("../../../tests/fixtures/terminal_ws_golden/attach.json"),
        ),
    ] {
        let actual = messages
            .iter()
            .find(|message| message.get("type") == Some(&json!(kind)))
            .unwrap_or_else(|| panic!("missing outbound {kind}"));
        assert_fixture(actual, fixture);
    }
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn late_control_reply_cannot_settle_a_newer_request() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("terminal_take_control");
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let attachment = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let cancelled = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": attachment,
                    "takeover": false
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while !mock.requests().iter().any(|request| {
            request.body.as_ref().and_then(|body| body.get("type"))
                == Some(&json!("terminal_take_control"))
        }) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("control write started");
    cancelled.abort();
    assert!(cancelled
        .await
        .expect_err("cancelled control")
        .is_cancelled());
    let (_, mut observed) = daemon.subscribe();
    mock.send_event_and_wait(json!({
        "type": "terminal_control_result",
        "terminal_id": "terminal-1",
        "attachment_id": attachment,
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }))
    .await;
    mock.send_event_and_wait(json!({
        "type": "terminal_event",
        "event": "updated",
        "terminal_id": "terminal-1",
        "daemon_epoch": "epoch-1",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z"
    }))
    .await;
    assert!(matches!(
        timeout(Duration::from_secs(1), observed.recv())
            .await
            .expect("sentinel event deadline")
            .expect("sentinel event"),
        DaemonEvent::Terminal { seq: 1, .. }
    ));
    assert_eq!(
        daemon
            .send(json!({
                "type": "terminal_release_control",
                "terminal_id": "terminal-1",
                "attachment_id": attachment
            }))
            .await,
        Err(DaemonError::ControlScopeIndeterminate)
    );

    daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "fresh-control-scope",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("fresh attach clears indeterminate control scope");
    daemon
        .send(json!({
            "type": "terminal_release_control",
            "terminal_id": "terminal-1",
            "attachment_id": attachment
        }))
        .await
        .expect("new control request receives its own result");
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}
