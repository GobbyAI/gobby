use super::*;

use std::ffi::OsStr;
use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::thread;

use gobby_core::process_identity::current_cli_pid;
use gobby_core::session_credential::{self, cli_pid_key};
use serde_json::{Map, json};
use tempfile::tempdir;

use crate::daemon::{Dialer, RequestOptions};

const MACHINE_KEY: &str = "machine-key-for-tests";
const TOKEN: &str = "gobby-agent-v1.payload.signature";
const SESSION_UUID: &str = "11111111-1111-4111-8111-111111111111";
const TMUX: &str = "/tmp/tmux-501/default,4242,0";

fn credential() -> SessionCredential {
    SessionCredential {
        token: TOKEN.into(),
        session_id: SESSION_UUID.into(),
        project_id: "22222222-2222-4222-8222-222222222222".into(),
        provider_session_id: "provider-abc".into(),
    }
}

/// The environment one client sees: any managed-run marker, the tmux pane it runs in,
/// and the daemon it dials. Everything else that could select a session is cleared.
#[derive(Default)]
struct Surroundings<'a> {
    tmux: Option<&'a str>,
    pane: Option<&'a str>,
    run_id: Option<&'a str>,
    daemon_url: Option<&'a str>,
}

fn in_surroundings<R>(home: &Path, surroundings: Surroundings<'_>, body: impl FnOnce() -> R) -> R {
    temp_env::with_vars(
        [
            ("GOBBY_HOME", Some(home.as_os_str())),
            ("GOBBY_AGENT_RUN_ID", surroundings.run_id.map(OsStr::new)),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            (gobby_core::grant::MANAGED_BOOTSTRAP_ENV, None),
            (gobby_core::local_token::AGENT_API_TOKEN_ENV, None),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_DAEMON_URL", surroundings.daemon_url.map(OsStr::new)),
            ("GOBBY_PORT", None),
            ("GOBBY_DAEMON_PORT", None),
            ("TMUX", surroundings.tmux.map(OsStr::new)),
            ("TMUX_PANE", surroundings.pane.map(OsStr::new)),
        ],
        body,
    )
}

fn machine_key_home() -> tempfile::TempDir {
    let home = tempdir().unwrap();
    fs::write(
        home.path().join("bootstrap.yaml"),
        format!("api_key: {MACHINE_KEY}\n"),
    )
    .unwrap();
    home
}

/// Store the session credential under the CLI process this test runs in.
fn store_for_this_cli(home: &Path) {
    let cli = current_cli_pid().expect("the parent CLI process is readable");
    session_credential::store(home, &[cli_pid_key(cli)], &credential()).unwrap();
}

/// Read one request head (through the blank line) from a loopback connection.
fn read_head(stream: &mut impl Read) -> String {
    let mut bytes = Vec::new();
    let mut buf = [0u8; 1024];
    while !bytes.windows(4).any(|window| window == b"\r\n\r\n") {
        let read = stream.read(&mut buf).unwrap();
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..read]);
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

/// The values of one request header, matched case-insensitively.
fn header_values(head: &str, name: &str) -> Vec<String> {
    head.split("\r\n")
        .filter_map(|line| line.split_once(':'))
        .filter(|(key, _)| key.trim().eq_ignore_ascii_case(name))
        .map(|(_, value)| value.trim().to_string())
        .collect()
}

#[test]
fn an_unspawned_cli_reads_the_record_stored_for_its_process() {
    let home = machine_key_home();
    store_for_this_cli(home.path());

    in_surroundings(home.path(), Surroundings::default(), || {
        let binding = cli_binding().expect("record stored for this CLI");
        assert_eq!(binding.token, TOKEN);
        assert_eq!(binding.session_id, SESSION_UUID);
    });
}

#[test]
fn the_cli_binding_does_not_depend_on_tmux() {
    let home = machine_key_home();
    store_for_this_cli(home.path());

    in_surroundings(
        home.path(),
        Surroundings {
            tmux: Some(TMUX),
            pane: Some("%4"),
            ..Surroundings::default()
        },
        || assert!(cli_binding().is_some()),
    );
}

