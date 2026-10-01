//! Wire-contract tests for the daemon-side gterm clients.
//!
//! The oracle is gterm's own read-only golden corpus under
//! `crates/gterminal/tests/fixtures/wire_golden/`: these clients must speak the
//! existing host wire byte-for-byte without any change to gterm.

use std::collections::BTreeSet;
use std::path::PathBuf;

use anyhow::{Context, Result};
use gobby_terminals::control::{
    self, ControlClient, ControlError, EventCursor, EventError, Hello, HostEvent, HostEventKind,
    InputKind, SpawnRequest, Subscription, WriteKind, WriteRequest,
};
use gobby_terminals::frames::{
    self, CellData, ClientMessage, CursorState, DeltaQueue, FrameClient, FrameData, FrameError,
    PaneLocator, PaneModes, RenderEncoding, RgbColor, ServerMessage, TerminalFrame,
    ThemeDeclaration, TmuxClientIdentity,
};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, DuplexStream, duplex};
use tokio::task::JoinHandle;

fn golden(name: &str) -> PathBuf {
    PathBuf::from(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../gterminal/tests/fixtures/wire_golden"
    ))
    .join(name)
}

fn json_fixture(name: &str) -> Result<Value> {
    let raw = std::fs::read(golden(name)).with_context(|| format!("read {name}"))?;
    Ok(serde_json::from_slice(&raw)?)
}

fn bin_fixture(name: &str) -> Result<Vec<u8>> {
    std::fs::read(golden(name)).with_context(|| format!("read {name}"))
}

fn with_id(mut value: Value, id: &Value) -> Value {
    if let Some(object) = value.as_object_mut() {
        object.insert("id".into(), id.clone());
    }
    value
}

/// One host turn: event lines written first, then the reply carrying the
/// request's own id (`Value::Null` writes no reply).
struct Turn {
    events: Vec<Value>,
    reply: Value,
}

fn reply(value: Value) -> Turn {
    Turn {
        events: Vec::new(),
        reply: value,
    }
}

/// A scripted gterm control host. It records every request line until the
/// client side closes, so a client that writes nothing fails an assertion
/// instead of hanging.
fn scripted_host(stream: DuplexStream, script: Vec<Turn>) -> JoinHandle<Result<Vec<Value>>> {
    tokio::spawn(async move {
        let (reader, mut writer) = tokio::io::split(stream);
        let mut lines = BufReader::new(reader).lines();
        let mut script = script.into_iter();
        let mut requests = Vec::new();
        while let Some(line) = lines.next_line().await? {
            let request: Value = serde_json::from_str(&line)?;
            let id = request.get("id").cloned().unwrap_or(Value::Null);
            requests.push(request);
            let Some(turn) = script.next() else { continue };
            for event in turn.events {
                writer.write_all(format!("{event}\n").as_bytes()).await?;
            }
            if !turn.reply.is_null() {
                let line = with_id(turn.reply, &id);
                writer.write_all(format!("{line}\n").as_bytes()).await?;
            }
        }
        Ok(requests)
    })
}

/// The fixture as the client must send it: the client allocates its own
/// request id, and `operation_seq` follows this connection's ledger.
fn expected_request(fixture: &str, actual: &Value, operation_seq: Option<u64>) -> Result<Value> {
    let mut expected = with_id(json_fixture(fixture)?, &actual["id"]);
    if let (Some(seq), Some(object)) = (operation_seq, expected.as_object_mut()) {
        object.insert("operation_seq".into(), json!(seq));
    }
    Ok(expected)
}

fn hello_reply(protocol_version: u32) -> Value {
    json!({
        "ok": true,
        "host_epoch": "epoch-1",
        "version": "0.1.0",
        "protocol_version": protocol_version,
        "capabilities": ["terminal_theme"],
    })
}

fn subscribed(epoch: &str, seq: u64, gap: bool) -> Value {
    json!({"ok": true, "subscribed": true, "epoch": epoch, "seq": seq, "gap": gap})
}

