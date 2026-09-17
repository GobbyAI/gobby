//! Host process lifecycle: sockets, ping/list, shutdown drain (plan 3.1.1, 3.1.22).

#![cfg(unix)]

mod host_support;

use host_support::{
    connect, recv_json, send_json, socket_mode, spawn_host, temp_socket_dir, wait_exit,
    wait_socket, wait_until, write_token, CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE,
};
use serde_json::json;
use std::collections::BTreeMap;
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::time::{Duration, Instant};

fn hello(stream: &mut std::os::unix::net::UnixStream, token: &str) -> serde_json::Value {
    send_json(
        stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    recv_json(stream)
}

fn test_host(
    token: &str,
) -> (
    tempfile::TempDir,
    host_support::HostProc,
    std::os::unix::net::UnixStream,
) {
    let dir = temp_socket_dir();
    write_token(dir.path(), token);
    let host = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    wait_socket(&control);
    let mut stream = connect(&control);
    let response = hello(&mut stream, token);
    assert_eq!(response["ok"], true, "{response}");
    (dir, host, stream)
}

fn prepare_terminal(
    stream: &mut std::os::unix::net::UnixStream,
    host: &mut host_support::HostProc,
    operation_seq: u64,
    suffix: &str,
    argv: &[&str],
    env: BTreeMap<&str, &str>,
) -> (String, String, String, u32) {
    let terminal_id = format!("term-{suffix}");
    let spawn_key = format!("spawn-{suffix}");
    let reserve_key = format!("reserve-{suffix}");
    send_json(
        stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": terminal_id,
            "reserve_key": reserve_key,
        }),
    );
    let reservation = recv_json(stream);
    assert_eq!(reservation["ok"], true, "{reservation}");
    send_json(
        stream,
        &json!({
            "method": "spawn",
            "operation_seq": operation_seq,
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "reservation_id": reservation["reservation_id"],
            "reserve_key": reserve_key,
            "argv": argv,
            "env": env,
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 30_000,
        }),
    );
    let prepared = recv_json(stream);
    assert_eq!(prepared["ok"], true, "{prepared}");
    let pgid = prepared["pgid"].as_i64().expect("prepared pgid") as i32;
    host.track_pgid(pgid);
    (
        terminal_id,
        spawn_key,
        prepared["host_terminal_id"]
            .as_str()
            .expect("host terminal id")
            .to_string(),
        pgid as u32,
    )
}

fn commit_terminal(
    stream: &mut std::os::unix::net::UnixStream,
    terminal_id: &str,
    spawn_key: &str,
    commit_deadline_ms: u64,
) -> serde_json::Value {
    send_json(
        stream,
        &json!({
            "method": "spawn_commit",
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "commit_deadline_ms": commit_deadline_ms,
        }),
    );
    recv_json(stream)
}

fn assert_no_terminals(stream: &mut std::os::unix::net::UnixStream) {
    send_json(stream, &json!({"method": "list"}));
    let listed = recv_json(stream);
    assert_eq!(listed["ok"], true, "{listed}");
    assert_eq!(listed["terminals"], json!([]), "{listed}");
}

fn wait_for_no_terminals(stream: &mut std::os::unix::net::UnixStream) {
    wait_until("terminal slot removal", || {
        send_json(stream, &json!({"method": "list"}));
        recv_json(stream)["terminals"] == json!([])
    });
}

fn process_name(pid: u32) -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        return std::fs::read_to_string(format!("/proc/{pid}/comm"))
            .ok()
            .map(|name| name.trim().to_string());
    }
    #[cfg(target_os = "macos")]
    {
        let mut name = [0_i8; 256];
        let len =
            unsafe { libc::proc_name(pid as i32, name.as_mut_ptr().cast(), name.len() as u32) };
        if len <= 0 {
            return None;
        }
        let bytes = unsafe { std::slice::from_raw_parts(name.as_ptr().cast::<u8>(), len as usize) };
        return Some(String::from_utf8_lossy(bytes).into_owned());
    }
    #[allow(unreachable_code)]
    None
}

