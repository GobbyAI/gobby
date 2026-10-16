use gobby_daemon::nodes::hooks::ledger::{
    Ledger, PROCESSED_MARKER_PRUNE_MAX_ENTRIES, PROCESSED_MARKER_RETENTION_SECONDS,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::Path, process::Command, time::SystemTime};
use tempfile::TempDir;
use time::{Duration, OffsetDateTime};

#[path = "node_support/mod.rs"]
mod node_support;

use axum::http::{Method, StatusCode};
use gobby_daemon::nodes::hooks::receipts::{release_receipt, sweep_receipts};
use node_support::{NODE_KEY, NodeHome, fake_hub::FakeHub};

const RECEIPTS: &str = "/api/nodes/receipts";
const ACK_ID: &str = "n-1730000000000-ack-envelope";

#[tokio::test]
async fn ingress_forwards_and_finalizes() -> anyhow::Result<()> {
    use axum::{body::Body, http::Request};
    use gobby_daemon::front_door::FrontDoor;

    let hub = FakeHub::start().await?;
    let response = json!({"decision":"allow", "reason":"node hook accepted"});
    hub.script(
        "/api/hooks/execute",
        vec![(StatusCode::OK, response.clone())],
    );
    let node = NodeHome::new(&hub)?;
    let ledger = Ledger::new(node.home.path().join("hooks/inbox"));
    let payload = json!({"hook_type":"pre_tool_use", "response_capability":"hook-response.v1"});
    let request = Request::builder()
        .method(Method::POST)
        .uri("/api/hooks/execute")
        .header("authorization", format!("Bearer {NODE_KEY}"))
        .header("x-gobby-envelope-id", "ingress-envelope")
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&payload)?))?;
    let answer = FrontDoor::node(node.node.clone(), false)
        .handle(request, "127.0.0.1:12345".parse()?, false)
        .await;
    assert_eq!(answer.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(answer.into_body(), 1024 * 1024).await?;
    assert_eq!(bytes.as_ref(), serde_json::to_vec(&response)?);
    let requests = hub.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].method, Method::POST);
    assert_eq!(requests[0].path, "/api/hooks/execute");
    assert_eq!(
        requests[0].headers["x-gobby-envelope-id"],
        "ingress-envelope"
    );
    assert_eq!(
        requests[0].headers["authorization"],
        format!("Bearer {NODE_KEY}")
    );
    assert_eq!(requests[0].body, payload);
    let marker = ledger.marker("ingress-envelope").await?;
    assert!(
        marker.is_some(),
        "successful ingress must finalize its claim"
    );
    let marker = marker.unwrap();
    assert_eq!(marker["status"], "processed");
    assert_eq!(marker["response"], response);
    assert_eq!(marker["hook_type"], "pre_tool_use");
    hub.shutdown().await;
    Ok(())
}

fn hook_request(
    id: Option<&str>,
    capability: &str,
    hook: &str,
) -> axum::http::Request<axum::body::Body> {
    let mut request = axum::http::Request::builder()
        .method(Method::POST)
        .uri("/api/hooks/execute")
        .header("authorization", format!("Bearer {NODE_KEY}"))
        .header("content-type", "application/json");
    if let Some(id) = id {
        request = request.header("x-gobby-envelope-id", id);
    }
    request
        .body(axum::body::Body::from(
            serde_json::to_vec(&json!({
                "hook_type": hook,
                "response_capability": capability,
            }))
            .unwrap(),
        ))
        .unwrap()
}

async fn call_hook(
    node: std::sync::Arc<gobby_daemon::state::NodeServices>,
    request: axum::http::Request<axum::body::Body>,
) -> axum::http::Response<axum::body::Body> {
    gobby_daemon::front_door::FrontDoor::node(node, false)
        .handle(request, "127.0.0.1:12345".parse().unwrap(), false)
        .await
}

