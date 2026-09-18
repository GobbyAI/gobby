//! 3.3.1 subscribe-first attention handshake.

mod mock_daemon;

use gobby_client::app::run_loop::{ReconnectAttempt, ReconnectSupervisor};
use gobby_client::daemon::{Daemon, DaemonError, DaemonEvent, LiveDaemon};
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::{timeout, Instant};

async fn wait_disconnected(daemon: &LiveDaemon) {
    timeout(Duration::from_secs(1), async {
        while daemon.ready() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("disconnect observed");
}

async fn send_event_and_observe(mock: &MockDaemon, daemon: &LiveDaemon, event: Value) {
    let (_, mut observed) = daemon.subscribe();
    mock.send_event_and_wait(event).await;
    timeout(Duration::from_secs(1), observed.recv())
        .await
        .expect("daemon event delivery")
        .expect("daemon event");
}

#[test]
fn subscribe_first_no_regression() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().push_attention_event(json!({
        "type": "attention",
        "epoch": "e1",
        "seq": 4,
        "entry_id": "run:early",
        "kind": "blocked"
    }));
    ws.daemon_mut().push_attention_event(json!({
        "type": "attention",
        "epoch": "e1",
        "seq": 6,
        "entry_id": "run:late",
        "kind": "blocked"
    }));
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 5,
        "entries": [{"entry_id": "run:roster", "kind": "idle"}]
    }));

    ws.reconcile_subscribe_first().expect("reconcile");

    let calls = ws.daemon().rest_paths();
    assert_eq!(calls.first().map(String::as_str), Some("WS subscribe"));
    assert!(
        calls.iter().any(|c| c == "GET /api/attention/roster"),
        "roster after subscribe: {calls:?}"
    );
    assert_eq!(calls[0], "WS subscribe");
    assert!(calls[1].starts_with("GET /api/attention/roster"));

    let ids: Vec<_> = ws.attention_entry_ids();
    assert!(
        !ids.contains(&"run:early".to_string()),
        "seq 4 same-epoch must be discarded, got {ids:?}"
    );
    assert!(ids.contains(&"run:roster".to_string()));
    assert!(ids.contains(&"run:late".to_string()));
    assert_eq!(ws.attention_applied_seqs(), vec![5, 6]);

    ws.daemon_mut().push_attention_event(json!({
        "type": "attention",
        "epoch": "e2",
        "seq": 1,
        "entry_id": "run:epoch2",
        "kind": "blocked"
    }));
    ws.daemon_mut().set_roster(json!({
        "epoch": "e2",
        "seq": 1,
        "entries": [{"entry_id": "run:epoch2", "kind": "blocked"}]
    }));
    ws.apply_ws(&json!({
        "type": "attention",
        "epoch": "e2",
        "seq": 1,
        "entry_id": "run:epoch2",
        "kind": "blocked"
    }))
    .expect("epoch change");
    assert!(
        ws.daemon()
            .rest_paths()
            .iter()
            .filter(|c| c.as_str() == "GET /api/attention/roster")
            .count()
            >= 2,
        "epoch change must refetch roster"
    );
    assert_eq!(ws.attention_epoch(), "e2");
}