fn spawn_request() -> SpawnRequest {
    SpawnRequest {
        terminal_id: "t".into(),
        spawn_key: "s".into(),
        argv: vec!["/bin/sh".into()],
        cwd: "/tmp".into(),
        env: Default::default(),
        cols: 80,
        rows: 24,
        commit_deadline_ms: 30_000,
        reservation_id: Some("rsv".into()),
        reserve_key: Some("rk".into()),
    }
}

#[tokio::test]
async fn control_json_lines_match_gterm() -> Result<()> {
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(
        host_side,
        vec![
            reply(hello_reply(1)),
            reply(json_fixture("control_ping.json")?),
            reply(json_fixture("control_list.json")?),
            reply(json_fixture("control_spawn_prepared.json")?),
            reply(json!({"ok": true})),
            reply(json!({"ok": true})),
            reply(json!({"ok": true})),
            reply(json!({"ok": true, "written": 1})),
            reply(json_fixture("control_spawn_prepared.json")?),
            reply(json!({"ok": true})),
            reply(json!({"ok": true, "reservation_id": "rsv"})),
            reply(json!({"ok": true})),
            reply(subscribed("epoch-1", 41, false)),
        ],
    );
    let mut client = ControlClient::new(client_side);

    let hello = client.hello("token").await?;
    assert_eq!(hello.host_epoch, "epoch-1");
    assert_eq!(hello.protocol_version, 1);
    assert_eq!(hello.capabilities, vec!["terminal_theme".to_owned()]);
    let ping = client.ping().await?;
    assert_eq!((ping.host_epoch.as_str(), ping.host_pid), ("epoch-1", 1234));
    let inventory = client.list().await?;
    assert_eq!((inventory.epoch.as_str(), inventory.seq), ("epoch-1", 41));
    assert!(inventory.terminals.is_empty());
    let prepared = client.spawn(&spawn_request()).await?;
    assert_eq!(prepared.host_terminal_id, "ht-1");
    assert_eq!((prepared.pgid, prepared.reserve_generation), (99, Some(1)));
    client.spawn_commit("t", "s", None).await?;
    client.kill("ht-1", 50).await?;
    client.resize("ht-1", 30, 100).await?;
    let write = WriteRequest {
        host_terminal_id: "ht-1".into(),
        kind: WriteKind::Text,
        data: b"x".to_vec(),
        submit: Some(false),
    };
    assert_eq!(client.write(&write).await?["written"], json!(1));
    let aborted = client.spawn(&spawn_request()).await?;
    client.spawn_abort(&aborted, 50).await?;
    let reservation = client.reserve_observer("t", "rk").await?;
    assert_eq!(reservation["reservation_id"], json!("rsv"));
    client.release_observer("rsv", "rk").await?;
    let subscription = client.subscribe_events(Some(41)).await?;
    assert_eq!(subscription, Subscription::new("epoch-1", 41, false));
    drop(client);

    let requests = host.await??;
    let expected_fixtures = [
        ("control_hello.json", None),
        ("ping", None),
        ("list", None),
        ("control_spawn.json", Some(1)),
        ("control_spawn_commit.json", None),
        ("control_kill.json", Some(2)),
        ("control_resize.json", Some(3)),
        ("control_write.json", Some(4)),
        ("control_spawn.json", Some(5)),
        ("control_kill.json", Some(6)),
        ("control_reserve_observer.json", None),
        ("control_release_observer.json", None),
        ("control_subscribe_events.json", None),
    ];
    assert_eq!(requests.len(), expected_fixtures.len(), "{requests:#?}");
    for (actual, (fixture, seq)) in requests.iter().zip(expected_fixtures) {
        if !fixture.ends_with(".json") {
            // gterm's ping/list goldens are host replies; the request is the bare verb.
            assert_eq!(actual, &json!({"id": actual["id"], "method": fixture}));
            continue;
        }
        assert_eq!(
            actual,
            &expected_request(fixture, actual, seq)?,
            "{fixture}"
        );
    }
    let ids: BTreeSet<String> = requests
        .iter()
        .map(|request| request["id"].to_string())
        .collect();
    assert_eq!(
        ids.len(),
        requests.len(),
        "request ids are unique per connection"
    );
    Ok(())
}

