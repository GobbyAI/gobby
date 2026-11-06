//! Shared helpers for gterm host integration tests.

#![cfg(unix)]
#![allow(dead_code)]

use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

pub const CONTROL_SOCKET: &str = "gterm-control.sock";
pub const FRAMES_SOCKET: &str = "gterm-frames.sock";
pub const PID_FILE: &str = "gterm.pid";
pub const TOKEN_FILE: &str = "gterm-control.token";
/// The pid of the host this test spawned, so a readiness timeout can tell a
/// host that died before binding from one still starting.
pub const SPAWN_PID_FILE: &str = "gterm.spawned-pid";
/// Default bound for ordinary test operations.
pub const COLD_START: Duration = Duration::from_secs(15);
/// A debug host pins and verifies its image, then re-execs before binding.
/// Under heavy load measured binds reached 11s and the old 15s guard expired
/// with a live process still starting (#25653). Allow scheduler headroom here
/// without extending operation or upgrade deadlines. This remains a hang guard.
const SOCKET_STARTUP: Duration = Duration::from_secs(60);
static REQUEST_ID: AtomicU64 = AtomicU64::new(1);

pub struct HostProc {
    child: Child,
    socket_dir: PathBuf,
    owned_socket_dir: Option<tempfile::TempDir>,
    pgids: Vec<i32>,
}

impl HostProc {
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }

    pub fn kill(&mut self) -> std::io::Result<()> {
        self.child.kill()
    }

    pub fn track_pgid(&mut self, pgid: i32) {
        self.pgids.push(pgid);
    }

    pub fn socket_dir(&self) -> &Path {
        &self.socket_dir
    }

    pub fn own_socket_dir(&mut self, dir: tempfile::TempDir) {
        assert_eq!(dir.path(), self.socket_dir);
        self.owned_socket_dir = Some(dir);
    }
}