fn process_exits_within(pid: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if (unsafe { libc::kill(pid as i32, 0) }) != 0 {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    (unsafe { libc::kill(pid as i32, 0) }) != 0
}

#[test]
fn host_exits_when_socket_dir_vanishes() {
    let dir = temp_socket_dir();
    write_token(dir.path(), "control-token-socket-dir");
    let mut host = spawn_host(dir.path());
    wait_socket(&dir.path().join(CONTROL_SOCKET));

    let started = Instant::now();
    let socket_dir = dir.keep();
    let removed_dir = socket_dir.with_extension("removed");
    std::fs::rename(&socket_dir, &removed_dir).expect("remove live socket dir");
    assert!(
        wait_exit(&mut host, Duration::from_millis(250)).is_some(),
        "host must exit within two health ticks after its socket directory vanishes"
    );
    assert!(
        started.elapsed() < Duration::from_millis(250),
        "socket-directory shutdown took {:?}",
        started.elapsed()
    );
    let log = std::fs::read_to_string(removed_dir.join("gterm.log")).expect("host log");
    assert!(log.contains("socket_dir_removed"), "{log}");
    let _ = std::fs::remove_dir_all(&removed_dir);
}

#[test]
fn drain_honours_grace_then_kills() {
    let (dir, mut host, mut stream) = test_host("control-token-real-drain");
    let polite_marker = dir.path().join("polite-hup");
    let stubborn_marker = dir.path().join("stubborn-hup");
    let polite_ready = dir.path().join("polite-ready");
    let stubborn_ready = dir.path().join("stubborn-ready");
    let polite_script = "trap 'printf hup > \"$HUP_MARKER\"; exit 0' HUP; printf ready > \"$READY_MARKER\"; while :; do sleep 1; done";
    let stubborn_script = "trap 'printf hup > \"$HUP_MARKER\"' HUP; printf ready > \"$READY_MARKER\"; while :; do sleep 1; done";

    let (polite_terminal, polite_spawn, _, polite_pid) = prepare_terminal(
        &mut stream,
        &mut host,
        1,
        "polite-drain",
        &["/bin/sh", "-c", polite_script],
        BTreeMap::from([
            (
                "HUP_MARKER",
                polite_marker.to_str().expect("polite marker path"),
            ),
            (
                "READY_MARKER",
                polite_ready.to_str().expect("polite ready path"),
            ),
        ]),
    );
    assert_eq!(
        commit_terminal(&mut stream, &polite_terminal, &polite_spawn, 1_000)["ok"],
        true
    );
    let (stubborn_terminal, stubborn_spawn, _, stubborn_pid) = prepare_terminal(
        &mut stream,
        &mut host,
        2,
        "stubborn-drain",
        &["/bin/sh", "-c", stubborn_script],
        BTreeMap::from([
            (
                "HUP_MARKER",
                stubborn_marker.to_str().expect("stubborn marker path"),
            ),
            (
                "READY_MARKER",
                stubborn_ready.to_str().expect("stubborn ready path"),
            ),
        ]),
    );
    assert_eq!(
        commit_terminal(&mut stream, &stubborn_terminal, &stubborn_spawn, 1_000,)["ok"],
        true
    );
    wait_until("drain children to install SIGHUP traps", || {
        polite_ready.exists() && stubborn_ready.exists()
    });

    let started = Instant::now();
    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 500}),
    );
    let shutdown = recv_json(&mut stream);
    assert_eq!(shutdown["ok"], true, "{shutdown}");
    assert!(
        wait_exit(&mut host, Duration::from_secs(3)).is_some(),
        "host must exit after draining"
    );
    let elapsed = started.elapsed();
    assert!(
        elapsed >= Duration::from_millis(400),
        "host skipped the requested drain grace: {elapsed:?}"
    );
    assert!(elapsed < Duration::from_secs(2), "drain took {elapsed:?}");
    assert!(polite_marker.exists(), "polite child never received SIGHUP");
    assert!(
        stubborn_marker.exists(),
        "stubborn child never received SIGHUP"
    );
    assert!(process_exits_within(polite_pid, Duration::from_millis(200)));
    assert!(process_exits_within(
        stubborn_pid,
        Duration::from_millis(200)
    ));
}

