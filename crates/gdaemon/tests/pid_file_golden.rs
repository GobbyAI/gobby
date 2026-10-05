#![cfg(unix)]

use gobby_daemon::lifecycle::pid_file::*;
use serde_json::{Value, json};
use std::fs;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Stdio};
use tempfile::TempDir;

const DAEMON: &[u8] = include_bytes!("../../../tests/fixtures/pid_file_records/daemon_claim.json");
const RESERVATION: &[u8] =
    include_bytes!("../../../tests/fixtures/pid_file_records/service_reservation.json");

fn record(path: &Path) -> anyhow::Result<Value> {
    parse_record(&fs::read(path.with_extension("pid.lock"))?)
}

fn parse_record(raw: &[u8]) -> anyhow::Result<Value> {
    anyhow::ensure!(decode_record(raw).is_some(), "invalid record");
    Ok(serde_json::from_slice(raw)?)
}

#[test]
fn python_records_parse() -> anyhow::Result<()> {
    for (raw, state, generation) in [(DAEMON, "daemon", 7), (RESERVATION, "reservation", 8)] {
        let parsed = parse_record(raw)?;
        assert_eq!(parsed["state"], state);
        assert_eq!(parsed["generation"], generation);
        assert_eq!(
            encode_record(&parsed)?,
            raw.strip_suffix(b"\n").unwrap_or(raw)
        );
    }
    let parsed = parse_record(RESERVATION)?;
    assert_eq!(
        parsed["reservation"]["nonce_path"],
        "/tmp/gobby-雪-😀/gobby.pid.service-nonce"
    );
    assert_eq!(parsed["reservation"]["issued_at"], 1791195000.123456);
    Ok(())
}

#[test]
fn corrupt_records_fail_closed() {
    let corrupt = String::from_utf8_lossy(DAEMON).replace("4242", "4243");
    for raw in [corrupt.as_bytes(), b"[]", b"{", b"", b"\xff"] {
        assert!(decode_record(raw).is_none());
    }
}

#[test]
fn canonical_float_and_string_boundaries_match_python() -> anyhow::Result<()> {
    let values = json!({"floats":[0.0,-0.0,0.0001,0.00001,1e16,1e23,5e-324,
        1791195000.123456,1.2345678901234567],"text":"雪😀\u{7f}\n\t\"\\"});
    let encoded = encode_record(&values)?;
    let script = r#"import sys
from gobby.runner_pid_record import decode_record, encode_record
raw = sys.argv[1].encode()
record = decode_record(raw)
assert record is not None, raw
assert encode_record(record) == raw
"#;
    let output = Command::new("uv")
        .args(["run", "python", "-c", script])
        .arg(String::from_utf8(encoded)?)
        .env("GOBBY_TEST_PROTECT", "1")
        .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

#[test]
fn exclusive_roles_preserve_winner_and_release_is_idempotent() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let mut winner = claim_pid_file(&path, Role::Maintenance)?.expect("maintenance claim");
    assert!(!path.exists());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Maintenance);
    let before = record(&path)?;
    assert!(claim_pid_file(&path, Role::Daemon)?.is_none());
    assert_eq!(record(&path)?, before);
    assert_eq!(cancel_service_reservation(&path)?, ProbeState::Maintenance);
    winner.release();
    winner.release();
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    let daemon = claim_pid_file(&path, Role::Daemon)?.expect("daemon after maintenance");
    assert_eq!(daemon.generation.to_string(), "2");
    assert_eq!(fs::read_to_string(&path)?, std::process::id().to_string());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Daemon);
    drop(daemon);
    assert!(path.exists());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    Ok(())
}

#[test]
fn cross_process_flock_and_descriptor_non_inheritance() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let mut claim = claim_pid_file(&path, Role::Daemon)?.expect("claim");
    let fd = claim.fileno().expect("descriptor");
    // SAFETY: claim keeps the descriptor open; F_GETFD has no third argument.
    assert_ne!(
        unsafe { libc::fcntl(fd, libc::F_GETFD) } & libc::FD_CLOEXEC,
        0
    );
    let script = r#"import errno, os, sys
