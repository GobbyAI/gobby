#![cfg(unix)]

mod common;

use anyhow::{Context, Result};
use axum::{
    body::Body,
    http::{Request, Response, StatusCode},
};
use common::{TIMEOUT, channel::*};
use futures_util::{SinkExt, StreamExt};
use gobby_daemon::{front_door::FrontDoor, node_commands::CONTRACT_VERSION, nodes::NativeRoutes};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tokio::{
    task::JoinHandle,
    time::{Duration, timeout},
};
use tokio_tungstenite::tungstenite::Message;
use uuid::Uuid;

fn path(machine: Uuid) -> String {
    format!("/api/internal/nodes/{machine}/commands")
}

fn cancel() -> Value {
    json!({"command":"agent.cancel","payload":{"run_id":"run","terminal_id":"term",
        "signal":"TERM","close_terminal":true},"timeout_ms":1000})
}

fn request(path: &str, secret: Option<&str>, body: Option<Value>) -> Request<Body> {
    let mut request =
        Request::builder()
            .uri(path)
            .method(if body.is_some() { "POST" } else { "GET" });
    if let Some(secret) = secret {
        request = request.header("X-Gobby-Backend-Secret", secret);
    }
    request
        .body(body.map_or_else(Body::empty, |body| Body::from(body.to_string())))
        .unwrap()
}