impl Drop for HostProc {
    fn drop(&mut self) {
        for pgid in self.pgids.drain(..) {
            if pgid > 0 {
                unsafe {
                    libc::killpg(pgid, libc::SIGKILL);
                }
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn gterm_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_gterm"))
}

/// macOS `sockaddr_un.sun_path` is 104 bytes. Sandbox `TMPDIR` is already long,
/// so the default `tempfile` prefix does not leave enough room for `gterm-control.sock`.
pub fn temp_socket_dir() -> tempfile::TempDir {
    tempfile::Builder::new()
        .prefix("g")
        .rand_bytes(4)
        .tempdir()
        .expect("socket tempdir")
}

pub fn write_token(dir: &Path, token: &str) {
    let path = dir.join(TOKEN_FILE);
    std::fs::write(&path, token).expect("write control token");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&path, perms).unwrap();
    }
}

pub fn spawn_host(socket_dir: &Path) -> HostProc {
    spawn_host_with_args(socket_dir, &[])
}

/// Start the once-built Cargo image with additional host arguments.
pub fn spawn_host_with_args(socket_dir: &Path, extra: &[&str]) -> HostProc {
    spawn_host_with_env_removed(socket_dir, extra, &[])
}

/// Spawns the host without the named variables of the test's environment.
pub fn spawn_host_with_env_removed(
    socket_dir: &Path,
    extra: &[&str],
    removed: &[&str],
) -> HostProc {
    spawn_host_with_env(socket_dir, extra, &[], removed)
}

/// Spawns the host with `env` added and `removed` taken from the test's
/// environment.
pub fn spawn_host_with_env(
    socket_dir: &Path,
    extra: &[&str],
    env: &[(&str, &str)],
    removed: &[&str],
) -> HostProc {
    // Cargo built this image once for the run. Reuse its inode instead of
    // triggering another macOS executable assessment for each host launch.
    // Nextest groups host fixtures with nested Cargo builders to protect this
    // image from replacement. The host still pins its private runtime image.
    spawn_host_binary(&gterm_bin(), socket_dir, extra, env, removed)
}

/// Spawns `binary` as the host, for a test that launches it through a path
/// other than the private copy.
pub fn spawn_host_binary(
    binary: &Path,
    socket_dir: &Path,
    extra: &[&str],
    env: &[(&str, &str)],
    removed: &[&str],
) -> HostProc {
    let log_path = socket_dir.join("gterm.log");
    let token_path = socket_dir.join("bootstrap.yaml");
    if !token_path.exists() {
        std::fs::write(&token_path, "api_key: local-token\n").expect("write API key");
    }
    let stderr_path = socket_dir.join("gterm.stderr");
    let stderr_file = std::fs::File::create(&stderr_path).ok();
    let mut cmd = Command::new(binary);
    cmd.arg("host")
        .arg("--socket-dir")
        .arg(socket_dir)
        .args(extra)
        .env("GTERM_LOG_FILE", &log_path)
        .env("GTERM_TEST_HELPER", "1")
        .env("GOBBY_HOME", socket_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(match stderr_file {
            Some(file) => Stdio::from(file),
            None => Stdio::piped(),
        });
    cmd.envs(env.iter().copied());
    for name in removed {
        cmd.env_remove(name);
    }
    let child = cmd.spawn().expect("spawn gterm host");
    std::fs::write(socket_dir.join(SPAWN_PID_FILE), child.id().to_string())
        .expect("record spawned host pid");
    HostProc {
        child,
        socket_dir: socket_dir.to_path_buf(),
        owned_socket_dir: None,
        pgids: Vec::new(),
    }
}

pub fn hello_control(stream: &mut UnixStream, token: &str) -> Value {
    send_json(
        stream,
        &serde_json::json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    recv_json(stream)
}

pub fn rpc(stream: &mut UnixStream, method: &str, extra: serde_json::Value) -> Value {
    let mut req = extra;
    if let Some(obj) = req.as_object_mut() {
        obj.insert("method".into(), serde_json::Value::String(method.into()));
    }
    send_json(stream, &req);
    recv_json(stream)
}

pub fn wait_socket(path: &Path) {
    let started = Instant::now();
    wait_socket_with(
        path,
        || started.elapsed(),
        || std::thread::sleep(Duration::from_millis(20)),
    );
}

/// Clock and polling seams let the startup guard be tested without a slow sleep.
pub fn wait_socket_with(
    path: &Path,
    mut elapsed: impl FnMut() -> Duration,
    mut pause: impl FnMut(),
) {
    while elapsed() < SOCKET_STARTUP {
        if path.exists() && UnixStream::connect(path).is_ok() {
            return;
        }
        pause();
    }
    let dir = path.parent().unwrap_or(path);
    let log = std::fs::read_to_string(dir.join("gterm.log")).unwrap_or_default();
    let stderr = std::fs::read_to_string(dir.join("gterm.stderr")).unwrap_or_default();
    let pid = std::fs::read_to_string(dir.join(PID_FILE)).unwrap_or_default();
    let spawned = std::fs::read_to_string(dir.join(SPAWN_PID_FILE)).unwrap_or_default();
    let state = host_process_state(&spawned);
    panic!(
        "timed out waiting for {}; elapsed={:?}; budget={SOCKET_STARTUP:?}; \
         path_len={}; exists={}; pid={pid:?}; \
         spawned_pid={spawned:?}; state={state:?}; log={log:?}; stderr={stderr:?}",
        path.display(),
        elapsed(),
        path.as_os_str().len(),
        path.exists(),
    );
}

/// `ps` state of the host this test spawned. `Z` means it exited before its
/// socket appeared; `gone` means no such process remains.
fn host_process_state(spawned: &str) -> String {
    let Ok(pid) = spawned.trim().parse::<u32>() else {
        return "no spawned pid recorded".to_string();
    };
    match Command::new("ps")
        .args(["-o", "stat=,etime=", "-p", &pid.to_string()])
        .output()
    {
        Ok(output) => {
            let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if text.is_empty() {
                "gone".to_string()
            } else {
                text
            }
        }
        Err(err) => format!("ps failed: {err}"),
    }
}

/// Poll `ready` until it reports true, panicking after five seconds.
pub fn wait_until(what: &str, ready: impl FnMut() -> bool) {
    wait_until_within(what, Duration::from_secs(5), ready);
}

/// Poll `ready` until it reports true, panicking after `budget`.
pub fn wait_until_within(what: &str, budget: Duration, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if ready() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("timed out waiting for {what}");
}

pub fn connect(path: &Path) -> UnixStream {
    UnixStream::connect(path).unwrap_or_else(|err| {
        panic!("connect {}: {err}", path.display());
    })
}

pub fn send_json(stream: &mut UnixStream, value: &Value) {
    let mut value = value.clone();
    if let Some(object) = value.as_object_mut() {
        object.entry("id").or_insert_with(|| {
            Value::String(format!(
                "test-{}",
                REQUEST_ID.fetch_add(1, Ordering::Relaxed)
            ))
        });
    }
    send_json_without_id(stream, &value);
}

pub fn send_json_without_id(stream: &mut UnixStream, value: &Value) {
    let mut line = serde_json::to_string(value).expect("serialize");
    line.push('\n');
    stream.write_all(line.as_bytes()).expect("write request");
    stream.flush().expect("flush request");
}

pub fn recv_json(stream: &mut UnixStream) -> Value {
    recv_json_within(stream, Duration::from_secs(5))
}

/// `recv_json` for a reply that can take longer, such as an upgrade probe.
pub fn recv_json_within(stream: &mut UnixStream, timeout: Duration) -> Value {
    stream
        .set_read_timeout(Some(timeout))
        .expect("read timeout");
    // This helper does not retain a BufReader between calls. A one-byte buffer
    // prevents it from reading and then discarding the next correlated reply.
    let mut reader = BufReader::with_capacity(1, stream);
    let mut line = String::new();
    reader.read_line(&mut line).expect("read response line");
    assert!(
        !line.is_empty(),
        "control socket closed before a response line"
    );
    serde_json::from_str(line.trim_end()).expect("parse response json")
}

pub fn socket_mode(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .unwrap_or_else(|err| panic!("stat {}: {err}", path.display()))
        .permissions()
        .mode()
        & 0o777
}

pub fn wait_exit(host: &mut HostProc, timeout: Duration) -> Option<std::process::ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        match host.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => return None,
        }
    }
}

