//! Host handover (plan gterm-host-handover 1.2 and 1.3): frozen reaping, the
//! resume primitive, the Stage/Commit restore of a carried state file, and
//! `host_upgrade` from admission through exec or rollback.

#![cfg(unix)]

mod handover_support;
mod host_support;

use std::collections::HashMap;
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::time::{Duration, Instant};

use base64::Engine as _;
use gobby_terminal::host::handover::CarriedEvents;
use gobby_terminal::pane::{ChildExit, PaneLaunchEnv, PaneRuntime};
use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
};
use gobby_terminal::terminal_theme::{RgbColor, TerminalTheme, ThemeDeclaration};
use handover_support::{
    ino, is_zombie, launch, list_rows, mtime_ns, pgid_file, restore, wait_until, Bound, HelperPane,
    HelperSpec, BOUND_FILE, WAIT,
};
use host_support::{
    connect, recv_json, rpc, temp_socket_dir, CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE,
};
use serde_json::{json, Value};

/// The helper process lane's entry point; see `handover_support`.
#[test]
#[ignore = "run only as the helper process of the restore tests"]
fn handover_helper() {
    // test-quality: allow NO_ASSERTION, UNCONDITIONAL_SKIP -- not a test: the entry point of the helper process the restore tests launch, which becomes the host they assert on
    handover_support::run_helper();
}

fn spawn_pane(cwd: &Path, script: &str) -> PaneRuntime {
    PaneRuntime::spawn_argv_command(
        24,
        80,
        cwd.to_path_buf(),
        &["sh".to_string(), "-c".to_string(), script.to_string()],
        &PaneLaunchEnv::default(),
        1 << 20,
        TerminalTheme::default(),
        None,
    )
    .expect("spawn pane")
}

/// Whether `pid` is no longer this process's child to reap.
fn is_reaped(pid: u32) -> bool {
    let mut status = 0;
    // SAFETY: status is a valid out-pointer; WNOHANG never blocks.
    let rc = unsafe { libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG) };
    rc == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD)
}

fn snapshot_text(stream: &mut std::os::unix::net::UnixStream, host_terminal_id: &str) -> String {
    let snap = rpc(
        stream,
        "snapshot",
        json!({
            "host_terminal_id": host_terminal_id,
            "mode": "text",
            "max_bytes": 65536,
            "max_lines": 200,
        }),
    );
    assert_eq!(snap["ok"], true, "{snap}");
    snap["text"].as_str().unwrap_or_default().to_string()
}

#[tokio::test(flavor = "multi_thread")]
async fn rollback_reaps_exit_seen_while_frozen() {
    let dir = tempfile::tempdir().expect("tempdir");
    let runtime = spawn_pane(dir.path(), "while [ ! -e go ]; do sleep 0.02; done; exit 7");
    let pid = runtime.child_pid().expect("child pid");
    let watch = runtime.child_exit_watch().expect("exit watch");

    runtime.freeze_reaping();
    std::fs::write(dir.path().join("go"), b"").expect("release child");
    // A reaper that kept running would reap the exit before any poll saw it
    // as a zombie, so seeing one proves the frozen pane left it unreaped.
    wait_until("the frozen child to linger as a zombie", || is_zombie(pid));
    assert_eq!(
        runtime.child_exit(),
        None,
        "no exit is recorded while frozen"
    );

    runtime.unfreeze_reaping();
    let exit = tokio::time::timeout(WAIT, watch.wait())
        .await
        .expect("exit delivered after unfreeze");
    let expected = ChildExit {
        exit_code: Some(7),
        signal: None,
    };
    assert_eq!(exit, Some(expected.clone()), "the real status is recorded");
    assert_eq!(runtime.child_exit(), Some(expected));
    assert!(is_reaped(pid), "unfreeze reaped the child exactly once");
}

#[test]
fn state_round_trips_host_and_pane_fields() {
    let dir = temp_socket_dir();
    let mut pane = HelperPane::new("ht-7", "printf 'before-capture\\n'; exec sleep 600");
    pane.rows = 30;
    pane.cols = 100;
    pane.ready_text = Some("before-capture".into());
    pane.title = "agent-seven".into();
    pane.input_grant = Some("att-7".into());
    pane.entitled = true;
    let mut spec = HelperSpec::new(dir.path(), vec![pane]);
    spec.events = CarriedEvents {
        cursor: 5,
        ring: vec![json!({
            "event": "terminal_exited",
            "epoch": "epoch-handover",
            "seq": 5,
            "host_terminal_id": "ht-gone",
        })],
    };
    let host = restore(&spec);
    let mut control = host.control();

    let ping = rpc(&mut control, "ping", json!({}));
    assert_eq!(ping["host_epoch"], "epoch-handover", "{ping}");
    assert_eq!(ping["host_pid"], host.pid, "{ping}");

    let listed = rpc(&mut control, "list", json!({}));
    assert_eq!(listed["epoch"], "epoch-handover", "{listed}");
    assert_eq!(listed["seq"], 5, "the event cursor survives: {listed}");
    let rows = listed["terminals"].as_array().expect("terminal rows");
    assert_eq!(rows.len(), 1, "{listed}");
    let row = &rows[0];
    let expected = [
        ("host_terminal_id", json!("ht-7")),
        ("terminal_id", json!("term-ht-7")),
        ("spawn_key", json!("spawn-ht-7")),
        ("title", json!("agent-seven")),
        ("rows", json!(30)),
        ("cols", json!(100)),
        ("commit_state", json!("committed")),
        ("observer_bind", json!("entitled")),
        ("reservation_id", json!("res-ht-7")),
        ("reserve_generation", json!(2)),
        ("observation_state", json!("live")),
    ];
    for (field, value) in expected {
        assert_eq!(row[field], value, "{field} in {row}");
    }

    assert!(
        snapshot_text(&mut control, "ht-7").contains("before-capture"),
        "the carried screen is restored"
    );
    let released = rpc(
        &mut control,
        "release_observer",
        json!({"reservation_id": "res-ht-7", "reserve_key": "term-ht-7"}),
    );
    assert_eq!(
        released["released"], false,
        "the committed reservation is carried as prepared: {released}"
    );
    let revoked = rpc(
        &mut control,
        "revoke_input",
        json!({"host_terminal_id": "ht-7", "attachment_id": "att-7"}),
    );
    assert_eq!(
        revoked["revoked"], true,
        "the input grant survives: {revoked}"
    );

    let mut events = host.control();
    let ack = rpc(&mut events, "subscribe_events", json!({"since": 4}));
    assert_eq!(
        (&ack["epoch"], &ack["seq"], &ack["gap"]),
        (&json!("epoch-handover"), &json!(5), &json!(false)),
        "{ack}"
    );
    let replayed = recv_json(&mut events);
    assert_eq!(
        (&replayed["seq"], &replayed["host_terminal_id"]),
        (&json!(5), &json!("ht-gone")),
        "the carried ring replays: {replayed}"
    );

    let reserved = rpc(
        &mut control,
        "reserve_observer",
        json!({"terminal_id": "term-new", "reserve_key": "rk-new"}),
    );
    assert_eq!(reserved["ok"], true, "{reserved}");
    let spawned = rpc(
        &mut control,
        "spawn",
        json!({
            "operation_seq": 1,
            "terminal_id": "term-new",
            "spawn_key": "sk-new",
            "reservation_id": reserved["reservation_id"],
            "reserve_key": "rk-new",
            "argv": ["/bin/sh", "-c", "exit 0"],
            "cwd": dir.path().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    assert_eq!(
        spawned["host_terminal_id"], "ht-100",
        "next_host_id survives: {spawned}"
    );
}

/// The next event line on `stream`, or `None` once `timeout` passes quietly.
fn next_event(stream: &mut UnixStream, timeout: Duration) -> Option<Value> {
    stream
        .set_read_timeout(Some(timeout))
        .expect("read timeout");
    // One-byte buffer: nothing past this line is read and lost.
    let mut reader = BufReader::with_capacity(1, stream);
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) => panic!("event stream closed"),
        Ok(_) => Some(serde_json::from_str(line.trim_end()).expect("event json")),
        Err(err) if matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => None,
        Err(err) => panic!("read event: {err}"),
    }
}

/// Collects `terminal_exited` exit codes by host terminal id until `done`.
fn collect_exits(
    stream: &mut UnixStream,
    exits: &mut HashMap<String, Vec<Value>>,
    done: impl Fn(&HashMap<String, Vec<Value>>) -> bool,
) {
    let deadline = Instant::now() + WAIT;
    while !done(exits) {
        assert!(Instant::now() < deadline, "exits so far: {exits:?}");
        if let Some(event) = next_event(stream, Duration::from_millis(100)) {
            if event["event"] == "terminal_exited" {
                let id = event["host_terminal_id"].as_str().unwrap_or_default();
                exits
                    .entry(id.to_string())
                    .or_default()
                    .push(event["exit_code"].clone());
            }
        }
    }
}

#[test]
fn exit_and_output_during_window_survive() {
    let dir = temp_socket_dir();
    let path = |name: &str| dir.path().join(name);
    // Panes run in the socket dir, so their scripts name these files bare.
    let wait_for = |name: &str| format!("while [ ! -e {name} ]; do sleep 0.02; done");

    let mut recorded = HelperPane::new("ht-b", &format!("{}; exit 4", wait_for("b-go")));
    recorded.window_trigger = Some(path("b-go"));
    recorded.recorded_exit = true;
    let mut frozen = HelperPane::new("ht-c", &format!("{}; exit 5", wait_for("c-go")));
    frozen.window_trigger = Some(path("c-go"));
    frozen.exits_in_window = true;
    let mut later = HelperPane::new(
        "ht-d",
        &format!(
            "{}; echo window-output; touch d-done; {}; exit 6",
            wait_for("d-go"),
            wait_for("d-exit")
        ),
    );
    later.window_trigger = Some(path("d-go"));
    later.window_done = Some(path("d-done"));
    let mut spec = HelperSpec::new(dir.path(), vec![recorded, frozen, later]);
    // ht-a exited and was delivered before capture: only its event remains.
    spec.events = CarriedEvents {
        cursor: 1,
        ring: vec![json!({
            "event": "terminal_exited",
            "epoch": "epoch-handover",
            "seq": 1,
            "terminal_id": "term-ht-a",
            "host_terminal_id": "ht-a",
            "exit_code": 3,
        })],
    };
    let host = restore(&spec);
    let mut events = host.control();
    let ack = rpc(&mut events, "subscribe_events", json!({"since": 0}));
    assert_eq!(ack["gap"], false, "{ack}");

    let mut exits = HashMap::new();
    collect_exits(&mut events, &mut exits, |exits| {
        ["ht-a", "ht-b", "ht-c"]
            .iter()
            .all(|id| exits.contains_key(*id))
    });
    let mut control = host.control();
    let mut text = String::new();
    wait_until("window output after restore", || {
        text = snapshot_text(&mut control, "ht-d");
        text.contains("window-output")
    });
    std::fs::write(path("d-exit"), b"").expect("release ht-d");
    collect_exits(&mut events, &mut exits, |exits| exits.contains_key("ht-d"));
    while let Some(event) = next_event(&mut events, Duration::from_millis(300)) {
        if event["event"] == "terminal_exited" {
            panic!("duplicate exit {event} after {exits:?}");
        }
    }

    let expected: HashMap<String, Vec<Value>> =
        [("ht-a", 3), ("ht-b", 4), ("ht-c", 5), ("ht-d", 6)]
            .into_iter()
            .map(|(id, code)| (id.to_string(), vec![json!(code)]))
            .collect();
    assert_eq!(exits, expected, "each exit once, with its real status");
}

/// Queries OSC 10 and 11 every 300 ms and prints the raw answers, as in
/// `terminal_theme.rs`, so a marker written to the pane dates the answers.
const QUERY_LOOP: &str = "stty raw -echo min 0 time 2; \
    while :; do printf '\\033]10;?\\033\\\\\\033]11;?\\033\\\\'; sleep 0.1; \
    printf 'A<'; dd bs=256 count=1 2>/dev/null | cat -v; printf '>\\r\\n'; sleep 0.2; done";

/// The first answer line after the line that echoed `marker`: its queries
/// were sent after the child read the marker.
fn answer_after<'a>(screen: &'a str, marker: &str) -> Option<&'a str> {
    screen
        .lines()
        .skip_while(|line| !line.contains(marker))
        .skip(1)
        .find(|line| line.contains("A<") && line.contains('>'))
}