#[tokio::test]
async fn control_client_fails_closed_on_auth_version_and_ids() -> Result<()> {
    // No command leaves the client before hello authenticates the connection.
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(host_side, Vec::new());
    let mut client = ControlClient::new(client_side);
    let refused = client
        .ping()
        .await
        .expect_err("ping before hello must be refused");
    assert!(
        matches!(refused, ControlError::NotAuthenticated),
        "{refused:?}"
    );
    drop(client);
    assert!(host.await??.is_empty(), "nothing is written before hello");

    // A host speaking another protocol version is refused.
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(host_side, vec![reply(hello_reply(2))]);
    let mut client = ControlClient::new(client_side);
    let refused = client
        .hello("token")
        .await
        .expect_err("version 2 must be refused");
    assert!(
        matches!(refused, ControlError::UnsupportedProtocol { host: Some(2) }),
        "{refused:?}"
    );
    drop(client);
    host.await??;

    // A reply carrying another request's id is never accepted as this reply.
    let (client_side, host_side) = duplex(64 * 1024);
    let mut host_lines = BufReader::new(host_side);
    let mut client = ControlClient::new(client_side);
    let host = tokio::spawn(async move {
        let mut line = String::new();
        host_lines.read_line(&mut line).await?;
        let reply = with_id(hello_reply(1), &json!("someone-else"));
        host_lines
            .get_mut()
            .write_all(format!("{reply}\n").as_bytes())
            .await?;
        anyhow::Ok(())
    });
    let refused = client
        .hello("token")
        .await
        .expect_err("foreign id must be refused");
    assert!(
        matches!(refused, ControlError::UnexpectedResponseId { .. }),
        "{refused:?}"
    );
    drop(client);
    host.await??;

    // A host error is surfaced with its code, never as success.
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(
        host_side,
        vec![reply(json!({"ok": false, "error": "invalid_token"}))],
    );
    let mut client = ControlClient::new(client_side);
    let refused = client
        .hello("wrong")
        .await
        .expect_err("invalid token must fail");
    assert!(
        matches!(&refused, ControlError::Host { error, .. } if error == "invalid_token"),
        "{refused:?}"
    );
    drop(client);
    host.await??;

    // The control line ceiling counts the newline, as gterm's reader does.
    let at_limit = hello_against_reply_of(control::MAX_CONTROL_LINE).await?;
    assert!(at_limit.is_ok(), "{at_limit:?}");
    let over = hello_against_reply_of(control::MAX_CONTROL_LINE + 1).await?;
    assert!(matches!(over, Err(ControlError::LineTooLong)), "{over:?}");
    Ok(())
}

/// Answer one hello with a valid reply padded to exactly `total` bytes,
/// newline included, and return what the client made of it.
async fn hello_against_reply_of(total: usize) -> Result<Result<Hello, ControlError>> {
    let (client_side, host_side) = duplex(64 * 1024);
    let mut host_lines = BufReader::new(host_side);
    let host = tokio::spawn(async move {
        let mut line = String::new();
        host_lines.read_line(&mut line).await?;
        let request: Value = serde_json::from_str(&line)?;
        let mut reply = with_id(hello_reply(1), &request["id"]);
        reply["pad"] = json!("");
        let unpadded = serde_json::to_vec(&reply)?.len() + 1;
        reply["pad"] = json!("a".repeat(total - unpadded));
        let mut bytes = serde_json::to_vec(&reply)?;
        bytes.push(b'\n');
        anyhow::ensure!(
            bytes.len() == total,
            "padded reply is {} bytes",
            bytes.len()
        );
        host_lines.get_mut().write_all(&bytes).await?;
        anyhow::Ok(())
    });
    let mut client = ControlClient::new(client_side);
    let result = client.hello("token").await;
    drop(client);
    host.await??;
    Ok(result)
}