#[tokio::test]
async fn ingress_dedupes_retries() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    let response = json!({"decision":"allow"});
    hub.script(
        "/api/hooks/execute",
        vec![(StatusCode::OK, response.clone())],
    );
    let node = NodeHome::new(&hub)?;
    let ledger = node.node.hook_ledger.clone();
    let (entered, release) = hub.pause_next("/api/hooks/execute");
    let forwarding = tokio::spawn(call_hook(
        node.node.clone(),
        hook_request(Some("renewed"), "hook-response.v1", "pre_tool_use"),
    ));
    tokio::time::timeout(std::time::Duration::from_secs(3), entered).await??;
    let initial = ledger.marker("renewed").await?.unwrap();
    tokio::time::pause();
    let started = tokio::time::Instant::now();
    tokio::time::advance(std::time::Duration::from_secs(16)).await;
    assert!(started.elapsed() >= std::time::Duration::from_secs(16));
    tokio::time::resume();
    let renewed = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let marker = ledger.marker("renewed").await?.unwrap();
            if marker["lease_expires_at"].as_str() > initial["lease_expires_at"].as_str() {
                return Ok::<_, std::io::Error>(marker);
            }
            tokio::task::yield_now().await;
        }
    })
    .await;
    assert!(renewed.is_ok(), "lease must renew while the hub is blocked");
    let renewed = renewed.unwrap()?;
    assert!(
        renewed["lease_expires_at"].as_str() > initial["lease_expires_at"].as_str(),
        "lease must move forward before the slow hub returns"
    );
    assert!(!forwarding.is_finished());
    let duplicate = call_hook(
        node.node.clone(),
        hook_request(Some("renewed"), "hook-response.v1", "pre_tool_use"),
    )
    .await;
    assert_eq!(duplicate.status(), StatusCode::CONFLICT);
    let bytes = axum::body::to_bytes(duplicate.into_body(), 1024).await?;
    assert_eq!(
        serde_json::from_slice::<Value>(&bytes)?,
        json!({"status":"processing", "reason":"duplicate envelope already processing"})
    );
    release.send(()).unwrap();
    assert_eq!(forwarding.await?.status(), StatusCode::OK);
    let replay = call_hook(
        node.node.clone(),
        hook_request(Some("renewed"), "hook-response.v1", "pre_tool_use"),
    )
    .await;
    assert_eq!(replay.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(replay.into_body(), 1024).await?;
    assert_eq!(serde_json::from_slice::<Value>(&bytes)?, response);
    assert_eq!(hub.requests().len(), 1, "retries must not reach the hub");
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn ingress_failure_dispositions() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let ledger = node.node.hook_ledger.clone();
    for (id, status, response, processed) in [
        (
            "retry",
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"status":"retry"}),
            false,
        ),
        (
            "busy",
            StatusCode::CONFLICT,
            json!({"status":"processing"}),
            false,
        ),
        (
            "error",
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"error":"failed"}),
            false,
        ),
        (
            "done",
            StatusCode::CONFLICT,
            json!({"status":"processed"}),
            true,
        ),
    ] {
        hub.script("/api/hooks/execute", vec![(status, response.clone())]);
        let answer = call_hook(
            node.node.clone(),
            hook_request(Some(id), "hook-response.v1", "pre_tool_use"),
        )
        .await;
        assert_eq!(answer.status(), status, "response status for {id}");
        let body = axum::body::to_bytes(answer.into_body(), 1024).await?;
        assert_eq!(serde_json::from_slice::<Value>(&body)?, response);
        let marker = ledger.marker(id).await?;
        if processed {
            let marker = marker.expect("terminal hub409 must retain a marker");
            assert_eq!(marker["status"], "processed");
            assert!(marker.get("response").is_none_or(Value::is_null));
        } else {
            assert!(marker.is_none(), "retryable {id} must release its claim");
        }
    }
    assert_eq!(hub.requests().len(), 4, "core must never release a receipt");
    let offline = NodeHome::new(&hub)?;
    hub.shutdown().await;
    let answer = call_hook(
        offline.node.clone(),
        hook_request(Some("offline"), "hook-response.v1", "pre_tool_use"),
    )
    .await;
    assert!(answer.status().is_server_error());
    assert!(offline.node.hook_ledger.marker("offline").await?.is_none());
    Ok(())
}