fn declaration(fg: (u8, u8, u8), bg: (u8, u8, u8)) -> ThemeDeclaration {
    let rgb = |(r, g, b)| RgbColor { r, g, b };
    ThemeDeclaration {
        foreground: Some(rgb(fg)),
        background: Some(rgb(bg)),
        palette: Vec::new(),
    }
}

fn write_msg(stream: &mut UnixStream, msg: &ClientMessage) {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).expect("encode message");
    stream.write_all(&buf).expect("write message");
}

fn read_msg(stream: &mut UnixStream) -> ServerMessage {
    stream.set_read_timeout(Some(WAIT)).expect("read timeout");
    read_message(stream, MAX_FRAME_SIZE).expect("frame message")
}

/// Attaches a frames stream to `host_terminal_id` and returns it once the
/// first broadcast frame arrives.
fn attach(socket_dir: &Path, host_terminal_id: &str, reservation_id: Option<&str>) -> UnixStream {
    let mut stream = connect(&socket_dir.join(FRAMES_SOCKET));
    write_msg(
        &mut stream,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: "local-token".into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    );
    assert!(matches!(
        read_msg(&mut stream),
        ServerMessage::Welcome { .. }
    ));
    write_msg(
        &mut stream,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.into(),
            reservation_id: reservation_id.map(Into::into),
            locator: None,
        },
    );
    loop {
        match read_msg(&mut stream) {
            ServerMessage::Frame(_) | ServerMessage::Terminal(_) => return stream,
            ServerMessage::Error { code, .. } => panic!("attach refused: {code}"),
            _ => {}
        }
    }
}

/// Returns once the host has handled everything sent on `stream` so far.
fn barrier(stream: &mut UnixStream) {
    write_msg(
        stream,
        &ClientMessage::ReadText {
            start_rows_from_live_edge: 0,
            start_col: 0,
            end_rows_from_live_edge: 0,
            end_col: 1,
        },
    );
    loop {
        match read_msg(stream) {
            ServerMessage::TextRead { .. } => return,
            ServerMessage::Error { code, .. } => panic!("barrier refused: {code}"),
            _ => {}
        }
    }
}

#[test]
fn restored_panes_keep_wrapper_state_and_entitlements() {
    let dir = temp_socket_dir();
    let light = declaration((0x20, 0x21, 0x22), (0xfa, 0xfb, 0xfc));
    let dark = declaration((0xe0, 0xe1, 0xe2), (0x10, 0x11, 0x12));
    let query = |id: &str, prefix: &str| {
        let mut pane = HelperPane::new(id, &format!("{prefix}{QUERY_LOOP}"));
        pane.ready_text = Some("A<".into());
        pane
    };
    let mut titled = query("ht-light", "printf '\\033]0;agent-light\\007'; ");
    titled.title = "agent-light".into();
    titled.theme = Some(light.clone());
    titled.entitled = true;
    let mut dark_pane = query("ht-dark", "");
    dark_pane.theme = Some(dark.clone());
    let overridden = query(
        "ht-osc",
        "printf '\\033]10;rgb:12/34/56\\033\\\\\\033]11;rgb:65/43/21\\033\\\\'; ",
    );
    let host = restore(&HelperSpec::new(
        dir.path(),
        vec![titled, dark_pane, overridden],
    ));
    let mut control = host.control();

    // The carried entitlement rebinds, and the broadcast that answered the
    // attach kept the agent title.
    let _bound = attach(dir.path(), "ht-light", Some("res-ht-light"));
    let rows = list_rows(&mut control);
    let row = rows
        .iter()
        .find(|row| row["host_terminal_id"] == "ht-light")
        .expect("ht-light row");
    assert_eq!(row["title"], "agent-light", "{row}");
    assert_eq!(row["observer_bind"], "bound", "{row}");
    assert_eq!(row["reservation_id"], "res-ht-light", "{row}");

    // A host theme refresh leaves the child's OSC 10/11 override in place.
    let mut refresh = attach(dir.path(), "ht-osc", None);
    write_msg(
        &mut refresh,
        &ClientMessage::SetTerminalTheme { theme: dark },
    );
    barrier(&mut refresh);

    let expected = [
        ("ht-light", "rgb:2020/2121/2222", "rgb:fafa/fbfb/fcfc"),
        ("ht-dark", "rgb:e0e0/e1e1/e2e2", "rgb:1010/1111/1212"),
        ("ht-osc", "rgb:1212/3434/5656", "rgb:6565/4343/2121"),
    ];
    for (seq, (id, fg, bg)) in (1..).zip(expected) {
        let marker = format!("MARK-{id}");
        let written = rpc(
            &mut control,
            "write",
            json!({
                "operation_seq": seq,
                "host_terminal_id": id,
                "kind": "text",
                "encoding": "utf8-b64",
                "data": base64::engine::general_purpose::STANDARD.encode(&marker),
            }),
        );
        assert_eq!(written["ok"], true, "{written}");
        let mut screen = String::new();
        wait_until(&format!("{id} answers after its marker"), || {
            screen = snapshot_text(&mut control, id);
            answer_after(&screen, &marker).is_some()
        });
        let answer = answer_after(&screen, &marker).unwrap_or_default();
        assert!(
            answer.contains(fg) && answer.contains(bg),
            "{id} answered {answer:?}"
        );
    }
}

#[test]
fn restore_adopts_listeners_without_rebinding() {
    let dir = temp_socket_dir();
    let spec = HelperSpec::new(dir.path(), vec![HelperPane::new("ht-1", "exec sleep 600")]);
    let host = restore(&spec);
    let _ = host.control();
    let bound: Bound =
        serde_json::from_slice(&std::fs::read(dir.path().join(BOUND_FILE)).expect("bound file"))
            .expect("decode bound file");

    assert_eq!(ino(&dir.path().join(CONTROL_SOCKET)), bound.control_ino);
    assert_eq!(ino(&dir.path().join(FRAMES_SOCKET)), bound.frames_ino);
    let pid_file = dir.path().join(PID_FILE);
    assert_eq!(ino(&pid_file), bound.pidfile_ino, "pidfile never replaced");
    assert_eq!(
        mtime_ns(&pid_file),
        bound.pidfile_mtime_ns,
        "pidfile never rewritten"
    );
    let published = std::fs::read_to_string(&pid_file).expect("pidfile");
    assert_eq!(published.trim(), host.pid.to_string());
    let frames = std::os::unix::net::UnixStream::connect(dir.path().join(FRAMES_SOCKET));
    assert!(frames.is_ok(), "the adopted frames listener accepts");
}