#[tokio::test]
async fn event_epoch_and_cursor_are_fail_closed() -> Result<()> {
    let exited = json_fixture("control_terminal_exited.json")?;
    let activity = json_fixture("control_input_activity.json")?;

    // Subscribe, then receive events that arrive ahead of a later reply.
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(
        host_side,
        vec![
            reply(hello_reply(1)),
            reply(subscribed("epoch-1", 41, false)),
            Turn {
                events: vec![exited.clone(), activity.clone()],
                reply: json_fixture("control_ping.json")?,
            },
        ],
    );
    let mut client = ControlClient::new(client_side);
    client.hello("token").await?;
    let subscription = client.subscribe_events(None).await?;
    let mut cursor = EventCursor::start(&subscription);
    assert_eq!(cursor.since(), 41);
    client.ping().await?;
    let first = client.next_event().await?;
    assert_eq!(
        first,
        HostEvent {
            epoch: "epoch-1".into(),
            seq: 42,
            kind: HostEventKind::TerminalExited {
                terminal_id: "t".into(),
                host_terminal_id: "ht-1".into(),
                exit_code: Some(7),
            },
        }
    );
    cursor.accept(&first)?;
    cursor.commit(first.seq)?;
    let second = client.next_event().await?;
    assert!(
        matches!(
            &second.kind,
            HostEventKind::InputActivity {
                kind: InputKind::Input,
                bytes: 1,
                interrupt: None,
                ..
            }
        ),
        "{second:?}"
    );
    cursor.accept(&second)?;
    // Delivered but not committed: a resume must replay it.
    assert_eq!(cursor.since(), 42);
    drop(client);
    host.await??;

    // Resume from the last committed cursor and accept the replayed event.
    let (client_side, host_side) = duplex(64 * 1024);
    let host = scripted_host(
        host_side,
        vec![
            reply(hello_reply(1)),
            reply(subscribed("epoch-1", 43, false)),
        ],
    );
    let mut client = ControlClient::new(client_side);
    client.hello("token").await?;
    let resumed = client.subscribe_events(Some(cursor.since())).await?;
    cursor.resume(&resumed)?;
    drop(client);
    let requests = host.await??;
    assert_eq!(
        requests[1]["since"],
        json!(42),
        "resume asks for the committed cursor"
    );
    cursor.accept(&HostEvent::from_value(&activity)?)?;
    cursor.commit(43)?;
    assert_eq!(cursor.since(), 43);

    // A replayed or reordered sequence is rejected.
    let stale = cursor
        .accept(&HostEvent::from_value(&exited)?)
        .expect_err("seq 42 after 43");
    assert_eq!(stale, EventError::NonMonotonic { last: 43, seq: 42 });
    // A skipped sequence is a gap, reported as a reconcile requirement.
    let mut skipped = activity.clone();
    skipped["seq"] = json!(45);
    let gap = cursor
        .accept(&HostEvent::from_value(&skipped)?)
        .expect_err("seq 45 after 43");
    assert_eq!(
        gap,
        EventError::ReconcileRequired {
            epoch: "epoch-1".into(),
            cursor: 43
        }
    );
    // An event from another host epoch is rejected.
    let mut foreign = activity.clone();
    foreign["epoch"] = json!("epoch-2");
    foreign["seq"] = json!(44);
    let changed = cursor
        .accept(&HostEvent::from_value(&foreign)?)
        .expect_err("epoch-2 event");
    assert_eq!(
        changed,
        EventError::EpochChanged {
            expected: "epoch-1".into(),
            actual: "epoch-2".into()
        }
    );
    // The host cannot replay from our cursor: reconcile, never skip.
    let unreplayable = cursor
        .resume(&Subscription::new("epoch-1", 900, true))
        .expect_err("gap must not resume");
    assert_eq!(
        unreplayable,
        EventError::ReconcileRequired {
            epoch: "epoch-1".into(),
            cursor: 43
        }
    );
    let restarted = cursor
        .resume(&Subscription::new("epoch-2", 0, false))
        .expect_err("epoch change must not resume");
    assert_eq!(
        restarted,
        EventError::EpochChanged {
            expected: "epoch-1".into(),
            actual: "epoch-2".into()
        }
    );
    assert_eq!(
        cursor.since(),
        43,
        "failed resumes leave the committed cursor"
    );
    let undelivered = cursor
        .commit(44)
        .expect_err("cannot commit an undelivered seq");
    assert_eq!(undelivered, EventError::NonMonotonic { last: 43, seq: 44 });
    let unknown = HostEvent::from_value(&json!({"event": "mystery", "epoch": "epoch-1", "seq": 1}))
        .expect_err("unknown events are refused");
    assert_eq!(unknown, EventError::UnknownEvent("mystery".into()));
    Ok(())
}