/// A native pane made through `reserve_observer`, `spawn`, and `spawn_commit`.
pub struct CommittedPane {
    pub terminal_id: String,
    pub spawn_key: String,
    pub host_terminal_id: String,
    pub reservation_id: String,
    /// The child leads its own session, so this is also its pid.
    pub pgid: i32,
}

/// Reserves, spawns `sh -c <script>` in the socket dir, and commits it. `seq`
/// is the next `operation_seq` on `stream`.
pub fn committed_pane(
    host: &mut HostProc,
    stream: &mut UnixStream,
    seq: u64,
    terminal_id: &str,
    script: &str,
) -> CommittedPane {
    let reserved = rpc(
        stream,
        "reserve_observer",
        serde_json::json!({"terminal_id": terminal_id, "reserve_key": terminal_id}),
    );
    assert_eq!(reserved["ok"], true, "reserve {terminal_id}: {reserved}");
    let reservation_id = reserved["reservation_id"]
        .as_str()
        .expect("reservation id")
        .to_string();
    let spawn_key = format!("{terminal_id}-spawn");
    let prepared = rpc(
        stream,
        "spawn",
        serde_json::json!({
            "operation_seq": seq,
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "reservation_id": reservation_id,
            "reserve_key": terminal_id,
            "argv": ["/bin/sh", "-c", script],
            "cwd": host.socket_dir().to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000,
        }),
    );
    assert_eq!(prepared["ok"], true, "spawn {terminal_id}: {prepared}");
    let pgid = prepared["pgid"].as_i64().expect("pgid") as i32;
    host.track_pgid(pgid);
    let committed = rpc(
        stream,
        "spawn_commit",
        serde_json::json!({"terminal_id": terminal_id, "spawn_key": spawn_key}),
    );
    assert_eq!(committed["ok"], true, "commit {terminal_id}: {committed}");
    CommittedPane {
        terminal_id: terminal_id.to_string(),
        spawn_key,
        host_terminal_id: prepared["host_terminal_id"]
            .as_str()
            .expect("host terminal id")
            .to_string(),
        reservation_id,
        pgid,
    }
}

