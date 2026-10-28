//! A batch paste uses the child's bracketed-paste mode and preserves every byte.

#![cfg(all(unix, feature = "vt-engine"))]

mod host_support;

use base64::Engine;
use host_support::{
    committed_pane, control, pane_text, rpc, spawn_host, temp_socket_dir, wait_until, write_token,
};
use serde_json::json;

#[test]
fn batch_paste_preserves_long_prompt_and_separate_return() {
    let dir = temp_socket_dir();
    let token = "batch-paste-isolated-token";
    write_token(dir.path(), token);
    let mut host = spawn_host(dir.path());
    let mut stream = control(dir.path(), token);
    let text = format!("HEAD:{}:TAIL", "界".repeat(2001));
    let expected = [b"\x1b[200~".as_slice(), text.as_bytes(), b"\x1b[201~\r"].concat();
    let script = format!(
        "stty raw -echo; printf '\\033[?2004hREADY'; dd bs=1 count={} of=input.bin 2>/dev/null; printf DONE",
        expected.len()
    );
    let pane = committed_pane(&mut host, &mut stream, 1, "batch-paste", &script);
    wait_until("child enabled bracketed paste", || {
        pane_text(&mut stream, &pane.host_terminal_id).contains("READY")
    });
    let response = rpc(
        &mut stream,
        "write_batch",
        json!({
            "operation_seq": 2,
            "targets": [{
                "recipient_id": "seat",
                "host_terminal_id": pane.host_terminal_id,
                "operations": [
                    {"kind": "paste", "encoding": "utf8-b64",
                     "data": base64::engine::general_purpose::STANDARD.encode(text.as_bytes())},
                    {"kind": "key", "encoding": "utf8-b64", "data": "DQ==", "delay_ms": 15},
                ],
            }],
        }),
    );
    assert_eq!(response["ok"], true, "batch reply: {response}");
    assert_eq!(
        response["results"][0]["ok"], true,
        "paste result: {response}"
    );
    let captured = dir.path().join("input.bin");
    wait_until("complete child input", || {
        std::fs::metadata(&captured).is_ok_and(|metadata| metadata.len() == expected.len() as u64)
    });
    assert_eq!(
        std::fs::read(captured).expect("captured PTY input"),
        expected
    );
}