#[test]
fn commit_reports_exec_failure_with_errno() {
    use std::os::unix::fs::PermissionsExt;

    let (dir, mut host, mut stream) = test_host("control-token-exec-errors");
    let no_exec = dir.path().join("not-executable");
    std::fs::write(&no_exec, b"not executable\n").expect("write non-executable");
    std::fs::set_permissions(&no_exec, std::fs::Permissions::from_mode(0o600))
        .expect("chmod non-executable");
    let no_header = dir.path().join("no-header");
    let shell_marker = dir.path().join("shell-ran");
    std::fs::write(
        &no_header,
        format!("printf shell-ran > {}\n", shell_marker.display()),
    )
    .expect("write headerless executable");
    std::fs::set_permissions(&no_header, std::fs::Permissions::from_mode(0o700))
        .expect("chmod headerless executable");

    let cases = [
        ("missing", "/definitely/missing/gterm-command", "ENOENT"),
        ("eacces", no_exec.to_str().expect("utf8 path"), "EACCES"),
        ("enoexec", no_header.to_str().expect("utf8 path"), "ENOEXEC"),
    ];
    for (index, (suffix, command, code)) in cases.into_iter().enumerate() {
        let (terminal_id, spawn_key, _, _) = prepare_terminal(
            &mut stream,
            &mut host,
            index as u64 + 1,
            suffix,
            &[command],
            BTreeMap::new(),
        );
        let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
        assert_eq!(response["ok"], false, "{response}");
        assert_eq!(response["error"], "exec_failed", "{response}");
        assert_eq!(response["code"], code, "{response}");
        assert_eq!(response["stage"], "execve", "{response}");
        assert!(
            response["detail"]
                .as_str()
                .is_some_and(|detail| !detail.is_empty()),
            "{response}"
        );
        assert_no_terminals(&mut stream);
    }
    assert!(!shell_marker.exists(), "ENOEXEC must never start a shell");
}

#[test]
fn commit_times_out_and_rejects_malformed_status() {
    let (_dir, mut host, mut stream) = test_host("control-token-status-errors");
    for (index, (fault, expected)) in [
        ("timeout", "exec_timeout"),
        ("malformed", "malformed_status"),
    ]
    .into_iter()
    .enumerate()
    {
        let (terminal_id, spawn_key, _, _) = prepare_terminal(
            &mut stream,
            &mut host,
            index as u64 + 1,
            fault,
            &["/usr/bin/true"],
            BTreeMap::from([("GTERM_GATE_FAULT", fault)]),
        );
        let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
        assert_eq!(response["ok"], false, "{response}");
        assert_eq!(response["error"], expected, "{response}");
        assert_no_terminals(&mut stream);
    }
}

#[test]
fn commit_returns_after_exec() {
    let (_dir, mut host, mut stream) = test_host("control-token-exec-barrier");
    let (terminal_id, spawn_key, host_terminal_id, pid) = prepare_terminal(
        &mut stream,
        &mut host,
        1,
        "exec-barrier",
        &["sleep", "30"],
        BTreeMap::from([("PATH", "/bin:/usr/bin"), ("GTERM_GATE_FAULT", "delay")]),
    );
    let started = Instant::now();
    let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
    assert_eq!(response["ok"], true, "{response}");
    assert!(
        started.elapsed() >= Duration::from_millis(150),
        "commit returned before the delayed exec barrier"
    );
    assert_eq!(process_name(pid).as_deref(), Some("sleep"));
    send_json(
        &mut stream,
        &json!({
            "method": "kill",
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 0,
        }),
    );
    let killed = recv_json(&mut stream);
    assert_eq!(killed["ok"], true, "{killed}");
}

