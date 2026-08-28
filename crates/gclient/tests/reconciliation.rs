//! 3.3.1 subscribe-first attention handshake.

use gobby_client::Workspace;
use serde_json::json;

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