from pathlib import Path
from gobby.runner_pid_file import claim_pid_file, probe_daemon_lock, ProbeState
from gobby.runner_pid_record import current_boot_id
path, fd, boot = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
try:
    os.fstat(fd)
except OSError as e:
    assert e.errno == errno.EBADF
else:
    raise AssertionError('claim descriptor inherited')
assert current_boot_id() == boot
assert claim_pid_file(path) is None
assert probe_daemon_lock(path).state is ProbeState.DAEMON
print('ready', flush=True)
sys.stdin.readline()
"#;
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    // Exec the interpreter directly: no intermediate launcher can close FDs and
    // accidentally make the inheritance regression pass.
    let mut child = Command::new(root.join(".venv/bin/python"))
        .args(["-c", script])
        .arg(&path)
        .arg(fd.to_string())
        .arg(current_boot_id())
        .env(
            "DATABASE_URL",
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
        )
        .env("GOBBY_TEST_PROTECT", "1")
        .current_dir(&root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut ready = String::new();
    BufReader::new(child.stdout.take().expect("stdout pipe")).read_line(&mut ready)?;
    assert_eq!(ready.trim(), "ready");
    claim.release();
    // A live child must not prolong the parent's flock after explicit release.
    assert!(child.try_wait()?.is_none());
    let successor = claim_pid_file(&path, Role::Maintenance)?.expect("child has no inherited lock");
    drop(successor);
    fs::write(&path, child.id().to_string())?;
    assert!(claim_pid_file(&path, Role::Daemon)?.is_none());
    assert!(reserve_service_start(&path, "launchd").is_err());
    child.stdin.take();
    let output = child.wait_with_output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

#[test]
fn nonce_conversion_is_one_shot_and_preserves_mismatches() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let reserved = reserve_service_start(&path, "launchd")?;
    let mut exited_issuer = record(&path)?;
    exited_issuer["pid"] = json!(99999999);
    fs::write(
        path.with_extension("pid.lock"),
        encode_record(&exited_issuer)?,
    )?;
    assert_eq!(reserved.nonce.len(), 32);
    assert!(reserved.nonce.bytes().all(|b| b.is_ascii_hexdigit()));
    assert_eq!(
        fs::metadata(&reserved.nonce_path)?.permissions().mode() & 0o7777,
        0o600
    );
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::LiveReservation);
    let before = record(&path)?;
    assert!(reserve_service_start(&path, "systemd").is_err());
    assert!(claim_pid_file(&path, Role::Daemon)?.is_none());
    assert!(convert_or_acquire_service_claim(&path, None).is_err());
    assert!(convert_or_acquire_service_claim(&path, Some(&path)).is_err());
    assert_eq!(record(&path)?, before);
    fs::write(&reserved.nonce_path, "unrelated")?;
    assert!(convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path)).is_err());
    assert_eq!(fs::read_to_string(&reserved.nonce_path)?, "unrelated");
    fs::write(&reserved.nonce_path, &reserved.nonce)?;
    fs::set_permissions(&reserved.nonce_path, fs::Permissions::from_mode(0o644))?;
    assert!(convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path)).is_err());
    fs::set_permissions(&reserved.nonce_path, fs::Permissions::from_mode(0o600))?;
    let claim = convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path))?;
    assert_eq!(claim.generation.to_string(), "2");
    assert!(!reserved.nonce_path.exists());
    assert_eq!(
        record(&path)?["ack"],
        json!({"status":"converted","pid":std::process::id()})
    );
    drop(claim);
    fs::write(&reserved.nonce_path, &reserved.nonce)?;
    assert!(convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path)).is_err());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    Ok(())
}

