use super::*;

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::net::TcpListener;
use std::path::Path;
use std::thread;

use gobby_core::process_identity::current_cli_pid;
use gobby_core::session_credential::{self, SESSION_CREDENTIAL_FIELD, cli_pid_key, provider_key};
use serde_json::{Value, json};
use tempfile::tempdir;

use crate::test_http::read_http_request;
use crate::transport::{DeliveryOutcome, enqueue_to, post_and_cleanup};

const MACHINE_KEY: &str = "machine-key-for-tests";
const TOKEN: &str = "gobby-agent-v1.payload.signature";
const SESSION_UUID: &str = "11111111-1111-4111-8111-111111111111";

fn credential(provider_session_id: &str) -> SessionCredential {
    SessionCredential {
        token: TOKEN.into(),
        session_id: SESSION_UUID.into(),
        project_id: "22222222-2222-4222-8222-222222222222".into(),
        provider_session_id: provider_session_id.into(),
    }
}

fn envelope(provider_session_id: &str, headers: &[(&str, &str)]) -> Envelope {
    let headers: BTreeMap<String, String> = headers
        .iter()
        .map(|(name, value)| (name.to_string(), value.to_string()))
        .collect();
    Envelope::new(
        false,
        "PreToolUse".into(),
        json!({"session_id": provider_session_id}),
        "codex".into(),
        headers,
    )
}

/// Run `body` against an isolated home with no managed-run markers and no tmux.
fn isolated<R>(home: &Path, body: impl FnOnce() -> R) -> R {
    isolated_with(home, &[], body)
}

fn isolated_with<R>(home: &Path, extra: &[(&str, Option<&str>)], body: impl FnOnce() -> R) -> R {
    temp_env::with_vars(
        [
            ("GOBBY_HOME", Some(home.as_os_str())),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            (gobby_core::grant::MANAGED_BOOTSTRAP_ENV, None),
            (gobby_core::local_token::AGENT_API_TOKEN_ENV, None),
            ("GOBBY_SESSION_ID", None),
            ("TMUX", None),
            ("TMUX_PANE", None),
        ],
        || {
            let owned: Vec<(&str, Option<&str>)> = extra.to_vec();
            temp_env::with_vars(owned, body)
        },
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

/// Serve one request, hand its raw text to `check`, and reply 200.
fn serve_once(check: impl FnOnce(&str) + Send + 'static) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let request = read_http_request(&mut stream);
        check(&request);
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            .unwrap();
    });
    (format!("http://{addr}"), handle)
}

/// The values of one request header, matched case-insensitively.
fn header_values(request: &str, name: &str) -> Vec<String> {
    let head = request.split("\r\n\r\n").next().unwrap_or_default();
    head.split("\r\n")
        .filter_map(|line| line.split_once(':'))
        .filter(|(key, _)| key.trim().eq_ignore_ascii_case(name))
        .map(|(_, value)| value.trim().to_string())
        .collect()
}

#[test]
fn bound_request_presents_its_session_token_and_no_machine_key() {
    let home = machine_key_home();
    session_credential::store(home.path(), &[provider_key("sid-1")], &credential("sid-1")).unwrap();

    isolated(home.path(), || {
        let envelope = envelope(
            "sid-1",
            &[
                ("X-Gobby-Session-Id", "sid-1"),
                ("X-Gobby-Project-Id", "project-1"),
            ],
        );
        let inbox = home.path().join("inbox");
        let path = enqueue_to(&envelope, &inbox).unwrap();
        let (url, server) = serve_once(|request| {
            assert_eq!(
                header_values(request, "Authorization"),
                vec![format!("Bearer {TOKEN}")]
            );
            assert_eq!(
                header_values(request, "X-Gobby-Session-Id"),
                vec![SESSION_UUID]
            );
            assert_eq!(
                header_values(request, "X-Gobby-Project-Id"),
                vec!["project-1"]
            );
            assert!(
                !request.contains(MACHINE_KEY),
                "machine key must not be sent"
            );
        });

        let report = post_and_cleanup(&envelope, &path, &url);
        server.join().unwrap();
        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
    });
}