#[test]
fn restored_panes_accept_input_after_commit() {
    let dir = temp_socket_dir();
    let panes = ["ht-1", "ht-2"]
        .into_iter()
        .map(|id| {
            let mut pane = HelperPane::new(id, "stty -echo; echo READY; exec cat");
            pane.ready_text = Some("READY".into());
            pane
        })
        .collect();
    let host = restore(&HelperSpec::new(dir.path(), panes));
    let mut control = host.control();

    let mut seq = 0;
    for id in ["ht-1", "ht-2"] {
        for line in ["first", "second"] {
            seq += 1;
            let text = format!("{id}-{line}\n");
            let written = rpc(
                &mut control,
                "write",
                json!({
                    "operation_seq": seq,
                    "host_terminal_id": id,
                    "kind": "text",
                    "encoding": "utf8-b64",
                    "data": base64::engine::general_purpose::STANDARD.encode(text),
                }),
            );
            assert_eq!(written["ok"], true, "{written}");
        }
    }

    for id in ["ht-1", "ht-2"] {
        let first = format!("{id}-first");
        let second = format!("{id}-second");
        let mut text = String::new();
        wait_until(&format!("{id} echoes its input"), || {
            text = snapshot_text(&mut control, id);
            text.contains(&second)
        });
        assert_eq!(text.matches(&first).count(), 1, "{text}");
        assert_eq!(text.matches(&second).count(), 1, "{text}");
        assert!(
            text.find(&first) < text.find(&second),
            "writes arrive in order: {text}"
        );
    }
}

/// Faults the second staged pane, `ht-b`, in the run `var` names:
/// `GTERM_RESTORE_FAULT` for the first restore, `GTERM_FALLBACK_FAULT` for
/// the fallback.
fn fault(var: &str, kind: &str) -> [(String, String); 2] {
    [
        ("GTERM_TEST_HELPER".into(), "1".into()),
        (var.into(), format!("{kind}:ht-b")),
    ]
}

/// The `gterm host restored` lines the host logged.
fn restored_lines(socket_dir: &Path) -> Vec<String> {
    std::fs::read_to_string(socket_dir.join("gterm.log"))
        .unwrap_or_default()
        .lines()
        .filter(|line| line.contains("gterm host restored"))
        .map(str::to_string)
        .collect()
}

fn echo_pane(id: &str) -> HelperPane {
    let mut pane = HelperPane::new(id, "stty -echo; echo READY; exec cat");
    pane.ready_text = Some("READY".into());
    pane
}

#[test]
fn stage_failure_falls_back_with_checkpoint_intact() {
    for kind in ["decode", "panic", "actor"] {
        let dir = temp_socket_dir();
        let path = |name: &str| dir.path().join(name);
        let mut recorded =
            HelperPane::new("ht-c", "while [ ! -e c-go ]; do sleep 0.02; done; exit 4");
        recorded.window_trigger = Some(path("c-go"));
        recorded.recorded_exit = true;
        let mut queued = HelperPane::new(
            "ht-d",
            "stty -echo; while [ ! -e d-go ]; do sleep 0.02; done; \
             echo window-output; touch d-done; exec cat",
        );
        queued.window_trigger = Some(path("d-go"));
        queued.window_done = Some(path("d-done"));
        let mut spec = HelperSpec::new(
            dir.path(),
            vec![echo_pane("ht-a"), echo_pane("ht-b"), recorded, queued],
        );
        spec.env = fault("GTERM_RESTORE_FAULT", kind).to_vec();
        let host = restore(&spec);
        let mut control = host.control();

        let restored = committed_restores(dir.path(), kind);
        assert_eq!(restored.len(), 1, "{kind}: one restore: {restored:?}");
        assert!(
            restored[0].contains("Fallback"),
            "{kind}: the fallback image committed: {restored:?}"
        );

        let bound: Bound =
            serde_json::from_slice(&std::fs::read(path(BOUND_FILE)).expect("bound file"))
                .expect("decode bound file");
        assert_eq!(ino(&path(CONTROL_SOCKET)), bound.control_ino, "{kind}");
        assert_eq!(ino(&path(FRAMES_SOCKET)), bound.frames_ino, "{kind}");
        for id in ["ht-a", "ht-b", "ht-d"] {
            let pgid: i32 = std::fs::read_to_string(path(&pgid_file(id)))
                .expect("pgid file")
                .trim()
                .parse()
                .expect("pgid");
            // SAFETY: signal 0 only checks that the pane leader exists.
            let alive = unsafe { libc::kill(pgid, 0) } == 0;
            assert!(alive && !is_zombie(pgid as u32), "{kind}: {id} survives");
        }

        let mut events = host.control();
        let ack = rpc(&mut events, "subscribe_events", json!({"since": 0}));
        assert_eq!(ack["gap"], false, "{kind}: {ack}");
        let mut exits = HashMap::new();
        collect_exits(&mut events, &mut exits, |exits| exits.contains_key("ht-c"));
        assert_eq!(
            exits["ht-c"],
            vec![json!(4)],
            "{kind}: the real exit status"
        );

        let mut text = String::new();
        wait_until(&format!("{kind}: window output"), || {
            text = snapshot_text(&mut control, "ht-d");
            text.contains("window-output")
        });
        assert_writes_echo(&mut control, kind, &["ht-a", "ht-b", "ht-d"]);
    }
}

/// Waits for a restore to commit and returns every committed restore line.
fn committed_restores(socket_dir: &Path, label: &str) -> Vec<String> {
    let mut restored = Vec::new();
    wait_until(&format!("{label}: a restore commits"), || {
        restored = restored_lines(socket_dir);
        !restored.is_empty()
    });
    restored
}

/// Writes a line to each pane and waits for it to echo. An attempt records
/// its outcome before it releases the mutation gate (plan 1.3 step 7), so a
/// write refused `host_upgrading` just after the outcome is sent again.
fn assert_writes_echo(control: &mut UnixStream, label: &str, ids: &[&str]) {
    for (seq, id) in (1..).zip(ids) {
        let line = format!("{id}-after-{label}");
        let request = json!({
            "operation_seq": seq,
            "host_terminal_id": id,
            "kind": "text",
            "encoding": "utf8-b64",
            "data": base64::engine::general_purpose::STANDARD.encode(format!("{line}\n")),
        });
        let mut written = Value::Null;
        wait_until(&format!("{label}: the gate opens for {id}"), || {
            written = rpc(control, "write", request.clone());
            written["error"] != "host_upgrading"
        });
        assert_eq!(written["ok"], true, "{label}: {written}");
        wait_until(&format!("{label}: {id} echoes"), || {
            snapshot_text(control, id).contains(&line)
        });
    }
}

/// A new image that rejects the carried argv or config, before it can stage,
/// still falls back to the earlier image with every pane live.
#[test]
fn setup_failure_in_new_image_falls_back() {
    let cases: [(&str, &[&str]); 2] = [
        ("unknown-flag", &["--bogus"]),
        ("invalid-config", &["--max-attachments-total", "0"]),
    ];
    for (label, args) in cases {
        let dir = temp_socket_dir();
        let mut spec = HelperSpec::new(dir.path(), vec![echo_pane("ht-a"), echo_pane("ht-b")]);
        spec.primary_args = args.iter().map(|arg| arg.to_string()).collect();
        let host = restore(&spec);

        let restored = committed_restores(dir.path(), label);
        assert_eq!(restored.len(), 1, "{label}: one restore: {restored:?}");
        assert!(
            restored[0].contains("Fallback"),
            "{label}: the earlier image committed: {restored:?}"
        );
        assert_writes_echo(&mut host.control(), label, &["ht-a", "ht-b"]);
    }
}

#[test]
fn fallback_failure_and_wedged_restore_end_the_process() {
    let panes = || vec![echo_pane("ht-a"), echo_pane("ht-b")];

    // The fallback's own Stage fails.
    let dir = temp_socket_dir();
    let mut spec = HelperSpec::new(dir.path(), panes());
    spec.env = fault("GTERM_RESTORE_FAULT", "decode").to_vec();
    spec.env.extend(fault("GTERM_FALLBACK_FAULT", "decode"));
    let mut host = launch(&spec);
    host.wait_captured();
    let status = host.wait_exit(WAIT);
    assert_eq!(
        status.and_then(|status| status.code()),
        Some(70),
        "a failed fallback stage: {}",
        host.diagnostics()
    );

    // The fallback exec fails.
    let dir = temp_socket_dir();
    let mut spec = HelperSpec::new(dir.path(), panes());
    spec.env = fault("GTERM_RESTORE_FAULT", "decode").to_vec();
    spec.previous_image = Some(dir.path().join("missing-gterm"));
    let mut host = launch(&spec);
    host.wait_captured();
    let status = host.wait_exit(WAIT);
    assert_eq!(
        status.and_then(|status| status.code()),
        Some(70),
        "a failed fallback exec: {}",
        host.diagnostics()
    );

    // The named earlier image is not the one the attempt recorded: it must
    // not run.
    let dir = temp_socket_dir();
    let impostor = dir.path().join("impostor-gterm");
    std::fs::copy("/bin/sh", &impostor).expect("copy sh");
    let mut spec = HelperSpec::new(dir.path(), panes());
    spec.env = fault("GTERM_RESTORE_FAULT", "decode").to_vec();
    spec.previous_image = Some(impostor);
    let mut host = launch(&spec);
    host.wait_captured();
    let status = host.wait_exit(WAIT);
    assert_eq!(
        status.and_then(|status| status.code()),
        Some(70),
        "an unverified fallback image: {}",
        host.diagnostics()
    );

    // Stage wedges; the alarm the earlier image armed ends it well before
    // the state file's 30 s deadline.
    let dir = temp_socket_dir();
    let mut spec = HelperSpec::new(dir.path(), panes());
    spec.env = fault("GTERM_RESTORE_FAULT", "wedge").to_vec();
    spec.alarm_secs = Some(3);
    let mut host = launch(&spec);
    host.wait_captured();
    let status = host.wait_exit(WAIT);
    assert_eq!(
        status.and_then(|status| status.signal()),
        Some(libc::SIGALRM),
        "a wedged restore: {}",
        host.diagnostics()
    );
}

