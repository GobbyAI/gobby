//! Full-daemon harness tests (design-v2 sections 6.2 C14-C15 and 7.3): this checkout's gdaemon
//! and Python worker on a per-run database of the isolated test hub. Database-backed tests print
//! the skip line and return when `DATABASE_URL` is unset (H18).

use std::fs;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use gobby_core::grant::{encode_grant_header, parse_grant_json};
use gobby_test_support::corpus_v2::{load_all_cases, load_manifest};
use gobby_test_support::database::{create_database, database_exists};
use gobby_test_support::full_daemon::{
    FullDaemon, FullDaemonError, SeedContext, TeardownStep, pid_alive, processes_in_run,
    test_hub_url_or_skip,
};
use gobby_test_support::http_sender::{
    Credentials, agent, classify_response, get_status, post_json, send_case,
};
use gobby_test_support::hub_guard::{GuardMode, check_test_hub, maintenance_url, operator_home};
use serde_json::{Value, json};

const GDAEMON: &str = env!("CARGO_BIN_EXE_gdaemon");
const REPO_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");
const MACHINE_ID: &str = "21000000-0000-4000-8000-000000000002";
const PROJECT_ID: &str = "00000000-0000-0000-0000-000000000e2e";
/// A loopback test hub on a port that is not shared, so a rehearsal profile may name it.
const REHEARSAL_URL: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:61503/gobby_test";
const REHEARSAL_PORT: u16 = 61503;
/// The 13 v2 cases the full daemon replays on code defaults (section 7.3.7), in manifest order.
const DORMANCY_CASES: [&str; 13] = [
    "health_ok",
    "config_schema",
    "config_values",
    "tasks_list",
    "runtime_handshake_challenge",
    "runtime_handshake",
    "auth_missing_auth",
    "auth_missing_grant",
    "auth_forged_identity",
    "hooks_execute_ok",
    "hooks_execute_replay",
    "hooks_execute_below_floor",
    "hooks_execute_bad_schema",
];

/// The test hub URL from the process environment, or `None` after printing the skip line.
fn test_hub() -> Option<String> {
    test_hub_url_or_skip(&|name| std::env::var(name).ok())
}

/// The e2e fixture's database state, applied to the run database outside pytest: the canonical
/// user and machine rows, the runtime rows of `_seed_e2e_runtime_state`, and the local operator
/// key minted by `ensure_local_api_key` (tests/e2e/conftest.py:547-612,
/// tests/e2e/test_runtime_boundary.py:76-84, tests/contracts/test_http_corpus.py:43-68). The key
/// is printed as the last line of stdout.
const SEED_SCRIPT: &str = r#"
import json
import sys
from pathlib import Path

from gobby.config.bootstrap_io import read_bootstrap_yaml
from gobby.storage.api_keys import ensure_local_api_key
from gobby.storage.hub.postgres import PostgresHubDatabase
from tests.e2e.conftest import _seed_e2e_runtime_state, build_e2e_project
from tests.e2e.test_runtime_boundary import E2E_MACHINE_ID, _seed_identity_rows
from tests.fixtures.postgres import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    TEST_USER_NAME,
    TEST_USER_PASSWORD_HASH,
)