#[tokio::test]
async fn ingress_sweeps_receipts_first() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    hub.script(
        RECEIPTS,
        vec![(
            StatusCode::OK,
            json!({"acknowledged":true, "carrying_envelope_ids":[]}),
        )],
    );
    hub.script(
        "/api/hooks/execute",
        vec![(StatusCode::OK, json!({"decision":"allow"}))],
    );
    let node = NodeHome::new(&hub)?;
    let inbox = &node.node.hook_ledger.inbox;
    fs::create_dir_all(inbox)?;
    let ack = inbox.join(format!("{ACK_ID}.json"));
    fs::write(&ack, serde_json::to_vec(&receipt())?)?;
    let answer = call_hook(
        node.node.clone(),
        hook_request(Some("after-ack"), "hook-response.v1", "pre_tool_use"),
    )
    .await;
    assert_eq!(answer.status(), StatusCode::OK);
    let requests = hub.requests();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].path, RECEIPTS);
    assert_eq!(requests[0].body["op"], "ack");
    assert_eq!(requests[1].path, "/api/hooks/execute");
    assert!(!ack.exists());
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn ingress_session_start_bumps_epoch() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    let ledger = &node.node.hook_ledger;
    assert!(ledger.read_stop_epoch().await?.is_none());
    for hook in ["session_start", "SessionStart"] {
        let before = OffsetDateTime::now_utc();
        hub.script(
            "/api/hooks/execute",
            vec![(StatusCode::OK, json!({"decision":"allow"}))],
        );
        let answer = call_hook(
            node.node.clone(),
            hook_request(Some(hook), "hook-response.v1", hook),
        )
        .await;
        assert_eq!(answer.status(), StatusCode::OK);
        let epoch = ledger.read_stop_epoch().await?;
        assert!(
            epoch.is_some(),
            "successful session start must bump STOP epoch"
        );
        assert!(epoch.unwrap() >= before);
    }
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn ingress_disconnect_finalizes_and_releases() -> anyhow::Result<()> {
    for drop_during_forward in [true, false] {
        let hub = FakeHub::start().await?;
        let reply = json!({"decision":"allow", "_gobby_delivery_receipt":receipt()});
        hub.script("/api/hooks/execute", vec![(StatusCode::OK, reply.clone())]);
        hub.script(RECEIPTS, vec![(StatusCode::OK, json!({"released":true}))]);
        let (receipt_entered, receipt_release) = hub.pause_next(RECEIPTS);
        let node = NodeHome::new(&hub)?;
        let (entered, release) = hub.pause_next("/api/hooks/execute");
        let handler = tokio::spawn(call_hook(
            node.node.clone(),
            hook_request(Some("disconnected"), "hook-response.v1", "pre_tool_use"),
        ));
        tokio::time::timeout(std::time::Duration::from_secs(3), entered).await??;
        if drop_during_forward {
            handler.abort();
            assert!(handler.await.unwrap_err().is_cancelled());
            release.send(()).unwrap();
        } else {
            release.send(()).unwrap();
            let response = handler.await?;
            assert_eq!(response.status(), StatusCode::OK);
            drop(response);
        }
        tokio::time::timeout(std::time::Duration::from_secs(3), receipt_entered).await??;
        receipt_release.send(()).unwrap();
        let marker = node.node.hook_ledger.marker("disconnected").await?;
        assert_eq!(
            marker.as_ref().map(|m| &m["status"]),
            Some(&json!("processed"))
        );
        assert_eq!(marker.unwrap()["response"], reply);
        let requests = hub.requests();
        assert_eq!(
            requests.len(),
            2,
            "undeliverable response must release its receipt"
        );
        assert_eq!(requests[1].path, RECEIPTS);
        assert_eq!(requests[1].body["op"], "release");
        assert_eq!(requests[1].body["receipt_id"], receipt()["receipt_id"]);
        hub.shutdown().await;
    }
    Ok(())
}

#[tokio::test]
async fn ingress_unledgered_passthrough() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    let node = NodeHome::new(&hub)?;
    for (id, capability) in [
        (None, "hook-response.v1"),
        (Some("legacy"), "hook-response.v0"),
    ] {
        let response = json!({"decision":"allow"});
        hub.script(
            "/api/hooks/execute",
            vec![(StatusCode::OK, response.clone())],
        );
        let request = hook_request(id, capability, "pre_tool_use");
        let answer = call_hook(node.node.clone(), request).await;
        assert_eq!(answer.status(), StatusCode::OK);
        let body = axum::body::to_bytes(answer.into_body(), 1024).await?;
        assert_eq!(serde_json::from_slice::<Value>(&body)?, response);
        let requests = hub.requests();
        let last = requests.last().unwrap();
        assert_eq!(last.body["response_capability"], capability);
        assert_eq!(last.body["hook_type"], "pre_tool_use");
        assert_eq!(last.headers["authorization"], format!("Bearer {NODE_KEY}"));
        assert_eq!(
            last.headers
                .get("x-gobby-envelope-id")
                .map(|h| h.to_str().unwrap()),
            id
        );
    }
    assert!(
        !node.node.hook_ledger.inbox.exists(),
        "passthrough must never claim"
    );
    hub.shutdown().await;
    Ok(())
}

