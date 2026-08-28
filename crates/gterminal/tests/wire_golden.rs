//! Golden wire corpus for gterm frame (bincode) and control (JSON) protocols.

use gobby_terminal::protocol::{
    write_message, CellData, ClientMessage, CursorState, FrameData, PaneLocator, RenderEncoding,
    ServerMessage, TerminalFrame, TmuxClientIdentity, MAX_CELLS, MAX_COLS, MAX_FRAME_SIZE,
    MAX_ROWS, MIN_COLS, MIN_ROWS, PROTOCOL_VERSION, WORST_CELL_BYTES,
};
use std::fs;
use std::io::Cursor;
use std::path::PathBuf;

fn dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/wire_golden")
}

fn write_bin(name: &str, msg: &impl serde::Serialize) -> Vec<u8> {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).unwrap();
    let path = dir().join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let existing = fs::read(&path).unwrap_or_default();
    if existing.is_empty() {
        fs::write(&path, &buf).unwrap();
    }
    assert_eq!(fs::read(&path).unwrap(), buf, "{name}");
    buf
}

fn write_json(name: &str, value: serde_json::Value) {
    let mut bytes = serde_json::to_vec(&value).unwrap();
    bytes.push(b'\n');
    let path = dir().join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let existing = fs::read(&path).unwrap_or_default();
    if existing.is_empty() {
        fs::write(&path, &bytes).unwrap();
    }
    assert_eq!(fs::read(&path).unwrap(), bytes, "{name}");
}

fn hello() -> ClientMessage {
    ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: "local-token".into(),
        cols: 80,
        rows: 24,
        tmux_identity: Some(TmuxClientIdentity {
            socket_path: "/tmp/tmux-sock".into(),
            server_pid: 9,
            server_start_time: 1,
            pane_id: "%0".into(),
        }),
    }
}

#[test]
fn golden_corpus_bytes_and_fragmented_reads() {
    write_bin("hello.bin", &hello());
    write_bin(
        "welcome.bin",
        &ServerMessage::Welcome {
            host_epoch: "epoch-1".into(),
        },
    );
    write_bin(
        "attach_terminal.bin",
        &ClientMessage::AttachTerminal {
            host_terminal_id: "ht-1".into(),
            reservation_id: None,
            locator: Some(PaneLocator {
                socket_path: "/tmp/tmux-sock".into(),
                server_pid: 9,
                server_start_time: 1,
                pane_id: "%0".into(),
            }),
        },
    );
    write_bin(
        "attach_terminal_reserved.bin",
        &ClientMessage::AttachTerminal {
            host_terminal_id: "ht-1".into(),
            reservation_id: Some("rsv-1".into()),
            locator: None,
        },
    );
    write_bin(
        "set_viewport.bin",
        &ClientMessage::SetViewport { rows: 24, cols: 80 },
    );
    write_bin(
        "set_scroll_offset.bin",
        &ClientMessage::SetScrollOffset {
            rows_from_live_edge: 12,
        },
    );
    write_bin(
        "scroll_offset_applied.bin",
        &ServerMessage::ScrollOffsetApplied {
            applied_rows: 12,
            max_rows: 40,
        },
    );
    write_bin("detach.bin", &ClientMessage::Detach);
    let frame = FrameData {
        cells: vec![CellData {
            symbol: "A".into(),
            fg: 1,
            bg: 2,
            modifier: 0,
            skip: false,
            hyperlink: None,
        }],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 1,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: gobby_terminal::protocol::PaneModes::default(),
    };
    write_bin("frame.bin", &ServerMessage::Frame(frame));
    write_bin(
        "terminal_ansi.bin",
        &ServerMessage::Terminal(TerminalFrame {
            seq: 1,
            width: 80,
            height: 24,
            full: true,
            bytes: b"\x1b[0mhi".to_vec(),
        }),
    );
    write_bin(
        "graphics.bin",
        &ServerMessage::Graphics {
            bytes: b"\x1b_G".to_vec(),
        },
    );
    write_bin(
        "attach_history.bin",
        &ServerMessage::AttachHistory {
            text: "history".into(),
            truncated: false,
            dropped_bytes: 0,
            total_bytes: 7,
        },
    );
    write_bin(
        "terminal_exited.bin",
        &ServerMessage::TerminalExited {
            host_terminal_id: "ht-1".into(),
            exit_code: Some(0),
        },
    );
    write_bin(
        "error_frame.bin",
        &ServerMessage::Error {
            code: "lag".into(),
            message: None,
        },
    );

    write_json(
        "control_hello.json",
        serde_json::json!({"method":"hello","protocol_version":1,"control_token":"token"}),
    );
    write_json(
        "control_ping.json",
        serde_json::json!({"ok":true,"host_epoch":"epoch-1","version":"0.1.0","host_pid":1234}),
    );
    write_json(
        "control_list.json",
        serde_json::json!({"ok":true,"terminals":[]}),
    );
    write_json(
        "control_host_shutdown.json",
        serde_json::json!({"method":"host_shutdown","grace_ms":1000}),
    );
    write_json(
        "control_spawn.json",
        serde_json::json!({
            "method":"spawn","operation_seq":1,"terminal_id":"t","spawn_key":"s",
            "reservation_id":"rsv","reserve_key":"rk","argv":["/bin/sh"],"env":{},
            "cwd":"/tmp","rows":24,"cols":80,"commit_deadline_ms":30000
        }),
    );
    write_json(
        "control_spawn_prepared.json",
        serde_json::json!({
            "ok":true,"method":"spawn_prepared","terminal_id":"t","spawn_key":"s",
            "host_terminal_id":"ht-1","pgid":99,"start_time":1.0,
            "reservation_id":"rsv","reserve_key":"rk","reserve_generation":1
        }),
    );
    write_json(
        "control_spawn_commit.json",
        serde_json::json!({"method":"spawn_commit","terminal_id":"t","spawn_key":"s"}),
    );
    write_json(
        "control_kill.json",
        serde_json::json!({"method":"kill","operation_seq":2,"host_terminal_id":"ht-1","grace_ms":50}),
    );
    write_json(
        "control_resize.json",
        serde_json::json!({"method":"resize","operation_seq":3,"host_terminal_id":"ht-1","rows":30,"cols":100}),
    );
    write_json(
        "control_snapshot.json",
        serde_json::json!({"method":"snapshot","host_terminal_id":"ht-1","mode":"ansi","max_bytes":262144,"max_lines":500}),
    );
    write_json(
        "control_write.json",
        serde_json::json!({
            "method":"write","operation_seq":4,"host_terminal_id":"ht-1",
            "kind":"text","encoding":"utf8-b64","data":"eA==","submit":false
        }),
    );
    write_json(
        "control_write_paste_on.json",
        serde_json::json!({
            "method":"write","operation_seq":5,"host_terminal_id":"ht-1",
            "kind":"paste","encoding":"utf8-b64","data":"eA=="
        }),
    );
    write_json(
        "control_write_paste_off.json",
        serde_json::json!({
            "method":"write","operation_seq":6,"host_terminal_id":"ht-1",
            "kind":"paste","encoding":"utf8-b64","data":"eA=="
        }),
    );
    write_json(
        "control_subscribe_events.json",
        serde_json::json!({"method":"subscribe_events"}),
    );
    write_json(
        "control_reserve_observer.json",
        serde_json::json!({"method":"reserve_observer","terminal_id":"t","reserve_key":"rk"}),
    );
    write_json(
        "control_release_observer.json",
        serde_json::json!({"method":"release_observer","reservation_id":"rsv","reserve_key":"rk"}),
    );

    let hello_bytes = fs::read(dir().join("hello.bin")).unwrap();
    let mut chunked = Chunked(&hello_bytes, 0, 3);
    let decoded: ClientMessage =
        gobby_terminal::protocol::read_message(&mut chunked, MAX_FRAME_SIZE).unwrap();
    assert_eq!(decoded, hello());

    let mut oversized = (MAX_FRAME_SIZE as u32 + 1).to_le_bytes().to_vec();
    oversized.extend_from_slice(&[0; 8]);
    let err: Result<ClientMessage, _> =
        gobby_terminal::protocol::read_message(&mut Cursor::new(oversized), MAX_FRAME_SIZE);
    assert!(err.is_err());
}