#[test]
fn cancel_and_stale_cleanup_touch_only_matching_nonce() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let reserved = reserve_service_start(&path, "systemd")?;
    assert_eq!(cancel_service_reservation(&path)?, ProbeState::Absent);
    assert!(!reserved.nonce_path.exists());
    assert_eq!(fs::read(path.with_extension("pid.lock"))?, b"");
    for previous_boot in [false, true] {
        let reserved = reserve_service_start(&path, "systemd")?;
        let mut stale = record(&path)?;
        if previous_boot {
            stale["reservation"]["boot_id"] = json!("previous boot");
        } else {
            stale["reservation"]["issued_at"] = json!(0.0);
        }
        fs::write(path.with_extension("pid.lock"), encode_record(&stale)?)?;
        assert_eq!(probe_daemon_lock(&path)?, ProbeState::StaleReservation);
        let claim = claim_pid_file(&path, Role::Daemon)?.expect("stale admission");
        assert!(!reserved.nonce_path.exists());
        assert!(record(&path)?["reservation"].is_null());
        drop(claim);
    }
    let reserved = reserve_service_start(&path, "systemd")?;
    fs::write(&reserved.nonce_path, "other owner")?;
    let mut stale = record(&path)?;
    stale["reservation"]["boot_id"] = json!("previous boot");
    fs::write(path.with_extension("pid.lock"), encode_record(&stale)?)?;
    let claim = claim_pid_file(&path, Role::Maintenance)?.expect("stale foreign nonce admission");
    assert_eq!(fs::read_to_string(&reserved.nonce_path)?, "other owner");
    drop(claim);
    cancel_service_reservation(&path)?;
    assert_eq!(fs::read_to_string(&reserved.nonce_path)?, "other owner");
    assert!(reserve_service_start(&path, "systemd").is_err());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    assert_eq!(fs::read_to_string(&reserved.nonce_path)?, "other owner");
    let claim = claim_pid_file(&path, Role::Maintenance)?.expect("failure released flock");
    drop(claim);
    Ok(())
}

#[test]
fn held_claim_converts_without_an_exclusion_gap() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let mut claim = claim_pid_file(&path, Role::Daemon)?.expect("claim");
    let reserved = claim.into_service_reservation("launchd")?;
    assert!(claim.fileno().is_none());
    assert_eq!(record(&path)?["generation"], 2);
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::LiveReservation);
    assert!(claim_pid_file(&path, Role::Maintenance)?.is_none());
    let converted = convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path))?;
    assert_eq!(converted.generation.to_string(), "3");
    Ok(())
}

#[test]
fn failed_pid_publication_releases_only_its_descriptor() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    fs::create_dir(&path)?;
    assert!(claim_pid_file(&path, Role::Daemon).is_err());
    assert!(path.is_dir());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    let mut claim =
        claim_pid_file(&path, Role::Maintenance)?.expect("failed writer released flock");
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Maintenance);
    let nonce_path = dir.path().join("gobby.pid.service-nonce");
    fs::write(&nonce_path, "foreign nonce")?;
    assert!(claim.into_service_reservation("launchd").is_err());
    assert!(claim.fileno().is_some());
    assert!(claim_pid_file(&path, Role::Maintenance)?.is_none());
    assert_eq!(fs::read_to_string(&nonce_path)?, "foreign nonce");
    claim.release();
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Transitioning);
    let claim = claim_pid_file(&path, Role::Maintenance)?.expect("retry recovers transition");
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Maintenance);
    drop(claim);
    fs::remove_file(&nonce_path)?;
    let reserved = reserve_service_start(&path, "launchd")?;
    assert!(convert_or_acquire_service_claim(&path, Some(&reserved.nonce_path)).is_err());
    assert_eq!(probe_daemon_lock(&path)?, ProbeState::Absent);
    assert!(!reserved.nonce_path.exists());
    Ok(())
}

