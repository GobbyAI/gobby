//! 3.3.3 attention respond through the daemon API.

use gobby_client::Workspace;
use serde_json::json;

#[test]
fn respond_via_daemon_api() {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:1",
            "kind": "blocked",
            "attention": {
                "attention_id": "att-1",
                "fingerprint": "fp-1",
                "prompt": {"options": ["approve", "deny"]}
            }
        }]
    }));
    ws.reconcile_subscribe_first().unwrap();
    ws.respond(
        "run:1",
        json!({
            "attention_id": "att-1",
            "fingerprint": "fp-1",
            "answer": {"option": 1}
        }),
    )
    .expect("respond");
    assert!(
        ws.daemon()
            .rest_paths()
            .iter()
            .any(|p| p == "POST /api/attention/run:1/respond"),
        "respond must hit daemon API, not a PTY keystroke: {:?}",
        ws.daemon().rest_paths()
    );
    assert!(!ws
        .daemon()
        .ws_sent_types()
        .contains(&"terminal_input".into()));

    ws.daemon_mut().set_respond_status(409, "stale-episode");
    let err = ws
        .respond(
            "run:1",
            json!({
                "attention_id": "att-old",
                "fingerprint": "fp-stale",
                "answer": {"option": 0}
            }),
        )
        .unwrap_err();
    assert_eq!(err.status(), 409);
    assert!(
        err.to_string().contains("stale-episode") || err.code() == "stale-episode",
        "{err}"
    );
}