#[test]
fn golden_corpus_covers_pane_attach() {
    write_bin(
        "attach_terminal_created.bin",
        &ServerMessage::Attached {
            created: true,
            host_terminal_id: "ht-1".into(),
        },
    );
    write_bin(
        "error_self_view.bin",
        &ServerMessage::Error {
            code: "self_view".into(),
            message: None,
        },
    );
    write_bin(
        "error_copy_mode.bin",
        &ServerMessage::Error {
            code: "copy_mode".into(),
            message: None,
        },
    );
    write_bin(
        "error_stale.bin",
        &ServerMessage::Error {
            code: "stale".into(),
            message: None,
        },
    );
    write_bin(
        "error_capacity.bin",
        &ServerMessage::Error {
            code: "capacity".into(),
            message: None,
        },
    );
    for name in [
        "hello.bin",
        "attach_terminal.bin",
        "attach_terminal_reserved.bin",
        "attach_terminal_created.bin",
        "attach_history.bin",
        "error_self_view.bin",
        "error_copy_mode.bin",
        "error_stale.bin",
        "error_capacity.bin",
    ] {
        let bytes = fs::read(dir().join(name)).unwrap();
        assert!(!bytes.is_empty(), "{name}");
    }
    golden_corpus_bytes_and_fragmented_reads();
}

#[test]
fn golden_corpus_covers_host_shutdown() {
    assert!(dir().join("control_host_shutdown.json").exists());
}

#[test]
fn python_dimensions_match_wire_constants() {
    let py = fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../src/gobby/terminals/dimensions.py"),
    )
    .unwrap();
    assert!(py.contains("MAX_FRAME_SIZE = 2 * 1024 * 1024"));
    assert!(py.contains(&format!("WORST_CELL_BYTES = {WORST_CELL_BYTES}")));
    assert!(py.contains(&format!("MAX_ROWS = {MAX_ROWS}")));
    assert!(py.contains(&format!("MAX_COLS = {MAX_COLS}")));
    assert!(py.contains(&format!("MIN_ROWS = {MIN_ROWS}")));
    assert!(py.contains(&format!("MIN_COLS = {MIN_COLS}")));
    assert!(py.contains("MAX_CELLS"));
    let _ = MAX_FRAME_SIZE;
    let _ = MAX_CELLS;
}

struct Chunked<'a>(&'a [u8], usize, usize);

impl std::io::Read for Chunked<'_> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.1 >= self.0.len() {
            return Ok(0);
        }
        let n = (self.0.len() - self.1).min(self.2).min(buf.len());
        buf[..n].copy_from_slice(&self.0[self.1..self.1 + n]);
        self.1 += n;
        Ok(n)
    }
}
