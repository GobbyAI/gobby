//! Grant/daemon state reported by `gwiki status` without acquiring a grant.

use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use gobby_core::grant::{
    GrantBundle, TrustedBinding, interactive_cache_path, write_binding, write_grant_file,
};
use serde_json::Value;

const PROJECT_ID: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const DEPLOYMENT_TOKEN: &str = "cafebabedeadbeef";

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_secs() as i64
}

fn golden_grant() -> GrantBundle {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/runtime_grants/golden/direct_datastores.json");
    let raw = std::fs::read(&path).expect("read golden grant");
    serde_json::from_slice(&raw).expect("parse golden grant")
}

fn write_project(root: &Path) {
    let gobby = root.join(".gobby");
    std::fs::create_dir_all(&gobby).expect("create .gobby");
    std::fs::write(
        gobby.join("project.json"),
        format!(r#"{{"id":"{PROJECT_ID}","name":"gwiki-status-grant"}}"#),
    )
    .expect("write project.json");
}

fn write_cached_grant(home: &Path, daemon_url: &str, expires_at: i64) -> GrantBundle {
    let now = now_unix();
    let mut grant = golden_grant();
    grant.principal.project_id = PROJECT_ID.to_string();
    grant.principal.kind = gobby_core::grant::PrincipalKind::Interactive;
    grant.deployment.token = DEPLOYMENT_TOKEN.to_string();
    grant.issued_at = now - 60;
    grant.expires_at = expires_at;
    grant = grant.with_checksum();
    write_binding(
        home,
        &TrustedBinding {
            endpoint: daemon_url.to_string(),
            deployment_token: DEPLOYMENT_TOKEN.to_string(),
        },
    )
    .expect("write binding");
    let path = interactive_cache_path(home, DEPLOYMENT_TOKEN, PROJECT_ID);
    write_grant_file(&path, &grant).expect("write grant cache");
    grant
}

fn run_gwiki(home: &Path, cwd: &Path, daemon_url: &str, args: &[&str]) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_gwiki"));
    cmd.args(args)
        .current_dir(cwd)
        .env("HOME", home)
        .env("GOBBY_HOME", home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd.output().expect("run gwiki")
}

fn json_stdout(output: &Output) -> Value {
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim()).unwrap_or_else(|error| {
        panic!(
            "expected JSON stdout: {error}\nstdout:\n{stdout}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn unused_loopback() -> (String, TcpListener) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind unused port");
    let port = listener.local_addr().expect("addr").port();
    (format!("http://127.0.0.1:{port}"), listener)
}

#[test]
fn status_reports_grant_state() {
    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_project(project.path());
    let (daemon_url, listener) = unused_loopback();
    let grant = write_cached_grant(home.path(), &daemon_url, now_unix() + 3_600);

    let output = run_gwiki(
        home.path(),
        project.path(),
        &daemon_url,
        &["--format", "json", "status", "--topic", "rust"],
    );
    drop(listener);
    assert!(
        output.status.success(),
        "status must succeed with a valid cached grant\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let payload = json_stdout(&output);
    assert_ne!(payload["status"].as_str(), Some("shell-ready"));
    assert_ne!(payload["runtime"].as_str(), Some("memory"));
    assert_eq!(payload["grant"]["state"].as_str(), Some("valid"));
    assert_eq!(
        payload["grant"]["deployment_token"].as_str(),
        Some(DEPLOYMENT_TOKEN)
    );
    assert_eq!(
        payload["grant"]["epoch"].as_i64(),
        Some(grant.deployment.fencing_epoch)
    );
    assert_eq!(payload["daemon"]["reachable"].as_bool(), Some(true));
    assert_eq!(payload["daemon_url"].as_str(), Some(daemon_url.as_str()));
}

#[test]
fn expired_grant_reports_not_fails() {
    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_project(project.path());
    let daemon_url = {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        drop(listener);
        format!("http://127.0.0.1:{port}")
    };
    let grant = write_cached_grant(home.path(), &daemon_url, now_unix() - 30);

    let status = run_gwiki(
        home.path(),
        project.path(),
        &daemon_url,
        &["--format", "json", "status", "--topic", "rust"],
    );
    assert!(
        status.status.success(),
        "status must report an expired grant without failing\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&status.stdout),
        String::from_utf8_lossy(&status.stderr)
    );
    let payload = json_stdout(&status);
    assert_eq!(payload["grant"]["state"].as_str(), Some("expired"));
    assert_eq!(
        payload["grant"]["deployment_token"].as_str(),
        Some(DEPLOYMENT_TOKEN)
    );
    assert_eq!(
        payload["grant"]["epoch"].as_i64(),
        Some(grant.deployment.fencing_epoch)
    );
    assert_eq!(payload["daemon"]["reachable"].as_bool(), Some(false));
    assert!(!String::from_utf8_lossy(&status.stdout).contains("shell-ready"));

    let help = run_gwiki(home.path(), project.path(), &daemon_url, &["--help"]);
    assert!(
        help.status.success(),
        "help must work with an expired grant\nstderr:\n{}",
        String::from_utf8_lossy(&help.stderr)
    );

    let contract = run_gwiki(
        home.path(),
        project.path(),
        &daemon_url,
        &["contract", "--format", "json"],
    );
    assert!(
        contract.status.success(),
        "contract must work with an expired grant\nstderr:\n{}",
        String::from_utf8_lossy(&contract.stderr)
    );

    let absent_home = tempfile::tempdir().expect("absent home");
    let absent = run_gwiki(
        absent_home.path(),
        project.path(),
        &daemon_url,
        &["--format", "json", "status", "--topic", "rust"],
    );
    assert!(
        absent.status.success(),
        "status must report an absent grant without failing\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&absent.stdout),
        String::from_utf8_lossy(&absent.stderr)
    );
    let absent_payload = json_stdout(&absent);
    assert_eq!(absent_payload["grant"]["state"].as_str(), Some("absent"));
}
