mod mock_daemon;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use futures_util::FutureExt;
use gobby_client::daemon::{
    encode_message, Answer, Daemon, DaemonError, DaemonEvent, EventReceiver, KillOutcome,
    LiveDaemon, SpawnOutcome, SpawnRequest, CONTROL_REQUEST_DEADLINE, REQUEST_DEADLINE,
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

    let repeated = MockDaemon::start("local-token").await;
    for snapshot in [Some(json!({"daemon_epoch": "epoch-1", "seq": 7})), None] {
        repeated.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": TERMINAL_1, "state": "live"}],
                "next_cursor": CURSOR_1,
                "snapshot": snapshot
            }),
        );
    }
    let repeated_daemon = LiveDaemon::connect(repeated.url(), "local-token")
        .await
        .expect("connect repeated-cursor daemon");
    let mut repeated_workspace = Workspace::live(repeated_daemon.clone());
    repeated_workspace.select_project(PROJECT_ID);
    assert!(matches!(
        repeated_workspace.reconcile_subscribe_first().await,
        Err(DaemonError::Protocol { detail }) if detail == "terminal cursor repeated"
    ));
    assert_eq!(
        repeated
            .requests()
            .iter()
            .filter(|request| request.target.starts_with("/api/terminals?"))
            .count(),
        2,
        "a repeated cursor is a local protocol error, not a retriable cursor_stale refusal"
    );
    repeated_daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close repeated-cursor daemon");
    repeated.shutdown().await;
}