#[test]
fn preexec_signal_settles_as_exit_and_injected_faults_never_commit() {
    let (_dir, mut host, mut stream) = test_host("control-token-preexec-faults");
    let (terminal_id, spawn_key, _, _) = prepare_terminal(
        &mut stream,
        &mut host,
        1,
        "signal",
        &["/usr/bin/true"],
        BTreeMap::from([("GTERM_GATE_FAULT", "signal")]),
    );
    let signaled = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
    assert_eq!(signaled["ok"], true, "{signaled}");
    assert_eq!(signaled["commit_state"], "committed", "{signaled}");
    wait_for_no_terminals(&mut stream);

    let (terminal_id, spawn_key, _, pid) = prepare_terminal(
        &mut stream,
        &mut host,
        2,
        "precommit-signal",
        &["/usr/bin/true"],
        BTreeMap::new(),
    );
    unsafe {
        libc::kill(pid as i32, libc::SIGKILL);
    }
    assert!(process_exits_within(pid, Duration::from_secs(1)));
    let died_before_commit = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
    assert_eq!(died_before_commit["ok"], false, "{died_before_commit}");
    assert_eq!(
        died_before_commit["error"], "exec_failed",
        "{died_before_commit}"
    );
    assert_eq!(died_before_commit["code"], "EPIPE", "{died_before_commit}");
    assert_eq!(died_before_commit["stage"], "gate", "{died_before_commit}");
    assert_no_terminals(&mut stream);

    for (index, stage) in ["setsid", "dup2", "PATH"].into_iter().enumerate() {
        let (terminal_id, spawn_key, _, _) = prepare_terminal(
            &mut stream,
            &mut host,
            index as u64 + 3,
            stage,
            &["/usr/bin/true"],
            BTreeMap::from([("GTERM_GATE_FAULT", stage)]),
        );
        let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
        assert_eq!(response["ok"], false, "{response}");
        assert_eq!(response["error"], "exec_failed", "{response}");
        assert_eq!(response["stage"], stage, "{response}");
        assert_no_terminals(&mut stream);
    }
}

/// Setsid and dup2 faults report and exit before the gate, so a commit that
/// arrives after the child is gone cannot deliver the gate byte. Waiting for
/// the exit pins that ordering: the child's reported stage must still win.
#[test]
fn precommit_failure_reports_child_stage_after_child_exits() {
    let (_dir, mut host, mut stream) = test_host("control-token-precommit-exit");
    for (index, (stage, code)) in [("setsid", "EINVAL"), ("dup2", "EBADF")]
        .into_iter()
        .enumerate()
    {
        let (terminal_id, spawn_key, _, pid) = prepare_terminal(
            &mut stream,
            &mut host,
            index as u64 + 1,
            stage,
            &["/usr/bin/true"],
            BTreeMap::from([("GTERM_GATE_FAULT", stage)]),
        );
        assert!(
            process_exits_within(pid, Duration::from_secs(1)),
            "the {stage} fault exits before commit"
        );
        let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
        assert_eq!(response["ok"], false, "{response}");
        assert_eq!(response["error"], "exec_failed", "{response}");
        assert_eq!(response["stage"], stage, "{response}");
        assert_eq!(response["code"], code, "{response}");
        assert_no_terminals(&mut stream);
    }
}

#[test]
fn fast_exit_after_exec_is_committed_then_exited() {
    let (_dir, mut host, mut stream) = test_host("control-token-fast-exit");
    for (index, (command, exit_code)) in [("/usr/bin/true", 0_u64), ("/usr/bin/false", 1_u64)]
        .into_iter()
        .enumerate()
    {
        let suffix = format!("fast-{index}");
        let (terminal_id, spawn_key, _, _) = prepare_terminal(
            &mut stream,
            &mut host,
            index as u64 + 1,
            &suffix,
            &[command],
            BTreeMap::from([("GTERM_TEST_WAIT_FOR_CHILD_EXIT_BEFORE_STATUS", "1")]),
        );
        let response = commit_terminal(&mut stream, &terminal_id, &spawn_key, 1_000);
        assert_eq!(response["ok"], true, "{response}");
        assert_eq!(response["commit_state"], "committed", "{response}");
        assert_eq!(response["terminal_state"], "exited", "{response}");
        assert_eq!(response["exit_code"], exit_code, "{response}");
        assert!(response["signal"].is_null(), "{response}");
        assert_ne!(response["error"], "exec_failed", "{response}");
        assert_no_terminals(&mut stream);
    }
}

