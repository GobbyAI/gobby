//! Host handover (plan gterm-host-handover 1.2): frozen reaping, the resume
//! primitive, and the Stage/Commit restore of a carried state file.

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

/// Writes a line to each pane and waits for it to echo.
fn assert_writes_echo(control: &mut UnixStream, label: &str, ids: &[&str]) {
    for (seq, id) in (1..).zip(ids) {
        let line = format!("{id}-after-{label}");
        let written = rpc(
            control,
            "write",
            json!({
                "operation_seq": seq,
                "host_terminal_id": id,
                "kind": "text",
                "encoding": "utf8-b64",
                "data": base64::engine::general_purpose::STANDARD.encode(format!("{line}\n")),
            }),
        );
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
    let status = host.wait_exit(WAIT);
    assert_eq!(
        status.and_then(|status| status.signal()),
        Some(libc::SIGALRM),
        "a wedged restore: {}",
        host.diagnostics()
    );
}
