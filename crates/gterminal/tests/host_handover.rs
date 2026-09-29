//! Host handover (plan gterm-host-handover 1.2): frozen reaping, the resume
//! primitive, and the Stage/Commit restore of a carried state file.

#![cfg(unix)]

mod handover_support;
mod host_support;

use std::path::Path;
use std::time::Duration;

use base64::Engine as _;
use gobby_terminal::host::handover::CarriedEvents;
use gobby_terminal::pane::{ChildExit, PaneLaunchEnv, PaneRuntime};
use gobby_terminal::terminal_theme::TerminalTheme;
use handover_support::{
    ino, is_zombie, mtime_ns, restore, wait_until, Bound, HelperPane, HelperSpec, BOUND_FILE, WAIT,
};
use host_support::{recv_json, rpc, temp_socket_dir, CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE};
use serde_json::json;

/// The helper process lane's entry point; see `handover_support`.
#[test]
#[ignore = "run only as the helper process of the restore tests"]
fn handover_helper() {
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
    wait_until("the frozen child to exit", || is_zombie(pid));
    std::thread::sleep(Duration::from_millis(200));
    assert!(
        is_zombie(pid),
        "a frozen pane leaves its exited child unreaped"
    );
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