#[tokio::test]
async fn replay_never_rewinds_applied_state() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue_with_event(
        "GET",
        "/api/terminals?",
        json!({
            "items": [{"id": "terminal-1", "terminal_id": "terminal-1", "state": "live"}],
            "next_cursor": "cursor-1",
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 5}
        }),
        json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-2",
            "daemon_epoch": "epoch-1",
            "seq": 6,
            "timestamp": "2026-01-01T00:00:00Z"
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-1", "terminal_id": "terminal-1", "state": "live"}],
            "next_cursor": null
        }),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 3, "entries": []}),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconciliation");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["terminal-1".to_string(), "terminal-2".to_string()]
    );

    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-3",
            "daemon_epoch": "epoch-1",
            "seq": 8,
            "timestamp": "2026-01-01T00:00:01Z"
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("advance live high-water before reconnect");
    assert!(workspace
        .roster_terminal_ids()
        .contains(&"terminal-3".to_string()));

    mock.enqueue_with_events(
        "GET",
        "/api/terminals?",
        json!({
            "items": [
                {"id": "terminal-1", "terminal_id": "terminal-1", "state": "live"},
                {"id": "terminal-2", "terminal_id": "terminal-2", "state": "live"},
                {"id": "terminal-3", "terminal_id": "terminal-3", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 8}
        }),
        vec![
            json!({
                "type": "terminal_event",
                "event": "exited",
                "terminal_id": "terminal-3",
                "daemon_epoch": "epoch-1",
                "seq": 7,
                "timestamp": "2026-01-01T00:00:02Z"
            }),
            json!({
                "type": "terminal_event",
                "event": "created",
                "terminal_id": "terminal-4",
                "daemon_epoch": "epoch-1",
                "seq": 9,
                "timestamp": "2026-01-01T00:00:03Z"
            }),
        ],
    );
    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    let mut supervisor = ReconnectSupervisor::new();
    drop(supervisor.request(
        daemon.generation(),
        &DaemonError::Unavailable { retry_after: None },
    ));
    let generation = match supervisor.attempt_when_due(&daemon).await {
        ReconnectAttempt::Reconnected(generation) => generation,
        outcome => panic!("transport reconnect failed: {outcome:?}"),
    };
    workspace
        .reconnect_daemon_ws()
        .await
        .expect("reconnect and replay buffered events");
    supervisor.handshake_complete(generation);
    assert!(
        workspace
            .roster_terminal_ids()
            .contains(&"terminal-3".to_string()),
        "seq below the applied high-water cannot rewind state"
    );
    assert!(
        workspace
            .roster_terminal_ids()
            .contains(&"terminal-4".to_string()),
        "seq above the reconnect pin is replayed"
    );

    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-new-epoch", "terminal_id": "terminal-new-epoch", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-2", "seq": 1}
        }),
    );
    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-new-epoch",
            "daemon_epoch": "epoch-2",
            "seq": 1,
            "timestamp": "2026-01-01T00:00:04Z"
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("epoch change resets high-water and refetches");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["terminal-new-epoch".to_string()]
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn lagged_subscriber_relists_and_converges() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-initial", "terminal_id": "terminal-initial"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 0}
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
        .expect("connect");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial reconcile");

    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"id": "terminal-recovered", "terminal_id": "terminal-recovered"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1025}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 0, "entries": []}),
    );
    let (_, mut lag_probe) = daemon.subscribe();
    let (_, mut monitor) = daemon.subscribe();
    let monitor_task = tokio::spawn(async move {
        loop {
            match monitor.recv().await {
                Ok(DaemonEvent::Terminal { seq: 1025, .. }) => break,
                Ok(_) | Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                Err(tokio::sync::broadcast::error::RecvError::Closed) => panic!("events closed"),
            }
        }
    });
    for seq in 1..=1025 {
        mock.send_event(json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": format!("terminal-{seq}"),
            "daemon_epoch": "epoch-1",
            "seq": seq,
            "timestamp": "2026-01-01T00:00:00Z"
        }));
    }
    timeout(Duration::from_secs(2), monitor_task)
        .await
        .expect("reader processed burst")
        .expect("monitor task");
    assert!(matches!(
        lag_probe
            .recv()
            .await
            .expect("lag is delivered as a semantic daemon event"),
        DaemonEvent::Lagged
    ));
    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    let disconnected = daemon.subscribe().0;
    assert!(!disconnected.ready);
    assert_eq!(
        disconnected.last_error,
        Some(gobby_client::daemon::DaemonError::Unavailable { retry_after: None })
    );
    let activity_before_recovery = mock.activity().len();
    let mut supervisor = ReconnectSupervisor::new();
    drop(supervisor.request(
        disconnected.generation,
        &DaemonError::Unavailable { retry_after: None },
    ));
    let generation = match supervisor.attempt_when_due(&daemon).await {
        ReconnectAttempt::Reconnected(generation) => generation,
        outcome => panic!("transport reconnect failed: {outcome:?}"),
    };
    workspace.drain_live_events().await.expect("lag recovery");
    supervisor.handshake_complete(generation);
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["terminal-recovered".to_string()]
    );
    assert!(workspace.daemon_ready());
    assert_eq!(mock.websocket_handshakes(), 2);
    let recovery_activity = mock.activity();
    let recovery_activity = &recovery_activity[activity_before_recovery..];
    let connected = recovery_activity
        .iter()
        .position(|entry| entry == "WS connected")
        .expect("one recovery connection");
    let relisted = recovery_activity
        .iter()
        .position(|entry| entry.starts_with("GET /api/terminals?"))
        .expect("one recovery listing");
    assert!(connected < relisted, "recovery completes before re-listing");
    assert_eq!(
        recovery_activity
            .iter()
            .filter(|entry| entry.as_str() == "WS connected")
            .count(),
        1,
        "the lag triggers one recovery episode"
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn reconnect_reattaches_once_per_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    assert_eq!(mock.websocket_handshakes(), 1);
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-1", "backend": "tmux", "state": "live"},
                {"terminal_id": "terminal-2", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );

    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial reconciliation");
    assert_eq!(workspace.pane_count(), 2);
    let initial_attach_count = mock
        .requests()
        .iter()
        .filter(|request| {
            request.body.as_ref().and_then(|body| body.get("type"))
                == Some(&json!("terminal_attach"))
        })
        .count();
    assert_eq!(initial_attach_count, 2);
    assert_eq!(mock.websocket_handshakes(), 1);
    let old_attachments = ["attachment-1", "attachment-2"];

    let observed = daemon.generation();
    mock.drop_websockets();
    timeout(Duration::from_secs(1), async {
        while daemon.ready() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("disconnect observed");
    // The event subscription is part of the transport handshake, so it is
    // the one request a reconnect is allowed to send.
    let requests_without_subscribe = || {
        mock.requests()
            .iter()
            .filter(|request| {
                request.body.as_ref().and_then(|body| body.get("type")) != Some(&json!("subscribe"))
            })
            .count()
    };
    let requests_before_reconnect = requests_without_subscribe();
    let replacement = daemon
        .reconnect(observed)
        .await
        .expect("transport reconnect");
    let ready = daemon.subscribe().0;
    assert_eq!(ready.generation, replacement);
    assert!(
        ready.ready,
        "late subscribers read generation-ready as a value"
    );
    assert_eq!(
        requests_without_subscribe(),
        requests_before_reconnect,
        "LiveDaemon reconnect performs neither listing nor attachment"
    );
    for (sequence, old_attachment) in old_attachments.iter().enumerate() {
        assert!(matches!(
            daemon
                .send(json!({
                    "type": "terminal_input",
                    "terminal_id": format!("terminal-{}", sequence + 1),
                    "attachment_id": old_attachment,
                    "client_write_seq": sequence,
                    "data": "stale"
                }))
                .await,
            Err(gobby_client::daemon::DaemonError::Protocol { .. })
        ));
    }
    mock.enqueue_with_event(
        "GET",
        "/api/terminals?",
        json!({
            "items": [
                {"terminal_id": "terminal-1", "backend": "tmux", "state": "live"},
                {"terminal_id": "terminal-2", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 10}
        }),
        json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-3",
            "backend": "native",
            "daemon_epoch": "epoch-1",
            "seq": 11,
            "timestamp": "2026-01-01T00:00:00Z"
        }),
    );
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("workspace re-subscribes and re-lists");
    workspace
        .drain_live_events()
        .await
        .expect("drain without duplicate attach");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec![
            "terminal-1".to_string(),
            "terminal-2".to_string(),
            "terminal-3".to_string()
        ],
        "reconnect replay applies only the event above the pinned seq"
    );

    let requests = mock.requests();
    let attachments: Vec<_> = requests
        .iter()
        .filter_map(|request| {
            let body = request.body.as_ref()?;
            (body.get("type") == Some(&json!("terminal_attach"))).then_some(body)
        })
        .collect();
    assert_eq!(attachments.len(), 5);
    assert_eq!(mock.websocket_handshakes(), 2);
    for terminal_id in ["terminal-1", "terminal-2"] {
        assert_eq!(
            attachments
                .iter()
                .filter(|body| body.get("terminal_id") == Some(&json!(terminal_id)))
                .count(),
            2,
            "each shown pane attaches exactly once per generation"
        );
    }
    assert_eq!(
        attachments
            .iter()
            .filter(|body| body.get("terminal_id") == Some(&json!("terminal-3")))
            .count(),
        1
    );

    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-1", "backend": "tmux", "state": "live"},
                {"terminal_id": "terminal-2", "backend": "native", "state": "live"},
                {"terminal_id": "terminal-3", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 11}
        }),
    );
    let mut late_workspace = Workspace::live(daemon.clone());
    late_workspace.select_project("project-1");
    late_workspace
        .reconcile_subscribe_first()
        .await
        .expect("late subscriber reconciles from ready snapshot");
    assert_eq!(late_workspace.pane_count(), 3);
    let late_attachments = mock
        .requests()
        .iter()
        .filter_map(|request| request.body.as_ref())
        .filter(|body| body.get("type") == Some(&json!("terminal_attach")))
        .count();
    assert_eq!(
        late_attachments, 8,
        "late workspace attaches each pane once"
    );
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn typed_lifecycle_events_update_live_panes() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-1", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial reconciliation");
    let pane_id = workspace
        .pane_for_terminal("terminal-1")
        .expect("terminal pane");
    let attachment_id = workspace.pane(pane_id).attachment_id().to_string();

    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "terminal_lease_lost",
            "terminal_id": "terminal-1",
            "attachment_id": attachment_id,
            "lease_generation": 2,
            "daemon_epoch": "epoch-1",
            "seq": 2
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("apply lease loss");
    assert!(workspace.pane(pane_id).is_lease_lost());
    assert!(workspace.pane(pane_id).has_take_back());
    assert_eq!(workspace.pane(pane_id).lease_generation(), 2);

    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "terminal_attachment_finalized",
            "terminal_id": "terminal-1",
            "attachment_id": attachment_id,
            "reason": "server finalized attachment",
            "daemon_epoch": "epoch-1",
            "seq": 3
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("apply attachment finalization");
    assert!(!workspace.pane(pane_id).is_live());

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn disconnect_during_workspace_drain_fails_the_handshake() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial reconciliation");

    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    assert_eq!(
        workspace
            .drain_live_events()
            .await
            .expect_err("a disconnect observed during the drain fails the handshake"),
        gobby_client::daemon::DaemonError::Unavailable { retry_after: None }
    );
    assert!(!workspace.daemon_ready());

    mock.shutdown().await;
}