#[tokio::test]
async fn every_method_has_success_and_typed_failure() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-1", "terminal_id": "terminal-1", "state": "live"}],
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
        json!({"id": "terminal-1", "terminal_id": "terminal-1", "state": "live"}),
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
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        403,
        json!({"detail": "forbidden"}),
    );
    mock.enqueue(
        "POST",
        "/api/attention/run:missing/respond",
        404,
        json!({"detail": "missing"}),
    );
    mock.enqueue_retry_after("POST", "/api/attention/run:retry/seen", 503, 11);

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let (initial, mut events) = daemon.subscribe();
    assert!(initial.ready);
    assert_eq!(initial.last_error, None);
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
    assert_eq!(
        daemon.roster().await.expect_err("403 must fail"),
        DaemonError::Unauthorized
    );
    assert_eq!(
        daemon
            .respond(
                "run:missing",
                "attention-missing",
                &Answer::option("fingerprint-missing", 1),
            )
            .await,
        Err(DaemonError::NotFound)
    );
    assert_eq!(
        daemon.mark_seen("run:retry", "attention-retry").await,
        Err(DaemonError::Unavailable {
            retry_after: Some(Duration::from_secs(11))
        })
    );
    mock.set_token("rotated-token");
    assert_eq!(
        daemon
            .list_terminals("project-1", None)
            .await
            .expect_err("stale bearer token must fail"),
        DaemonError::Unauthorized
    );
    mock.set_token("local-token");

    assert!(matches!(
        daemon.spawn(SpawnRequest::default()).await.expect("spawn"),
        SpawnOutcome::Created { .. }
    ));
    mock.set_spawn_refusal("capacity");
    assert_eq!(
        daemon
            .spawn(SpawnRequest::default())
            .await
            .expect("create refusal"),
        SpawnOutcome::Refused {
            reason: "capacity".into()
        }
    );
    assert_eq!(
        daemon.terminate("terminal-1").await.expect("terminate"),
        KillOutcome::Killed {
            terminal_id: "terminal-1".into()
        }
    );
    let attached = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "every-method-attach",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("send");
    daemon
        .notify(json!({
            "type": "terminal_set_viewport",
            "terminal_id": "terminal-1",
            "attachment_id": attached["attachment_id"],
            "rows": 24,
            "cols": 80
        }))
        .await
        .expect("notify");
    timeout(Duration::from_secs(1), async {
        while !mock.requests().iter().any(|request| {
            request
                .body
                .as_ref()
                .and_then(|body| body.get("type"))
                .and_then(serde_json::Value::as_str)
                == Some("terminal_set_viewport")
        }) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("notify recorded by mock");

    mock.fail_next_websocket();
    mock.drop_websockets();
    let disconnected_generation = timeout(Duration::from_secs(1), async {
        loop {
            if let DaemonEvent::Disconnected { generation, error } =
                events.recv().await.expect("daemon event")
            {
                assert_eq!(
                    error,
                    DaemonError::Unavailable { retry_after: None },
                    "subscribe exposes a typed connection failure"
                );
                break generation;
            }
        }
    })
    .await
    .expect("disconnect deadline");
    assert_eq!(disconnected_generation, initial.generation);
    let disconnected = daemon.subscribe().0;
    assert!(!disconnected.ready);
    assert_eq!(
        disconnected.last_error,
        Some(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon.reconnect(initial.generation).await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    let failed_reconnect = daemon.subscribe().0;
    assert!(!failed_reconnect.ready);
    assert_eq!(
        failed_reconnect.last_error,
        Some(DaemonError::Unavailable { retry_after: None })
    );
    let reconnected = daemon
        .reconnect(initial.generation)
        .await
        .expect("reconnect after connection failure");
    assert!(reconnected > initial.generation);
    let ready_again = daemon.subscribe().0;
    assert!(ready_again.ready);
    assert_eq!(ready_again.generation, reconnected);
    assert_eq!(ready_again.last_error, None);

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
    for kind in [
        "terminal_create",
        "terminal_kill",
        "terminal_attach",
        "terminal_set_viewport",
    ] {
        assert!(requests.iter().any(|request| {
            request
                .body
                .as_ref()
                .and_then(|body| body.get("type"))
                .and_then(serde_json::Value::as_str)
                == Some(kind)
        }));
    }
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    assert_eq!(
        daemon.spawn(SpawnRequest::default()).await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon.terminate("terminal-1").await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": "after-close",
                "terminal_id": "terminal-1",
                "frame_delivery": "proxy"
            }))
            .await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon
            .notify(json!({
                "type": "terminal_set_viewport",
                "terminal_id": "terminal-1",
                "attachment_id": "after-close",
                "rows": 24,
                "cols": 80
            }))
            .await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        daemon.reconnect(reconnected).await,
        Err(DaemonError::Unavailable { retry_after: None })
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("idempotent close");
    mock.shutdown().await;

    let close_timeout_mock = MockDaemon::start("local-token").await;
    let close_timeout_daemon = LiveDaemon::connect(close_timeout_mock.url(), "local-token")
        .await
        .expect("connect close-timeout daemon");
    assert_eq!(
        close_timeout_daemon
            .close(Instant::now() - Duration::from_millis(1))
            .await,
        Err(DaemonError::Timeout)
    );
    close_timeout_mock.shutdown().await;
}

#[tokio::test]
async fn single_reader_routes_replies_and_events() {
    let mock = MockDaemon::start("local-token").await;
    for kind in [
        "terminal_create",
        "terminal_kill",
        "terminal_input",
        "terminal_take_control",
    ] {
        mock.suppress_ws(kind);
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let (generation, mut events) = daemon.subscribe();
    let spawn = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.spawn(SpawnRequest::default()).await })
    };
    let terminate = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.terminate("terminal-1").await })
    };
    let write = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-1",
                    "client_write_seq": 3,
                    "data": "echo ready\n"
                }))
                .await
        })
    };
    let control = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-1",
                    "takeover": false
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while daemon.pending_counts() != (2, 1, 1)
            || mock
                .requests()
                .iter()
                .filter(|request| request.method == "WS")
                .count()
                < 4
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("all interleaved waiters registered and written");
    let requests = mock.requests();
    let request_id = |kind: &str| {
        requests
            .iter()
            .filter_map(|request| request.body.as_ref())
            .find(|body| body.get("type") == Some(&json!(kind)))
            .and_then(|body| body.get("request_id"))
            .and_then(serde_json::Value::as_str)
            .expect("request id")
            .to_string()
    };
    let spawn_id = request_id("terminal_create");
    let kill_id = request_id("terminal_kill");

    mock.send_event(json!({
        "type": "terminal_kill_result",
        "request_id": "unmatched-request-id",
        "terminal_id": "terminal-other",
        "success": true
    }));
    mock.send_event_and_wait(json!({
        "type": "terminal_event",
        "event": "created",
        "terminal_id": "terminal-event",
        "daemon_epoch": "epoch-1",
        "seq": 1,
        "timestamp": "2026-01-01T00:00:00Z"
    }))
    .await;
    assert!(matches!(
        timeout(Duration::from_secs(1), events.recv())
            .await
            .expect("interleaved lifecycle deadline")
            .expect("interleaved lifecycle"),
        DaemonEvent::Terminal { seq: 1, .. }
    ));
    assert_eq!(
        daemon.pending_counts(),
        (2, 1, 1),
        "an unmatched request and lifecycle fan-out settle no waiter"
    );
    mock.send_event(json!({
        "type": "terminal_write_outcome",
        "attachment_id": "attachment-1",
        "terminal_id": "terminal-1",
        "client_write_seq": 3,
        "outcome": "applied",
        "reason": null
    }));
    mock.send_event(json!({
        "type": "terminal_create_result",
        "request_id": spawn_id,
        "success": true,
        "terminal_id": "terminal-created",
        "backend": "native",
        "reason": null
    }));
    mock.send_event(json!({
        "type": "terminal_control_result",
        "attachment_id": "attachment-1",
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }));
    mock.send_event_and_wait(json!({
        "type": "terminal_kill_result",
        "request_id": kill_id,
        "terminal_id": "terminal-1",
        "success": true
    }))
    .await;
    assert_eq!(
        write.await.expect("write task").expect("write")["client_write_seq"],
        3
    );
    assert_eq!(
        control.await.expect("control task").expect("control")["granted"],
        true
    );
    assert!(matches!(
        spawn.await.expect("spawn task").expect("spawn"),
        SpawnOutcome::Created { .. }
    ));
    assert!(matches!(
        terminate.await.expect("terminate task").expect("terminate"),
        KillOutcome::Killed { .. }
    ));

    let finalized_write = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-finalized",
                    "client_write_seq": 9,
                    "data": "pending"
                }))
                .await
        })
    };
    let finalized_control = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-finalized",
                    "takeover": false
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while daemon.pending_counts() != (0, 1, 1) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("attachment waiters registered");
    mock.send_event_and_wait(json!({
        "type": "terminal_attachment_finalized",
        "terminal_id": "terminal-1",
        "attachment_id": "attachment-finalized",
        "daemon_epoch": "epoch-1",
        "seq": 2,
        "reason": "detached"
    }))
    .await;
    assert!(matches!(
        timeout(Duration::from_secs(1), events.recv())
            .await
            .expect("finalization fan-out deadline")
            .expect("finalization event"),
        DaemonEvent::AttachmentFinalized { seq: 2, .. }
    ));
    assert_eq!(
        daemon.pending_counts(),
        (0, 0, 0),
        "finalization removes attachment maps before fan-out"
    );
    assert_eq!(
        finalized_write.await.expect("finalized write task"),
        Err(DaemonError::ControlScopeIndeterminate)
    );
    assert_eq!(
        finalized_control.await.expect("finalized control task"),
        Err(DaemonError::ControlScopeIndeterminate)
    );

    let dropped_request = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.spawn(SpawnRequest::default()).await })
    };
    let dropped_write = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-drop",
                    "client_write_seq": 10,
                    "data": "pending"
                }))
                .await
        })
    };
    let dropped_control = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-drop",
                    "takeover": false
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while daemon.pending_counts() != (1, 1, 1) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("drop waiters registered");
    mock.drop_websockets();
    let unavailable = DaemonError::Unavailable { retry_after: None };
    assert_eq!(
        dropped_request.await.expect("dropped request task"),
        Err(unavailable.clone())
    );
    assert_eq!(
        dropped_write.await.expect("dropped write task"),
        Err(unavailable.clone())
    );
    assert_eq!(
        dropped_control.await.expect("dropped control task"),
        Err(unavailable)
    );
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    daemon
        .reconnect(generation.generation)
        .await
        .expect("next generation connects only after pending waiters fail");
    mock.allow_ws("terminal_create");
    assert!(matches!(
        daemon
            .spawn(SpawnRequest::default())
            .await
            .expect("next request"),
        SpawnOutcome::Created { .. }
    ));
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