fn receipt() -> Value {
    json!({
        "schema_version": 1,
        "kind": "delivery-receipt",
        "enqueued_at": "2024-10-27T03:33:20Z",
        "receipt_id": "bba98c84-c1bd-4f80-ae57-e400fe516559",
        "original_envelope_id": "original",
        "delivery_generation": 2
    })
}

#[tokio::test]
async fn receipts_ack_forwarded_and_terminalized() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    hub.script(
        RECEIPTS,
        vec![(
            StatusCode::OK,
            json!({"acknowledged":true, "carrying_envelope_ids":["original", "current"]}),
        )],
    );
    let node = NodeHome::new(&hub)?;
    let ledger = Ledger::new(node.home.path().join("hooks/inbox"));
    fs::create_dir_all(&ledger.inbox)?;
    let path = ledger.inbox.join(format!("{ACK_ID}.json"));
    fs::write(&path, serde_json::to_vec(&receipt())?)?;
    let ordinary = ledger.inbox.join("hook.json");
    fs::write(&ordinary, r#"{"kind":"hook"}"#)?;
    let (one, two) = tokio::join!(
        sweep_receipts(&node.node, &ledger),
        sweep_receipts(&node.node, &ledger)
    );
    one?;
    two?;
    let requests = hub.requests();
    assert_eq!(requests.len(), 1, "concurrent sweeps must claim once");
    assert_eq!(requests[0].method, Method::POST);
    assert_eq!(requests[0].path, RECEIPTS);
    assert_eq!(
        requests[0].headers["authorization"],
        format!("Bearer {NODE_KEY}")
    );
    assert_eq!(
        requests[0].body,
        json!({"v":1,"op":"ack","receipt_id":receipt()["receipt_id"],"delivery_generation":2})
    );
    for id in ["original", "current", ACK_ID] {
        assert!(ledger.is_processed(id).await?, "missing marker: {id}");
    }
    assert!(!path.exists());
    assert!(ordinary.exists());
    assert!(fs::read_dir(&ledger.inbox)?.all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".claimed.tmp")
    }));
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn receipts_failed_post_and_orphan_restore() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    hub.script(
        RECEIPTS,
        vec![
            (StatusCode::SERVICE_UNAVAILABLE, json!({"error":"offline"})),
            (
                StatusCode::OK,
                json!({"acknowledged":true,"carrying_envelope_ids":["original"]}),
            ),
        ],
    );
    let node = NodeHome::new(&hub)?;
    let ledger = Ledger::new(node.home.path().join("hooks/inbox"));
    fs::create_dir_all(&ledger.inbox)?;
    let path = ledger.inbox.join(format!("{ACK_ID}.json"));
    let orphan = ledger
        .inbox
        .join(format!("{ACK_ID}.json.deadbeef.claimed.tmp"));
    fs::write(&orphan, serde_json::to_vec(&receipt())?)?;
    sweep_receipts(&node.node, &ledger).await?;
    assert!(!orphan.exists());
    assert!(path.exists(), "failed post restores the ack");
    assert!(!ledger.is_processed(ACK_ID).await?);
    assert_eq!(hub.requests().len(), 1);
    sweep_receipts(&node.node, &ledger).await?;
    assert!(!path.exists());
    assert!(ledger.is_processed(ACK_ID).await?);
    assert!(ledger.is_processed("original").await?);
    assert_eq!(hub.requests().len(), 2);
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn receipts_stale_ack_terminalizes_filename_id() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    hub.script(
        RECEIPTS,
        vec![(
            StatusCode::OK,
            json!({"acknowledged":false,"carrying_envelope_ids":[]}),
        )],
    );
    let node = NodeHome::new(&hub)?;
    let ledger = Ledger::new(node.home.path().join("hooks/inbox"));
    fs::create_dir_all(&ledger.inbox)?;
    let path = ledger.inbox.join(format!("{ACK_ID}.json"));
    fs::write(&path, serde_json::to_vec(&receipt())?)?;
    sweep_receipts(&node.node, &ledger).await?;
    assert!(ledger.is_processed(ACK_ID).await?);
    assert!(!ledger.is_processed("original").await?);
    assert!(!path.exists());
    sweep_receipts(&node.node, &ledger).await?;
    assert_eq!(
        hub.requests().len(),
        1,
        "terminalized stale ack must not repost"
    );
    hub.shutdown().await;
    Ok(())
}