#[test]
fn host_death_releases_prepared_child() {
    let (_dir, mut host, mut stream) = test_host("control-token-host-death");
    let (_, _, _, pid) = prepare_terminal(
        &mut stream,
        &mut host,
        1,
        "host-death",
        &["/usr/bin/true"],
        BTreeMap::new(),
    );
    unsafe {
        libc::kill(host.id() as i32, libc::SIGKILL);
    }
    assert!(
        wait_exit(&mut host, Duration::from_secs(1)).is_some(),
        "host must die after SIGKILL"
    );
    assert!(
        process_exits_within(pid, Duration::from_secs(1)),
        "prepared gate child {pid} survived host death"
    );
}

#[test]
fn host_starts_and_serves_ping_and_list() {
    let dir = temp_socket_dir();
    let token = "control-token-lifecycle";
    write_token(dir.path(), token);

    let stale = dir.path().join(CONTROL_SOCKET);
    let _stale_listener = UnixListener::bind(&stale).expect("bind stale control socket");
    drop(_stale_listener);
    assert!(stale.exists(), "stale socket file must exist before start");

    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);

    assert_eq!(socket_mode(&control), 0o600);
    assert_eq!(socket_mode(&frames), 0o600);

    let pid_text = std::fs::read_to_string(dir.path().join(PID_FILE)).expect("pidfile");
    let pidfile_pid: u32 = pid_text.trim().parse().expect("pidfile int");
    assert_eq!(pidfile_pid, child.id());

    let mut stream = connect(&control);
    let hello = hello(&mut stream, token);
    assert_eq!(hello["ok"], true);
    assert_eq!(hello["protocol_version"], 1);
    let epoch = hello["host_epoch"]
        .as_str()
        .expect("host_epoch")
        .to_string();
    assert!(!epoch.is_empty());
    assert!(hello["version"].as_str().is_some());

    send_json(&mut stream, &json!({"method": "ping"}));
    let ping = recv_json(&mut stream);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], epoch);
    assert_eq!(ping["host_pid"].as_u64().unwrap(), u64::from(child.id()));
    assert!(ping["version"].as_str().is_some());

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
        "host must exit after accepted host_shutdown"
    );
    assert!(!control.exists() || UnixListener::bind(&control).is_ok());
}

#[test]
fn host_shutdown_drains_and_is_idempotent() {
    let dir = temp_socket_dir();
    let token = "control-token-drain";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);

    // Unauthenticated host_shutdown is refused like any other verb.
    {
        let mut stream = connect(&control);
        send_json(
            &mut stream,
            &json!({"method": "host_shutdown", "grace_ms": 20}),
        );
        let reply = recv_json(&mut stream);
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["error"], "unauthenticated");
    }

    let mut stream = connect(&control);
    let hello = hello(&mut stream, token);
    assert_eq!(hello["ok"], true);

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 200}),
    );
    let first = recv_json(&mut stream);
    assert_eq!(first["ok"], true);
    assert_eq!(first["accepted"], true);
    assert_eq!(first["draining"], true);

    send_json(
        &mut stream,
        &json!({"method": "host_shutdown", "grace_ms": 200}),
    );
    let second = recv_json(&mut stream);
    assert_eq!(second["ok"], true);
    assert_eq!(second["accepted"], true);
    assert_eq!(second["draining"], true);

    send_json(&mut stream, &json!({"method": "spawn"}));
    let spawn = recv_json(&mut stream);
    assert_eq!(spawn["ok"], false);
    assert_eq!(spawn["error"], "host_draining");

    send_json(&mut stream, &json!({"method": "attach"}));
    let attach = recv_json(&mut stream);
    assert_eq!(attach["ok"], false);
    assert_eq!(attach["error"], "unknown_method:attach");

    // Lost response plus verified host death is success for the caller.
    drop(stream);
    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after drain even if the response socket is dropped"
    );
    assert_host_dead(dir.path(), child.id());
}

