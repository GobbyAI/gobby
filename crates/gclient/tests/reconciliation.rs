//! 3.3.1 subscribe-first attention handshake.

mod mock_daemon;

use gobby_client::daemon::{Daemon, DaemonEvent, LiveDaemon};
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
            "items": [{"id": "terminal-1", "state": "live"}],
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
            "items": [{"id": "terminal-1", "state": "live"}],
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
            "event": "exited",
            "terminal_id": "terminal-2",
            "daemon_epoch": "epoch-1",
            "seq": 5,
            "timestamp": "2026-01-01T00:00:01Z"
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("discard stale event");
    assert!(workspace
        .roster_terminal_ids()
        .contains(&"terminal-2".to_string()));

    send_event_and_observe(
        &mock,
        &daemon,
        json!({
            "type": "terminal_event",
            "event": "exited",
            "terminal_id": "terminal-2",
            "daemon_epoch": "epoch-1",
            "seq": 7,
            "timestamp": "2026-01-01T00:00:02Z"
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("apply fresh event");
    assert!(!workspace
        .roster_terminal_ids()
        .contains(&"terminal-2".to_string()));
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
            "items": [{"id": "terminal-initial"}],
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
            "items": [{"id": "terminal-recovered"}],
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
    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    workspace.drain_live_events().await.expect("lag recovery");
    assert_eq!(
        workspace.roster_terminal_ids(),
        vec!["terminal-recovered".to_string()]
    );
    assert!(workspace.daemon_ready());
    assert_eq!(mock.websocket_handshakes(), 2);
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn reconnect_reattaches_once_per_pane() {
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    assert_eq!(mock.websocket_handshakes(), 1);
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    for seq in [1, 2] {
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
                "snapshot": {"daemon_epoch": "epoch-1", "seq": seq}
            }),
        );
    }

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

    mock.drop_websockets();
    timeout(Duration::from_secs(1), async {
        while daemon.ready() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("disconnect observed");
    workspace
        .reconnect_daemon_ws()
        .await
        .expect("workspace reconnect");
    workspace
        .drain_live_events()
        .await
        .expect("drain without duplicate attach");

    let requests = mock.requests();
    let attachments: Vec<_> = requests
        .iter()
        .filter_map(|request| {
            let body = request.body.as_ref()?;
            (body.get("type") == Some(&json!("terminal_attach"))).then_some(body)
        })
        .collect();
    assert_eq!(attachments.len(), 4);
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
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
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

    mock.drop_websockets();
    wait_disconnected(&daemon).await;
    assert_eq!(
        daemon.reconnect(first_generation).await,
        Err(gobby_client::daemon::DaemonError::Unavailable { retry_after: None })
    );
    assert_eq!(mock.websocket_handshakes(), 2);

    mock.fail_next_websocket();
    let failed_gate = mock.pause_next_websocket();
    let failed_first = {
        let daemon = daemon.clone();
        tokio::spawn(async move { daemon.reconnect(replacement).await })
    };
    timeout(Duration::from_secs(1), async {
        while mock.websocket_handshakes() < 3 {
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
    assert_eq!(mock.websocket_handshakes(), 3);

    let retried = daemon
        .reconnect(replacement)
        .await
        .expect("later retry succeeds");
    assert!(retried > replacement);
    assert_eq!(mock.websocket_handshakes(), 4);
    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close");
    mock.shutdown().await;
}

#[tokio::test]
async fn live_attention_subscribe_first_no_regression() {
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
    mock.enqueue_with_event(
        "GET",
        "/api/attention/roster",
        json!({
            "epoch": "attention-1",
            "seq": 3,
            "entries": [{"entry_id": "run:base"}]
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
    assert!(matches!(
        timeout(Duration::from_secs(1), during_roster.recv())
            .await
            .expect("event during roster deadline")
            .expect("event during roster"),
        DaemonEvent::Attention { seq: 4, .. }
    ));
    workspace
        .drain_live_events()
        .await
        .expect("apply event sent during roster fetch");
    assert_eq!(
        workspace.attention_entry_ids(),
        vec!["run:base".to_string(), "run:during".to_string()]
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
}