// Plan gterm-host-handover 1.3: in-process upgrades of a real host.

use gobby_terminal::host::handover::STATE_FILE;
use gobby_terminal::host::image::IMAGES_DIR;
use host_support::{
    candidate_script, committed_pane, control as connect_control, gterm_bin, held_probe,
    host_upgrade, process_exists, recv_json_within, send_held_upgrade, send_json, send_upgrade,
    spawn_host_with_env, try_ping, wait_outcome, wait_socket, write_text, write_token,
    CommittedPane, HostProc,
};

const UPGRADE_TOKEN: &str = "upgrade-token";
/// Echoes each input line; typed input itself is not echoed.
const ECHO: &str = "stty -echo; echo READY; exec cat";
const OUTCOME_WAIT: Duration = Duration::from_secs(30);
/// How long an attempt retries the mutation gate before deferring `host_busy`.
const GATE_WAIT: Duration = Duration::from_millis(500);

/// A real host from the test build that owns its socket dir.
fn live_host(env: &[(&str, &str)]) -> HostProc {
    let dir = temp_socket_dir();
    write_token(dir.path(), UPGRADE_TOKEN);
    let mut host = spawn_host_with_env(dir.path(), &[], env, &[]);
    wait_socket(&dir.path().join(CONTROL_SOCKET));
    host.own_socket_dir(dir);
    host
}

/// Commits `count` ready echo panes on `ctl`, using `operation_seq` 1..=count.
fn echo_panes(host: &mut HostProc, ctl: &mut UnixStream, count: u64) -> Vec<CommittedPane> {
    let panes: Vec<_> = (1..=count)
        .map(|n| committed_pane(host, ctl, n, &format!("term-{n}"), ECHO))
        .collect();
    for pane in &panes {
        wait_until(&format!("{} is ready", pane.host_terminal_id), || {
            snapshot_text(ctl, &pane.host_terminal_id).contains("READY")
        });
    }
    panes
}

fn ids(panes: &[CommittedPane]) -> Vec<&str> {
    panes
        .iter()
        .map(|pane| pane.host_terminal_id.as_str())
        .collect()
}

/// Every listed pane's `(host_terminal_id, pgid)`, sorted.
fn pane_rows(ctl: &mut UnixStream) -> Vec<(String, i64)> {
    let mut rows: Vec<_> = list_rows(ctl)
        .iter()
        .map(|row| {
            (
                row["host_terminal_id"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string(),
                row["pgid"].as_i64().unwrap_or_default(),
            )
        })
        .collect();
    rows.sort();
    rows
}

fn expected_rows(panes: &[CommittedPane]) -> Vec<(String, i64)> {
    let mut rows: Vec<_> = panes
        .iter()
        .map(|pane| (pane.host_terminal_id.clone(), i64::from(pane.pgid)))
        .collect();
    rows.sort();
    rows
}

fn ping(ctl: &mut UnixStream) -> Value {
    rpc(ctl, "ping", json!({}))
}

fn assert_outcome(ping: &Value, attempt_id: &str, outcome: &str, reason: Option<&str>) {
    let upgrade = &ping["upgrade"];
    assert_eq!(upgrade["phase"], "idle", "{ping}");
    let last = &upgrade["last_outcome"];
    assert_eq!(last["attempt_id"], attempt_id, "{ping}");
    assert_eq!(last["outcome"], outcome, "{ping}");
    if let Some(reason) = reason {
        assert_eq!(last["reason"], reason, "{ping}");
    }
}

fn wait_phase(socket_dir: &Path, attempt_id: &str, phase: &str) {
    wait_until(&format!("{attempt_id} reaches {phase}"), || {
        try_ping(socket_dir, UPGRADE_TOKEN).is_some_and(|ping| {
            ping["upgrade"]["attempt_id"] == attempt_id && ping["upgrade"]["phase"] == phase
        })
    });
}

fn pin_path(socket_dir: &Path, sha256: &str) -> std::path::PathBuf {
    socket_dir.join(IMAGES_DIR).join(format!("gterm-{sha256}"))
}

fn shutdown(host: &mut HostProc, ctl: &mut UnixStream) {
    let _ = rpc(ctl, "host_shutdown", json!({"grace_ms": 50}));
    assert!(
        host_support::wait_exit(host, Duration::from_secs(10)).is_some(),
        "host exits after host_shutdown"
    );
}

/// Reads lines until the peer closes, as the old image's connections do at exec.
fn lines_until_closed(stream: &mut UnixStream, timeout: Duration) -> Vec<Value> {
    // macOS refuses a timeout with EINVAL once the peer has closed; the
    // buffered lines then read to EOF without blocking.
    if let Err(err) = stream.set_read_timeout(Some(Duration::from_millis(200))) {
        assert_eq!(err.kind(), ErrorKind::InvalidInput, "read timeout: {err}");
    }
    let deadline = Instant::now() + timeout;
    let mut reader = BufReader::new(stream);
    let mut lines = Vec::new();
    let mut line = String::new();
    loop {
        match reader.read_line(&mut line) {
            Ok(0) => return lines,
            Ok(_) => {
                lines.push(serde_json::from_str(line.trim_end()).expect("line json"));
                line.clear();
            }
            Err(err) if matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                assert!(
                    Instant::now() < deadline,
                    "the connection outlived the exec: {lines:?}"
                );
            }
            Err(err) => panic!("read lines: {err}"),
        }
    }
}

/// Events that arrive within `span`.
fn events_for(stream: &mut UnixStream, span: Duration) -> Vec<Value> {
    let deadline = Instant::now() + span;
    let mut events = Vec::new();
    while Instant::now() < deadline {
        if let Some(event) = next_event(stream, Duration::from_millis(100)) {
            events.push(event);
        }
    }
    events
}

/// A request whose reply may never come because the host is ending.
fn try_request(stream: &mut UnixStream, request: &Value) -> Option<Value> {
    let mut line = serde_json::to_vec(request).ok()?;
    line.push(b'\n');
    stream.write_all(&line).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut reader = BufReader::with_capacity(1, stream);
    let mut reply = String::new();
    match reader.read_line(&mut reply) {
        Ok(0) | Err(_) => None,
        Ok(_) => serde_json::from_str(reply.trim_end()).ok(),
    }
}

/// Sends `list` until the connection closes, or a reply takes over 5 s, and
/// returns how many were answered.
fn list_until_closed(socket_dir: &Path) -> usize {
    let mut stream = connect_control(socket_dir, UPGRADE_TOKEN);
    let mut answered = 0;
    while try_request(
        &mut stream,
        &json!({"method": "list", "id": format!("list-{answered}")}),
    )
    .is_some()
    {
        answered += 1;
    }
    answered
}

/// Waits for the host to die and returns its status and when it died.
fn wait_death(host: &mut HostProc, within: Duration) -> (std::process::ExitStatus, Instant) {
    let status = host_support::wait_exit(host, within).expect("the host ends");
    (status, Instant::now())
}

/// Plan gterm-host-handover 1.3.1. The second upgrade starts from a restored
/// host, whose argv must not carry the first state file.
#[test]
fn upgrade_keeps_pids_epoch_and_panes() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let panes = echo_panes(&mut host, &mut ctl, 2);
    let rows = expected_rows(&panes);
    assert_eq!(pane_rows(&mut ctl), rows);
    let before = ping(&mut ctl);
    assert_eq!(before["generation"], 0, "{before}");

    for (generation, attempt_id) in [(1, "attempt-first"), (2, "attempt-second")] {
        let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
        let accepted = host_upgrade(&mut upgrader, &gterm_bin(), attempt_id, Value::Null);
        assert_eq!(accepted["accepted"], true, "{attempt_id}: {accepted}");
        let after = wait_outcome(&dir, UPGRADE_TOKEN, attempt_id, OUTCOME_WAIT);
        assert_outcome(&after, attempt_id, "succeeded", None);
        assert_eq!(
            after["upgrade"]["last_outcome"]["candidate_sha256"], before["binary_sha256"],
            "{after}"
        );
        assert_eq!(after["generation"], generation, "{after}");
        assert_eq!(after["host_pid"], host.id(), "{after}");
        assert_eq!(after["host_epoch"], before["host_epoch"], "{after}");
        assert!(
            host.try_wait().expect("host status").is_none(),
            "{attempt_id}: the host process never exited"
        );
        let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
        assert_eq!(
            pane_rows(&mut ctl),
            rows,
            "{attempt_id}: same panes and child pids"
        );
        for pane in &panes {
            assert!(
                process_exists(pane.pgid),
                "{attempt_id}: {} lives",
                pane.host_terminal_id
            );
        }
        assert_writes_echo(&mut ctl, attempt_id, &ids(&panes));
    }
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    shutdown(&mut host, &mut ctl);
}