fn assert_host_dead(socket_dir: &Path, pid: u32) {
    let pidfile = socket_dir.join(PID_FILE);
    if pidfile.exists() {
        let text = std::fs::read_to_string(&pidfile).unwrap_or_default();
        if let Ok(stored) = text.trim().parse::<u32>() {
            assert_ne!(
                stored, pid,
                "pidfile must not still name the drained host pid"
            );
        }
    }
    let control = socket_dir.join(CONTROL_SOCKET);
    assert!(
        UnixListener::bind(&control).is_ok() || !control.exists(),
        "control socket must be gone after host death"
    );
    #[cfg(unix)]
    {
        let alive = unsafe { libc::kill(pid as i32, 0) == 0 };
        assert!(!alive, "host pid {pid} must be gone");
    }
}

/// #22002: the host outlives its daemon. A restarted daemon re-adopts the same
/// host (same epoch, pid, terminals) with the same token, and a gclient frames
/// attachment opened before the disconnect keeps working throughout.
#[test]
fn host_survives_daemon_disconnect_and_readopts() {
    use gobby_terminal::protocol::{
        read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, MAX_FRAME_SIZE,
        PROTOCOL_VERSION,
    };
    use std::io::{ErrorKind, Read, Write};

    const LOCAL: &str = "local-token-readopt";

    let dir = temp_socket_dir();
    let token = "control-token-readopt";
    write_token(dir.path(), token);
    std::fs::write(dir.path().join("local_cli_token"), LOCAL).expect("local token");
    let mut child = spawn_host(dir.path());
    let control = dir.path().join(CONTROL_SOCKET);
    let frames = dir.path().join(FRAMES_SOCKET);
    wait_socket(&control);
    wait_socket(&frames);
    let pidfile = dir.path().join(PID_FILE);
    let pid_before = std::fs::read_to_string(&pidfile).expect("pidfile");

    // Daemon #1 adopts the host and spawns a committed terminal.
    let mut first = connect(&control);
    let hello_one = hello(&mut first, token);
    assert_eq!(hello_one["ok"], true, "{hello_one}");
    let epoch = hello_one["host_epoch"].as_str().expect("epoch").to_string();
    send_json(
        &mut first,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-readopt",
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(&mut first);
    let reservation_id = reserved["reservation_id"]
        .as_str()
        .expect("reservation id")
        .to_string();
    send_json(
        &mut first,
        &json!({
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": "term-readopt",
            "spawn_key": "sk",
            "reservation_id": reservation_id,
            "reserve_key": "rk",
            "argv": ["/bin/sleep", "30"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    let prepared = recv_json(&mut first);
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"]
        .as_str()
        .expect("host terminal id")
        .to_string();
    send_json(
        &mut first,
        &json!({"method": "spawn_commit", "terminal_id": "term-readopt", "spawn_key": "sk"}),
    );
    let committed = recv_json(&mut first);
    assert_eq!(committed["ok"], true, "{committed}");

    // A gclient attaches on the frames socket while daemon #1 is still up.
    let mut viewer = connect(&frames);
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    )
    .expect("encode hello");
    write_message(
        &mut buf,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    )
    .expect("encode attach");
    viewer.write_all(&buf).expect("send hello+attach");
    viewer
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("read timeout");
    match read_message(&mut viewer, MAX_FRAME_SIZE).expect("welcome") {
        ServerMessage::Welcome { host_epoch } => assert_eq!(host_epoch, epoch),
        other => panic!("expected welcome: {other:?}"),
    }
    match read_message(&mut viewer, MAX_FRAME_SIZE).expect("attached") {
        ServerMessage::Attached {
            host_terminal_id: attached,
            ..
        } => assert_eq!(attached, host_terminal_id),
        other => panic!("expected attached: {other:?}"),
    }

    // Daemon #1 also holds an unprepared reservation; the host drops it when it
    // processes the disconnect, which is the signal that the disconnect landed.
    send_json(
        &mut first,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "term-readopt-pending",
            "reserve_key": "rk-pending",
        }),
    );
    let pending = recv_json(&mut first);
    let pending_reservation = pending["reservation_id"]
        .as_str()
        .expect("pending reservation id")
        .to_string();

    // Daemon #1 stops: its control connection goes away with no host_shutdown.
    drop(first);

    // Daemon #2 adopts the same host with the same token and epoch.
    let mut second = connect(&control);
    let hello_two = hello(&mut second, token);
    assert_eq!(hello_two["ok"], true, "{hello_two}");
    assert_eq!(
        hello_two["host_epoch"], epoch,
        "epoch continuity across daemons"
    );
    // A wrong-key release reports `released: false` while the reservation
    // exists and `released: true` once the host has reaped it on disconnect.
    wait_until("host to process daemon #1's disconnect", || {
        send_json(
            &mut second,
            &json!({
                "method": "release_observer",
                "reservation_id": pending_reservation,
                "reserve_key": "wrong-key",
            }),
        );
        recv_json(&mut second)["released"] == true
    });
    assert!(
        child.try_wait().expect("try_wait").is_none(),
        "host must keep running after its daemon disconnects"
    );
    assert_eq!(
        std::fs::read_to_string(&pidfile).expect("pidfile after disconnect"),
        pid_before,
        "pidfile must keep naming the surviving host"
    );
    send_json(&mut second, &json!({"method": "ping"}));
    let ping = recv_json(&mut second);
    assert_eq!(ping["ok"], true);
    assert_eq!(ping["host_epoch"], epoch);
    assert_eq!(ping["host_pid"].as_u64().unwrap(), u64::from(child.id()));
    send_json(&mut second, &json!({"method": "list"}));
    let list = recv_json(&mut second);
    assert_eq!(list["ok"], true);
    let rows = list["terminals"].as_array().expect("terminal rows");
    assert!(
        rows.iter()
            .any(|row| row["host_terminal_id"] == host_terminal_id
                && row["commit_state"] == "committed"),
        "the committed terminal must survive the daemon handover: {list}"
    );

    // The pre-restart attachment is still open (a read must not report EOF)...
    viewer
        .set_read_timeout(Some(Duration::from_millis(200)))
        .expect("peek timeout");
    let mut peek = [0u8; 1];
    match viewer.read(&mut peek) {
        Ok(0) => panic!("host closed the frames attachment during the daemon handover"),
        Ok(_) => {}
        Err(err) => assert!(
            matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut),
            "unexpected frames error: {err}"
        ),
    }
    // ...and the terminal still accepts new attachments after re-adoption.
    let mut late = connect(&frames);
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    )
    .expect("encode late hello");
    write_message(
        &mut buf,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    )
    .expect("encode late attach");
    late.write_all(&buf).expect("send late hello+attach");
    late.set_read_timeout(Some(Duration::from_secs(5)))
        .expect("late read timeout");
    let _: ServerMessage = read_message(&mut late, MAX_FRAME_SIZE).expect("late welcome");
    match read_message(&mut late, MAX_FRAME_SIZE).expect("late attached") {
        ServerMessage::Attached {
            host_terminal_id: attached,
            ..
        } => assert_eq!(attached, host_terminal_id),
        other => panic!("expected late attached: {other:?}"),
    }

    // Only an explicit host_shutdown takes the host down.
    send_json(
        &mut second,
        &json!({"method": "host_shutdown", "grace_ms": 50}),
    );
    let shutdown = recv_json(&mut second);
    assert_eq!(shutdown["ok"], true);
    assert!(
        wait_exit(&mut child, Duration::from_secs(5)).is_some(),
        "host must exit after an explicit host_shutdown"
    );
}
