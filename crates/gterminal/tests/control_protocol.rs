//! Control-socket authentication and typed refusal (plan 3.1.10).

#![cfg(unix)]

mod host_support;

use host_support::{
    connect, recv_json, send_json, spawn_host, wait_exit, wait_socket, write_token, CONTROL_SOCKET,
};
use serde_json::json;
use std::io::Read;
use std::time::Duration;

const LOCAL_CLI_TOKEN: &str = "local-cli-token-must-not-authenticate-control";

#[test]
fn hello_required_before_any_verb() {
    let dir = tempfile::tempdir().expect("tempdir");
    let token = "control-token-hello-required";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    wait_socket(&control);

    {
        let mut stream = connect(&control);
        send_json(&mut stream, &json!({"method": "ping"}));
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unauthenticated");
        let mut buf = [0u8; 8];
        let n = stream.read(&mut buf).unwrap_or(0);
        assert_eq!(n, 0, "unauthenticated connection must close");
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 1,
                "control_token": "wrong-token",
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "invalid_token");
        assert!(reply.get("terminals").is_none());
        assert!(reply.get("host_epoch").is_none());
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 999,
                "control_token": token,
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unsupported_protocol");
        assert!(reply.get("terminals").is_none());
    }

    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({
                "method": "hello",
                "protocol_version": 1,
                "control_token": LOCAL_CLI_TOKEN,
            }),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "invalid_token");
    }

    let mut stream = connect(&control);
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    let hello = recv_json(&mut stream);
    assert_eq!(hello["ok"], true);
    assert_eq!(hello["protocol_version"], 1);
    assert!(hello["host_epoch"].as_str().unwrap().len() > 8);
    assert!(hello["version"].as_str().is_some());

    send_json(&mut stream, &json!({"method": "ping"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], hello["host_epoch"]);
    assert!(ping["host_pid"].as_u64().unwrap() > 0);

    send_json(&mut stream, &json!({"method": "list"}));
    let list = recv_json(&mut stream);
    assert_eq!(list["ok"], true);
    assert_eq!(list["terminals"], json!([]));

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut stream);
    assert_eq!(shutdown["ok"], true);
    assert_eq!(shutdown["accepted"], true);
    assert_eq!(shutdown["draining"], true);

    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after host_shutdown"
    );
}