/// Plan gterm-host-handover 1.3.3.
#[test]
fn probe_refusal_leaves_panes_untouched() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let panes = echo_panes(&mut host, &mut ctl, 1);
    let id = panes[0].host_terminal_id.clone();
    let running = ping(&mut ctl)["binary_sha256"]
        .as_str()
        .expect("running sha")
        .to_string();

    for (label, body, attempt_id, slow) in [
        ("exits-non-zero", "exit 3", "attempt-exit", false),
        ("times-out", "sleep 30", "attempt-timeout", true),
    ] {
        let candidate = candidate_script(&dir, &format!("{label}.sh"), body);
        let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
        let started = Instant::now();
        send_upgrade(&mut upgrader, &candidate, attempt_id, Value::Null);
        if slow {
            wait_phase(&dir, attempt_id, "probing");
            let mut writer = connect_control(&dir, UPGRADE_TOKEN);
            assert_writes_echo(&mut writer, &format!("{label}-probing"), &[id.as_str()]);
        }
        let refused = recv_json_within(&mut upgrader, Duration::from_secs(20));
        let elapsed = started.elapsed();
        assert_eq!(refused["ok"], false, "{label}: {refused}");
        assert_eq!(refused["error"], "upgrade_refused", "{label}: {refused}");
        assert!(refused["detail"].is_string(), "{label}: {refused}");
        if slow {
            assert!(
                elapsed >= Duration::from_millis(4500) && elapsed < Duration::from_secs(8),
                "{label}: the probe is cut off at 5 s, took {elapsed:?}"
            );
        }
        let after = ping(&mut ctl);
        assert_outcome(&after, attempt_id, "refused", None);
        assert_eq!(after["generation"], 0, "{after}");
        let sha = after["upgrade"]["last_outcome"]["candidate_sha256"]
            .as_str()
            .expect("candidate sha")
            .to_string();
        assert_ne!(sha, running, "{label}");
        assert!(
            !pin_path(&dir, &sha).exists(),
            "{label}: the refused pin is removed"
        );
        assert!(
            pin_path(&dir, &running).exists(),
            "{label}: the running pin stays"
        );
        let mut writer = connect_control(&dir, UPGRADE_TOKEN);
        assert_writes_echo(&mut writer, label, &[id.as_str()]);
    }
    shutdown(&mut host, &mut ctl);
}

/// Plan gterm-host-handover 1.3.8.
#[test]
fn committed_panes_are_admitted_and_pending_reservations_are_busy() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut kept = connect_control(&dir, UPGRADE_TOKEN);
    let mut panes = echo_panes(&mut host, &mut kept, 1);
    {
        let mut creator = connect_control(&dir, UPGRADE_TOKEN);
        let orphan = committed_pane(&mut host, &mut creator, 1, "orphan", ECHO);
        wait_until("the orphan is ready", || {
            snapshot_text(&mut creator, &orphan.host_terminal_id).contains("READY")
        });
        panes.push(orphan);
    }
    let rows = expected_rows(&panes);

    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let pending = rpc(
        &mut ctl,
        "reserve_observer",
        json!({"terminal_id": "pending", "reserve_key": "pending"}),
    );
    assert_eq!(pending["ok"], true, "{pending}");
    let busy = host_upgrade(&mut ctl, &gterm_bin(), "attempt-pending", Value::Null);
    assert_eq!(busy["error"], "host_busy", "{busy}");
    assert!(ping(&mut ctl)["upgrade"]["last_outcome"].is_null());
    let released = rpc(
        &mut ctl,
        "release_observer",
        json!({"reservation_id": pending["reservation_id"], "reserve_key": "pending"}),
    );
    assert_eq!(released["released"], true, "{released}");

    let accepted = host_upgrade(&mut ctl, &gterm_bin(), "attempt-committed", Value::Null);
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-committed", OUTCOME_WAIT);
    assert_outcome(&after, "attempt-committed", "succeeded", None);
    assert_eq!(after["generation"], 1, "{after}");
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    assert_eq!(pane_rows(&mut ctl), rows);
    assert_writes_echo(&mut ctl, "committed", &ids(&panes));

    let pending = rpc(
        &mut ctl,
        "reserve_observer",
        json!({"terminal_id": "pending-again", "reserve_key": "pending-again"}),
    );
    assert_eq!(pending["ok"], true, "{pending}");
    let busy = host_upgrade(&mut ctl, &gterm_bin(), "attempt-pending-again", Value::Null);
    assert_eq!(busy["error"], "host_busy", "{busy}");
    shutdown(&mut host, &mut ctl);
}

