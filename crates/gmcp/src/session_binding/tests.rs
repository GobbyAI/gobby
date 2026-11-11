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

const THREAD: &str = "01a1b24d-0000-7000-8000-000000025904";

/// Serve one request with `status` and `body`; return the request's head and body.
fn serve_once(status: &'static str, body: &'static str) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = read_head(&mut stream);
        let length: usize = header_values(&request, "Content-Length")
            .first()
            .map_or(0, |value| value.parse().unwrap());
        let already = request.split("\r\n\r\n").nth(1).map_or(0, str::len);
        let mut rest = vec![0u8; length.saturating_sub(already)];
        stream.read_exact(&mut rest).unwrap();
        request.push_str(&String::from_utf8_lossy(&rest));
        let reply = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(reply.as_bytes()).unwrap();
        request
    });
    (url, server)
}

#[test]
fn a_tool_calls_meta_names_its_codex_thread() {
    let direct = json!({"threadId": THREAD, "sessionId": "other"});
    let turn = json!({"x-codex-turn-metadata": {"thread_id": THREAD}});
    let empty = json!({"threadId": ""});

    assert_eq!(
        codex_thread_id(direct.as_object().unwrap()).as_deref(),
        Some(THREAD)
    );
    assert_eq!(
        codex_thread_id(turn.as_object().unwrap()).as_deref(),
        Some(THREAD)
    );
    assert_eq!(codex_thread_id(empty.as_object().unwrap()), None);
    assert_eq!(codex_thread_id(&Map::new()), None);
}

#[test]
fn the_exchange_presents_the_thread_and_returns_its_own_credential() {
    let (url, server) = serve_once(
        "200 OK",
        r#"{"success":true,"token":"gobby-agent-v1.thread.sig","session_id":"33333333-3333-4333-8333-333333333333","project_id":"22222222-2222-4222-8222-222222222222"}"#,
    );
    let agent = ureq::AgentBuilder::new().build();

    let credential =
        crate::daemon::exchange_thread_credential(&agent, &url, MACHINE_KEY, THREAD).unwrap();

    let request = server.join().unwrap();
    assert!(request.starts_with("POST /api/hooks/codex-thread-credential "));
    assert_eq!(
        header_values(&request, "Authorization"),
        vec![format!("Bearer {MACHINE_KEY}")]
    );
    assert!(request.contains(&format!(r#""thread_id":"{THREAD}""#)));
    assert_eq!(credential.token, "gobby-agent-v1.thread.sig");
    assert_eq!(
        credential.session_id,
        "33333333-3333-4333-8333-333333333333"
    );
    assert_eq!(credential.provider_session_id, THREAD);
}

#[test]
fn a_refused_exchange_binds_nothing() {
    let (url, server) = serve_once(
        "403 Forbidden",
        r#"{"success":false,"error_code":"session_credential_unbound"}"#,
    );
    let agent = ureq::AgentBuilder::new().build();

    assert!(crate::daemon::exchange_thread_credential(&agent, &url, MACHINE_KEY, THREAD).is_none());
    server.join().unwrap();
}

#[test]
fn a_call_thread_is_scoped_to_its_call() {
    use crate::daemon::{call_thread, for_thread};

    assert_eq!(call_thread(), None);
    for_thread(Some(THREAD.into()), || {
        assert_eq!(call_thread().as_deref(), Some(THREAD));
        // Another OS thread serves another call and never sees this one's thread.
        let elsewhere = std::thread::spawn(call_thread).join().unwrap();
        assert_eq!(elsewhere, None);
        for_thread(Some("inner".into()), || {
            assert_eq!(call_thread().as_deref(), Some("inner"));
        });
        assert_eq!(call_thread().as_deref(), Some(THREAD));
    });
    let after_panic = std::panic::catch_unwind(|| for_thread(Some("p".into()), || panic!()));
    assert!(after_panic.is_err());
    assert_eq!(call_thread(), None);
}

#[test]
fn a_client_outside_the_shared_worker_never_takes_the_thread_path() {
    // The test's parent is not Codex's worker: a call naming a thread is still refused
    // locally, with no exchange and no machine key sent.
    let home = machine_key_home();
    let cwd = tempdir().unwrap();

    in_surroundings(
        home.path(),
        Surroundings {
            daemon_url: Some("http://127.0.0.1:9"),
            ..Surroundings::default()
        },
        || {
            assert!(!under_shared_host());
            let dialer = Dialer::with_context(cwd.path(), Map::new()).unwrap();
            let answer = crate::daemon::for_thread(Some(THREAD.into()), || {
                dialer.request("GET", "/api/probe", None, RequestOptions::default())
            });
            assert_eq!(answer["error_code"], "session_credential_unbound");
        },
    );
}

#[test]
fn a_managed_run_is_never_under_the_shared_worker() {
    let home = machine_key_home();
    in_surroundings(
        home.path(),
        Surroundings {
            run_id: Some("run-1"),
            ..Surroundings::default()
        },
        || assert!(!under_shared_host()),
    );
}