#[tokio::test]
async fn concurrent_reconnect_is_single_flight() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let first_generation = daemon.generation();

    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    let gate = mock.pause_next_websocket();
    let first = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(first_generation).await })
    };
    timeout(Duration::from_secs(1), async {
        while mock.websocket_handshakes() < 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("replacement handshake started");
    let second = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(first_generation).await })
    };
    tokio::task::yield_now().await;
    gate.notify_one();
    let replacement = first
        .await
        .expect("first reconnect task")
        .expect("first reconnect");
    assert_eq!(
        second
            .await
            .expect("second reconnect task")
            .expect("joined reconnect"),
        replacement
    );
    assert_eq!(mock.websocket_handshakes(), 2);
    assert_eq!(
        daemon
            .reconnect(first_generation)
            .await
            .expect("stale observation sees ready generation"),
        replacement
    );
    assert_eq!(mock.websocket_handshakes(), 2);

    // A stale observation with no live connection and no flight opens a fresh
    // connection instead of replaying the cached error (#22002).
    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    let reopened = daemon
        .reconnect(first_generation)
        .await
        .expect("stale observation reconnects without a live connection");
    assert!(reopened > replacement);
    assert_eq!(mock.websocket_handshakes(), 3);

    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    mock.fail_next_websocket();
    let failed_gate = mock.pause_next_websocket();
    let failed_first = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(reopened).await })
    };
    timeout(Duration::from_secs(1), async {
        while mock.websocket_handshakes() < 4 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("failed handshake started");
    let failed_second = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(first_generation).await })
    };
    tokio::task::yield_now().await;
    failed_gate.notify_one();
    let failed_result = failed_first.await.expect("failed owner task");
    assert_eq!(
        failed_result,
        failed_second.await.expect("failed joiner task")
    );
    assert!(failed_result.is_err());
    assert_eq!(mock.websocket_handshakes(), 4);

    let retried = daemon
        .reconnect(reopened)
        .await
        .expect("later retry succeeds");
    assert!(retried > reopened);
    assert_eq!(mock.websocket_handshakes(), 5);
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn live_attention_subscribe_first_no_regression() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue_with_event(
        "GET",
        "/api/terminals?",
        json!({
            "items": [],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
        json!({
            "type": "agent_event",
            "event": "attention_metadata_changed",
            "epoch": "attention-1",
            "seq": 2,
            "metadata": {"entry_id": "run:before"}
        }),
    );
    mock.enqueue_with_event(
        "GET",
        "/api/attention/roster",
        json!({
            "epoch": "attention-1",
            "seq": 3,
            "entries": [
                {"entry_id": "run:base"},
                {"entry_id": "run:before"}
            ]
        }),
        json!({
            "type": "agent_event",
            "event": "attention_metadata_changed",
            "epoch": "attention-1",
            "seq": 4,
            "metadata": {"entry_id": "run:during"}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let (_, mut during_roster) = daemon.subscribe();
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconciliation");
    let mut handshake_sequences = Vec::new();
    for _ in 0..2 {
        if let DaemonEvent::Attention { seq, .. } =
            timeout(Duration::from_secs(1), during_roster.recv())
                .await
                .expect("attention handshake event deadline")
                .expect("attention handshake event")
        {
            handshake_sequences.push(seq);
        }
    }
    assert_eq!(
        handshake_sequences,
        vec![2, 4],
        "attention broadcasts arrive before and during the roster GET"
    );
    workspace
        .drain_live_events()
        .await
        .expect("apply event sent during roster fetch");
    assert_eq!(
        workspace.attention_entry_ids(),
        vec![
            "run:base".to_string(),
            "run:before".to_string(),
            "run:during".to_string()
        ]
    );
    assert_eq!(workspace.attention_applied_seqs(), vec![3, 4]);

    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "agent_event",
            "event": "attention_metadata_changed",
            "epoch": "attention-1",
            "seq": 5,
            "metadata": {"entry_id": "run:after"}
        }),
    )
    .await;
    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "agent_event",
            "event": "attention_metadata_changed",
            "epoch": "attention-1",
            "seq": 4,
            "metadata": {"entry_id": "run:duplicate"}
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("apply post-roster event once");
    assert_eq!(workspace.attention_applied_seqs(), vec![3, 4, 5]);
    assert!(!workspace
        .attention_entry_ids()
        .contains(&"run:duplicate".to_string()));

    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-2",
            "seq": 2,
            "entries": [{"entry_id": "run:new-epoch"}]
        }),
    );
    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "agent_event",
            "event": "attention_metadata_changed",
            "epoch": "attention-2",
            "seq": 1,
            "metadata": {"entry_id": "run:stale-epoch"}
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("epoch change refetches attention roster");
    assert_eq!(workspace.attention_epoch(), "attention-2");
    assert_eq!(
        workspace.attention_entry_ids(),
        vec!["run:new-epoch".to_string()]
    );
    assert_eq!(workspace.attention_applied_seqs(), vec![2]);
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn buffer_overflow_and_cursor_stale_restart_the_listing() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-partial", "state": "live"}],
            "next_cursor": "stale-cursor",
            "snapshot": {"daemon_epoch": "epoch-old", "seq": 7}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        400,
        json!({"detail": "cursor_stale"}),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-recovered", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-new", "seq": 9}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("cursor-stale traversal restarts");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["terminal-recovered".to_string()]
    );
    let listings: Vec<_> = mock
        .requests()
        .into_iter()
        .filter(|request| request.target.starts_with("/api/terminals?"))
        .collect();
    assert_eq!(listings.len(), 3);
    assert!(listings[1].target.contains("cursor=stale-cursor"));
    assert!(!listings[2].target.contains("cursor="));
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;

    let overflow = MockDaemon::start("local-token").await;
    overflow.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-partial-a", "state": "live"}],
            "next_cursor": "overflow-cursor",
            "snapshot": {"daemon_epoch": "epoch-old", "seq": 7}
        }),
    );
    let buffered_events = (8..=1032)
        .map(|seq| {
            json!({
                "type": "terminal_event",
                "event": "created",
                "terminal_id": format!("terminal-buffered-{seq}"),
                "daemon_epoch": "epoch-old",
                "seq": seq,
                "timestamp": "2026-01-01T00:00:00Z"
            })
        })
        .collect();
    overflow.enqueue_with_events(
        "GET",
        "/api/terminals?",
        json!({
            "items": [{"terminal_id": "terminal-partial-b", "state": "live"}],
            "next_cursor": null
        }),
        buffered_events,
    );
    overflow.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-overflow-recovered", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-new", "seq": 2000}
        }),
    );
    let overflow_daemon = LiveDaemon::connect(overflow.url(), "local-token")
        .await
        .expect("connect overflow daemon");
    overflow.wait_for_websocket().await;
    let mut overflow_workspace = Workspace::live(overflow_daemon.clone());
    overflow_workspace.select_project("project-1");
    overflow_workspace
        .reconcile_subscribe_first()
        .await
        .expect("1,025th buffered lifecycle event restarts listing");
    assert_eq!(
        overflow_workspace.roster_terminal_ids(),
        vec!["terminal-overflow-recovered".to_string()],
        "overflow discards both partial pages and their old pin"
    );
    let overflow_listings: Vec<_> = overflow
        .requests()
        .into_iter()
        .filter(|request| request.target.starts_with("/api/terminals?"))
        .collect();
    assert_eq!(overflow_listings.len(), 3);
    assert!(overflow_listings[1]
        .target
        .contains("cursor=overflow-cursor"));
    assert!(
        !overflow_listings[2].target.contains("cursor="),
        "overflow restarts from a fresh first page and re-pins"
    );
    send_event_and_observe(
        &overflow,
        &overflow_daemon,
        json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-at-recovery-pin",
            "daemon_epoch": "epoch-new",
            "seq": 2000,
            "timestamp": "2026-01-01T00:00:01Z"
        }),
    )
    .await;
    overflow_workspace
        .drain_live_events()
        .await
        .expect("recovery pin remains authoritative");
    assert!(!overflow_workspace
        .roster_terminal_ids()
        .contains(&"terminal-at-recovery-pin".to_string()));
    overflow_daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close overflow daemon");
    overflow.shutdown().await;
}