#[test]
fn a_record_for_another_cli_process_is_not_read() {
    let home = machine_key_home();
    session_credential::store(home.path(), &[cli_pid_key(1)], &credential()).unwrap();

    in_surroundings(home.path(), Surroundings::default(), || {
        assert!(cli_binding().is_none());
    });
}

#[test]
fn a_managed_run_has_no_cli_binding() {
    let home = machine_key_home();
    store_for_this_cli(home.path());

    in_surroundings(
        home.path(),
        Surroundings {
            run_id: Some("run-1"),
            ..Surroundings::default()
        },
        || assert!(cli_binding().is_none()),
    );
}

#[test]
fn an_unspawned_request_presents_its_cli_token_and_no_machine_key() {
    let home = machine_key_home();
    store_for_this_cli(home.path());
    let cwd = tempdir().unwrap();

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let head = read_head(&mut stream);
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            .unwrap();
        head
    });

    let url = format!("http://{addr}");
    in_surroundings(
        home.path(),
        Surroundings {
            daemon_url: Some(url.as_str()),
            ..Surroundings::default()
        },
        || {
            let dialer = Dialer::with_context(cwd.path(), Map::new()).unwrap();
            let answer = dialer.request("GET", "/api/probe", None, RequestOptions::default());
            assert_eq!(answer, json!({}));
        },
    );

    let head = server.join().unwrap();
    assert_eq!(
        header_values(&head, "Authorization"),
        vec![format!("Bearer {TOKEN}")]
    );
    assert_eq!(
        header_values(&head, "X-Gobby-Session-Id"),
        vec![SESSION_UUID]
    );
    assert!(header_values(&head, "X-Gobby-Terminal-Context").is_empty());
    assert!(!head.contains(MACHINE_KEY), "machine key must not be sent");
}

#[test]
fn an_unspawned_request_without_a_record_is_refused_before_any_connection() {
    // No bootstrap file: an unspawned client must never read the machine key.
    let home = tempdir().unwrap();
    let cwd = tempdir().unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());

    in_surroundings(
        home.path(),
        Surroundings {
            daemon_url: Some(url.as_str()),
            ..Surroundings::default()
        },
        || {
            let dialer = Dialer::with_context(cwd.path(), Map::new())
                .expect("an unspawned dialer reads no machine key");
            let answer = dialer.request("GET", "/api/probe", None, RequestOptions::default());

            assert_eq!(answer["success"], json!(false));
            assert_eq!(answer["error_code"], json!("session_credential_unbound"));
            assert!(
                answer["error"]
                    .as_str()
                    .is_some_and(|text| !text.is_empty())
            );
        },
    );

    assert!(
        matches!(listener.accept(), Err(error) if error.kind() == ErrorKind::WouldBlock),
        "the refusal makes no connection"
    );
}

#[test]
fn a_rejected_cli_token_is_never_retried_with_the_machine_key() {
    let home = machine_key_home();
    store_for_this_cli(home.path());
    let cwd = tempdir().unwrap();

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let served = listener.try_clone().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = served.accept().unwrap();
        let head = read_head(&mut stream);
        stream
            .write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}")
            .unwrap();
        head
    });

    in_surroundings(
        home.path(),
        Surroundings {
            daemon_url: Some(url.as_str()),
            ..Surroundings::default()
        },
        || {
            let dialer = Dialer::with_context(cwd.path(), Map::new()).unwrap();
            let answer = dialer.request("GET", "/api/probe", None, RequestOptions::default());
            assert_eq!(answer["success"], json!(false));
        },
    );

    let head = server.join().unwrap();
    assert_eq!(
        header_values(&head, "Authorization"),
        vec![format!("Bearer {TOKEN}")]
    );
    listener.set_nonblocking(true).unwrap();
    assert!(
        matches!(listener.accept(), Err(error) if error.kind() == ErrorKind::WouldBlock),
        "no second connection carries the machine key"
    );
}