/// Plan gterm-host-handover 1.3.9.
#[test]
fn background_mutators_wait_through_exec() {
    // Holds the early pane's watcher in this image until the write guard.
    let mut host = live_host(&[("GTERM_TEST_EXIT_WATCHER_HOLD", "early")]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let survivor = echo_panes(&mut host, &mut ctl, 1).remove(0);
    let early = committed_pane(
        &mut host,
        &mut ctl,
        2,
        "early",
        "while [ ! -e early-go ]; do sleep 0.02; done; exit 5",
    );
    let exiting = committed_pane(
        &mut host,
        &mut ctl,
        3,
        "exiting",
        "while [ ! -e exit-go ]; do sleep 0.02; done; exit 7",
    );
    let mut old_events = connect_control(&dir, UPGRADE_TOKEN);
    let ack = rpc(&mut old_events, "subscribe_events", json!({}));
    assert_eq!(ack["ok"], true, "{ack}");
    let cursor = ack["seq"].as_u64().expect("event cursor");

    // An exit recorded before the write guard, whose watcher is still held:
    // a `list` just before the guard keeps its slot.
    std::fs::write(dir.join("early-go"), b"").expect("release the early pane");
    wait_until("the early child ends", || {
        !process_exists(early.pgid) || is_zombie(early.pgid as u32)
    });
    assert!(
        pane_rows(&mut ctl)
            .iter()
            .any(|(id, _)| id == &early.host_terminal_id),
        "the early pane left the list while its watcher was held"
    );

    // `list` runs from before the write guard until the exec closes it.
    let lister = std::thread::spawn({
        let dir = dir.clone();
        move || list_until_closed(&dir)
    });
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let accepted = host_upgrade(
        &mut upgrader,
        &gterm_bin(),
        "attempt-window",
        json!({"hold_accepted_ms": 1500}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    std::fs::write(dir.join("exit-go"), b"").expect("release the exiting pane");
    wait_until("the exiting child ends", || {
        !process_exists(exiting.pgid) || is_zombie(exiting.pgid as u32)
    });
    let mut window_list = connect_control(&dir, UPGRADE_TOKEN);
    send_json(
        &mut window_list,
        &json!({"method": "list", "id": "window-list"}),
    );

    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-window", OUTCOME_WAIT);
    assert_outcome(&after, "attempt-window", "succeeded", None);
    assert!(
        lines_until_closed(&mut window_list, OUTCOME_WAIT).is_empty(),
        "a list in the window is never answered by the old image"
    );
    let before_exec = lines_until_closed(&mut old_events, OUTCOME_WAIT);
    assert!(
        before_exec
            .iter()
            .all(|event| event["event"] != "terminal_exited"),
        "the event cursor advanced before exec: {before_exec:?}"
    );
    assert!(
        lister.join().expect("lister") > 0,
        "list answered before the window"
    );

    let mut resumed = connect_control(&dir, UPGRADE_TOKEN);
    let ack = rpc(&mut resumed, "subscribe_events", json!({"since": cursor}));
    assert_eq!(ack["gap"], false, "{ack}");
    let events = events_for(&mut resumed, Duration::from_secs(2));
    let exits: Vec<_> = events
        .iter()
        .filter(|event| event["event"] == "terminal_exited")
        .collect();
    assert_eq!(exits.len(), 2, "{events:?}");
    for (pane, code) in [(&early, 5), (&exiting, 7)] {
        assert!(
            exits.iter().any(
                |exit| exit["host_terminal_id"] == pane.host_terminal_id.as_str()
                    && exit["exit_code"] == code
            ),
            "one exit with {code}: {events:?}"
        );
    }

    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    wait_until("the exited panes leave the list after their events", || {
        pane_rows(&mut ctl)
            .iter()
            .all(|(id, _)| id != &early.host_terminal_id && id != &exiting.host_terminal_id)
    });
    assert_writes_echo(&mut ctl, "window", &[survivor.host_terminal_id.as_str()]);
    shutdown(&mut host, &mut ctl);
}

/// One 1.3.4 failure: the fault, what the host must record, and the setup it needs.
struct RollbackCase {
    label: &'static str,
    /// The `test_fault`, given the first pane's `host_terminal_id`.
    fault: fn(&str) -> Value,
    outcome: &'static str,
    reason: &'static str,
    errno: Option<i64>,
    /// A pane whose child reads nothing until the outcome is recorded,
    /// holding a write the actor cannot drain inside its 2 s quiesce cap.
    stalled_writer: bool,
    /// A directory where the state file goes, so its rename fails.
    block_state_file: bool,
    /// A pane whose child exits after acceptance and before the freeze; its
    /// exit must settle exactly once after the rollback.
    exiting_pane: bool,
    /// Replace the candidate's pin with other bytes after acceptance.
    tamper_pin: bool,
}

fn no_fault(_: &str) -> Value {
    Value::Null
}

fn rollback_case(case: &RollbackCase) {
    let label = case.label;
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let mut panes = echo_panes(&mut host, &mut ctl, 2);
    if case.stalled_writer {
        let stalled = committed_pane(
            &mut host,
            &mut ctl,
            3,
            "stalled",
            "stty raw -echo; while [ ! -e stall-go ]; do sleep 0.02; done; exec cat",
        );
        let filler = format!("{}\r\n", "x".repeat(79)).repeat(3000);
        let written = write_text(&mut ctl, 4, &stalled.host_terminal_id, &filler);
        assert_eq!(written["ok"], true, "{label}: {written}");
        panes.push(stalled);
    }
    if case.block_state_file {
        std::fs::create_dir(dir.join(STATE_FILE)).expect("block the state file");
    }
    let exiting = case.exiting_pane.then(|| {
        let seq = if case.stalled_writer { 5 } else { 3 };
        let script = "while [ ! -e exit-go ]; do sleep 0.02; done; exit 7";
        committed_pane(&mut host, &mut ctl, seq, "exiting", script)
    });
    let control_ino = ino(&dir.join(CONTROL_SOCKET));
    let frames_ino = ino(&dir.join(FRAMES_SOCKET));
    let mut events = connect_control(&dir, UPGRADE_TOKEN);
    assert_eq!(rpc(&mut events, "subscribe_events", json!({}))["ok"], true);

    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let fault = (case.fault)(&panes[0].host_terminal_id);
    let accepted = host_upgrade(&mut upgrader, &gterm_bin(), label, fault);
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{label}: {accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));
    if case.tamper_pin {
        // A new inode: the running image keeps its own.
        use std::os::unix::fs::PermissionsExt;
        let tampered = dir.join("tampered");
        std::fs::write(&tampered, b"tampered").expect("write other bytes");
        std::fs::set_permissions(&tampered, std::fs::Permissions::from_mode(0o700))
            .expect("tampered mode");
        let sha = accepted["candidate_sha256"]
            .as_str()
            .expect("candidate sha");
        std::fs::rename(&tampered, pin_path(&dir, sha)).expect("replace the pin");
    }
    if let Some(exiting) = &exiting {
        std::fs::write(dir.join("exit-go"), b"").expect("release the exiting pane");
        wait_until("the exiting child ends", || {
            !process_exists(exiting.pgid) || is_zombie(exiting.pgid as u32)
        });
    }

    let after = wait_outcome(&dir, UPGRADE_TOKEN, label, OUTCOME_WAIT);
    if case.stalled_writer {
        std::fs::write(dir.join("stall-go"), b"").expect("release the stalled pane");
    }
    assert_outcome(&after, label, case.outcome, Some(case.reason));
    if let Some(errno) = case.errno {
        assert_eq!(
            after["upgrade"]["last_outcome"]["errno"], errno,
            "{label}: {after}"
        );
    }
    assert_eq!(after["generation"], 0, "{label}: {after}");
    assert_eq!(after["host_pid"], host.id(), "{label}: {after}");
    assert_eq!(
        ino(&dir.join(CONTROL_SOCKET)),
        control_ino,
        "{label}: same control socket"
    );
    assert_eq!(
        ino(&dir.join(FRAMES_SOCKET)),
        frames_ino,
        "{label}: same frames socket"
    );
    let failed = events_for(&mut events, Duration::from_secs(1));
    assert!(
        failed
            .iter()
            .any(|event| event["event"] == "host_upgrade_failed"
                && event["attempt_id"] == label
                && event["reason"] == case.reason),
        "{label}: host_upgrade_failed with the reason: {failed:?}"
    );
    if let Some(exiting) = &exiting {
        let later = events_for(&mut events, Duration::from_secs(1));
        let stream: Vec<_> = failed.iter().chain(&later).collect();
        let exits: Vec<_> = (0..stream.len())
            .filter(|&at| stream[at]["event"] == "terminal_exited")
            .collect();
        assert_eq!(exits.len(), 1, "{label}: one exit event: {stream:?}");
        let exit = stream[exits[0]];
        assert_eq!(exit["host_terminal_id"], exiting.host_terminal_id.as_str());
        assert_eq!(exit["exit_code"], 7, "{label}: {exit:?}");
        // The watcher waits on the gate, which the rollback holds until it
        // has emitted `host_upgrade_failed`.
        let failed_at = stream
            .iter()
            .position(|event| event["event"] == "host_upgrade_failed")
            .expect("host_upgrade_failed");
        assert!(
            failed_at < exits[0],
            "{label}: the exit precedes host_upgrade_failed: {stream:?}"
        );
    }

    let mut writer = connect_control(&dir, UPGRADE_TOKEN);
    assert_writes_echo(&mut writer, label, &ids(&panes));

    // The attempt cleared its alarm: the host outlives the hard deadline.
    let past_deadline = accepted_at + remaining + Duration::from_millis(1500);
    std::thread::sleep(past_deadline.saturating_duration_since(Instant::now()));
    assert!(
        host.try_wait().expect("host status").is_none(),
        "{label}: the attempt's alarm ended the host"
    );
    assert!(
        try_ping(&dir, UPGRADE_TOKEN).is_some(),
        "{label}: the host answers"
    );
    shutdown(&mut host, &mut writer);
}

/// Plan gterm-host-handover 1.3.4. The cases run on their own hosts at once,
/// since each waits out its attempt's deadline.
#[test]
fn every_pre_exec_failure_rolls_back() {
    let cases = [
        RollbackCase {
            label: "quiesce-timeout",
            fault: no_fault,
            outcome: "aborted",
            reason: "quiesce_timeout",
            errno: None,
            stalled_writer: true,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            label: "late-quiesce",
            fault: |id| json!({"quiesce_ack": {"host_terminal_id": id, "after_budget_ms": 300}}),
            outcome: "aborted",
            reason: "quiesce_timeout",
            errno: None,
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            label: "late-rollback-ack",
            // Past resume's first attempt, so only its retry sees the ack.
            fault: |id| json!({"quiesce_ack": {"host_terminal_id": id, "after_budget_ms": 2500}}),
            outcome: "aborted",
            reason: "quiesce_timeout",
            errno: None,
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            label: "encode-error",
            fault: |id| json!({"encode_error": id}),
            outcome: "aborted",
            reason: "capture_failed",
            errno: None,
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            label: "state-write-error",
            fault: no_fault,
            outcome: "aborted",
            reason: "state_write_failed",
            errno: None,
            stalled_writer: false,
            block_state_file: true,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            // Every async check passes; the masked exec job reaches the soft
            // cutoff after entering `exec`, so only the commit gate refuses.
            label: "soft-cutoff-at-exec",
            fault: |_| json!({"soft_deadline_in_exec": true}),
            outcome: "aborted",
            reason: "soft_deadline",
            errno: None,
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: false,
        },
        RollbackCase {
            label: "exec-failure",
            fault: |_| json!({"exec_error": true, "hold_accepted_ms": 1000}),
            outcome: "rolled_back",
            reason: "exec_failed",
            errno: Some(i64::from(libc::ENOENT)),
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: true,
            tamper_pin: false,
        },
        RollbackCase {
            label: "pin-tamper",
            fault: |_| json!({"hold_accepted_ms": 1000}),
            outcome: "aborted",
            reason: "pin_mismatch",
            errno: None,
            stalled_writer: false,
            block_state_file: false,
            exiting_pane: false,
            tamper_pin: true,
        },
    ];
    std::thread::scope(|scope| {
        let runs: Vec<_> = cases
            .iter()
            .map(|case| (case.label, scope.spawn(move || rollback_case(case))))
            .collect();
        let failed: Vec<_> = runs
            .into_iter()
            .filter_map(|(label, run)| run.join().err().map(|_| label))
            .collect();
        assert!(failed.is_empty(), "failed cases: {failed:?}");
    });
}

#[test]
fn overdue_capture_ends_the_host_inside_rollback_reserve() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let id = echo_panes(&mut host, &mut ctl, 1)
        .remove(0)
        .host_terminal_id;
    let accepted = host_upgrade(
        &mut ctl,
        &gterm_bin(),
        "attempt-capture-reserve",
        json!({"soft_deadline_in_capture": true, "encode_error": id}),
    );
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));
    let (status, died_at) = wait_death(&mut host, remaining);
    assert_eq!(status.signal(), Some(libc::SIGALRM), "{status:?}");
    // The injected capture stays active one second past the soft cutoff.
    // Fatal exit must leave time in the three-second rollback reserve rather
    // than waiting for the attempt's hard alarm. Allow only observation skew
    // at the reserve's start, as in the existing hard-alarm tests.
    assert!(
        died_at + Duration::from_millis(500) >= accepted_at + remaining - Duration::from_secs(3),
        "capture ended before the rollback reserve: {status:?}"
    );
    assert!(
        died_at + Duration::from_secs(2) < accepted_at + remaining,
        "capture consumed the rollback reserve: {status:?}"
    );
}

/// Plan gterm-host-handover 1.3.10.
#[test]
fn persistent_rollback_failure_ends_the_host() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let panes = echo_panes(&mut host, &mut ctl, 2);
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let accepted = host_upgrade(
        &mut upgrader,
        &gterm_bin(),
        "attempt-stuck",
        json!({
            "hold_accepted_ms": 1000,
            "encode_error": panes[1].host_terminal_id,
            "rollback_fail": panes[0].host_terminal_id,
        }),
    );
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));

    // Until the host ends, no write gets through the gate.
    let mut writer = connect_control(&dir, UPGRADE_TOKEN);
    let mut seq = 1;
    while host_support::wait_exit(&mut host, Duration::from_millis(20)).is_none() {
        let reply = try_request(
            &mut writer,
            &json!({
                "method": "write",
                "id": format!("write-{seq}"),
                "operation_seq": seq,
                "host_terminal_id": panes[0].host_terminal_id,
                "kind": "text",
                "encoding": "utf8-b64",
                "data": "eAo=",
            }),
        );
        if let Some(reply) = reply {
            assert_eq!(reply["error"], "host_upgrading", "{reply}");
        }
        seq += 1;
        assert!(
            accepted_at.elapsed() < OUTCOME_WAIT,
            "the host outlived its failed rollback"
        );
    }
    let (status, died_at) = wait_death(&mut host, Duration::from_secs(1));
    assert_eq!(status.signal(), Some(libc::SIGALRM), "{status:?}");
    assert!(
        died_at < accepted_at + remaining - Duration::from_secs(5),
        "the host raised SIGALRM itself, well before its alarm"
    );
}