fn identity() -> TmuxClientIdentity {
    TmuxClientIdentity {
        socket_path: "/tmp/tmux-sock".into(),
        server_pid: 9,
        server_start_time: 1,
        pane_id: "%0".into(),
    }
}

fn client_frames() -> Vec<(&'static str, ClientMessage)> {
    let rgb = |r, g, b| RgbColor { r, g, b };
    vec![
        (
            "hello.bin",
            ClientMessage::Hello {
                version: frames::PROTOCOL_VERSION,
                encoding: RenderEncoding::SemanticFrame,
                local_token: "local-token".into(),
                cols: 80,
                rows: 24,
                tmux_identity: Some(identity()),
            },
        ),
        (
            "attach_terminal.bin",
            ClientMessage::AttachTerminal {
                host_terminal_id: "ht-1".into(),
                reservation_id: None,
                locator: Some(PaneLocator {
                    socket_path: "/tmp/tmux-sock".into(),
                    server_pid: 9,
                    server_start_time: 1,
                    pane_id: "%0".into(),
                }),
            },
        ),
        (
            "attach_terminal_reserved.bin",
            ClientMessage::AttachTerminal {
                host_terminal_id: "ht-1".into(),
                reservation_id: Some("rsv-1".into()),
                locator: None,
            },
        ),
        (
            "set_viewport.bin",
            ClientMessage::SetViewport { rows: 24, cols: 80 },
        ),
        (
            "set_scroll_offset.bin",
            ClientMessage::SetScrollOffset {
                rows_from_live_edge: 12,
            },
        ),
        ("detach.bin", ClientMessage::Detach),
        (
            "frame_bind_attachment.bin",
            ClientMessage::BindAttachment {
                attachment_id: "att-1".into(),
            },
        ),
        (
            "frame_input.bin",
            ClientMessage::Input {
                data: b"x".to_vec(),
            },
        ),
        (
            "frame_paste.bin",
            ClientMessage::Paste {
                text: "pasted".into(),
            },
        ),
        (
            "read_text.bin",
            ClientMessage::ReadText {
                start_rows_from_live_edge: 31,
                start_col: 2,
                end_rows_from_live_edge: 4,
                end_col: 17,
            },
        ),
        (
            "set_terminal_theme.bin",
            ClientMessage::SetTerminalTheme {
                theme: ThemeDeclaration {
                    foreground: Some(rgb(0x20, 0x21, 0x22)),
                    background: Some(rgb(0xfa, 0xfb, 0xfc)),
                    palette: vec![(1, rgb(0xc0, 0x10, 0x20))],
                },
            },
        ),
    ]
}

fn server_frames() -> Vec<(&'static str, ServerMessage)> {
    let error = |code: &str| ServerMessage::Error {
        code: code.into(),
        message: None,
    };
    vec![
        (
            "welcome.bin",
            ServerMessage::Welcome {
                host_epoch: "epoch-1".into(),
            },
        ),
        (
            "attach_terminal_created.bin",
            ServerMessage::Attached {
                created: true,
                host_terminal_id: "ht-1".into(),
            },
        ),
        (
            "scroll_offset_applied.bin",
            ServerMessage::ScrollOffsetApplied {
                applied_rows: 12,
                max_rows: 40,
            },
        ),
        (
            "frame_input_refused.bin",
            ServerMessage::InputRefused {
                code: "input_not_granted".into(),
            },
        ),
        (
            "text_read.bin",
            ServerMessage::TextRead {
                text: "retained text".into(),
                truncated: false,
            },
        ),
        (
            "frame.bin",
            ServerMessage::Frame(FrameData {
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
                modes: PaneModes::default(),
            }),
        ),
        (
            "terminal_ansi.bin",
            ServerMessage::Terminal(TerminalFrame {
                seq: 1,
                width: 80,
                height: 24,
                full: true,
                bytes: b"\x1b[0mhi".to_vec(),
            }),
        ),
        (
            "graphics.bin",
            ServerMessage::Graphics {
                bytes: b"\x1b_G".to_vec(),
            },
        ),
        (
            "attach_history.bin",
            ServerMessage::AttachHistory {
                text: "history".into(),
                truncated: false,
                dropped_bytes: 0,
                total_bytes: 7,
            },
        ),
        (
            "terminal_exited.bin",
            ServerMessage::TerminalExited {
                host_terminal_id: "ht-1".into(),
                exit_code: Some(0),
            },
        ),
        ("error_frame.bin", error("lag")),
        ("error_self_view.bin", error("self_view")),
        ("error_copy_mode.bin", error("copy_mode")),
        ("error_stale.bin", error("stale")),
        ("error_capacity.bin", error("capacity")),
    ]
}