async fn response(response: Response<Body>, expected: StatusCode) -> Value {
    assert_eq!(response.status(), expected);
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

fn call(door: &FrontDoor, body: Value, machine: Uuid) -> JoinHandle<Response<Body>> {
    let door = door.clone();
    tokio::spawn(async move {
        door.handle(
            request(&path(machine), Some("test-secret"), Some(body)),
            "127.0.0.1:1234".parse().unwrap(),
            false,
        )
        .await
    })
}

async fn live(db: &ChannelDb) -> Result<(ChannelHub, ChannelPeer)> {
    let hub = channel_hub(db, channel_timing()).await?;
    let mut peer = open_channel(hub.addr).await?;
    hello(&mut peer, "internal-route-test").await?;
    ack(&mut peer, db.machine).await?;
    Ok((hub, peer))
}

// If HTTP completes early, assert its contract before waiting for a frame. RED
// therefore reaches an application assertion, rather than a fixture timeout.
async fn command(peer: &mut ChannelPeer, http: &mut JoinHandle<Response<Body>>) -> Value {
    timeout(TIMEOUT, async {
        loop {
            tokio::select! {
                result = &mut *http => {
                    let status = result.unwrap().status();
                    assert_eq!(status, StatusCode::OK, "command must reach the node before HTTP completes");
                    panic!("HTTP completed before node replied");
                }
                frame = peer.next() => match frame.unwrap().unwrap() {
                    Message::Text(text) => return serde_json::from_str(&text).unwrap(),
                    Message::Ping(data) => peer.send(Message::Pong(data)).await.unwrap(),
                    other => panic!("expected command, got {other:?}"),
                }
            }
        }
    }).await.expect("command frame")
}

fn reply(frame: &Value) -> Value {
    json!({"type":"reply","v":CONTRACT_VERSION,"request_id":frame["request_id"],
        "ok":false,"error":{"code":"test_refusal","message":"node owns execution admission","stage":"none"}})
}

async fn no_frame(peer: &mut ChannelPeer) {
    assert!(
        timeout(Duration::from_millis(30), peer.next())
            .await
            .is_err(),
        "refused request sent a frame"
    );
}

#[tokio::test]
async fn command_round_trips() -> Result<()> {
    channel_database(async |db| {
        let (hub, mut peer) = live(db).await?;
        let corpus: Value =
            serde_json::from_str(include_str!("fixtures/node_commands/corpus.json"))?;
        let mut ids = std::collections::HashSet::new();
        for case in corpus["messages"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|case| case["kind"] == "command")
        {
            let original: Value = serde_json::from_str(case["json"].as_str().unwrap())?;
            let body = json!({"command":original["command"],"payload":original["payload"]});
            let mut http = call(&hub.door, body, db.machine);
            let frame = command(&mut peer, &mut http).await;
            assert_eq!(frame["type"], "command");
            assert_eq!(frame["v"], CONTRACT_VERSION);
            assert_eq!(frame["command"], original["command"]);
            assert_eq!(frame["payload"], original["payload"]);
            let id = Uuid::parse_str(frame["request_id"].as_str().unwrap())?;
            assert!(ids.insert(id));
            let mut answer = reply(&frame);
            // The hub handler, not the transport, owns unsupported-version replies.
            answer["v"] = json!(CONTRACT_VERSION + 1);
            peer.send(Message::Text(answer.to_string().into())).await?;
            assert_eq!(
                response(timeout(TIMEOUT, http).await??, StatusCode::OK).await,
                answer
            );
        }
        Ok(())
    })
    .await
}

#[tokio::test]
async fn offline_and_timeout() -> Result<()> {
    channel_database(async |db| {
        let hub = channel_hub(db, channel_timing()).await?;
        let answer = response(call(&hub.door, cancel(), db.machine).await?, StatusCode::OK).await;
        assert_eq!(answer["error"]["code"], "node_offline");
        assert_eq!(answer["error"]["stage"], "none");
        let mut peer = open_channel(hub.addr).await?;
        hello(&mut peer, "test").await?;
        ack(&mut peer, db.machine).await?;
        let mut body = cancel();
        body["timeout_ms"] = json!(40);
        let mut http = call(&hub.door, body, db.machine);
        let frame = command(&mut peer, &mut http).await;
        let answer = response(timeout(TIMEOUT, http).await??, StatusCode::OK).await;
        assert_eq!(answer["request_id"], frame["request_id"]);
        assert_eq!(answer["error"]["code"], "node_timeout");
        assert!(answer["error"]["stage"].is_null());
        Ok(())
    })
    .await
}

#[tokio::test]
async fn pending_commands_end_with_channel() -> Result<()> {
    channel_database(async |db| {
        let (hub, mut peer) = live(db).await?;
        let mut waiting = Vec::new();
        for _ in 0..3 {
            let mut http = call(&hub.door, cancel(), db.machine);
            let frame = command(&mut peer, &mut http).await;
            waiting.push((http, frame));
        }
        let mut replacement = open_channel(hub.addr).await?;
        hello(&mut replacement, "replacement").await?;
        ack(&mut replacement, db.machine).await?;
        for (http, frame) in waiting {
            let answer = response(timeout(TIMEOUT, http).await??, StatusCode::OK).await;
            assert_eq!(answer["request_id"], frame["request_id"]);
            assert_eq!(answer["error"]["code"], "channel_closed");
            assert!(answer["error"]["stage"].is_null());
        }
        for dropped in [false, true] {
            let mut body = cancel();
            body["timeout_ms"] = json!(40);
            let mut http = call(&hub.door, body, db.machine);
            let expired = command(&mut replacement, &mut http).await;
            if dropped {
                http.abort();
                assert!(http.await.unwrap_err().is_cancelled());
            } else {
                let answer = response(timeout(TIMEOUT, http).await??, StatusCode::OK).await;
                assert_eq!(answer["error"]["code"], "node_timeout");
            }
            // A late reply must not satisfy or kill the next command.
            let mut next = call(&hub.door, cancel(), db.machine);
            let fresh = command(&mut replacement, &mut next).await;
            replacement
                .send(Message::Text(reply(&expired).to_string().into()))
                .await?;
            assert!(timeout(Duration::from_millis(20), &mut next).await.is_err());
            let answer = reply(&fresh);
            replacement
                .send(Message::Text(answer.to_string().into()))
                .await?;
            assert_eq!(
                response(timeout(TIMEOUT, next).await??, StatusCode::OK).await,
                answer
            );
        }
        let mut http = call(&hub.door, cancel(), db.machine);
        let frame = command(&mut replacement, &mut http).await;
        replacement.close(None).await?;
        let answer = response(timeout(TIMEOUT, http).await??, StatusCode::OK).await;
        assert_eq!(answer["request_id"], frame["request_id"]);
        assert_eq!(answer["error"]["code"], "channel_closed");
        Ok(())
    })
    .await
}

#[tokio::test]
async fn admission_is_loopback_and_secret() -> Result<()> {
    channel_database(async |db| {
        let (hub, mut peer) = live(db).await?;
        for (remote, secret) in [
            ("192.0.2.1:1", Some("test-secret")),
            ("127.0.0.1:1", None),
            ("127.0.0.1:1", Some("wrong")),
            ("127.0.0.1:1", Some("test-secreu")),
            ("127.0.0.1:1", Some("test-secrett")),
        ] {
            for path in [
                path(db.machine),
                "/api/internal/nodes/not-a-uuid/commands".into(),
                "/api/internal/nodes/connected".into(),
            ] {
                let body = if path.ends_with("connected") {
                    None
                } else {
                    Some(cancel())
                };
                let answer = hub
                    .door
                    .handle(request(&path, secret, body), remote.parse()?, false)
                    .await;
                assert_eq!(answer.status(), StatusCode::NOT_FOUND);
                no_frame(&mut peer).await;
            }
        }
        let standalone =
            channel_door(db, channel_timing(), Some(NativeRoutes::Standalone(None))).await?;
        let node = node("http://127.0.0.1:1", None, false).await;
        // Real HTTP proves the node refuses locally instead of forwarding upstream.
        for addr in [standalone.addr, node.addr] {
            let client =
                hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new())
                    .build_http::<Body>();
            for suffix in [path(db.machine), "/api/internal/nodes/connected".into()] {
                let body = if suffix.ends_with("connected") {
                    None
                } else {
                    Some(cancel())
                };
                let mut req = request(&suffix, Some("test-secret"), body);
                *req.uri_mut() = format!("http://{addr}{suffix}").parse()?;
                assert_eq!(client.request(req).await?.status(), StatusCode::NOT_FOUND);
            }
        }
        no_frame(&mut peer).await;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn malformed_requests_send_nothing() -> Result<()> {
    channel_database(async |db| {
        let (hub, mut peer) = live(db).await?;
        let valid = cancel();
        let mut invalid = vec![
            json!({}),
            json!(null),
            json!([]),
            json!({"command":1,"payload":{}}),
        ];
        for field in ["command", "payload"] {
            let mut body = valid.clone();
            body.as_object_mut().unwrap().remove(field);
            invalid.push(body);
        }
        for value in [json!(null), json!([]), json!("text"), json!(1)] {
            let mut body = valid.clone();
            body["payload"] = value;
            invalid.push(body);
        }
        for value in [
            json!(0),
            json!(-1),
            json!(600001),
            json!(1.5),
            json!("100"),
            json!(null),
            json!(true),
        ] {
            let mut body = valid.clone();
            body["timeout_ms"] = value;
            invalid.push(body);
        }
        let mut body = valid.clone();
        body["extra"] = json!(true);
        invalid.push(body);
        let mut body = valid.clone();
        body["command"] = json!("unknown");
        invalid.push(body);
        // Strict field types and required nullable fields follow the canonical
        // command parser for every command variant, including nested payloads.
        let corpus: Value =
            serde_json::from_str(include_str!("fixtures/node_commands/corpus.json"))?;
        let mut per_command = std::collections::BTreeMap::<String, usize>::new();
        for case in corpus["messages"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["kind"] == "command")
        {
            let original: Value = serde_json::from_str(case["json"].as_str().unwrap())?;
            let name = original["command"].as_str().unwrap().to_owned();
            for field in original["payload"].as_object().unwrap().keys() {
                let mut missing = original.clone();
                missing["payload"].as_object_mut().unwrap().remove(field);
                if serde_json::from_value::<gobby_daemon::node_commands::CommandFrame>(
                    missing.clone(),
                )
                .is_err()
                {
                    invalid
                        .push(json!({"command":missing["command"],"payload":missing["payload"]}));
                    *per_command.entry(name.clone()).or_default() += 1;
                }
                let mut wrong = original.clone();
                wrong["payload"][field] = json!({"invalid":true});
                if serde_json::from_value::<gobby_daemon::node_commands::CommandFrame>(
                    wrong.clone(),
                )
                .is_err()
                {
                    invalid.push(json!({"command":wrong["command"],"payload":wrong["payload"]}));
                    *per_command.entry(name.clone()).or_default() += 1;
                }
            }
        }
        // Every command the canonical parser dispatches (frame_dispatch.rs
        // `command_frame`) must contribute at least one malformed case, so an
        // empty or silently skipped case set fails instead of passing vacuously.
        let expected = [
            "agent.spawn",
            "agent.cancel",
            "terminal.write",
            "workspace.merge",
            "workspace.remove",
            "checkout.status",
            "checkout.read",
        ];
        for command in expected {
            assert!(
                per_command.get(command).is_some_and(|n| *n > 0),
                "no malformed corpus case ran for {command}: {per_command:?}"
            );
        }
        assert_eq!(
            per_command.keys().map(String::as_str).collect::<Vec<_>>(),
            {
                let mut sorted = expected.to_vec();
                sorted.sort_unstable();
                sorted
            },
            "corpus commands drifted from the canonical command set"
        );
        for body in invalid {
            let answer = response(
                call(&hub.door, body.clone(), db.machine).await?,
                StatusCode::BAD_REQUEST,
            )
            .await;
            assert_eq!(answer["error"], "invalid_request", "{body}");
        }
        let answer = hub
            .door
            .handle(
                request(
                    "/api/internal/nodes/not-a-uuid/commands",
                    Some("test-secret"),
                    Some(valid),
                ),
                "127.0.0.1:1".parse()?,
                false,
            )
            .await;
        assert_eq!(
            response(answer, StatusCode::BAD_REQUEST).await["error"],
            "invalid_request"
        );
        no_frame(&mut peer).await;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn connected_lists_live_channels() -> Result<()> {
    channel_database(async |db| {
        let hub = channel_hub(db, channel_timing()).await?;
        let (pending, _close, _commands) = hub.nodes.registry.register(Uuid::nil());
        let get = || request("/api/internal/nodes/connected", Some("test-secret"), None);
        let answer = response(
            hub.door.handle(get(), "[::1]:1".parse()?, false).await,
            StatusCode::OK,
        )
        .await;
        assert_eq!(answer, json!({"machine_ids":[]}));
        let mut peer = open_channel(hub.addr).await?;
        hello(&mut peer, "test").await?;
        ack(&mut peer, db.machine).await?;
        let answer = response(
            hub.door
                .handle(get(), "[::ffff:127.0.0.1]:1".parse()?, false)
                .await,
            StatusCode::OK,
        )
        .await;
        assert_eq!(answer, json!({"machine_ids":[db.machine]}));
        hub.nodes.registry.remove(Uuid::nil(), pending);
        peer.close(None).await?;
        timeout(TIMEOUT, async {
            while hub.nodes.registry.is_connected(db.machine) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .context("channel removal")?;
        let answer = response(
            hub.door.handle(get(), "127.0.0.1:1".parse()?, false).await,
            StatusCode::OK,
        )
        .await;
        assert_eq!(answer, json!({"machine_ids":[]}));
        Ok(())
    })
    .await
}