/// A daemon restart closes the socket with 1001, and the supervisor keeps
/// retrying with the generation it observed at that loss. When the next
/// handshake upgrades but dies before `subscribe_success`, the generation has
/// already rolled forward; the stale caller must still open a fresh
/// connection instead of replaying the cached failure forever (#22002).
#[tokio::test]
async fn stale_generation_reconnect_reopens_after_a_failed_handshake() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let (initial, mut events) = daemon.subscribe();
    let observed = initial.generation;

    mock.close_websockets_going_away();
    let lost = timeout(Duration::from_secs(1), async {
        loop {
            if let DaemonEvent::Disconnected { generation, error } =
                events.recv().await.expect("daemon event")
            {
                assert_eq!(generation, observed);
                break error;
            }
        }
    })
    .await
    .expect("going-away disconnect deadline");
    assert!(matches!(lost, DaemonError::GoingAway), "{lost:?}");

    // The next handshake upgrades but the daemon never confirms the
    // subscription and then drops the socket: the attempt fails after the
    // generation moved past `observed`.
    mock.suppress_ws("subscribe");
    let handshakes_before = mock.websocket_handshakes();
    let subscribes_before = count_ws_requests(&mock, "subscribe");
    let attempt = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(observed).await })
    };
    timeout(Duration::from_secs(1), async {
        while count_ws_requests(&mock, "subscribe") == subscribes_before {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("unanswered subscribe reached the mock");
    mock.drop_websockets();
    let failed = attempt.await.expect("reconnect task");
    assert!(failed.is_err(), "the dropped handshake fails: {failed:?}");
    assert!(!daemon.ready());
    assert!(
        daemon.generation() > observed,
        "the failed handshake already rolled the generation forward"
    );
    assert_eq!(mock.websocket_handshakes(), handshakes_before + 1);

    mock.allow_ws("subscribe");
    let reconnected = daemon
        .reconnect(observed)
        .await
        .expect("a stale generation still opens a fresh connection");
    assert!(reconnected > observed);
    assert!(daemon.ready());
    assert_eq!(daemon.generation(), reconnected);
    assert_eq!(
        mock.websocket_handshakes(),
        handshakes_before + 2,
        "the stale-generation attempt opened a new socket"
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

fn count_ws_requests(mock: &MockDaemon, kind: &str) -> usize {
    mock.requests()
        .iter()
        .filter(|request| {
            request
                .body
                .as_ref()
                .and_then(|body| body.get("type"))
                .and_then(serde_json::Value::as_str)
                == Some(kind)
        })
        .count()
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
    let mut requests = Vec::new();
    for sequence in 0..50 {
        let daemon = daemon.clone();
        requests.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_kill",
                    "request_id": format!("deadline-request-{sequence}"),
                    "terminal_id": "terminal-1"
                }))
                .await
        }));
    }
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
    let mut controls = Vec::new();
    for sequence in 0..50 {
        let daemon = daemon.clone();
        controls.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": format!("deadline-control-{sequence}"),
                    "takeover": false
                }))
                .await
        }));
    }
    for _ in 0..10_000 {
        if daemon.pending_counts() == (50, 50, 50) {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(daemon.pending_counts(), (50, 50, 50));
    tokio::time::advance(CONTROL_REQUEST_DEADLINE - Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert!(controls.iter().all(|request| !request.is_finished()));
    assert!(writes.iter().all(|request| !request.is_finished()));
    assert!(requests.iter().all(|request| !request.is_finished()));
    assert_eq!(daemon.pending_counts(), (50, 50, 50));
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    for control in controls {
        assert_eq!(
            control.await.expect("control deadline task"),
            Err(DaemonError::Timeout)
        );
    }
    assert_eq!(daemon.pending_counts(), (50, 50, 0));
    tokio::time::advance(REQUEST_DEADLINE - CONTROL_REQUEST_DEADLINE - Duration::from_millis(1))
        .await;
    tokio::task::yield_now().await;
    assert!(writes.iter().all(|request| !request.is_finished()));
    assert!(requests.iter().all(|request| !request.is_finished()));
    assert_eq!(daemon.pending_counts(), (50, 50, 0));
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    for write in writes {
        assert_eq!(write.await.expect("write task"), Err(DaemonError::Timeout));
    }
    for request in requests {
        assert_eq!(
            request.await.expect("request deadline task"),
            Err(DaemonError::Timeout)
        );
    }
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    tokio::time::resume();

    let (_, mut observed) = daemon.subscribe();
    mock.send_event(json!({
        "type": "terminal_kill_result",
        "request_id": "deadline-request-0",
        "terminal_id": "terminal-1",
        "success": true
    }));
    mock.send_event(json!({
        "type": "terminal_write_outcome",
        "attachment_id": "attachment-deadline",
        "terminal_id": "terminal-1",
        "client_write_seq": 0,
        "outcome": "applied",
        "reason": null
    }));
    mock.send_event(json!({
        "type": "terminal_control_result",
        "attachment_id": "deadline-control-0",
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }));
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
            .expect("late-reply sentinel deadline")
            .expect("late-reply sentinel"),
        DaemonEvent::Terminal { seq: 1, .. }
    ));
    assert_eq!(
        daemon.pending_counts(),
        (0, 0, 0),
        "withheld late replies recreate no exact-key entry"
    );

    let mut cancelled_requests = Vec::new();
    for sequence in 0..50 {
        let daemon = daemon.clone();
        cancelled_requests.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_kill",
                    "request_id": format!("cancelled-request-{sequence}"),
                    "terminal_id": "terminal-1"
                }))
                .await
        }));
    }
    let mut cancelled_writes = Vec::new();
    for sequence in 100..150 {
        let daemon = daemon.clone();
        cancelled_writes.push(tokio::spawn(async move {
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
    let mut cancelled_controls = Vec::new();
    for sequence in 0..50 {
        let daemon = daemon.clone();
        cancelled_controls.push(tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": format!("cancelled-control-{sequence}"),
                    "takeover": false
                }))
                .await
        }));
    }
    timeout(Duration::from_secs(1), async {
        while daemon.pending_counts() != (50, 50, 50) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("cancelled operations registered in all three maps");
    for request in cancelled_requests
        .into_iter()
        .chain(cancelled_writes)
        .chain(cancelled_controls)
    {
        request.abort();
        assert!(request
            .await
            .expect_err("cancelled correlation task")
            .is_cancelled());
    }
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    mock.send_event(json!({
        "type": "terminal_kill_result",
        "request_id": "cancelled-request-0",
        "terminal_id": "terminal-1",
        "success": true
    }));
    mock.send_event(json!({
        "type": "terminal_write_outcome",
        "attachment_id": "attachment-cancelled",
        "terminal_id": "terminal-1",
        "client_write_seq": 100,
        "outcome": "applied",
        "reason": null
    }));
    mock.send_event(json!({
        "type": "terminal_control_result",
        "attachment_id": "cancelled-control-0",
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }));
    mock.send_event_and_wait(json!({
        "type": "terminal_event",
        "event": "updated",
        "terminal_id": "terminal-1",
        "daemon_epoch": "epoch-1",
        "seq": 2,
        "timestamp": "2026-01-01T00:00:01Z"
    }))
    .await;
    loop {
        if matches!(
            timeout(Duration::from_secs(1), observed.recv())
                .await
                .expect("cancelled late-reply sentinel deadline")
                .expect("cancelled late-reply sentinel"),
            DaemonEvent::Terminal { seq: 2, .. }
        ) {
            break;
        }
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
            "request_id": "deadline-request-0",
            "terminal_id": "terminal-1"
        }))
        .await
        .expect("request id reusable after timeout");
    daemon
        .send(json!({
            "type": "terminal_input",
            "terminal_id": "terminal-1",
            "attachment_id": "attachment-cancelled",
            "client_write_seq": 100,
            "data": "reused after cancellation"
        }))
        .await
        .expect("write key reusable after cancellation");
    daemon
        .send(json!({
            "type": "terminal_kill",
            "request_id": "cancelled-request-0",
            "terminal_id": "terminal-1"
        }))
        .await
        .expect("request id reusable after cancellation");

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

    async fn expect_protocol_disconnect(events: &mut EventReceiver) {
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

    async fn observe_reader_progress(mock: &MockDaemon, events: &mut EventReceiver, seq: u64) {
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
    let pending_mock = MockDaemon::start("local-token").await;
    pending_mock.suppress_ws("terminal_kill");
    pending_mock.suppress_ws("terminal_input");
    pending_mock.suppress_ws("terminal_take_control");
    let pending_daemon = LiveDaemon::connect(pending_mock.url(), "local-token")
        .await
        .expect("connect pending live daemon");
    let pending_request = {
        let daemon = pending_daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_kill",
                    "request_id": "close-request",
                    "terminal_id": "terminal-1"
                }))
                .await
        })
    };
    let pending_write = {
        let daemon = pending_daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": "terminal-1",
                    "attachment_id": "close-attachment",
                    "client_write_seq": 1,
                    "data": "pending"
                }))
                .await
        })
    };
    let pending_control = {
        let daemon = pending_daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "close-attachment",
                    "takeover": false
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while pending_daemon.pending_counts() != (1, 1, 1) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("close waiters registered in all correlation maps");
    pending_daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close drains pending waiters and socket tasks");
    let unavailable = DaemonError::Unavailable { retry_after: None };
    assert_eq!(
        pending_request.await.expect("pending request task"),
        Err(unavailable.clone())
    );
    assert_eq!(
        pending_write.await.expect("pending write task"),
        Err(unavailable.clone())
    );
    assert_eq!(
        pending_control.await.expect("pending control task"),
        Err(unavailable)
    );
    assert_eq!(pending_daemon.pending_counts(), (0, 0, 0));
    pending_mock.wait_for_no_websockets().await;
    assert_eq!(pending_mock.websocket_closes(), 1, "sink sent one close");
    pending_mock.shutdown().await;

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
    let mut closing = Box::pin(daemon.close(Instant::now() + Duration::from_secs(1)));
    assert!(
        (&mut closing).now_or_never().is_none(),
        "close latches terminal state and awaits the admitted reconnect"
    );
    gate.notify_one();
    closing
        .await
        .expect("close cancels and awaits reconnect released after its handshake pause");
    assert_eq!(
        owner.await.expect("reconnect owner task"),
        Err(DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(
        joiner.await.expect("reconnect joiner task"),
        Err(DaemonError::Unavailable { retry_after: None })
    );
    let snapshot = daemon.subscribe().0;
    assert_eq!(snapshot.generation, observed);
    assert!(
        !snapshot.ready,
        "close prevents generation-ready publication"
    );
    mock.wait_for_no_websockets().await;
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
    mock.suppress_ws("terminal_release_control");
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let attachment = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

    tokio::time::pause();
    let timed = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": attachment,
                    "takeover": false,
                    "schedule": "exact-deadline"
                }))
                .await
        })
    };
    for _ in 0..10_000 {
        if daemon.pending_counts().2 == 1
            && mock.requests().iter().any(|request| {
                request.body.as_ref().and_then(|body| body.get("schedule"))
                    == Some(&json!("exact-deadline"))
            })
        {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(daemon.pending_counts().2, 1);
    tokio::time::advance(CONTROL_REQUEST_DEADLINE - Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert!(!timed.is_finished());
    tokio::time::advance(Duration::from_millis(1)).await;
    tokio::task::yield_now().await;
    assert_eq!(
        timed.await.expect("exact deadline task"),
        Err(DaemonError::Timeout)
    );
    assert_eq!(daemon.pending_counts().2, 0);
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
    tokio::time::resume();

    let (_, mut observed) = daemon.subscribe();
    mock.send_event(json!({
        "type": "terminal_control_result",
        "terminal_id": "terminal-1",
        "attachment_id": attachment,
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }));
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
            .expect("deadline late-reply sentinel")
            .expect("deadline late-reply sentinel"),
        DaemonEvent::Terminal { seq: 1, .. }
    ));
    daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": "fresh-after-deadline",
            "terminal_id": "terminal-1",
            "frame_delivery": "proxy"
        }))
        .await
        .expect("fresh attach clears deadline tombstone");

    let post_send = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": attachment,
                    "takeover": false,
                    "schedule": "post-send"
                }))
                .await
        })
    };
    timeout(Duration::from_secs(1), async {
        while !mock.requests().iter().any(|request| {
            request.body.as_ref().and_then(|body| body.get("schedule")) == Some(&json!("post-send"))
        }) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("post-send control write completed");
    post_send.abort();
    assert!(post_send
        .await
        .expect_err("post-send cancelled control")
        .is_cancelled());
    mock.send_event(json!({
        "type": "terminal_control_result",
        "terminal_id": "terminal-1",
        "attachment_id": attachment,
        "granted": true,
        "lease_generation": 1,
        "reason": null
    }));
    mock.send_event_and_wait(json!({
        "type": "terminal_event",
        "event": "updated",
        "terminal_id": "terminal-1",
        "daemon_epoch": "epoch-1",
        "seq": 2,
        "timestamp": "2026-01-01T00:00:01Z"
    }))
    .await;
    assert!(matches!(
        timeout(Duration::from_secs(1), observed.recv())
            .await
            .expect("sentinel event deadline")
            .expect("sentinel event"),
        DaemonEvent::Terminal { seq: 2, .. }
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

    let read_gate = mock.pause_websocket_reads().await;
    let mid_send = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-mid-send",
                    "takeover": false,
                    "schedule": "mid-send",
                    "padding": "x".repeat(8 * 1024 * 1024)
                }))
                .await
        })
    };
    while daemon.pending_counts().2 != 1 {
        tokio::task::yield_now().await;
    }
    for _ in 0..100 {
        tokio::task::yield_now().await;
    }
    let pre_write = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-pre-write",
                    "takeover": false,
                    "schedule": "pre-write-cancelled"
                }))
                .await
        })
    };
    while daemon.pending_counts().2 != 2 {
        tokio::task::yield_now().await;
    }
    pre_write.abort();
    assert!(pre_write
        .await
        .expect_err("pre-write cancelled control")
        .is_cancelled());
    let replacement = {
        let daemon = daemon.clone();
        tokio::spawn(async move {
            daemon
                .send(json!({
                    "type": "terminal_take_control",
                    "terminal_id": "terminal-1",
                    "attachment_id": "attachment-pre-write",
                    "takeover": false,
                    "schedule": "pre-write-replacement"
                }))
                .await
        })
    };
    while daemon.pending_counts().2 != 2 {
        tokio::task::yield_now().await;
    }
    mid_send.abort();
    assert!(mid_send
        .await
        .expect_err("mid-send cancelled control")
        .is_cancelled());
    assert_eq!(daemon.pending_counts().2, 1);
    assert_eq!(
        timeout(
            Duration::from_millis(100),
            daemon.send(json!({
                "type": "terminal_release_control",
                "terminal_id": "terminal-1",
                "attachment_id": "attachment-mid-send"
            }))
        )
        .await
        .expect("mid-send cancellation tombstones locally"),
        Err(DaemonError::ControlScopeIndeterminate)
    );
    read_gate.notify_one();
    timeout(Duration::from_secs(5), async {
        while !mock.requests().iter().any(|request| {
            request.body.as_ref().and_then(|body| body.get("schedule"))
                == Some(&json!("pre-write-replacement"))
        }) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("replacement control reaches resumed sink");
    let resumed_requests = mock.requests();
    let pre_write_schedules: Vec<_> = resumed_requests
        .iter()
        .filter_map(|request| request.body.as_ref())
        .filter_map(|body| body.get("schedule").and_then(serde_json::Value::as_str))
        .filter(|schedule| schedule.starts_with("pre-write"))
        .map(str::to_string)
        .collect();
    assert_eq!(
        pre_write_schedules,
        ["pre-write-replacement".to_string()],
        "a request cancelled before write_started never invokes the sink"
    );
    mock.send_event_and_wait(json!({
        "type": "terminal_control_result",
        "terminal_id": "terminal-1",
        "attachment_id": "attachment-pre-write",
        "granted": true,
        "lease_generation": 2,
        "reason": null
    }))
    .await;
    replacement
        .await
        .expect("pre-write replacement task")
        .expect("pre-write scope remains reusable");
    assert_eq!(daemon.pending_counts(), (0, 0, 0));
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

/// The daemon's terminal rows carry the id under two keys.
///
/// `inventory_item` (ws_protocol.py) emits `terminal_id`, and `_row_json`
/// (routes/terminals.py) then adds `id` with the same value, so every real
/// `/api/terminals` and `/api/terminals/{id}` row arrives with both. Decoding
/// must accept that row and take the canonical `terminal_id`.
#[test]
fn terminal_row_decodes_the_daemons_dual_keyed_row() {
    let row: gobby_client::daemon::TerminalRow = serde_json::from_value(json!({
        "terminal_id": "terminal-1",
        "id": "terminal-1",
        "backend": "native",
        "state": "live"
    }))
    .expect("a real daemon row must decode");

    assert_eq!(row.id(), "terminal-1");
}