#[tokio::test]
async fn frame_bytes_match_gterm() -> Result<()> {
    assert_eq!(frames::MAX_FRAME_SIZE, 2 * 1024 * 1024);
    assert_eq!(frames::MAX_WRITE_BYTES, 1024 * 1024);
    assert_eq!(frames::DELTA_QUEUE_ENTRIES, 64);
    assert_eq!(frames::DELTA_QUEUE_BYTES, frames::MAX_FRAME_SIZE);

    // Attach, input, output, history, lifecycle and close frames, byte for byte.
    for (name, message) in client_frames() {
        let golden = bin_fixture(name)?;
        assert_eq!(
            frames::encode_frame(&message)?,
            golden,
            "{name} encodes as gterm"
        );
        assert_eq!(
            frames::decode_frame::<ClientMessage>(&golden)?,
            message,
            "{name}"
        );
    }
    for (name, message) in server_frames() {
        let golden = bin_fixture(name)?;
        assert_eq!(
            frames::encode_frame(&message)?,
            golden,
            "{name} encodes as gterm"
        );
        assert_eq!(
            frames::decode_frame::<ServerMessage>(&golden)?,
            message,
            "{name}"
        );
    }
    // Non-default pane modes round-trip through the mirrored types.
    let modes = bin_fixture("frame_mouse_modes.bin")?;
    let decoded = frames::decode_frame::<ServerMessage>(&modes)?;
    let ServerMessage::Frame(frame) = &decoded else {
        panic!("frame_mouse_modes.bin is not a Frame: {decoded:?}");
    };
    assert!(frame.modes.mouse_all && frame.modes.mouse_sgr && frame.modes.alternate_on);
    assert_eq!(frame.modes.kitty_keyboard_flags, 5);
    assert_eq!(frames::encode_frame(&decoded)?, modes);

    // Length ceilings: an oversized prefix is refused before any payload read,
    // an oversized message is never written, and trailing bytes are refused.
    let oversized = (frames::MAX_FRAME_SIZE as u32 + 1).to_le_bytes();
    let refused = frames::decode_frame::<ServerMessage>(&oversized).expect_err("oversized prefix");
    assert!(
        matches!(refused, FrameError::Oversized { .. }),
        "{refused:?}"
    );
    let huge = ClientMessage::Input {
        data: vec![0; frames::MAX_FRAME_SIZE],
    };
    let refused = frames::encode_frame(&huge).expect_err("oversized message");
    assert!(
        matches!(refused, FrameError::Oversized { .. }),
        "{refused:?}"
    );
    let mut trailing = bin_fixture("detach.bin")?;
    trailing.push(0);
    let refused = frames::decode_frame::<ClientMessage>(&trailing).expect_err("trailing byte");
    assert!(matches!(refused, FrameError::Length { .. }), "{refused:?}");

    // Queue ceilings: at most DELTA_QUEUE_ENTRIES frames and DELTA_QUEUE_BYTES
    // bytes are buffered; overflow is a lag error, never a silent drop.
    let mut queue = DeltaQueue::default();
    let small = ServerMessage::Graphics {
        bytes: b"\x1b_G".to_vec(),
    };
    for _ in 0..frames::DELTA_QUEUE_ENTRIES {
        queue.push(small.clone())?;
    }
    assert!(matches!(queue.push(small.clone()), Err(FrameError::Lag)));
    assert_eq!(queue.len(), frames::DELTA_QUEUE_ENTRIES);
    assert_eq!(queue.pop(), Some(small));
    let mut bytes_queue = DeltaQueue::default();
    let large = ServerMessage::Graphics {
        bytes: vec![0; frames::DELTA_QUEUE_BYTES / 2],
    };
    bytes_queue.push(large.clone())?;
    assert!(matches!(bytes_queue.push(large), Err(FrameError::Lag)));
    assert_eq!(bytes_queue.len(), 1);

    // Write ceiling: Input and Paste payloads over MAX_WRITE_BYTES are refused
    // before anything is written, as gterm's host would refuse them.
    let at_cap = ClientMessage::Input {
        data: vec![b'a'; frames::MAX_WRITE_BYTES],
    };
    let (client_side, mut host_side) = duplex(64 * 1024);
    let host = tokio::spawn(async move {
        let mut received = Vec::new();
        host_side.read_to_end(&mut received).await?;
        anyhow::Ok(received)
    });
    let mut client = FrameClient::new(client_side);
    for over in [
        ClientMessage::Input {
            data: vec![b'a'; frames::MAX_WRITE_BYTES + 1],
        },
        ClientMessage::Paste {
            text: "a".repeat(frames::MAX_WRITE_BYTES + 1),
        },
    ] {
        let refused = client.send(&over).await.expect_err("over the write cap");
        assert!(
            matches!(
                refused,
                FrameError::Oversized { claimed, max }
                    if claimed == frames::MAX_WRITE_BYTES + 1 && max == frames::MAX_WRITE_BYTES
            ),
            "{refused:?}"
        );
    }
    client.send(&at_cap).await?;
    drop(client);
    assert_eq!(
        host.await??,
        frames::encode_frame(&at_cap)?,
        "only the at-cap write reaches the host"
    );

    // The handshake writes gterm's hello bytes and verifies the host epoch.
    let hello_len = bin_fixture("hello.bin")?.len();
    let (client_side, mut host_side) = duplex(64 * 1024);
    let host = tokio::spawn(async move {
        let mut hello = vec![0; hello_len];
        host_side.read_exact(&mut hello).await?;
        host_side.write_all(&bin_fixture("welcome.bin")?).await?;
        host_side
            .write_all(&bin_fixture("terminal_ansi.bin")?)
            .await?;
        host_side.write_all(&oversized).await?;
        anyhow::Ok(hello)
    });
    let mut client = FrameClient::new(client_side);
    client
        .handshake(
            "epoch-1",
            "local-token",
            RenderEncoding::SemanticFrame,
            80,
            24,
            Some(identity()),
        )
        .await?;
    let output = client.recv().await?;
    assert!(
        matches!(
            output,
            ServerMessage::Terminal(TerminalFrame { seq: 1, .. })
        ),
        "{output:?}"
    );
    let refused = client.recv().await.expect_err("oversized frame from host");
    assert!(
        matches!(refused, FrameError::Oversized { .. }),
        "{refused:?}"
    );
    drop(client);
    assert_eq!(
        host.await??,
        bin_fixture("hello.bin")?,
        "hello is gterm's bytes"
    );

    // A welcome from another host epoch is refused.
    let (client_side, mut host_side) = duplex(64 * 1024);
    let host = tokio::spawn(async move {
        let mut prefix = [0; 4];
        host_side.read_exact(&mut prefix).await?;
        let welcome = ServerMessage::Welcome {
            host_epoch: "epoch-2".into(),
        };
        host_side
            .write_all(&frames::encode_frame(&welcome)?)
            .await?;
        anyhow::Ok(())
    });
    let mut client = FrameClient::new(client_side);
    let refused = client
        .handshake(
            "epoch-1",
            "local-token",
            RenderEncoding::SemanticFrame,
            80,
            24,
            None,
        )
        .await
        .expect_err("epoch-2 welcome");
    assert!(
        matches!(refused, FrameError::EpochChanged { .. }),
        "{refused:?}"
    );
    drop(client);
    host.await??;
    Ok(())
}