#[tokio::test]
async fn receipts_release_uses_node_key() -> anyhow::Result<()> {
    let hub = FakeHub::start().await?;
    hub.script(RECEIPTS, vec![(StatusCode::OK, json!({"released":true}))]);
    let node = NodeHome::new(&hub)?;
    assert!(release_receipt(&node.node, &receipt()).await?);
    let requests = hub.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0].headers["authorization"],
        format!("Bearer {NODE_KEY}")
    );
    assert_eq!(requests[0].body["op"], "release");
    assert_eq!(requests[0].body["v"], 1);
    assert_eq!(requests[0].body["receipt_id"], receipt()["receipt_id"]);
    assert_eq!(requests[0].body["delivery_generation"], 2);
    hub.shutdown().await;
    Ok(())
}

fn ledger() -> (TempDir, Ledger) {
    let home = TempDir::new().unwrap();
    let ledger = Ledger::new(home.path().join("hooks/inbox"));
    (home, ledger)
}
fn marker_path(ledger: &Ledger, id: &str) -> std::path::PathBuf {
    ledger
        .inbox
        .join("processed")
        .join(format!("{:x}.json", Sha256::digest(id.as_bytes())))
}
fn write_marker(ledger: &Ledger, id: &str, record: Value) {
    fs::create_dir_all(ledger.inbox.join("processed")).unwrap();
    fs::write(marker_path(ledger, id), record.to_string()).unwrap();
}
fn iso(at: OffsetDateTime) -> String {
    at.format(&time::format_description::well_known::Rfc3339)
        .unwrap()
}
#[tokio::test]
async fn ledger_claim_is_exclusive() {
    let (_home, ledger) = ledger();
    let (a, b) = tokio::join!(ledger.claim("exclusive"), ledger.claim("exclusive"));
    let claims = [a.unwrap(), b.unwrap()];
    assert_eq!(claims.iter().filter(|token| token.is_some()).count(), 1);
    let token = claims.into_iter().flatten().next().unwrap();
    let record = ledger.marker("exclusive").await.unwrap().unwrap();
    assert_eq!(record["status"], "processing");
    assert_eq!(record["owner_token"], token);
    assert_eq!(record["owner_pid"], std::process::id());
    assert!(record["owner_create_time"].as_f64().unwrap() > 0.0);
    let claimed = OffsetDateTime::parse(
        record["claimed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    let expiry = OffsetDateTime::parse(
        record["lease_expires_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert_eq!((expiry - claimed).whole_seconds(), 15);
    let raw = fs::read_to_string(marker_path(&ledger, "exclusive")).unwrap();
    assert!(raw.ends_with('\n'));
    assert!(raw.find("claimed_at").unwrap() < raw.find("owner_token").unwrap());
    assert!(ledger.claim("").await.unwrap().is_none());
}
#[tokio::test]
async fn ledger_mutations_require_owner_token() {
    let (_home, ledger) = ledger();
    let token = ledger.claim("owned").await.unwrap().unwrap();
    let initial = ledger.marker("owned").await.unwrap();
    assert!(!ledger.renew("owned", "wrong").await.unwrap());
    assert!(
        !ledger
            .finalize(
                "owned",
                "wrong",
                Some(json!({"decision":"allow"})),
                Some("stop")
            )
            .await
            .unwrap()
    );
    assert!(!ledger.release("owned", "wrong").await.unwrap());
    assert_eq!(ledger.marker("owned").await.unwrap(), initial);
    assert!(ledger.renew("owned", &token).await.unwrap());
    let renewed = ledger.marker("owned").await.unwrap().unwrap();
    let before = OffsetDateTime::parse(
        initial.as_ref().unwrap()["renewed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    let after = OffsetDateTime::parse(
        renewed["renewed_at"].as_str().unwrap(),
        &time::format_description::well_known::Rfc3339,
    )
    .unwrap();
    assert!(after >= before);
    let response = json!({"decision":"allow", "nested":{"ok":true}});
    assert!(
        ledger
            .finalize("owned", &token, Some(response.clone()), Some("stop"))
            .await
            .unwrap()
    );
    assert!(!ledger.release("owned", &token).await.unwrap());
    assert!(ledger.is_processed("owned").await.unwrap());
    ledger.mark_processed("owned", None, None).await.unwrap();
    assert_eq!(
        ledger.terminal_response("owned", None).await.unwrap(),
        Some(response)
    );
    let token = ledger.claim("release").await.unwrap().unwrap();
    ledger.mark_processed("release", None, None).await.unwrap();
    assert!(ledger.release("release", &token).await.unwrap());
    assert!(ledger.claim("release").await.unwrap().is_some());
}
#[tokio::test]
async fn ledger_stale_and_malformed_markers() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    ledger.claim("live").await.unwrap().unwrap();
    let mut live = ledger.marker("live").await.unwrap().unwrap();
    live["lease_expires_at"] = json!(iso(now - Duration::seconds(30)));
    write_marker(&ledger, "live", live.clone());
    assert!(ledger.is_active("live", Some(now)).await.unwrap());
    assert!(!ledger.clear_stale("live", Some(now)).await.unwrap());
    live["owner_create_time"] = json!(0.0);
    write_marker(&ledger, "reused-pid", live.clone());
    assert!(!ledger.is_active("reused-pid", Some(now)).await.unwrap());
    assert!(ledger.clear_stale("reused-pid", Some(now)).await.unwrap());
    live["owner_pid"] = json!(i32::MAX);
    live["lease_expires_at"] = json!(iso(now + Duration::seconds(15)));
    write_marker(&ledger, "lease", live);
    assert!(ledger.is_active("lease", Some(now)).await.unwrap());
    assert!(!ledger.clear_stale("lease", Some(now)).await.unwrap());
    assert!(
        ledger
            .clear_stale("lease", Some(now + Duration::seconds(15)))
            .await
            .unwrap()
    );
    for (name, raw, processed) in [
        ("broken", "{garbage", true),
        ("empty", "  ", false),
        ("array", "[]", false),
    ] {
        fs::write(marker_path(&ledger, name), raw).unwrap();
        assert_eq!(
            ledger.is_processed(name).await.unwrap(),
            processed,
            "{name}"
        );
    }
    assert!(
        !ledger
            .clear_stale("broken", Some(now + Duration::days(1)))
            .await
            .unwrap()
    );
    assert!(
        ledger
            .clear_stale("empty", Some(now + Duration::days(1)))
            .await
            .unwrap()
    );
    write_marker(
        &ledger,
        "legacy",
        json!({"status":"processing","claimed_at":iso(now)}),
    );
    assert!(
        ledger
            .is_active("legacy", Some(now + Duration::seconds(119)))
            .await
            .unwrap()
    );
    assert!(
        ledger
            .clear_stale("legacy", Some(now + Duration::seconds(120)))
            .await
            .unwrap()
    );
}
#[tokio::test]
async fn ledger_stop_block_replay_window() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    let response = json!({"decision":"block","reason":"pending work"});
    for hook in [
        "stop",
        "subagent-stop",
        "subagent_end",
        "session_end",
        " TURN_END ",
        "stop_failure",
    ] {
        write_marker(
            &ledger,
            "stop",
            json!({"status":"processed","processed_at":iso(now),"hook_type":hook,"response":response}),
        );
        assert_eq!(
            ledger
                .terminal_response("stop", Some(now + Duration::seconds(119)))
                .await
                .unwrap(),
            Some(response.clone())
        );
        assert!(
            ledger
                .terminal_response("stop", Some(now + Duration::seconds(120)))
                .await
                .unwrap()
                .is_none()
        );
        assert!(ledger.marker("stop").await.unwrap().is_none());
    }
    ledger.bump_stop_epoch(Some(now)).await.unwrap();
    assert_eq!(ledger.read_stop_epoch().await.unwrap(), Some(now));
    for (id, at, replay) in [
        ("before", now - Duration::seconds(1), false),
        ("equal", now, true),
        ("future", now + Duration::seconds(1), false),
    ] {
        write_marker(
            &ledger,
            id,
            json!({"processed_at":iso(at),"hook_type":"stop","response":response}),
        );
        assert_eq!(
            ledger
                .terminal_response(id, Some(now))
                .await
                .unwrap()
                .is_some(),
            replay
        );
    }
    let legacy = json!({"decision":"deny","reason":"[block-terminal-validation-failure] retry"});
    write_marker(
        &ledger,
        "legacy",
        json!({"processed_at":iso(now - Duration::seconds(121)),"response":legacy}),
    );
    assert!(
        ledger
            .terminal_response("legacy", Some(now))
            .await
            .unwrap()
            .is_none()
    );
    let tool = json!({"decision":"deny","reason":"policy"});
    write_marker(
        &ledger,
        "tool",
        json!({"processed_at":iso(now - Duration::days(2)),"hook_type":"pre_tool_use","response":tool}),
    );
    assert_eq!(
        ledger.terminal_response("tool", Some(now)).await.unwrap(),
        Some(tool)
    );
    let allow = json!({"decision":"allow"});
    write_marker(
        &ledger,
        "allow",
        json!({"hook_type":"stop","response":allow}),
    );
    assert_eq!(
        ledger.terminal_response("allow", Some(now)).await.unwrap(),
        Some(allow)
    );
}
fn age(path: &Path, seconds: u64) {
    fs::File::open(path)
        .unwrap()
        .set_times(
            fs::FileTimes::new()
                .set_modified(SystemTime::now() - std::time::Duration::from_secs(seconds)),
        )
        .unwrap();
}
#[tokio::test]
async fn ledger_prune_retention() {
    let (_home, ledger) = ledger();
    let now = OffsetDateTime::now_utc();
    assert_eq!(PROCESSED_MARKER_PRUNE_MAX_ENTRIES, 100_000);
    assert_eq!(PROCESSED_MARKER_RETENTION_SECONDS, 86_400.0);
    for id in ["old-a", "old-b", "old-c", "fresh"] {
        ledger.mark_processed(id, None, None).await.unwrap();
        age(
            &marker_path(&ledger, id),
            if id == "fresh" { 100 } else { 86_500 },
        );
    }
    let pass = ledger
        .prune(Some(now), PROCESSED_MARKER_RETENTION_SECONDS, 2)
        .await
        .unwrap();
    assert!(pass.truncated);
    assert!(pass.deleted > 0);
    assert!(pass.deleted <= 2);
    let mut remaining = 0;
    for id in ["old-a", "old-b", "old-c"] {
        remaining += usize::from(ledger.is_processed(id).await.unwrap());
    }
    assert_eq!(remaining, 3 - pass.deleted);
    assert!(ledger.is_processed("fresh").await.unwrap());
    let pass2 = ledger
        .prune(
            Some(now),
            PROCESSED_MARKER_RETENTION_SECONDS,
            PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
        )
        .await
        .unwrap();
    assert_eq!(pass.deleted + pass2.deleted, 3);
    assert!(ledger.inbox.join("processed/.locks").is_dir());
    assert!(ledger.is_processed("fresh").await.unwrap());
    for id in ["old-a", "old-b", "old-c"] {
        assert!(!ledger.is_processed(id).await.unwrap());
    }
}
#[tokio::test]
async fn ledger_reads_python_markers() {
    let (_home, ledger) = ledger();
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output = Command::new("uv").args(["run", "--no-sync", "python", "-c", "from pathlib import Path; import sys; from gobby.hooks.envelope_dedupe import mark_envelope_processed; mark_envelope_processed('python', processed_dir=Path(sys.argv[1]), response={'decision':'deny','reason':'oracle','nested':{'count':2}}, hook_type='pre_tool_use')"]).arg(ledger.inbox.join("processed")).current_dir(root).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let record = ledger.marker("python").await.unwrap().unwrap();
    assert_eq!(record["status"], "processed");
    assert_eq!(record["hook_type"], "pre_tool_use");
    assert_eq!(
        ledger.terminal_response("python", None).await.unwrap(),
        Some(json!({"decision":"deny","reason":"oracle","nested":{"count":2}}))
    );
}
#[test]
fn ledger_inbox_freshness() {
    let now = OffsetDateTime::now_utc();
    let millis = now.unix_timestamp_nanos() / 1_000_000;
    assert!(Ledger::is_fresh(
        Path::new(&format!("n-{millis}-id.json")),
        Some(now),
        120.0
    ));
    assert!(!Ledger::is_fresh(
        Path::new(&format!("c-{}-id.json", millis - 120_000)),
        Some(now),
        120.0
    ));
    for name in [
        "n-no-id.json",
        "x-123-id.json",
        "n-123-",
        "n-99999999999999999999999999999-id",
    ] {
        assert!(
            !Ledger::is_fresh(Path::new(name), Some(now), 120.0),
            "{name}"
        );
    }
    assert!(!Ledger::is_fresh(
        Path::new(&format!("n-{}-id.json", millis + 1000)),
        Some(now),
        120.0
    ));
}