/// 2.3 roster reorder: the order a drag saved survives the next listing, and
/// a terminal the next page adds joins at the end.
#[tokio::test]
async fn roster_reorder_survives_the_next_page() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a", "terminal-b"]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a", "terminal-b"]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a", "terminal-b", "terminal-c"]),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");

    workspace.fetch_roster().await.expect("first listing");
    assert_eq!(
        workspace.roster_terminal_ids(),
        ["terminal-a", "terminal-b"]
    );
    workspace
        .set_tab_order(&["terminal-b", "terminal-a"])
        .expect("reorder");
    assert_eq!(
        workspace.roster_terminal_ids(),
        ["terminal-b", "terminal-a"]
    );

    workspace.fetch_roster().await.expect("second listing");
    assert_eq!(
        workspace.roster_terminal_ids(),
        ["terminal-b", "terminal-a"],
        "a relisting keeps the saved order"
    );
    workspace.fetch_roster().await.expect("third listing");
    assert_eq!(
        workspace.roster_terminal_ids(),
        ["terminal-b", "terminal-a", "terminal-c"],
        "a new terminal joins at the end"
    );
    mock.shutdown().await;
}

fn terminal_page(ids: &[&str]) -> Value {
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({"id": id, "terminal_id": id, "state": "live"}))
        .collect();
    json!({
        "items": items,
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
    })
}

