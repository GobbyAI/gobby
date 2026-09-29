//! The terminal WS golden corpus replays through the front door byte for byte.

mod common;

use std::path::PathBuf;

use common::{TIMEOUT, frame, read_exact_into, start_front_door, ws_backend, ws_client};
use tokio::io::AsyncWriteExt;

const TEXT_FIN: u8 = 0x81;

fn corpus() -> Vec<(String, Vec<u8>)> {
    let dir =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/terminal_ws_golden");
    let manifest: serde_json::Value = serde_json::from_slice(
        &std::fs::read(dir.join("manifest.json")).expect("read golden manifest"),
    )
    .expect("parse golden manifest");
    manifest["fixtures"]
        .as_array()
        .expect("fixtures array")
        .iter()
        .map(|name| {
            let name = name.as_str().expect("fixture name").to_owned();
            let bytes = std::fs::read(dir.join(&name)).expect("read fixture");
            (name, bytes)
        })
        .collect()
}

#[tokio::test]
async fn corpus_replays_byte_equal_through_proxy() {
    let corpus = corpus();
    assert!(!corpus.is_empty(), "golden manifest lists no fixtures");
    let client_frames: Vec<u8> = corpus
        .iter()
        .flat_map(|(_, payload)| frame(TEXT_FIN, payload, true))
        .collect();
    let backend_frames: Vec<u8> = corpus
        .iter()
        .flat_map(|(_, payload)| frame(TEXT_FIN, payload, false))
        .collect();

    let (backend, captured) = ws_backend(client_frames.len(), backend_frames.clone()).await;
    let front_door = start_front_door(backend).await;
    let (mut stream, head, mut received) = ws_client(front_door, "/api/terminals/ws").await;
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");

    stream.write_all(&client_frames).await.expect("send corpus");
    tokio::time::timeout(
        TIMEOUT,
        read_exact_into(&mut stream, &mut received, backend_frames.len()),
    )
    .await
    .expect("corpus echo timed out");
    let capture = tokio::time::timeout(TIMEOUT, captured)
        .await
        .expect("backend capture timed out")
        .expect("backend capture");

    assert!(
        capture.received == client_frames,
        "client-to-backend leg changed the corpus bytes"
    );
    assert!(
        received == backend_frames,
        "backend-to-client leg changed the corpus bytes"
    );
}
