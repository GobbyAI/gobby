//! Golden wire corpus for gterm frame (bincode) and control (JSON) protocols.

use gobby_terminal::protocol::{
    write_message, CellData, ClientMessage, CursorState, FrameData, PaneLocator, RenderEncoding,
    ServerMessage, SnapshotMode, TerminalFrame, TmuxClientIdentity, MAX_CELLS, MAX_COLS,
    MAX_FRAME_SIZE, MAX_ROWS, MIN_COLS, MIN_ROWS, PROTOCOL_VERSION, WORST_CELL_BYTES,
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
    write_bin(
        "frame_bind_attachment.bin",
        &ClientMessage::BindAttachment {
            attachment_id: "att-1".into(),
        },
    );
    write_bin(
        "frame_input.bin",
        &ClientMessage::Input {
            data: b"x".to_vec(),
        },
    );
    write_bin(
        "frame_paste.bin",
        &ClientMessage::Paste {
            text: "pasted".into(),
        },
    );
    write_bin(
        "frame_input_refused.bin",
        &ServerMessage::InputRefused {
            code: "input_not_granted".into(),
        },
    );
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
        serde_json::json!({"id":"hello-1","method":"hello","protocol_version":1,"control_token":"token"}),
    );
    write_json(
        "control_ping.json",
        serde_json::json!({"id":"ping-1","ok":true,"host_epoch":"epoch-1","version":"0.1.0","host_pid":1234}),
    );
    write_json(
        "control_list.json",
        serde_json::json!({"id":"list-1","ok":true,"terminals":[],"epoch":"epoch-1","seq":41}),
    );
    write_json(
        "control_host_shutdown.json",
        serde_json::json!({"id":"shutdown-1","method":"host_shutdown","grace_ms":1000}),
    );
    write_json(
        "control_spawn.json",
        serde_json::json!({
            "id":"spawn-1","method":"spawn","operation_seq":1,"terminal_id":"t","spawn_key":"s",
            "reservation_id":"rsv","reserve_key":"rk","argv":["/bin/sh"],"env":{},
            "cwd":"/tmp","rows":24,"cols":80,"commit_deadline_ms":30000
        }),
    );
    write_json(
        "control_spawn_prepared.json",
        serde_json::json!({
            "id":"spawn-1","ok":true,"method":"spawn_prepared","terminal_id":"t","spawn_key":"s",
            "host_terminal_id":"ht-1","pgid":99,"start_time":1.0,
            "reservation_id":"rsv","reserve_key":"rk","reserve_generation":1
        }),
    );
    write_json(
        "control_spawn_commit.json",
        serde_json::json!({"id":"commit-1","method":"spawn_commit","terminal_id":"t","spawn_key":"s"}),
    );
    write_json(
        "control_kill.json",
        serde_json::json!({"id":"kill-1","method":"kill","operation_seq":2,"host_terminal_id":"ht-1","grace_ms":50}),
    );
    write_json(
        "control_resize.json",
        serde_json::json!({"id":"resize-1","method":"resize","operation_seq":3,"host_terminal_id":"ht-1","rows":30,"cols":100}),
    );
    write_json(
        "control_snapshot.json",
        serde_json::json!({"id":"snapshot-1","method":"snapshot","host_terminal_id":"ht-1",
            "mode":SnapshotMode::Ansi.as_wire(),"max_bytes":262144,"max_lines":500}),
    );
    write_json(
        "control_snapshot_text.json",
        serde_json::json!({"id":"snapshot-2","method":"snapshot","host_terminal_id":"ht-1",
            "mode":SnapshotMode::Text.as_wire(),"max_bytes":262144,"max_lines":500}),
    );
    write_json(
        "control_snapshot_result.json",
        serde_json::json!({"id":"snapshot-2","ok":true,"mode":SnapshotMode::Text.as_wire(),
            "text":"one\ntwo","truncated":true,"dropped_bytes":4,"total_bytes":12}),
    );
    write_json(
        "control_write.json",
        serde_json::json!({
            "id":"write-1","method":"write","operation_seq":4,"host_terminal_id":"ht-1",
            "kind":"text","encoding":"utf8-b64","data":"eA==","submit":false
        }),
    );
    write_json(
        "control_write_batch.json",
        serde_json::json!({
            "id":"batch-1","method":"write_batch","operation_seq":7,"targets":[
                {"recipient_id":"r1","host_terminal_id":"ht-1","operations":[
                    {"kind":"text","encoding":"utf8-b64","data":"eA==","delay_ms":0}
                ]},
                {"recipient_id":"r2","host_terminal_id":"ht-2","operations":[
                    {"kind":"key","encoding":"utf8-b64","data":"ZW50ZXI=","delay_ms":15}
                ]},
                {"recipient_id":"r3","host_terminal_id":"ht-3","operations":[
                    {"kind":"text","encoding":"utf8-b64","data":"eQ==","delay_ms":0}
                ]}
            ]
        }),
    );
    write_json(
        "control_write_paste_on.json",
        serde_json::json!({
            "id":"paste-on-1","method":"write","operation_seq":5,"host_terminal_id":"ht-1",
            "kind":"paste","encoding":"utf8-b64","data":"eA=="
        }),
    );
    write_json(
        "control_write_paste_off.json",
        serde_json::json!({
            "id":"paste-off-1","method":"write","operation_seq":6,"host_terminal_id":"ht-1",
            "kind":"paste","encoding":"utf8-b64","data":"eA=="
        }),
    );
    write_json(
        "control_subscribe_events.json",
        serde_json::json!({"id":"subscribe-1","method":"subscribe_events","since":41}),
    );
    write_json(
        "control_reserve_observer.json",
        serde_json::json!({"id":"reserve-1","method":"reserve_observer","terminal_id":"t","reserve_key":"rk"}),
    );
    write_json(
        "control_release_observer.json",
        serde_json::json!({"id":"release-1","method":"release_observer","reservation_id":"rsv","reserve_key":"rk"}),
    );
    write_json(
        "control_grant_input.json",
        serde_json::json!({"id":"grant-1","method":"grant_input","host_terminal_id":"ht-1","attachment_id":"att-1"}),
    );
    write_json(
        "control_revoke_input.json",
        serde_json::json!({"id":"revoke-1","method":"revoke_input","host_terminal_id":"ht-1","attachment_id":"att-1"}),
    );
    write_json(
        "control_input_activity.json",
        serde_json::json!({
            "event":"input_activity","terminal_id":"t","host_terminal_id":"ht-1",
            "attachment_id":"att-1","kind":"input","bytes":1,"interrupt":null,
            "epoch":"epoch-1","seq":43
        }),
    );
    write_json(
        "control_terminal_exited.json",
        serde_json::json!({
            "event":"terminal_exited","terminal_id":"t","host_terminal_id":"ht-1",
            "exit_code":7,"epoch":"epoch-1","seq":42
        }),
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

#[test]
fn frame_modes_expose_mouse_tracking() {
    use gobby_terminal::protocol::{MouseTracking, PaneModes};

    let expected = [
        MouseTracking::Off,
        MouseTracking::X10,
        MouseTracking::Normal,
        MouseTracking::Normal,
        MouseTracking::ButtonMotion,
        MouseTracking::ButtonMotion,
        MouseTracking::ButtonMotion,
        MouseTracking::ButtonMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
        MouseTracking::AnyMotion,
    ];
    for (flags, expected) in expected.into_iter().enumerate() {
        for encoding_enabled in [false, true] {
            let modes = PaneModes {
                mouse_any: flags & 1 != 0,
                mouse_standard: flags & 2 != 0,
                mouse_button: flags & 4 != 0,
                mouse_all: flags & 8 != 0,
                mouse_sgr: encoding_enabled,
                mouse_utf8: encoding_enabled,
                alternate_on: encoding_enabled,
                ..Default::default()
            };
            assert_eq!(modes.mouse_tracking(), expected, "flags {flags:04b}");
        }
    }

    let buffer = ratatui::buffer::Buffer::empty(ratatui::layout::Rect::new(0, 0, 1, 1));
    let mut frame = FrameData::from_ratatui_buffer_with_hyperlinks(&buffer, None, &[]);
    frame.modes = PaneModes {
        mouse_all: true,
        mouse_sgr: true,
        alternate_on: true,
        kitty_keyboard_flags: 5,
        ..Default::default()
    };
    let message = ServerMessage::Frame(frame);
    let bytes = write_bin("frame_mouse_modes.bin", &message);
    let decoded: ServerMessage =
        gobby_terminal::protocol::read_message(&mut Chunked(&bytes, 0, 3), MAX_FRAME_SIZE).unwrap();
    assert_eq!(decoded, message);
    let ServerMessage::Frame(frame) = decoded else {
        panic!("expected frame")
    };
    assert_eq!(frame.modes.mouse_tracking(), MouseTracking::AnyMotion);
    assert!(frame.modes.mouse_sgr && frame.modes.alternate_on);
    assert_eq!(frame.modes.kitty_keyboard_flags, 5);
}