#[test]
fn canonical_generation_contract() -> anyhow::Result<()> {
    for (previous, expected) in [
        (json!("١"), 1),
        (json!("+1"), 1),
        (json!("1_0"), 1),
        (json!(" 1"), 1),
        (json!(""), 1),
        (json!("01"), 1),
        (json!("-0"), 1),
        (json!("-1"), 1),
        (json!(3.0), 1),
        (json!(true), 1),
        (json!(false), 1),
        (json!("1__2"), 1),
        (json!("3_"), 1),
        (json!("+_1"), 1),
        (json!(-2), 1),
        (json!([3]), 1),
        (json!(null), 1),
        (json!(0), 1),
        (json!("0"), 1),
        (json!(3), 4),
        (json!("10"), 11),
    ] {
        let dir = TempDir::new()?;
        let path = dir.path().join("gobby.pid");
        let mut previous_record = parse_record(DAEMON)?;
        previous_record["generation"] = previous;
        fs::write(
            path.with_extension("pid.lock"),
            encode_record(&previous_record)?,
        )?;
        let claim = claim_pid_file(&path, Role::Maintenance)?.expect("claim");
        assert_eq!(claim.generation.to_string(), expected.to_string());
        assert_eq!(record(&path)?["generation"], expected);
    }
    Ok(())
}

#[test]
fn unbounded_generations_interoperate_with_python() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let prepare = r#"import json, sys
from pathlib import Path
from gobby.runner_pid_record import encode_record, decode_record, next_generation
from gobby.runner_pid_file import reserve_service_start
generations = [2**63-1, 2**63, 2**64-1, 2**64, -(2**63)-1,
               10**100, -(10**100), str(10**100), str(-(10**100)), 1e100, -1e100,
               '١', '+1', '1_0', ' 1', '', '01', '-0', True, False, 0, '0', 3.0, None, [1]]
cases = []
for mode in ('claim', 'reserve', 'convert', 'held'):
    for i, generation in enumerate(generations):
        p = Path(sys.argv[1]) / f'{mode}-{i}.pid'
        lock = p.with_name(p.name + '.lock')
        if mode == 'convert':
            reserve_service_start(p, backend='launchd')
            record = decode_record(lock.read_bytes())
            assert record is not None
        else:
            record = {'version': 1, 'state': 'role', 'role': 'maintenance',
                      'pid': 99999999, 'boot_id': 'previous', 'reservation': None, 'ack': None}
        record['generation'] = generation
        record['extra'] = {'nested': [10**100, -(10**100)]}
        lock.write_bytes(encode_record(record))
        cases.append({'path': str(p), 'mode': mode,
                      'expected': str(next_generation(record) + (1 if mode == 'held' else 0))})
print(json.dumps(cases))
"#;
    let output = Command::new("uv")
        .args(["run", "python", "-c", prepare])
        .arg(dir.path())
        .current_dir(&root)
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let cases: Vec<Value> = serde_json::from_slice(&output.stdout)?;
    for case in &cases {
        let path = Path::new(case["path"].as_str().expect("path"));
        // Decoding must preserve Python's checksum before any acquisition.
        let raw = fs::read(path.with_extension("pid.lock"))?;
        let prior = decode_record(&raw).expect("Python record");
        assert_eq!(encode_record(&prior)?, raw);
        match case["mode"].as_str().expect("mode") {
            "claim" => {
                drop(claim_pid_file(path, Role::Maintenance)?.expect("claim"));
            }
            "reserve" => {
                reserve_service_start(path, "launchd")?;
            }
            "convert" => {
                let payload: Value = serde_json::from_str(prior["reservation"].get())?;
                let nonce = Path::new(payload["nonce_path"].as_str().expect("nonce path"));
                drop(convert_or_acquire_service_claim(path, Some(nonce))?);
                assert!(!nonce.exists());
            }
            "held" => {
                let mut claim = claim_pid_file(path, Role::Maintenance)?.expect("claim");
                claim.into_service_reservation("launchd")?;
                assert!(claim.fileno().is_none());
            }
            _ => panic!("unexpected mode"),
        }
        let result =
            decode_record(&fs::read(path.with_extension("pid.lock"))?).expect("Rust record");
        assert_eq!(
            result["generation"].get(),
            case["expected"].as_str().expect("expected"),
            "{case}"
        );
    }
    let verify = r#"import json, sys