/// Plan gterm-host-handover 1.3.11.
#[test]
fn fallback_image_survives_other_starts_and_same_image_attempts() {
    let mut host = live_host(&[("GTERM_RESTORE_FAULT", "decode:ht-1")]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let panes = echo_panes(&mut host, &mut ctl, 2);
    assert_eq!(panes[0].host_terminal_id, "ht-1");
    let running = ping(&mut ctl)["binary_sha256"]
        .as_str()
        .expect("running sha")
        .to_string();
    let pin = pin_path(&dir, &running);
    assert!(pin.exists(), "the running image is pinned");

    let mut loser = spawn_host_with_env(&dir, &[], &[], &[]);
    assert!(
        host_support::wait_exit(&mut loser, Duration::from_secs(10)).is_some(),
        "a second start on a live socket dir loses"
    );
    assert!(host.try_wait().expect("host status").is_none());
    assert!(pin.exists(), "a losing start keeps the running pin");

    let refusal = candidate_script(&dir, "refusal.sh", "exit 3");
    let refused = host_upgrade(&mut ctl, &refusal, "attempt-probe", Value::Null);
    assert_eq!(refused["error"], "upgrade_refused", "{refused}");
    assert!(pin.exists(), "a refused probe keeps the running pin");

    let refused = upgrade_once_idle(
        &mut ctl,
        &gterm_bin(),
        "attempt-same-refused",
        json!({"refuse_probe": true}),
    );
    assert_eq!(refused["error"], "upgrade_refused", "{refused}");
    assert_outcome(&ping(&mut ctl), "attempt-same-refused", "refused", None);
    assert!(pin.exists(), "a same-image refusal keeps the running pin");

    let accepted = upgrade_once_idle(
        &mut ctl,
        &gterm_bin(),
        "attempt-same-rollback",
        json!({"encode_error": panes[1].host_terminal_id}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-same-rollback", OUTCOME_WAIT);
    assert_outcome(
        &after,
        "attempt-same-rollback",
        "aborted",
        Some("capture_failed"),
    );
    assert!(pin.exists(), "a same-image rollback keeps the running pin");

    let accepted = upgrade_once_idle(&mut ctl, &gterm_bin(), "attempt-fallback", Value::Null);
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-fallback", OUTCOME_WAIT);
    assert_outcome(&after, "attempt-fallback", "fallback", None);
    assert_eq!(after["host_pid"], host.id(), "{after}");
    let restored = committed_restores(&dir, "fallback");
    assert!(
        restored.iter().any(|line| line.contains("Fallback")),
        "the earlier image committed: {restored:?}"
    );
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    assert_writes_echo(&mut ctl, "fallback", &ids(&panes));
    shutdown(&mut host, &mut ctl);
}

/// Sends `host_upgrade` again while it is refused `upgrade_in_progress`: an
/// attempt replies before it releases the upgrade lock (plan 1.3 step 7), so
/// one sent right after it can meet the lock still held.
fn upgrade_once_idle(stream: &mut UnixStream, exe: &Path, attempt_id: &str, fault: Value) -> Value {
    let mut reply = Value::Null;
    wait_until(&format!("{attempt_id} is admitted"), || {
        reply = host_upgrade(stream, exe, attempt_id, fault.clone());
        reply["error"] != "upgrade_in_progress"
    });
    reply
}

/// 1.3.12 on its own host: a `try_write` timeout defers; the attempt right
/// after a refused one keeps its alarm, which ends a stalled cleanup.
fn deferral_then_stalled_cleanup() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let id = echo_panes(&mut host, &mut ctl, 1)
        .remove(0)
        .host_terminal_id;

    // The probe blocks until the test releases it, so the batch below holds
    // the gate before the probe ends, however long the probe takes to start,
    // and through the whole gate wait after it.
    let (held, fifo) = held_probe(&dir, "held-probe");
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let release = send_held_upgrade(&mut upgrader, &held, &fifo, "attempt-busy");

    let mut batcher = connect_control(&dir, UPGRADE_TOKEN);
    send_json(
        &mut batcher,
        &json!({
            "method": "write_batch",
            "operation_seq": 1,
            "targets": [{
                "recipient_id": "batch",
                "host_terminal_id": id,
                "operations": [
                    {"kind": "text", "encoding": "utf8-b64", "data": "Zmlyc3QK", "delay_ms": 0},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eAo=", "delay_ms": 1000},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eAo=", "delay_ms": 1000},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eAo=", "delay_ms": 1000},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eAo=", "delay_ms": 1000},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eAo=", "delay_ms": 1000},
                ],
            }],
        }),
    );
    wait_until("the batch's first operation lands", || {
        snapshot_text(&mut ctl, &id).contains("first")
    });
    let released = Instant::now();
    drop(release);
    let busy = recv_json_within(&mut upgrader, Duration::from_secs(20));
    let waited = released.elapsed();
    assert_eq!(busy["error"], "host_busy", "{busy}");
    // The gate wait starts only after the released probe exits, and it gives
    // up while the batch still holds the gate.
    assert!(
        (GATE_WAIT..GATE_WAIT + Duration::from_secs(2)).contains(&waited),
        "deferred {waited:?} after the probe's release"
    );
    assert_outcome(
        &ping(&mut ctl),
        "attempt-busy",
        "deferred",
        Some("host_busy"),
    );
    assert_eq!(
        recv_json_within(&mut batcher, Duration::from_secs(10))["ok"],
        true
    );

    let refusal = candidate_script(&dir, "refusal.sh", "exit 3");
    let mut next = connect_control(&dir, UPGRADE_TOKEN);
    let refused = host_upgrade(&mut ctl, &refusal, "attempt-refused", Value::Null);
    assert_eq!(refused["error"], "upgrade_refused", "{refused}");
    let accepted = upgrade_once_idle(
        &mut next,
        &gterm_bin(),
        "attempt-stall",
        json!({"encode_error": id, "stall_cleanup": true}),
    );
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));
    let (status, died_at) = wait_death(&mut host, remaining + Duration::from_secs(5));
    assert_eq!(status.signal(), Some(libc::SIGALRM), "{status:?}");
    assert!(
        died_at + Duration::from_millis(500) >= accepted_at + remaining,
        "the stalled cleanup ran until the attempt's own alarm"
    );
}

/// 1.3.12 on its own host: a reservation made during the probe defers; after
/// a recovered rollback the gate reopens while the alarm is still armed.
fn reservation_deferral_then_alarm_after_release() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let id = echo_panes(&mut host, &mut ctl, 1)
        .remove(0)
        .host_terminal_id;
    let (held, fifo) = held_probe(&dir, "held-accept");

    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let release = send_held_upgrade(&mut upgrader, &held, &fifo, "attempt-reserve");
    let late = rpc(
        &mut ctl,
        "reserve_observer",
        json!({"terminal_id": "late", "reserve_key": "late"}),
    );
    assert_eq!(late["ok"], true, "{late}");
    drop(release);
    let deferred = recv_json_within(&mut upgrader, Duration::from_secs(20));
    assert_eq!(deferred["error"], "host_busy", "{deferred}");
    assert_outcome(
        &ping(&mut ctl),
        "attempt-reserve",
        "deferred",
        Some("host_busy"),
    );
    let released = rpc(
        &mut ctl,
        "release_observer",
        json!({"reservation_id": late["reservation_id"], "reserve_key": "late"}),
    );
    assert_eq!(released["released"], true, "{released}");

    let accepted = upgrade_once_idle(
        &mut upgrader,
        &gterm_bin(),
        "attempt-release",
        json!({"encode_error": id, "hold_after_guard_release_ms": 30000}),
    );
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));
    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-release", OUTCOME_WAIT);
    assert_outcome(&after, "attempt-release", "aborted", Some("capture_failed"));
    let mut writer = connect_control(&dir, UPGRADE_TOKEN);
    assert_writes_echo(&mut writer, "released", &[id.as_str()]);
    let (status, died_at) = wait_death(&mut host, remaining + Duration::from_secs(5));
    assert_eq!(status.signal(), Some(libc::SIGALRM), "{status:?}");
    assert!(
        died_at + Duration::from_millis(500) >= accepted_at + remaining,
        "the alarm stayed armed after the write guard was released"
    );
}

/// 1.3.12 on its own host with no live pane: the drain ends at once, and the
/// host still answers the deferral before it exits. `control_protocol`
/// covers a drain that lasts its grace.
fn draining_deferral() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let (held, fifo) = held_probe(&dir, "held-accept");
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let release = send_held_upgrade(&mut upgrader, &held, &fifo, "attempt-drain");
    let draining = rpc(&mut ctl, "host_shutdown", json!({"grace_ms": 3000}));
    assert_eq!(draining["ok"], true, "{draining}");
    drop(release);
    let deferred = recv_json_within(&mut upgrader, Duration::from_secs(20));
    assert_eq!(deferred["error"], "host_draining", "{deferred}");
    assert!(host_support::wait_exit(&mut host, Duration::from_secs(15)).is_some());
}

/// Plan gterm-host-handover 1.3.12.
#[test]
fn pre_accept_returns_and_rollback_cleanup_are_bounded() {
    std::thread::scope(|scope| {
        let runs = [
            (
                "deferral-then-stalled-cleanup",
                scope.spawn(deferral_then_stalled_cleanup),
            ),
            (
                "reservation-deferral-then-alarm-after-release",
                scope.spawn(reservation_deferral_then_alarm_after_release),
            ),
            ("draining-deferral", scope.spawn(draining_deferral)),
        ];
        let failed: Vec<_> = runs
            .into_iter()
            .filter_map(|(label, run)| run.join().err().map(|_| label))
            .collect();
        assert!(failed.is_empty(), "failed cases: {failed:?}");
    });
}