url, home, project = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
host_dir = Path(sys.argv[4])
db = PostgresHubDatabase(url)
db.execute(
    """
    INSERT INTO users (id, email, name, password_hash)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (id) DO NOTHING
    """,
    (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
)
_seed_identity_rows(db, E2E_MACHINE_ID, TEST_USER_ID)
build_e2e_project(project)
_seed_e2e_runtime_state(db, project, terminal_host_socket_dir=host_dir)
ensure_local_api_key(db, E2E_MACHINE_ID, home / "bootstrap.yaml")
key = read_bootstrap_yaml(home / "bootstrap.yaml")["api_key"]
print(json.dumps({"operator_key": key}))
"#;

/// The pre-start seed step: runs [`SEED_SCRIPT`] on the checkout's interpreter.
/// The seed interpreter's fixed PATH on Unix. Windows has no such system PATH, and the harness
/// refuses to launch there before the seed runs.
#[cfg(unix)]
const SEED_PATH: &str = "/usr/bin:/bin:/usr/sbin:/sbin";
#[cfg(windows)]
const SEED_PATH: &str = "";

/// The installed-user seed step. Shared with the loopback tests in this suite binary.
pub(crate) fn python_seed(context: &SeedContext<'_>) -> Result<String, String> {
    let output = Command::new(context.python)
        .arg("-c")
        .arg(SEED_SCRIPT)
        .arg(context.database_url)
        .arg(context.home)
        .arg(context.project_dir)
        .arg(context.terminal_host_dir)
        .current_dir(context.repo_root)
        .env_clear()
        .env("PATH", SEED_PATH)
        .env("HOME", context.home)
        .env("GOBBY_HOME", context.home)
        .env("GOBBY_TEST_PROTECT", "1")
        .env("PYTHONPATH", context.repo_root.join("src"))
        .output()
        .map_err(|error| format!("run the seed interpreter: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if !output.status.success() {
        return Err(format!(
            "the seed exited with {}:\n{stdout}{}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let last = stdout
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .ok_or_else(|| "the seed printed no operator key".to_owned())?;
    let value: Value =
        serde_json::from_str(last).map_err(|error| format!("the seed's output: {error}"))?;
    value["operator_key"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| "the seed's output names no operator_key".to_owned())
}

/// Start the full daemon from this checkout with the process environment.
fn start_daemon() -> Result<FullDaemon, FullDaemonError> {
    FullDaemon::builder(Path::new(GDAEMON), Path::new(REPO_ROOT))
        .seed(python_seed)
        .start()
}

/// The grant credential's headers: the operator bearer, then the presented grant, as the Python
/// harness's `grant_headers` builds them (tests/e2e/test_runtime_boundary.py:734-765).
fn grant_headers(daemon: &FullDaemon) -> Vec<(String, String)> {
    let body = json!({"machine_id": MACHINE_ID, "project_id": PROJECT_ID});
    let url = format!("{}/api/runtime/handshake", daemon.front_door());
    let (status, payload) =
        post_json(&url, Some(daemon.operator_key()), &body).expect("the handshake answers");
    assert_eq!(status, 200, "the handshake was refused: {payload}");
    let grant = parse_grant_json(payload["grant"].to_string().as_bytes())
        .expect("the handshake grant parses");
    let encoded = encode_grant_header(&grant).expect("the grant encodes");
    vec![
        (
            "Authorization".to_owned(),
            format!("Bearer {}", daemon.operator_key()),
        ),
        ("X-Gobby-Runtime-Grant".to_owned(), encoded),
        (
            "X-Gobby-Machine-Id".to_owned(),
            grant.principal.machine_id.clone(),
        ),
        (
            "X-Gobby-Caller-Project-Id".to_owned(),
            grant.principal.project_id.clone(),
        ),
        (
            "X-Gobby-Project-Id".to_owned(),
            grant.principal.project_id.clone(),
        ),
    ]
}

/// A rehearsal profile under `dir`, written owner-only unless `mode` says otherwise.
fn write_profile(
    dir: &Path,
    gobby_home: &Path,
    host: &str,
    port: u16,
    identifier: Option<&str>,
    mode: u32,
) -> PathBuf {
    let path = dir.join("rehearsal-profile.json");
    let mut body = json!({
        "gobby_home": gobby_home.display().to_string(),
        "postgres_host": host,
        "services": {"postgres": {"port": port}},
    });
    if let Some(identifier) = identifier {
        body["postgres_system_identifier"] = Value::String(identifier.to_owned());
    }
    fs::write(&path, body.to_string()).expect("write the profile");
    #[cfg(unix)]
    fs::set_permissions(&path, fs::Permissions::from_mode(mode)).expect("chmod the profile");
    #[cfg(windows)]
    let _ = mode; // Windows has no mode bits to set; the guard refuses such a profile there.
    path
}

/// The guard's verdict as `accept:<mode>` or `refuse:<rule>`.
fn verdict(url: &str, profile: Option<&Path>) -> String {
    match check_test_hub(Some(url), &|_| None, profile) {
        Ok(guard) => match guard.mode {
            GuardMode::TestHub => "accept:test_hub".to_owned(),
            GuardMode::Rehearsal => "accept:rehearsal".to_owned(),
        },
        Err(error) => format!("refuse:{}", error.rule),
    }
}

#[test]
fn unset_database_url_prints_the_skip_line_and_returns() {
    assert_eq!(test_hub_url_or_skip(&|_| None), None);
    assert_eq!(
        gobby_test_support::full_daemon::SKIP_LINE,
        "skipped: DATABASE_URL is unset (P0-HARNESS needs the isolated test hub)"
    );
}

#[test]
fn refused_database_url_fails_before_any_override_is_set() {
    let live = "postgresql://gobby:gobby@localhost:60891/gobby";
    // A missing gdaemon path would fail at spawn; the guard must refuse first.
    let result = FullDaemon::builder(Path::new("/nonexistent/gdaemon"), Path::new(REPO_ROOT))
        .environment(move |name| (name == "DATABASE_URL").then(|| live.to_owned()))
        .seed(python_seed)
        .start();
    match result {
        Err(FullDaemonError::Guard(error)) => assert_eq!(error.rule, "R3"),
        Err(other) => panic!("the guard must refuse before any spawn, got {other}"),
        Ok(_) => panic!("the live hub must never start a daemon"),
    }
    assert!(
        std::env::var_os("GOBBY_ALLOW_WORKTREE_DAEMON").is_none(),
        "the test process's own environment never receives the override"
    );
}

/// Set in the re-executed test binary, which plays the owner that dies without teardown (H60).
const OWNER_CHILD: &str = "GOBBY_HARNESS_OWNER_CHILD";

/// A process the test starts in a run's root. Killed and reaped on drop, so a failing assertion
/// never leaves it behind.
struct Stray(std::process::Child);

impl Drop for Stray {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Teardown treats any process left in the run's root as a failure of the run and stops it. A
/// terminal host or worker that survives its orderly stop is the case this guards: the host is
/// found by cwd even when its pid file was never written.
#[test]
fn teardown_fails_and_stops_a_process_left_in_the_run_root() {
    let Some(_url) = test_hub() else { return };
    let daemon = start_daemon().expect("the full daemon starts");
    let root = daemon.run_root().to_path_buf();
    let stray = Stray(
        Command::new("sleep")
            .arg("600")
            .current_dir(&root)
            .spawn()
            .expect("spawn a stray in the run root"),
    );
    let pid = stray.0.id();
    let result = daemon.teardown();
    assert!(
        matches!(&result, Err(FullDaemonError::Teardown(text)) if text.contains(&pid.to_string())),
        "teardown must fail naming the stray {pid}, got {result:?}"
    );
    assert!(!pid_alive(pid), "teardown must stop the stray {pid}");
}

/// H50: a start whose readiness never passes tears down the worker it had started, and reports
/// the pids it held. The worker must be running when readiness fails, so the test proves the
/// teardown reached a live worker and not an empty launch.
#[test]
fn failed_start_tears_down_a_running_worker() {
    let Some(_) = test_hub() else {
        return;
    };
    let started = FullDaemon::builder(Path::new(GDAEMON), Path::new(REPO_ROOT))
        .seed(python_seed)
        .readiness_path("/api/harness/never-ready")
        .ready_ceiling(Duration::from_secs(30))
        .start();
    match started {
        Err(FullDaemonError::FailedStart {
            daemon_pid,
            worker_pid,
            teardown,
            ..
        }) => {
            assert_eq!(teardown, None, "the failed start's teardown must complete");
            let worker = worker_pid.expect("the worker was running when readiness failed");
            assert!(!pid_alive(daemon_pid), "gdaemon outlived its failed start");
            assert!(!pid_alive(worker), "the worker outlived its failed start");
        }
        Err(other) => panic!("the start must fail at readiness, got {other}"),
        Ok(_) => panic!("a route that never answers 200 must not be ready"),
    }
}

/// The pid, parent pid, process group and command basename of each process, from one `ps` read.
fn process_rows() -> Vec<(u32, u32, u32, String)> {
    let Ok(output) = Command::new("ps")
        .args([
            "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "comm=",
        ])
        .output()
    else {
        return Vec::new();
    };
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let pid = fields.next()?.parse().ok()?;
            let ppid = fields.next()?.parse().ok()?;
            let pgid = fields.next()?.parse().ok()?;
            let command: Vec<&str> = fields.collect();
            let name = Path::new(&command.join(" "))
                .file_name()?
                .to_str()?
                .to_owned();
            Some((pid, ppid, pgid, name))
        })
        .collect()
}

/// A failed start that loses gdaemon after its worker came up still stops that worker. gdaemon
/// is killed alone, so the worker is reparented and no lookup by parent finds it afterwards: the
/// worker must have been recorded while gdaemon lived.
#[test]
fn failed_start_stops_a_worker_recorded_before_gdaemon_died() {
    let Some(_) = test_hub() else { return };
    let owner = std::process::id();
    let crash = std::thread::spawn(move || -> Option<u32> {
        let deadline = Instant::now() + Duration::from_secs(120);
        while Instant::now() < deadline {
            let rows = process_rows();
            let daemon = rows
                .iter()
                .find(|(_, ppid, _, name)| *ppid == owner && name == "gdaemon")
                .map(|(pid, ..)| *pid);
            if let Some(daemon) = daemon {
                let worker = rows
                    .iter()
                    .find(|(pid, ppid, pgid, name)| {
                        *ppid == daemon && pid == pgid && name.starts_with("python")
                    })
                    .map(|(pid, ..)| *pid);
                if let Some(worker) = worker {
                    // The readiness loop polls every 100 ms: two seconds lets it record the worker.
                    std::thread::sleep(Duration::from_secs(2));
                    let _ = Command::new("kill")
                        .args(["-KILL", &daemon.to_string()])
                        .status();
                    return Some(worker);
                }
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        None
    });
    let started = FullDaemon::builder(Path::new(GDAEMON), Path::new(REPO_ROOT))
        .seed(python_seed)
        .readiness_path("/api/harness/never-ready")
        .ready_ceiling(Duration::from_secs(120))
        .start();
    let worker = crash
        .join()
        .expect("the crash thread finishes")
        .expect("the worker came up before readiness failed");
    match started {
        Err(FullDaemonError::FailedStart {
            daemon_pid,
            worker_pid,
            teardown,
            ..
        }) => {
            assert_eq!(teardown, None, "the failed start's teardown must complete");
            assert_eq!(
                worker_pid,
                Some(worker),
                "the worker recorded before gdaemon died is the one reported"
            );
            assert!(!pid_alive(daemon_pid), "gdaemon outlived its failed start");
            assert!(!pid_alive(worker), "the worker outlived its failed start");
        }
        Err(other) => panic!("the start must fail at readiness, got {other}"),
        Ok(_) => panic!("gdaemon was killed before readiness, so the start cannot succeed"),
    }
}

/// H60: when the process that owns a daemon dies without teardown, the daemon's groups and the
/// terminal host still stop. The test re-executes this binary as the owner, which starts a
/// daemon, reports its pids, and exits with no Drop and no teardown. The watchdog must then stop
/// all three.
#[test]
fn owner_death_tears_down_the_daemon_groups() {
    let Some(_) = test_hub() else {
        return;
    };
    if std::env::var_os(OWNER_CHILD).is_some() {
        let daemon = start_daemon().expect("the owner's daemon starts");
        let host = daemon
            .terminal_host_pid()
            .expect("the owner's daemon started the terminal host");
        println!(
            "PIDS {} {} {}",
            daemon.daemon_pid(),
            daemon.worker_pid().expect("the owner's worker"),
            host
        );
        println!("ROOT {}", daemon.run_root().display());
        // Exit without teardown or Drop: the owner's death is what the watchdog must survive.
        std::process::exit(0);
    }
    let output = Command::new(std::env::current_exe().expect("this test binary"))
        .arg("owner_death_tears_down_the_daemon_groups")
        .arg("--nocapture")
        .env(OWNER_CHILD, "1")
        .output()
        .expect("run the owner");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let pids: Vec<u32> = stdout
        .lines()
        .find_map(|line| line.strip_prefix("PIDS "))
        .expect("the owner reports its pids")
        .split_whitespace()
        .map(|pid| pid.parse().expect("a pid"))
        .collect();
    let [daemon, worker, host]: [u32; 3] = pids
        .try_into()
        .unwrap_or_else(|pids: Vec<u32>| panic!("three pids expected, got {pids:?}"));
    assert!(
        daemon != 0 && worker != 0 && host != 0,
        "the owner's daemon, worker and terminal host are all reported: {daemon} {worker} {host}"
    );
    let root = stdout
        .lines()
        .find_map(|line| line.strip_prefix("ROOT "))
        .map(PathBuf::from)
        .expect("the owner reports its run root");
    let deadline = Instant::now() + Duration::from_secs(15);
    while [daemon, worker, host].iter().any(|&pid| pid_alive(pid)) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    let survivors = processes_in_run(&root, &[daemon, worker, host]);
    // The run root is left by the owner's death, so the test removes it once the check has run.
    let _ = fs::remove_dir_all(&root);
    for (name, pid) in [
        ("gdaemon", daemon),
        ("worker", worker),
        ("gterm host", host),
    ] {
        assert!(!pid_alive(pid), "the {name} (pid {pid}) outlived its owner");
    }
    assert!(
        survivors.is_empty(),
        "processes of the run outlived its owner: {survivors:?}"
    );
}

// Unix only: the accepted profile needs the 0600 owner check. The Windows verdicts for these
// profiles are the distinct refusal that guard.rs asserts.
#[cfg(unix)]
#[test]
fn rehearsal_profile_vectors_accept_and_refuse() {
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join("gobby-home");
    let identifier = Some("7000000000000000001");
    let accepted = write_profile(
        dir.path(),
        &home,
        "127.0.0.1",
        REHEARSAL_PORT,
        identifier,
        0o600,
    );
    assert_eq!(verdict(REHEARSAL_URL, Some(&accepted)), "accept:rehearsal");

    let operator = operator_home().expect("the operator home is known");
    let operator_profile = write_profile(
        dir.path(),
        &operator,
        "127.0.0.1",
        REHEARSAL_PORT,
        identifier,
        0o600,
    );
    assert_eq!(verdict(REHEARSAL_URL, Some(&operator_profile)), "refuse:R4");

    let loose = write_profile(
        dir.path(),
        &home,
        "127.0.0.1",
        REHEARSAL_PORT,
        identifier,
        0o644,
    );
    assert_eq!(verdict(REHEARSAL_URL, Some(&loose)), "refuse:R4");

    let named = write_profile(
        dir.path(),
        &home,
        "localhost",
        REHEARSAL_PORT,
        identifier,
        0o600,
    );
    assert_eq!(verdict(REHEARSAL_URL, Some(&named)), "refuse:R4");

    let shared = write_profile(dir.path(), &home, "127.0.0.1", 60892, identifier, 0o600);
    assert_eq!(
        verdict(
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
            Some(&shared)
        ),
        "refuse:R4"
    );

    let unidentified = write_profile(dir.path(), &home, "127.0.0.1", REHEARSAL_PORT, None, 0o600);
    assert_eq!(verdict(REHEARSAL_URL, Some(&unidentified)), "refuse:R4");

    let link = dir.path().join("profile-link.json");
    std::os::unix::fs::symlink(&accepted, &link).expect("symlink the profile");
    assert_eq!(verdict(REHEARSAL_URL, Some(&link)), "refuse:R4");
}

#[test]
fn start_serves_auth_status_and_tears_down() {
    let Some(_url) = test_hub() else { return };
    let daemon = start_daemon().expect("the full daemon starts");
    let status = get_status(&format!("{}/api/auth/status", daemon.front_door()))
        .expect("the auth status answers");
    assert_eq!(status, 200);
    let report = daemon.teardown().expect("teardown");
    assert!(!report.processes_alive_at_drop);
}

#[test]
fn teardown_stops_daemon_and_worker_before_the_database_drops() {
    let Some(url) = test_hub() else { return };
    let daemon = start_daemon().expect("the full daemon starts");
    let daemon_pid = daemon.daemon_pid();
    let worker_pid = daemon.worker_pid().expect("readiness recorded the worker");
    let host_pid = daemon
        .terminal_host_pid()
        .expect("the daemon started the terminal host");
    assert!(
        pid_alive(host_pid),
        "the terminal host runs before teardown"
    );
    let name = daemon.database_name().expect("a run database").to_owned();
    let root = daemon.run_root().to_path_buf();
    let report = daemon.teardown().expect("teardown");
    assert!(
        processes_in_run(&root, &[daemon_pid, worker_pid, host_pid]).is_empty(),
        "no process of the run may survive teardown"
    );
    assert_eq!(
        report.steps,
        [
            TeardownStep::DaemonStopped,
            TeardownStep::WorkerStopped,
            TeardownStep::TerminalHostStopped,
            TeardownStep::DatabaseDropped,
            TeardownStep::HomeRemoved,
        ]
    );
    assert!(
        !report.processes_alive_at_drop,
        "no process may outlive the database drop"
    );
    assert!(!pid_alive(daemon_pid), "gdaemon is gone");
    assert!(!pid_alive(worker_pid), "the worker is gone");
    assert!(!pid_alive(host_pid), "the terminal host is gone");
    assert!(!database_exists(&maintenance_url(&url), &name).expect("probe"));
}

/// What a test records about its daemon before it panics, for the checks after the unwind.
struct PanicRecord {
    daemon_pid: u32,
    worker_pid: Option<u32>,
    database: String,
    host_pid: Option<u32>,
    root: PathBuf,
}

#[test]
fn teardown_runs_from_drop_when_a_test_panics() {
    let Some(url) = test_hub() else { return };
    let mut seen: Option<PanicRecord> = None;
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let daemon = start_daemon().expect("the full daemon starts");
        let name = daemon.database_name().expect("a run database").to_owned();
        seen = Some(PanicRecord {
            daemon_pid: daemon.daemon_pid(),
            worker_pid: daemon.worker_pid(),
            database: name,
            host_pid: daemon.terminal_host_pid(),
            root: daemon.run_root().to_path_buf(),
        });
        panic!("simulated failure after start");
    }));
    assert!(result.is_err(), "the closure panicked");
    let PanicRecord {
        daemon_pid,
        worker_pid,
        database: name,
        host_pid,
        root,
    } = seen.expect("start recorded its processes");
    assert!(!pid_alive(daemon_pid), "gdaemon outlived the panic");
    assert!(
        !worker_pid.is_some_and(pid_alive),
        "the worker outlived the panic"
    );
    let groups: Vec<u32> = [Some(daemon_pid), worker_pid, host_pid]
        .into_iter()
        .flatten()
        .collect();
    assert!(
        processes_in_run(&root, &groups).is_empty(),
        "a process of the run outlived the panic"
    );
    assert!(
        !database_exists(&maintenance_url(&url), &name).expect("probe"),
        "the run database outlived the panic"
    );
}

#[test]
fn database_gone_after_teardown() {
    let Some(url) = test_hub() else { return };
    let daemon = start_daemon().expect("the full daemon starts");
    let name = daemon.database_name().expect("a run database").to_owned();
    let admin = maintenance_url(&url);
    assert!(
        database_exists(&admin, &name).expect("probe"),
        "the run database exists while running"
    );
    daemon.teardown().expect("teardown");
    assert!(
        !database_exists(&admin, &name).expect("probe"),
        "the run database is gone"
    );
}

#[test]
fn start_sweeps_dead_pid_database() {
    let Some(url) = test_hub() else { return };
    let admin = maintenance_url(&url);
    // This test binary, asked for its help text, exits at once on every platform.
    let mut child = Command::new(std::env::current_exe().expect("this test binary"))
        .arg("--help")
        .spawn()
        .expect("spawn a short-lived child");
    let dead = child.id();
    child.wait().expect("reap the child");
    let planted = format!("gobby_harness_{dead}_0123456789ab");
    create_database(&admin, &planted).expect("plant a dead pid's database");
    assert!(database_exists(&admin, &planted).expect("probe"));
    let daemon = start_daemon().expect("the full daemon starts");
    daemon.teardown().expect("teardown");
    assert!(
        !database_exists(&admin, &planted).expect("probe"),
        "start removed the dead pid's database"
    );
}

#[test]
fn dormancy_v2_13_cases_replay_unchanged() {
    let Some(_url) = test_hub() else { return };
    let daemon = start_daemon().expect("the full daemon starts");
    let manifest = load_manifest();
    let cases = load_all_cases(&manifest);
    let credentials = Credentials {
        operator_key: Some(daemon.operator_key().to_owned()),
        grant_headers: grant_headers(&daemon),
    };
    let client = agent();
    let mut replayed: Vec<&str> = Vec::new();
    for case in &cases {
        let name = case["name"].as_str().expect("case name");
        if !DORMANCY_CASES.contains(&name) {
            continue;
        }
        let captured = send_case(&client, &daemon.front_door(), case, &credentials)
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        classify_response(case, captured).unwrap_or_else(|diff| panic!("{diff}"));
        replayed.push(name);
    }
    assert_eq!(
        replayed, DORMANCY_CASES,
        "the 13 cases replay in manifest order"
    );
    daemon.teardown().expect("teardown");
}