from pathlib import Path
from gobby.runner_pid_record import decode_record, encode_record
for case in json.loads(sys.argv[1]):
    p = Path(case['path'])
    raw = p.with_name(p.name + '.lock').read_bytes()
    record = decode_record(raw)
    assert record is not None, case
    assert record['generation'] == int(case['expected']), (case, record)
    assert encode_record(record) == raw, case
"#;
    let output = Command::new("uv")
        .args(["run", "python", "-c", verify])
        .arg(serde_json::to_string(&cases)?)
        .current_dir(root)
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

#[test]
fn cleanup_preserves_matching_nonce_with_foreign_permissions() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("gobby.pid");
    let reserved = reserve_service_start(&path, "systemd")?;
    fs::set_permissions(&reserved.nonce_path, fs::Permissions::from_mode(0o644))?;
    assert_eq!(cancel_service_reservation(&path)?, ProbeState::Absent);
    assert_eq!(fs::read_to_string(&reserved.nonce_path)?, reserved.nonce);
    let mut stale = parse_record(RESERVATION)?;
    stale["reservation"]["nonce_path"] = json!(reserved.nonce_path);
    stale["reservation"]["nonce"] = json!(reserved.nonce);
    fs::write(path.with_extension("pid.lock"), encode_record(&stale)?)?;
    let claim = claim_pid_file(&path, Role::Maintenance)?.expect("stale admission");
    assert!(reserved.nonce_path.exists());
    drop(claim);
    Ok(())
}

#[test]
fn reservation_freshness_matches_python_coercions() {
    let base = json!({"boot_id":"boot","issued_at":" 100.25 ","age_bound_seconds":" 30 "});
    assert!(reservation_is_live(Some(&base), 130.0, "boot"));
    assert!(!reservation_is_live(Some(&base), 130.25, "boot"));
    assert!(!reservation_is_live(Some(&base), 100.0, "different"));
    assert!(reservation_is_live(Some(&base), 90.0, "boot"));
    let bad = json!({"boot_id":"boot","issued_at":[],"age_bound_seconds":30});
    assert!(!reservation_is_live(Some(&bad), 0.0, "boot"));
    let boolean = json!({"boot_id":"boot","issued_at":true,"age_bound_seconds":true});
    assert!(reservation_is_live(Some(&boolean), 1.5, "boot"));
    assert!(!reservation_is_live(Some(&boolean), 2.0, "boot"));
}

#[test]
fn rust_records_are_parsed_by_python() -> anyhow::Result<()> {
    let dir = TempDir::new()?;
    let path = dir.path().join("雪-😀.pid");
    let reserved = reserve_service_start(&path, "launchd")?;
    let script = r#"import sys
from pathlib import Path
from gobby.runner_pid_record import decode_record, encode_record
from gobby.runner_pid_file import probe_daemon_lock, ProbeState
p = Path(sys.argv[1]); raw = p.with_name(p.name + '.lock').read_bytes()
record = decode_record(raw)
assert record is not None and record['state'] == 'reservation'
assert encode_record(record) == raw
assert probe_daemon_lock(p).state is ProbeState.LIVE_RESERVATION
"#;
    let output = Command::new("uv")
        .args(["run", "python", "-c", script])
        .arg(&path)
        .env(
            "DATABASE_URL",
            "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
        )
        .env("GOBBY_TEST_PROTECT", "1")
        .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .stdin(Stdio::null())
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    cancel_service_reservation(&path)?;
    assert!(!reserved.nonce_path.exists());
    Ok(())
}