/// Plan gterm-host-handover 1.3.13.
#[test]
fn stalled_quiesce_recovers_within_hard_budget() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let panes = echo_panes(&mut host, &mut ctl, 2);
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let accepted = host_upgrade(
        &mut upgrader,
        &gterm_bin(),
        "attempt-stalled",
        json!({"quiesce_ack": {"host_terminal_id": panes[0].host_terminal_id, "hold": true}}),
    );
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));

    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-stalled", OUTCOME_WAIT);
    let recovered_at = Instant::now();
    assert_outcome(
        &after,
        "attempt-stalled",
        "aborted",
        Some("quiesce_timeout"),
    );
    assert!(
        recovered_at < accepted_at + remaining - Duration::from_millis(2500),
        "the attempt gave up by the soft cutoff and recovered inside its reserve"
    );
    assert_eq!(after["generation"], 0, "{after}");
    assert_eq!(after["host_pid"], host.id(), "{after}");
    assert!(restored_lines(&dir).is_empty(), "no execve ran");
    let mut writer = connect_control(&dir, UPGRADE_TOKEN);
    assert_writes_echo(&mut writer, "stalled", &ids(&panes));

    let past_alarm = (accepted_at + remaining + Duration::from_millis(1500))
        .saturating_duration_since(Instant::now());
    assert!(
        host_support::wait_exit(&mut host, past_alarm).is_none(),
        "the attempt's alarm was cleared"
    );
    shutdown(&mut host, &mut writer);
}

/// Plan gterm-host-handover 1.3.7.
#[test]
fn many_full_panes_upgrade_within_deadline() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let lines = gobby_terminal::protocol::DEFAULT_NATIVE_SCROLLBACK_MAX_LINES + 2000;
    let fill = format!("seq -f '%079g' 1 {lines}; echo FILLED; exec cat");
    let panes: Vec<_> = (1..=16)
        .map(|n| committed_pane(&mut host, &mut ctl, n, &format!("full-{n}"), &fill))
        .collect();
    for pane in &panes {
        wait_until(
            &format!("{} fills its scrollback", pane.host_terminal_id),
            || snapshot_text(&mut ctl, &pane.host_terminal_id).contains("FILLED"),
        );
    }
    let mut upgrader = connect_control(&dir, UPGRADE_TOKEN);
    let accepted = host_upgrade(&mut upgrader, &gterm_bin(), "attempt-full", Value::Null);
    let accepted_at = Instant::now();
    assert_eq!(accepted["accepted"], true, "{accepted}");
    let remaining = Duration::from_millis(accepted["remaining_ms"].as_u64().expect("remaining_ms"));
    let after = wait_outcome(&dir, UPGRADE_TOKEN, "attempt-full", OUTCOME_WAIT);
    assert!(
        accepted_at.elapsed() < remaining,
        "the upgrade committed inside the deadline"
    );
    assert_outcome(&after, "attempt-full", "succeeded", None);
    assert_eq!(after["host_pid"], host.id(), "{after}");
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    assert_eq!(pane_rows(&mut ctl), expected_rows(&panes));
    assert_writes_echo(&mut ctl, "full", &ids(&panes));
    shutdown(&mut host, &mut ctl);
}

/// Sends the host SIGTERM, as a service manager stops it.
fn terminate(host: &HostProc) {
    let sent = unsafe { libc::kill(host.id() as i32, libc::SIGTERM) };
    assert_eq!(sent, 0, "SIGTERM: {}", std::io::Error::last_os_error());
}

/// The attempt ended with `outcome`, no image restored, and the host
/// drained and exited.
fn assert_ended_and_drained(
    host: &mut HostProc,
    socket_dir: &Path,
    attempt_id: &str,
    outcome: &str,
    reason: &str,
) {
    // The existing post-rollback hold keeps the control socket open until
    // this outcome is observed, before the SIGTERM drain closes it.
    let after = wait_outcome(socket_dir, UPGRADE_TOKEN, attempt_id, OUTCOME_WAIT);
    assert_outcome(&after, attempt_id, outcome, Some(reason));
    let status =
        host_support::wait_exit(host, Duration::from_secs(20)).expect("the host drains and exits");
    assert!(status.success(), "{status:?}");
    let restored = restored_lines(socket_dir);
    assert!(restored.is_empty(), "no image restored: {restored:?}");
}

/// SIGTERM after acceptance aborts the attempt at its next cutoff, and the
/// host drains in its own image.
#[test]
fn sigterm_in_accepted_hold_aborts_and_exits() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    echo_panes(&mut host, &mut ctl, 1);
    let accepted = host_upgrade(
        &mut ctl,
        &gterm_bin(),
        "attempt-term-hold",
        json!({"hold_accepted_ms": 1500, "hold_after_guard_release_ms": 5000}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    terminate(&host);
    assert_ended_and_drained(
        &mut host,
        &dir,
        "attempt-term-hold",
        "aborted",
        "host_draining",
    );
}

/// SIGTERM after the state file is written and before exec starts aborts
/// the attempt instead of carrying the host into the new image.
#[test]
fn sigterm_before_exec_aborts_and_exits() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    echo_panes(&mut host, &mut ctl, 1);
    let accepted = host_upgrade(
        &mut ctl,
        &gterm_bin(),
        "attempt-term-write",
        json!({"hold_before_exec_ms": 1500, "hold_after_guard_release_ms": 5000}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    wait_until("the state file is written", || {
        dir.join(STATE_FILE).exists()
    });
    terminate(&host);
    assert_ended_and_drained(
        &mut host,
        &dir,
        "attempt-term-write",
        "aborted",
        "host_draining",
    );
}

/// SIGTERM that arrives once exec has started stays pending across it: the
/// new image restores every pane and then drains.
#[test]
fn sigterm_during_exec_reaches_the_new_image() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    echo_panes(&mut host, &mut ctl, 1);
    let accepted = host_upgrade(
        &mut ctl,
        &gterm_bin(),
        "attempt-term-exec",
        json!({"hold_in_exec_ms": 1500}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    wait_phase(&dir, "attempt-term-exec", "exec");
    terminate(&host);
    let status = host_support::wait_exit(&mut host, Duration::from_secs(20))
        .expect("the new image drains and exits");
    assert!(status.success(), "{status:?}");
    committed_restores(&dir, "term-exec");
}

/// SIGTERM that arrives during an exec that then fails stays pending until
/// the host unmasks it: the host rolls back in its own image, then drains.
#[test]
fn sigterm_during_failed_exec_drains_the_old_image() {
    use std::os::unix::fs::PermissionsExt;
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    echo_panes(&mut host, &mut ctl, 1);
    let accepted = host_upgrade(
        &mut ctl,
        &gterm_bin(),
        "attempt-term-failed",
        json!({"hold_in_exec_ms": 1500, "hold_after_guard_release_ms": 5000}),
    );
    assert_eq!(accepted["accepted"], true, "{accepted}");
    wait_phase(&dir, "attempt-term-failed", "exec");
    // The pin check has passed; a pin without execute permission fails the exec.
    let blocked = dir.join("not-executable");
    std::fs::write(&blocked, b"not executable").expect("write other bytes");
    std::fs::set_permissions(&blocked, std::fs::Permissions::from_mode(0o600))
        .expect("non-executable mode");
    let sha = accepted["candidate_sha256"]
        .as_str()
        .expect("candidate sha");
    std::fs::rename(&blocked, pin_path(&dir, sha)).expect("replace the pin");
    terminate(&host);
    assert_ended_and_drained(
        &mut host,
        &dir,
        "attempt-term-failed",
        "rolled_back",
        "exec_failed",
    );
}

/// SIGTERM without an attempt drains the host and removes what it owns.
#[test]
fn sigterm_drains_an_idle_host() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    terminate(&host);
    let status =
        host_support::wait_exit(&mut host, Duration::from_secs(20)).expect("the host exits");
    assert!(status.success(), "{status:?}");
    for owned in [CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE] {
        assert!(!dir.join(owned).exists(), "{owned} outlived the drain");
    }
}

/// The host blocks SIGTERM on every thread but one; a pane's child must not
/// inherit that mask.
#[test]
fn pane_children_start_unmasked() {
    let mut host = live_host(&[]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    // The split marker keeps the shell's report of the killed child, which
    // quotes this command, from reading as the child's own output.
    let script = r#"sh -c 'kill -TERM $$; echo "BLOCK""ED"'; echo CHECKED; exec cat"#;
    let pane = committed_pane(&mut host, &mut ctl, 1, "masked", script);
    let mut text = String::new();
    wait_until("the mask check runs", || {
        text = snapshot_text(&mut ctl, &pane.host_terminal_id);
        text.contains("CHECKED")
    });
    assert!(!text.contains("BLOCKED"), "SIGTERM was blocked: {text}");
    shutdown(&mut host, &mut ctl);
}

/// Nor may the clipboard writer the host starts for OSC 52: Linux's leave a
/// server running.
#[test]
fn clipboard_writers_start_unmasked() {
    use std::os::unix::fs::PermissionsExt;
    let bin = tempfile::tempdir().expect("writer dir");
    // The pane test's check, as whichever writer the platform runs; it drains
    // the copy so the host's write completes.
    let writer = "#!/bin/sh\ndir=$(dirname \"$0\")\ncat >/dev/null\n\
        (sh -c 'kill -TERM $$; : >>\"$1/blocked\"' sh \"$dir\") 2>/dev/null\n\
        : >>\"$dir/ran\"\n";
    for name in ["pbcopy", "wl-copy"] {
        let path = bin.path().join(name);
        std::fs::write(&path, writer).expect("write the writer");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
            .expect("make the writer executable");
    }
    let path = format!(
        "{}:{}",
        bin.path().display(),
        std::env::var("PATH").unwrap_or_default()
    );
    let mut host = live_host(&[("PATH", &path), ("WAYLAND_DISPLAY", "gterm-test")]);
    let dir = host.socket_dir().to_path_buf();
    let mut ctl = connect_control(&dir, UPGRADE_TOKEN);
    let script = r"printf '\033]52;c;aGk=\a'; exec cat";
    committed_pane(&mut host, &mut ctl, 1, "clipboard", script);
    wait_until("the clipboard writer runs", || {
        bin.path().join("ran").exists()
    });
    assert!(
        !bin.path().join("blocked").exists(),
        "the clipboard writer started with SIGTERM blocked"
    );
    shutdown(&mut host, &mut ctl);
}
