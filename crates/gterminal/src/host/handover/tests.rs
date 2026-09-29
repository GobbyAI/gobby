use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;

use serde_json::json;

use super::restore::stage;
use super::*;
use crate::host::state::{Identity, ObserverBind};
use crate::pane::{PaneLaunchEnv, PaneRuntime};
use crate::terminal_theme::TerminalTheme;

fn state_with(panes: Vec<CarriedPane>, control: RawFd, frames: RawFd) -> HandoverState {
    HandoverState {
        format_version: FORMAT_VERSION,
        host_epoch: "epoch-1".into(),
        generation: 3,
        host_pid: std::process::id(),
        deadline_monotonic_ns: monotonic_now_ns() + 60_000_000_000,
        attempt: UpgradeAttempt {
            attempt_id: "attempt-1".into(),
            candidate_sha256: "c".repeat(64),
            previous_sha256: "p".repeat(64),
        },
        previous_image: PathBuf::from("/nonexistent/gterm-previous"),
        argv: vec!["gterm".into(), "host".into()],
        control_listener_fd: control,
        frames_listener_fd: frames,
        next_host_id: 42,
        latest_theme: None,
        events: CarriedEvents {
            cursor: 7,
            ring: vec![
                json!({"event": "terminal_exited", "epoch": "epoch-1", "seq": 6}),
                json!({"event": "terminal_exited", "epoch": "epoch-1", "seq": 7}),
            ],
        },
        panes,
    }
}

#[test]
fn write_state_is_private_and_round_trips() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join(STATE_FILE);
    let state = state_with(Vec::new(), 7, 8);

    write_state(&path, &state).expect("write state");

    let mode = fs::metadata(&path).expect("metadata").permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
    let names: Vec<_> = fs::read_dir(dir.path())
        .expect("read dir")
        .map(|entry| entry.expect("entry").file_name())
        .collect();
    assert_eq!(names, vec![std::ffi::OsString::from(STATE_FILE)]);
    assert_eq!(read_state(&path).expect("read state"), state);
}

#[test]
fn read_state_refuses_an_unsupported_format() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join(STATE_FILE);
    let mut state = state_with(Vec::new(), 7, 8);
    state.format_version = FORMAT_VERSION + 1;
    write_state(&path, &state).expect("write state");

    let err = read_state(&path).expect_err("unsupported format");

    assert_eq!(err.kind(), io::ErrorKind::InvalidData);
    assert!(err.to_string().contains("unsupported handover format"));
}

#[tokio::test(flavor = "multi_thread")]
async fn stage_rebuilds_generation_attempt_and_registry() {
    let dir = tempfile::tempdir().expect("tempdir");
    let runtime = PaneRuntime::spawn_argv_command(
        24,
        80,
        dir.path().to_path_buf(),
        &["sh".into(), "-c".into(), "sleep 30".into()],
        &PaneLaunchEnv::default(),
        1 << 20,
        TerminalTheme::default(),
        None,
    )
    .expect("spawn pane");
    let pid = runtime.child_pid().expect("child pid");
    runtime
        .pause_handoff_reader(Duration::from_secs(1))
        .expect("pause reader");
    let master = runtime.duplicate_handoff_fd().expect("duplicate master");
    let (snapshot, core) = runtime.encode_handover().expect("encode pane");
    let control = std::os::unix::net::UnixListener::bind(dir.path().join("c.sock")).expect("bind");
    let frames = std::os::unix::net::UnixListener::bind(dir.path().join("f.sock")).expect("bind");
    let pid_file = dir.path().join("gterm.pid");
    fs::write(&pid_file, std::process::id().to_string()).expect("pidfile");
    let pane = CarriedPane {
        host_terminal_id: "ht-17".into(),
        terminal_id: "term-17".into(),
        spawn_key: "spawn-17".into(),
        master_fd: master,
        pid,
        pgid: pid as i32,
        start_time: 12.5,
        title: "agent".into(),
        rows: 24,
        cols: 80,
        pixel_width: 640,
        pixel_height: 480,
        last_seq: 9,
        fingerprint: 5,
        observation_state: ObservationState::Live,
        observation_generation: 2,
        observer_generation: 4,
        written_bytes: 10,
        dropped_bytes: 1,
        total_bytes: 11,
        truncated: false,
        input_grant: Some("att-1".into()),
        locator: None,
        reported_cwd: None,
        reservation_id: "res-17".into(),
        reserve_key: "key-17".into(),
        reserve_generation: 2,
        observer_bind: CarriedObserverBind::Entitled {
            reservation_id: "res-17".into(),
            generation: 2,
        },
        reservation: Some(CarriedReservation {
            id: "res-17".into(),
            key: "key-17".into(),
            generation: 2,
            terminal_id: "term-17".into(),
            identity: Some(CarriedIdentity {
                terminal_id: "term-17".into(),
                spawn_key: "spawn-17".into(),
            }),
        }),
        exit: None,
        snapshot_b64: encode_snapshot(&snapshot),
        core,
    };
    let path = dir.path().join(STATE_FILE);
    write_state(
        &path,
        &state_with(vec![pane], control.as_raw_fd(), frames.as_raw_fd()),
    )
    .expect("write state");

    let carried = read_state(&path).expect("read state");
    let staged = stage(carried, &path, &pid_file, 1 << 20, 1 << 20, false).expect("stage");

    assert_eq!(staged.host.host_epoch, "epoch-1");
    assert_eq!(staged.host.generation, 3);
    assert_eq!(staged.host.upgrade.attempt.attempt_id, "attempt-1");
    assert_eq!(staged.host.upgrade.outcome, None);
    assert_eq!(
        staged.host.events.cursor().await,
        ("epoch-1".to_string(), 7)
    );
    let inner = &staged.host.inner;
    assert_eq!(inner.next_host_id, 42);
    let identity = Identity {
        terminal_id: "term-17".into(),
        spawn_key: "spawn-17".into(),
    };
    assert_eq!(inner.by_host_id.get("ht-17"), Some(&identity));
    let slot = inner.terminals.get(&identity).expect("restored slot");
    assert_eq!(
        (slot.title.as_str(), slot.rows, slot.cols, slot.last_seq),
        ("agent", 24, 80, 9)
    );
    assert_eq!(slot.input_grant.as_deref(), Some("att-1"));
    assert!(matches!(
        &slot.observer_bind,
        ObserverBind::Entitled { reservation_id, generation: 2 } if reservation_id == "res-17"
    ));
    let reservation = inner.reservations.get("res-17").expect("reservation");
    assert!(reservation.prepared);
    assert_eq!(reservation.identity.as_ref(), Some(&identity));
    // Stage worked on duplicates: the carried descriptors are still open.
    // SAFETY: F_GETFD only reads descriptor flags.
    assert!(unsafe { libc::fcntl(master, libc::F_GETFD) } >= 0);
    drop(staged);
    // SAFETY: F_GETFD only reads descriptor flags.
    assert!(unsafe { libc::fcntl(master, libc::F_GETFD) } >= 0);
    // SAFETY: master is the duplicate this test took and still owns.
    unsafe { libc::close(master) };
    drop(runtime);
}
