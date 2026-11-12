use anyhow::{Context, Result};
use gobby_terminals::control::ControlClient;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::test]
async fn snapshot_preserves_unknown_byte_counts() -> Result<()> {
    let (client, host) = tokio::io::duplex(16_384);
    let script = tokio::spawn(async move {
        let mut host = BufReader::new(host);
        for method in ["hello", "snapshot"] {
            let mut line = String::new();
            host.read_line(&mut line).await?;
            let request: Value = serde_json::from_str(&line)?;
            assert_eq!(request["method"], method);
            let reply = if method == "hello" {
                json!({"id":request["id"],"ok":true,"protocol_version":1,
                    "host_epoch":"epoch","version":"fixture","capabilities":[]})
            } else {
                json!({"id":request["id"],"ok":true,"mode":"text","text":"tail",
                    "truncated":true,"dropped_bytes":null,"total_bytes":null})
            };
            host.get_mut()
                .write_all(format!("{reply}\n").as_bytes())
                .await?;
        }
        Ok::<_, anyhow::Error>(())
    });
    let mut client = ControlClient::new(client);
    client.hello("token").await?;
    let captured = client.snapshot("pane", "text", 128, 1).await?;
    assert_eq!(serde_json::to_value(captured.dropped_bytes)?, Value::Null);
    assert_eq!(serde_json::to_value(captured.total_bytes)?, Value::Null);
    assert_eq!(captured.text, "tail");
    assert!(captured.truncated);
    script.await??;
    Ok(())
}

#[tokio::test]
async fn snapshot_round_trips_and_checks_mode() -> Result<()> {
    let (client, host) = tokio::io::duplex(16_384);
    let script = tokio::spawn(async move {
        let mut host = BufReader::new(host);
        let mut requests = Vec::new();
        for mode in ["text", "ansi"] {
            let mut line = String::new();
            host.read_line(&mut line).await?;
            let hello: Value = serde_json::from_str(&line)?;
            // A single authenticated connection serves both captures.
            let request = if hello["method"] == "hello" {
                let reply = json!({"id":hello["id"],"ok":true,"protocol_version":1,
                    "host_epoch":"snapshot-epoch","version":"fixture","capabilities":[]});
                host.get_mut()
                    .write_all(format!("{reply}\n").as_bytes())
                    .await?;
                line.clear();
                host.read_line(&mut line).await?;
                serde_json::from_str::<Value>(&line)?
            } else {
                hello
            };
            let reply = json!({"id":request["id"],"ok":true,"mode":mode,
                "text":"ready ›\n","truncated":true,"dropped_bytes":19,"total_bytes":29});
            host.get_mut()
                .write_all(format!("{reply}\n").as_bytes())
                .await?;
            requests.push(request);
        }
        Ok::<_, anyhow::Error>(requests)
    });
    let mut client = ControlClient::new(client);
    client.hello("snapshot-token").await?;
    let captured = client.snapshot("host-codex", "text", 262_144, 40).await;
    assert!(
        captured.is_ok(),
        "snapshot must return the host reply: {captured:?}"
    );
    let captured = captured.context("snapshot reply")?;
    assert_eq!(captured.mode, "text");
    assert_eq!(captured.text, "ready ›\n");
    assert!(captured.truncated);
    assert_eq!(captured.dropped_bytes, Some(19));
    assert_eq!(captured.total_bytes, Some(29));
    let mismatch = client.snapshot("host-codex", "text", 8192, 7).await;
    assert!(
        mismatch
            .as_ref()
            .is_err_and(|error| error.to_string().contains("snapshot_mode_mismatch")),
        "mode mismatch must fail closed: {mismatch:?}"
    );
    let requests = script.await??;
    for (request, max_bytes, max_lines) in [(&requests[0], 262_144, 40), (&requests[1], 8192, 7)] {
        let mut actual = request.clone();
        assert!(
            actual
                .as_object_mut()
                .context("request object")?
                .remove("id")
                .is_some()
        );
        assert_eq!(
            actual,
            json!({"method":"snapshot","host_terminal_id":"host-codex",
            "mode":"text","max_bytes":max_bytes,"max_lines":max_lines})
        );
    }
    Ok(())
}