#[test]
fn unbound_request_sends_machine_key_and_no_session_selector() {
    let home = machine_key_home();

    isolated(home.path(), || {
        let envelope = envelope(
            "sid-1",
            &[
                ("X-Gobby-Session-Id", "sid-1"),
                ("X-Gobby-Terminal-Context", r#"{"parent_pid": 4242}"#),
                ("X-Gobby-Project-Id", "project-1"),
            ],
        );
        let inbox = home.path().join("inbox");
        let path = enqueue_to(&envelope, &inbox).unwrap();
        let (url, server) = serve_once(|request| {
            assert_eq!(
                header_values(request, "Authorization"),
                vec![format!("Bearer {MACHINE_KEY}")]
            );
            assert!(header_values(request, "X-Gobby-Session-Id").is_empty());
            assert!(header_values(request, "X-Gobby-Terminal-Context").is_empty());
            assert_eq!(
                header_values(request, "X-Gobby-Project-Id"),
                vec!["project-1"]
            );
        });

        let report = post_and_cleanup(&envelope, &path, &url);
        server.join().unwrap();
        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
    });
}

#[test]
fn a_credential_for_another_provider_session_is_not_used() {
    let home = machine_key_home();
    session_credential::store(
        home.path(),
        &[provider_key("sid-1")],
        &credential("sid-other"),
    )
    .unwrap();

    isolated(home.path(), || {
        assert!(matches!(
            caller_for(&envelope("sid-1", &[])),
            Caller::Unbound
        ));
    });
}

#[test]
fn a_managed_run_keeps_its_ordinary_identity() {
    let home = machine_key_home();
    session_credential::store(home.path(), &[provider_key("sid-1")], &credential("sid-1")).unwrap();

    isolated_with(
        home.path(),
        &[("GOBBY_AGENT_RUN_ID", Some("run-1"))],
        || {
            let caller = caller_for(&envelope("sid-1", &[]));
            assert!(matches!(caller, Caller::Managed));
            assert!(!caller.is_unspawned());
        },
    );
}

fn session_start_envelope(provider_session_id: &str, source: &str) -> Envelope {
    Envelope::new(
        true,
        "session-start".into(),
        json!({"session_id": provider_session_id, "source": source}),
        "claude".into(),
        BTreeMap::new(),
    )
}

fn record_count(home: &Path) -> usize {
    fs::read_dir(home.join("session-credentials"))
        .map(|entries| entries.count())
        .unwrap_or(0)
}

#[test]
fn response_credential_is_stored_for_the_provider_and_cli_and_stripped() {
    let home = machine_key_home();
    let body = json!({
        "continue": true,
        SESSION_CREDENTIAL_FIELD: {
            "token": TOKEN,
            "session_id": SESSION_UUID,
            "project_id": "22222222-2222-4222-8222-222222222222"
        }
    })
    .to_string();

    isolated_with(
        home.path(),
        &[
            ("TMUX", Some("/tmp/tmux-501/default,4242,0")),
            ("TMUX_PANE", Some("%4")),
        ],
        || {
            let stripped =
                remember_response_credential(&envelope("sid-1", &[]), &body).into_owned();

            let rest: Value = serde_json::from_str(&stripped).unwrap();
            assert_eq!(rest, json!({"continue": true}));
            let by_provider = session_credential::bound_for_provider(home.path(), "sid-1")
                .expect("record stored under the provider key");
            assert_eq!(by_provider.token, TOKEN);
            assert_eq!(by_provider.provider_session_id, "sid-1");
            let cli = current_cli_pid().expect("the parent CLI process is readable");
            let by_cli = session_credential::load(home.path(), &cli_pid_key(cli))
                .expect("record stored under the CLI key");
            assert_eq!(by_cli.token, TOKEN);
            assert_eq!(
                record_count(home.path()),
                2,
                "the provider and CLI keys only; a tmux pane is not a key"
            );
        },
    );
}

#[test]
fn a_bound_session_start_restores_the_record_under_the_current_cli() {
    let home = machine_key_home();
    session_credential::store(home.path(), &[provider_key("sid-1")], &credential("sid-1")).unwrap();

    isolated(home.path(), || {
        let envelope = session_start_envelope("sid-1", "resume");
        let path = enqueue_to(&envelope, &home.path().join("inbox")).unwrap();
        let (url, server) = serve_once(|request| {
            assert_eq!(
                header_values(request, "Authorization"),
                vec![format!("Bearer {TOKEN}")]
            );
        });

        let report = post_and_cleanup(&envelope, &path, &url);
        server.join().unwrap();

        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        let cli = current_cli_pid().expect("the parent CLI process is readable");
        let restored = session_credential::load(home.path(), &cli_pid_key(cli))
            .expect("the bound credential is restored under the CLI key");
        assert_eq!(restored.token, TOKEN);
        assert_eq!(restored.session_id, SESSION_UUID);
    });
}

#[test]
fn a_clear_session_start_is_minted_by_the_machine_path() {
    let home = machine_key_home();
    session_credential::store(home.path(), &[provider_key("sid-1")], &credential("sid-1")).unwrap();

    isolated(home.path(), || {
        for hook_type in ["session-start", "SessionStart"] {
            let mut clear = session_start_envelope("sid-1", "clear");
            clear.hook_type = hook_type.into();
            assert!(
                matches!(caller_for(&clear), Caller::Unbound),
                "{hook_type} with source clear mints a successor"
            );
            let mut resume = session_start_envelope("sid-1", "resume");
            resume.hook_type = hook_type.into();
            assert!(
                matches!(caller_for(&resume), Caller::Bound(_)),
                "{hook_type} with source resume keeps its bound credential"
            );
        }
    });
}

#[test]
fn delivered_report_never_holds_the_session_credential() {
    let home = machine_key_home();
    let response_body = json!({
        "continue": true,
        SESSION_CREDENTIAL_FIELD: {
            "token": TOKEN,
            "session_id": SESSION_UUID,
            "project_id": "22222222-2222-4222-8222-222222222222"
        }
    })
    .to_string();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let _ = read_http_request(&mut stream);
        let reply = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{response_body}",
            response_body.len()
        );
        stream.write_all(reply.as_bytes()).unwrap();
    });

    isolated(home.path(), || {
        let envelope = envelope("sid-1", &[]);
        let path = enqueue_to(&envelope, &home.path().join("inbox")).unwrap();
        let report = post_and_cleanup(&envelope, &path, &url);
        server.join().unwrap();

        assert_eq!(report.outcome, DeliveryOutcome::Delivered);
        let body = report.response_body.expect("delivered body");
        assert!(
            !body.contains(TOKEN),
            "the report must never hold the token"
        );
        assert!(!body.contains(SESSION_CREDENTIAL_FIELD));
        assert_eq!(
            serde_json::from_str::<Value>(&body).unwrap(),
            json!({"continue": true})
        );
        assert!(
            session_credential::bound_for_provider(home.path(), "sid-1").is_some(),
            "the credential is still recorded for the session"
        );
    });
}