/// An authenticated control connection.
pub fn control(socket_dir: &Path, token: &str) -> UnixStream {
    let mut stream = connect(&socket_dir.join(CONTROL_SOCKET));
    let hello = hello_control(&mut stream, token);
    assert_eq!(hello["ok"], true, "{hello}");
    stream
}

/// Sends `host_upgrade` without waiting for its reply.
pub fn send_upgrade(stream: &mut UnixStream, exe: &Path, attempt_id: &str, test_fault: Value) {
    let mut request = serde_json::json!({
        "method": "host_upgrade",
        "exe": exe,
        "attempt_id": attempt_id,
    });
    if !test_fault.is_null() {
        request["test_fault"] = test_fault;
    }
    send_json(stream, &request);
}

/// Sends `host_upgrade` and returns its reply, which waits for the probe.
pub fn host_upgrade(
    stream: &mut UnixStream,
    exe: &Path,
    attempt_id: &str,
    test_fault: Value,
) -> Value {
    send_upgrade(stream, exe, attempt_id, test_fault);
    recv_json_within(stream, Duration::from_secs(20))
}

/// Pings through a fresh connection; `None` while no host answers, as across
/// an exec or after the host ended.
pub fn try_ping(socket_dir: &Path, token: &str) -> Option<Value> {
    let mut stream = UnixStream::connect(socket_dir.join(CONTROL_SOCKET)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    let mut reader = BufReader::new(stream.try_clone().ok()?);
    let mut exchange = |request: Value| -> Option<Value> {
        let mut line = serde_json::to_vec(&request).ok()?;
        line.push(b'\n');
        stream.write_all(&line).ok()?;
        let mut reply = String::new();
        if reader.read_line(&mut reply).ok()? == 0 {
            return None;
        }
        serde_json::from_str(reply.trim_end()).ok()
    };
    let hello = exchange(serde_json::json!({
        "method": "hello",
        "id": "try-ping-hello",
        "protocol_version": 1,
        "control_token": token,
    }))?;
    if hello["ok"] != true {
        return None;
    }
    exchange(serde_json::json!({"method": "ping", "id": "try-ping"}))
}

/// Waits for an idle host whose `last_outcome` names `attempt_id`, and returns
/// that ping.
pub fn wait_outcome(socket_dir: &Path, token: &str, attempt_id: &str, timeout: Duration) -> Value {
    let deadline = Instant::now() + timeout;
    let mut last = None;
    loop {
        if let Some(ping) = try_ping(socket_dir, token) {
            let upgrade = &ping["upgrade"];
            if upgrade["phase"] == "idle" && upgrade["last_outcome"]["attempt_id"] == attempt_id {
                return ping;
            }
            last = Some(ping);
        }
        assert!(
            Instant::now() < deadline,
            "no outcome for {attempt_id}; last ping {last:?}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Writes an executable `sh` script that stands in for a candidate image, so
/// its probe exits and times out as the script says.
pub fn candidate_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    let path = dir.join(name);
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write candidate script");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
        .expect("candidate script mode");
    path
}

/// A candidate whose probe blocks reading a FIFO until the test closes the
/// write end `send_held_upgrade` returns; returns the candidate and its FIFO.
pub fn held_probe(dir: &Path, name: &str, exit_code: u8) -> (PathBuf, PathBuf) {
    let fifo = dir.join(format!("{name}.fifo"));
    let made = Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .expect("run mkfifo");
    assert!(made.success(), "mkfifo: {made}");
    // The marker shows the probe started, so a timed-out wait can tell an
    // absent probe from one that never opened the FIFO.
    let started = fifo.with_extension("started");
    let body = format!(
        ": > '{}'\nread line < '{}'\nexit {exit_code}",
        started.display(),
        fifo.display()
    );
    (candidate_script(dir, &format!("{name}.sh"), &body), fifo)
}

/// Sends `host_upgrade` for a `held_probe` candidate and returns once its
/// probe holds the FIFO; dropping the returned write end lets the probe
/// accept. An attempt replies before it releases the upgrade lock, so one
/// sent right after it can be refused `upgrade_in_progress`; it is sent again.
pub fn send_held_upgrade(
    stream: &mut UnixStream,
    exe: &Path,
    fifo: &Path,
    attempt_id: &str,
) -> std::fs::File {
    use std::os::unix::fs::OpenOptionsExt;
    send_upgrade(stream, exe, attempt_id, Value::Null);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        // A non-blocking open for writing succeeds only once the probe has
        // the FIFO open for reading.
        if let Ok(release) = std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(fifo)
        {
            return release;
        }
        if replied(stream) {
            let reply = recv_json(stream);
            assert_eq!(reply["error"], "upgrade_in_progress", "{reply}");
            send_upgrade(stream, exe, attempt_id, Value::Null);
        }
        if Instant::now() >= deadline {
            panic!(
                "timed out waiting for {attempt_id}'s probe holds its FIFO; {}",
                held_upgrade_report(fifo, stream)
            );
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// What a timed-out `send_held_upgrade` wait saw: the host's process state,
/// whether the probe started, whether an unread reply waits, and the log tail.
fn held_upgrade_report(fifo: &Path, stream: &UnixStream) -> String {
    let dir = fifo.parent().unwrap_or(fifo);
    let spawned = std::fs::read_to_string(dir.join(SPAWN_PID_FILE)).unwrap_or_default();
    let state = host_process_state(&spawned);
    let started = fifo.with_extension("started").exists();
    let log = std::fs::read_to_string(dir.join("gterm.log")).unwrap_or_default();
    let tail: Vec<&str> = log.lines().rev().take(20).collect();
    let tail: Vec<&str> = tail.into_iter().rev().collect();
    format!(
        "host_state={state:?}; probe_started={started}; unread_reply={}; log_tail={tail:?}",
        replied(stream)
    )
}

/// Whether a reply waits on `stream`, without reading it.
fn replied(stream: &UnixStream) -> bool {
    use std::os::fd::AsRawFd;
    let mut byte = [0u8; 1];
    let peeked = unsafe {
        libc::recv(
            stream.as_raw_fd(),
            byte.as_mut_ptr().cast(),
            1,
            libc::MSG_PEEK | libc::MSG_DONTWAIT,
        )
    };
    peeked > 0
}

/// The pane's text through the control `snapshot` verb.
pub fn pane_text(stream: &mut UnixStream, host_terminal_id: &str) -> String {
    let snap = rpc(
        stream,
        "snapshot",
        serde_json::json!({
            "host_terminal_id": host_terminal_id,
            "mode": "text",
            "max_bytes": 65536,
            "max_lines": 200,
        }),
    );
    assert_eq!(snap["ok"], true, "{snap}");
    snap["text"].as_str().unwrap_or_default().to_string()
}

/// Writes `text` to a pane through the control `write` verb.
pub fn write_text(stream: &mut UnixStream, seq: u64, host_terminal_id: &str, text: &str) -> Value {
    use base64::Engine as _;
    rpc(
        stream,
        "write",
        serde_json::json!({
            "operation_seq": seq,
            "host_terminal_id": host_terminal_id,
            "kind": "text",
            "encoding": "utf8-b64",
            "data": base64::engine::general_purpose::STANDARD.encode(text),
        }),
    )
}

/// Whether `pid` still names a process (a zombie included).
pub fn process_exists(pid: i32) -> bool {
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(pid, 0) == 0 }
}
