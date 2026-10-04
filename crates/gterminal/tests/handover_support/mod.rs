//! Helper-process lane for the restore tests. The test binary re-runs itself
//! as its ignored helper test, which plays the earlier host image: it binds
//! the sockets, spawns panes, captures them into a state file, and execs the
//! pinned `gterm host --resume-state`. The exec'd host keeps the helper's pid,
//! so it is the parent's child and every pane's parent.

use std::io::{BufRead, BufReader, Write};
use std::os::fd::{IntoRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use gobby_terminal::host::handover::{
    encode_snapshot, monotonic_now_ns, write_state, CarriedEvents, CarriedIdentity,
    CarriedObserverBind, CarriedPane, CarriedReservation, HandoverState, UpgradeAttempt,
    FORMAT_VERSION, STATE_FILE,
};
use gobby_terminal::host::image;
use gobby_terminal::pane::{PaneLaunchEnv, PaneRuntime};
use gobby_terminal::protocol::ObservationState;
use gobby_terminal::terminal_theme::{TerminalTheme, ThemeDeclaration};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::host_support::{
    connect, hello_control, rpc, write_token, CONTROL_SOCKET, FRAMES_SOCKET, PID_FILE,
};

/// The ignored test that runs [`run_helper`].
pub const HELPER_TEST: &str = "handover_helper";
pub const TOKEN: &str = "handover-token";
const SPEC_ENV: &str = "GTERM_HANDOVER_HELPER_SPEC";
pub const WAIT: Duration = Duration::from_secs(10);
/// Socket and pidfile identities the helper saw before exec.
pub const BOUND_FILE: &str = "helper-bound.json";

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HelperPane {
    pub host_terminal_id: String,
    pub script: String,
    pub rows: u16,
    pub cols: u16,
    /// Visible text the pane shows before capture.
    pub ready_text: Option<String>,
    /// Created after every reader pauses and before exec.
    pub window_trigger: Option<PathBuf>,
    /// Created by the child once it acted on `window_trigger`.
    pub window_done: Option<PathBuf>,
    /// The earlier image reaped this child's exit in the window but never
    /// delivered it, so the state file carries it.
    pub recorded_exit: bool,
    /// The child exits in the window while frozen; its zombie crosses exec.
    pub exits_in_window: bool,
    pub title: String,
    pub input_grant: Option<String>,
    /// Carries a prepared reservation and an `Entitled` observer bind.
    pub entitled: bool,
    /// The host theme the pane starts with; `None` is the default theme.
    pub theme: Option<ThemeDeclaration>,
}

impl HelperPane {
    pub fn new(host_terminal_id: &str, script: &str) -> Self {
        Self {
            host_terminal_id: host_terminal_id.into(),
            script: script.into(),
            rows: 24,
            cols: 80,
            ready_text: None,
            window_trigger: None,
            window_done: None,
            recorded_exit: false,
            exits_in_window: false,
            title: String::new(),
            input_grant: None,
            entitled: false,
            theme: None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HelperSpec {
    pub socket_dir: PathBuf,
    pub host_epoch: String,
    pub generation: u64,
    pub next_host_id: u64,
    pub events: CarriedEvents,
    pub panes: Vec<HelperPane>,
    /// Environment the helper and every image it execs run with.
    pub env: Vec<(String, String)>,
    /// The state file's `previous_image`; `None` names the helper's own pin.
    pub previous_image: Option<PathBuf>,
    /// Seconds of the upgrade alarm the helper arms before exec.
    pub alarm_secs: Option<u32>,
    /// Arguments only the helper's own exec of the pin receives; the state
    /// file's argv, which a fallback runs with, leaves them out.
    pub primary_args: Vec<String>,
}

impl HelperSpec {
    pub fn new(socket_dir: &Path, panes: Vec<HelperPane>) -> Self {
        Self {
            socket_dir: socket_dir.to_path_buf(),
            host_epoch: "epoch-handover".into(),
            generation: 1,
            next_host_id: 100,
            events: CarriedEvents {
                cursor: 0,
                ring: Vec::new(),
            },
            panes,
            env: Vec::new(),
            previous_image: None,
            alarm_secs: None,
            primary_args: Vec::new(),
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Bound {
    pub control_ino: u64,
    pub frames_ino: u64,
    pub pidfile_ino: u64,
    pub pidfile_mtime_ns: i128,
}

/// A host restored by the helper lane; drop kills it and every pane group.
pub struct RestoredHost {
    child: Child,
    pub pid: u32,
    pub socket_dir: PathBuf,
}

impl RestoredHost {
    pub fn control(&self) -> UnixStream {
        let mut stream = connect(&self.socket_dir.join(CONTROL_SOCKET));
        let hello = hello_control(&mut stream, TOKEN);
        assert_eq!(hello["ok"], true, "{hello}");
        stream
    }

    /// The helper's exit status, or `None` if it still runs after `timeout`.
    pub fn wait_exit(&mut self, timeout: Duration) -> Option<ExitStatus> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.child.try_wait().expect("poll helper") {
                return Some(status);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    pub fn diagnostics(&self) -> String {
        let read = |name: &str| std::fs::read_to_string(self.socket_dir.join(name));
        format!(
            "log={:?} stderr={:?}",
            read("gterm.log").unwrap_or_default(),
            read("helper.stderr").unwrap_or_default()
        )
    }
}

impl Drop for RestoredHost {
    fn drop(&mut self) {
        // Read the pgid files here, not after restore: a helper that dies
        // before restoring still leaves its pane groups behind.
        let pgids = std::fs::read_dir(&self.socket_dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter(|entry| {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                name.starts_with("helper-") && name.ends_with(".pgid")
            })
            .filter_map(|entry| {
                std::fs::read_to_string(entry.path())
                    .ok()?
                    .trim()
                    .parse::<i32>()
                    .ok()
            })
            // 0 would signal this test's own group and a failed getpgid's -1
            // would become pid 1.
            .filter(|pgid| *pgid > 1);
        for pgid in pgids {
            // SAFETY: signalling a test-owned process group.
            unsafe { libc::kill(-pgid, libc::SIGKILL) };
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Runs the helper for `spec` and waits until the restored host answers.
pub fn restore(spec: &HelperSpec) -> RestoredHost {
    let mut host = launch(spec);
    // The helper binds before it captures, so a connect proves nothing: wait
    // for the bound record it writes last before exec, then for the exec'd
    // image to answer on the adopted listener.
    let bound = spec.socket_dir.join(BOUND_FILE);
    let control = spec.socket_dir.join(CONTROL_SOCKET);
    let deadline = Instant::now() + WAIT;
    while !(bound.exists() && answers(&control)) {
        if let Ok(Some(status)) = host.child.try_wait() {
            panic!(
                "helper exited {status} before restoring: {}",
                host.diagnostics()
            );
        }
        assert!(
            Instant::now() < deadline,
            "helper never restored: {}",
            host.diagnostics()
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    host
}

/// Starts the helper and returns without waiting for the restore.
pub fn launch(spec: &HelperSpec) -> RestoredHost {
    let dir = &spec.socket_dir;
    write_token(dir, TOKEN);
    std::fs::write(dir.join("local_cli_token"), "local-token").expect("local token");
    let stderr = std::fs::File::create(dir.join("helper.stderr")).expect("helper stderr");
    let child = Command::new(std::env::current_exe().expect("test binary"))
        .args([HELPER_TEST, "--exact", "--ignored", "--nocapture"])
        .env(SPEC_ENV, serde_json::to_string(spec).expect("encode spec"))
        .env("GTERM_LOG_FILE", dir.join("gterm.log"))
        .envs(spec.env.iter().map(|(name, value)| (name, value)))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr))
        .spawn()
        .expect("spawn helper");
    RestoredHost {
        pid: child.id(),
        child,
        socket_dir: dir.clone(),
    }
}

/// Whether a host answers `hello` on `control`, without panicking when the
/// image behind it has exited or not exec'd yet.
fn answers(control: &Path) -> bool {
    let Ok(mut stream) = UnixStream::connect(control) else {
        return false;
    };
    let hello = serde_json::json!({
        "id": "restore-probe",
        "method": "hello",
        "protocol_version": 1,
        "control_token": TOKEN,
    });
    let mut line = hello.to_string();
    line.push('\n');
    if stream.write_all(line.as_bytes()).is_err()
        || stream
            .set_read_timeout(Some(Duration::from_millis(500)))
            .is_err()
    {
        return false;
    }
    let mut reply = String::new();
    if BufReader::new(&stream).read_line(&mut reply).is_err() {
        return false;
    }
    serde_json::from_str::<Value>(reply.trim_end()).is_ok_and(|reply| reply["ok"] == true)
}

pub fn pgid_file(host_terminal_id: &str) -> String {
    format!("helper-{host_terminal_id}.pgid")
}

/// The `list` rows of `host`, keyed by host terminal id.
pub fn list_rows(stream: &mut UnixStream) -> Vec<Value> {
    let listed = rpc(stream, "list", serde_json::json!({}));
    assert_eq!(listed["ok"], true, "{listed}");
    listed["terminals"].as_array().cloned().unwrap_or_default()
}

/// The ignored helper test's body: a no-op unless the parent set a spec.
pub fn run_helper() {
    let Ok(spec) = std::env::var(SPEC_ENV) else {
        return;
    };
    let spec: HelperSpec = serde_json::from_str(&spec).expect("decode helper spec");
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("helper runtime");
    let err = runtime.block_on(capture_and_exec(spec));
    panic!("helper exec failed: {err}");
}

async fn capture_and_exec(spec: HelperSpec) -> std::io::Error {
    let dir = &spec.socket_dir;
    let pin = image::pin_image(
        &dir.join(image::IMAGES_DIR),
        Path::new(env!("CARGO_BIN_EXE_gterm")),
    )
    .expect("pin gterm");
    let control = bind(&dir.join(CONTROL_SOCKET));
    let frames = bind(&dir.join(FRAMES_SOCKET));
    let pid_file = dir.join(PID_FILE);
    std::fs::write(&pid_file, std::process::id().to_string()).expect("pidfile");

    let mut runtimes = Vec::new();
    for pane in &spec.panes {
        let runtime = PaneRuntime::spawn_argv_command(
            pane.rows,
            pane.cols,
            dir.clone(),
            &["sh".into(), "-c".into(), pane.script.clone()],
            &PaneLaunchEnv::default(),
            1 << 20,
            pane.theme
                .as_ref()
                .map_or_else(TerminalTheme::default, ThemeDeclaration::terminal_theme),
            None,
        )
        .expect("spawn pane");
        let pid = runtime.child_pid().expect("pane pid");
        // SAFETY: getpgid only reads the child's process group.
        let pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
        std::fs::write(
            dir.join(pgid_file(&pane.host_terminal_id)),
            pgid.to_string(),
        )
        .expect("pgid file");
        if let Some(text) = &pane.ready_text {
            wait_until(&format!("pane text {text:?}"), || {
                runtime.visible_text().contains(text.as_str())
            });
        }
        runtimes.push((runtime, pid, pgid));
    }

    // Capture: freeze every pane whose exit must cross the exec, pause every
    // reader, then take each master and terminal state.
    let mut captured = Vec::new();
    for (pane, (runtime, _, _)) in spec.panes.iter().zip(&runtimes) {
        if !pane.recorded_exit {
            runtime.freeze_reaping();
        }
        runtime
            .pause_handoff_reader(Duration::from_secs(1))
            .expect("pause reader");
        let master = runtime.duplicate_handoff_fd().expect("duplicate master");
        let (snapshot, core) = runtime.encode_handover().expect("encode pane");
        captured.push((master, snapshot, core));
    }
    for pane in &spec.panes {
        if let Some(trigger) = &pane.window_trigger {
            std::fs::write(trigger, b"").expect("window trigger");
        }
    }
    for (pane, (runtime, pid, _)) in spec.panes.iter().zip(&runtimes) {
        if let Some(done) = &pane.window_done {
            wait_until("window output", || done.exists());
        }
        if pane.exits_in_window {
            wait_until("frozen exit", || is_zombie(*pid));
        }
        if pane.recorded_exit {
            wait_until("recorded exit", || runtime.child_exit().is_some());
        }
    }

    let mut panes = Vec::new();
    for ((pane, (runtime, pid, pgid)), (master, snapshot, core)) in
        spec.panes.iter().zip(&runtimes).zip(captured)
    {
        inherit(master);
        let reservation_id = format!("res-{}", pane.host_terminal_id);
        let terminal_id = format!("term-{}", pane.host_terminal_id);
        let spawn_key = format!("spawn-{}", pane.host_terminal_id);
        panes.push(CarriedPane {
            host_terminal_id: pane.host_terminal_id.clone(),
            terminal_id: terminal_id.clone(),
            spawn_key: spawn_key.clone(),
            master_fd: master,
            pid: *pid,
            pgid: *pgid,
            start_time: 1.0,
            title: pane.title.clone(),
            rows: pane.rows,
            cols: pane.cols,
            pixel_width: 0,
            pixel_height: 0,
            last_seq: 0,
            fingerprint: 0,
            observation_state: ObservationState::Live,
            observation_generation: 1,
            observer_generation: 1,
            written_bytes: 0,
            dropped_bytes: 0,
            total_bytes: 0,
            truncated: false,
            input_grant: pane.input_grant.clone(),
            locator: None,
            reported_cwd: None,
            reservation_id: reservation_id.clone(),
            reserve_key: terminal_id.clone(),
            reserve_generation: 2,
            observer_bind: if pane.entitled {
                CarriedObserverBind::Entitled {
                    reservation_id: reservation_id.clone(),
                    generation: 2,
                }
            } else {
                CarriedObserverBind::None
            },
            reservation: pane.entitled.then(|| CarriedReservation {
                id: reservation_id,
                key: terminal_id.clone(),
                generation: 2,
                terminal_id: terminal_id.clone(),
                identity: Some(CarriedIdentity {
                    terminal_id,
                    spawn_key,
                }),
            }),
            exit: if pane.recorded_exit {
                runtime.child_exit()
            } else {
                None
            },
            snapshot_b64: encode_snapshot(&snapshot),
            core,
        });
    }

    let argv = vec![
        "gterm".to_string(),
        "host".to_string(),
        "--socket-dir".to_string(),
        dir.to_string_lossy().into_owned(),
    ];
    let state = HandoverState {
        format_version: FORMAT_VERSION,
        host_epoch: spec.host_epoch.clone(),
        generation: spec.generation,
        host_pid: std::process::id(),
        deadline_monotonic_ns: monotonic_now_ns() + 30_000_000_000,
        attempt: UpgradeAttempt {
            attempt_id: "attempt-handover".into(),
            candidate_sha256: pin.sha256.clone(),
            previous_sha256: pin.sha256.clone(),
        },
        previous_image: spec
            .previous_image
            .clone()
            .unwrap_or_else(|| pin.path.clone()),
        argv: argv.clone(),
        control_listener_fd: control,
        frames_listener_fd: frames,
        next_host_id: spec.next_host_id,
        events: spec.events.clone(),
        panes,
    };
    let state_path = dir.join(STATE_FILE);
    write_state(&state_path, &state).expect("write state");
    let bound = Bound {
        control_ino: ino(&dir.join(CONTROL_SOCKET)),
        frames_ino: ino(&dir.join(FRAMES_SOCKET)),
        pidfile_ino: ino(&pid_file),
        pidfile_mtime_ns: mtime_ns(&pid_file),
    };
    std::fs::write(
        dir.join(BOUND_FILE),
        serde_json::to_vec(&bound).expect("encode bound"),
    )
    .expect("bound file");
    for (runtime, _, _) in runtimes {
        runtime.preserve_for_handoff();
    }
    if let Some(secs) = spec.alarm_secs {
        // SAFETY: alarm only schedules SIGALRM; the pending alarm crosses exec.
        unsafe { libc::alarm(secs) };
    }
    Command::new(&pin.path)
        .arg0("gterm")
        .args(&argv[1..])
        .args(&spec.primary_args)
        .arg("--resume-state")
        .arg(&state_path)
        .exec()
}

/// Binds a listener the way `run` does and leaks it for the next image.
fn bind(path: &Path) -> RawFd {
    let listener = UnixListener::bind(path).expect("bind helper socket");
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).expect("socket mode");
    let fd = listener.into_raw_fd();
    inherit(fd);
    fd
}

/// Clears close-on-exec so the next image inherits `fd`.
fn inherit(fd: RawFd) {
    // SAFETY: fd is open and owned by this helper for the next image.
    assert_eq!(
        unsafe { libc::fcntl(fd, libc::F_SETFD, 0) },
        0,
        "clear CLOEXEC"
    );
}

pub fn ino(path: &Path) -> u64 {
    std::fs::metadata(path).expect("metadata").ino()
}

pub fn mtime_ns(path: &Path) -> i128 {
    let metadata = std::fs::metadata(path).expect("metadata");
    i128::from(metadata.mtime()) * 1_000_000_000 + i128::from(metadata.mtime_nsec())
}

/// Whether `pid` has exited and is still waiting to be reaped.
pub fn is_zombie(pid: u32) -> bool {
    // SAFETY: siginfo_t is plain data; waitid fills it or leaves it zeroed.
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    // SAFETY: info is a valid out-pointer; WNOWAIT leaves the child unreaped.
    let rc = unsafe {
        libc::waitid(
            libc::P_PID,
            pid as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOWAIT | libc::WNOHANG,
        )
    };
    // SAFETY: waitid succeeded, so si_pid is initialized.
    rc == 0 && unsafe { info.si_pid() } == pid as libc::pid_t
}

pub fn wait_until(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + WAIT;
    while !ready() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(10));
    }
}