/// 2.3 roster reorder: the order a drag saved comes back on the next start,
/// and a terminal outside it keeps daemon order after it.
#[tokio::test]
async fn saved_roster_order_restores_on_the_next_start() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a", "terminal-b", "terminal-c"]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-d", "terminal-a", "terminal-b", "terminal-c"]),
    );
    let home = tempfile::tempdir().expect("tempdir");

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut first = Workspace::live(daemon);
    first.set_gobby_home(home.path().to_path_buf());
    first
        .restore_project("project-1")
        .expect("no snapshot yet leaves daemon order in charge");
    first.fetch_roster().await.expect("first listing");
    assert_eq!(
        first.roster_terminal_ids(),
        ["terminal-a", "terminal-b", "terminal-c"]
    );
    first
        .set_tab_order(&["terminal-c", "terminal-a", "terminal-b"])
        .expect("reorder");
    // The order that persists is the tab set's: one tab per pane, in order.
    let mut chrome = Chrome::dark();
    for terminal_id in first.tab_order() {
        let pane = first.pane_for_terminal(&terminal_id).expect("listed pane");
        chrome.open_tab(pane, &terminal_id);
    }
    first
        .persist_workspace(chrome.tabs(), &chrome.viewer)
        .expect("save the tab set");
    drop(first);

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("reconnect");
    let mut second = Workspace::live(daemon);
    second.set_gobby_home(home.path().to_path_buf());
    second
        .restore_project("project-1")
        .expect("restore the saved order");
    second.fetch_roster().await.expect("listing after restart");
    assert_eq!(
        second.roster_terminal_ids(),
        ["terminal-c", "terminal-a", "terminal-b", "terminal-d"],
        "the saved order leads and the new terminal follows in daemon order"
    );
    assert_eq!(second.tab_order(), second.roster_terminal_ids());
    mock.shutdown().await;
}
