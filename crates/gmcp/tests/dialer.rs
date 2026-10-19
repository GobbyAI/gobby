mod stub_daemon;

use gobby_mcp::daemon::{Dialer, RequestOptions};
use serde_json::json;
use std::fs;
use stub_daemon::{Response, StubDaemon};

fn isolated(test: impl FnOnce(&std::path::Path) -> anyhow::Result<()>) -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    fs::write(home.path().join("bootstrap.yaml"), "api_key: first\n")?;
    temp_env::with_vars(
        [
            ("GOBBY_HOME", home.path().to_str()),
            ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            ("GOBBY_AGENT_API_TOKEN", None),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_PROJECT_ID", None),
        ],
        || test(home.path()),
    )
}

#[test]
fn request_headers_match_python_wrapper() -> anyhow::Result<()> {
    isolated(|root| {
        fs::create_dir(root.join(".gobby"))?;
        fs::write(root.join(".gobby/project.json"), r#"{"id":"caller"}"#)?;
        let server = StubDaemon::start(vec![Response::json(200, json!({"success": true}))])?;
        temp_env::with_vars(
            [
                ("GOBBY_DAEMON_URL", Some(server.url().as_str())),
                ("GOBBY_SESSION_ID", Some("seat")),
            ],
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(
                    root,
                    json!({"tmux_pane":"%3"}).as_object().unwrap().clone(),
                )?;
                let result = dialer.request(
                    "POST",
                    "/test",
                    Some(&json!({"value": 1})),
                    RequestOptions {
                        project_id: Some("target"),
                        session_id: Some("body-seat"),
                        ..Default::default()
                    },
                );
                assert_eq!(result, json!({"success": true}));
                let requests = server.requests();
                assert_eq!(requests.len(), 1);
                let request = &requests[0];
                assert_eq!(request.method, "POST");
                assert_eq!(request.path, "/test");
                assert_eq!(request.body, json!({"value": 1}));
                assert_eq!(request.headers["x-gobby-mcp-wrapper-protocol-version"], "1");
                assert_eq!(request.headers["x-gobby-project-id"], "target");
                assert_eq!(request.headers["x-gobby-caller-project-id"], "caller");
                assert_eq!(request.headers["x-gobby-session-id"], "seat");
                assert_eq!(request.headers["authorization"], "Bearer first");
                assert!(!request.headers.contains_key("x-gobby-terminal-context"));
                Ok(())
            },
        )
    })
}

#[test]
fn headless_terminal_and_agent_identity_headers() -> anyhow::Result<()> {
    isolated(|root| {
        let server = StubDaemon::start((0..4).map(|_| Response::json(200, json!({}))).collect())?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let headless = Dialer::with_context(root, Default::default())?;
                headless.request(
                    "GET",
                    "/headless",
                    None,
                    RequestOptions {
                        session_id: Some("body-seat"),
                        ..Default::default()
                    },
                );
                let pane = Dialer::with_context(
                    root,
                    json!({"tmux_pane":"%7", "unknown":1, "tty":null})
                        .as_object()
                        .unwrap()
                        .clone(),
                )?;
                pane.request(
                    "GET",
                    "/pane",
                    None,
                    RequestOptions {
                        session_id: Some("guess"),
                        ..Default::default()
                    },
                );
                // A proxy may precede gobby init: retry project discovery while absent.
                fs::create_dir(root.join(".gobby"))?;
                fs::write(root.join(".gobby/project.json"), r#"{"id":"initialized"}"#)?;
                headless.request("GET", "/initialized", None, Default::default());
                temp_env::with_vars(
                    [
                        ("GOBBY_AGENT_API_TOKEN", Some("agent-key")),
                        ("GOBBY_SESSION_ID", Some("  trusted-seat  ")),
                        ("GOBBY_PROJECT_ID", Some("trusted-project")),
                        ("GOBBY_AGENT_RUN_ID", Some("run")),
                        ("GOBBY_MANAGED_EXECUTION_ID", Some("execution")),
                    ],
                    || -> anyhow::Result<()> {
                        Dialer::with_context(root, Default::default())?.request(
                            "GET",
                            "/agent",
                            None,
                            RequestOptions {
                                project_id: Some("target"),
                                session_id: Some("untrusted"),
                                ..Default::default()
                            },
                        );
                        Ok(())
                    },
                )?;
                let requests = server.requests();
                assert_eq!(requests[0].headers["x-gobby-session-id"], "body-seat");
                assert!(!requests[0].headers.contains_key("x-gobby-project-id"));
                assert!(!requests[1].headers.contains_key("x-gobby-session-id"));
                assert_eq!(
                    serde_json::from_str::<serde_json::Value>(
                        &requests[1].headers["x-gobby-terminal-context"]
                    )?,
                    json!({"tmux_pane":"%7"})
                );
                assert_eq!(requests[2].headers["x-gobby-project-id"], "initialized");
                assert_eq!(
                    requests[2].headers["x-gobby-caller-project-id"],
                    "initialized"
                );
                assert_eq!(requests[3].headers["authorization"], "Bearer agent-key");
                assert_eq!(requests[3].headers["x-gobby-session-id"], "trusted-seat");
                assert_eq!(
                    requests[3].headers["x-gobby-caller-project-id"],
                    "trusted-project"
                );
                assert_eq!(requests[3].headers["x-gobby-project-id"], "target");
                assert_eq!(requests[3].headers["x-gobby-agent-run-id"], "run");
                assert_eq!(
                    requests[3].headers["x-gobby-managed-execution-id"],
                    "execution"
                );
                Ok(())
            },
        )
    })
}

#[test]
fn unauthorized_rereads_credential_and_retries_once() -> anyhow::Result<()> {
    isolated(|root| {
        let server = StubDaemon::start(vec![
            Response::json(401, json!({})),
            Response::json(200, json!({"ok":true})),
            Response::json(401, json!({})),
            Response::json(401, json!({"denied":true})),
        ])?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                fs::write(root.join("bootstrap.yaml"), "api_key: renewed\n")?;
                assert_eq!(
                    dialer.request("GET", "/retry", None, Default::default()),
                    json!({"ok":true})
                );
                let denied = dialer.request("GET", "/deny", None, Default::default());
                assert_eq!(
                    denied,
                    json!({"success":false,"error":"HTTP 401: {\"denied\":true}"})
                );
                let requests = server.requests();
                assert_eq!(requests.len(), 4);
                assert_eq!(requests[0].headers["authorization"], "Bearer first");
                for request in &requests[1..] {
                    assert_eq!(request.headers["authorization"], "Bearer renewed");
                }
                Ok(())
            },
        )
    })
}

#[test]
fn conflict_and_error_statuses_match_wrapper() -> anyhow::Result<()> {
    isolated(|root| {
        let detail =
            json!({"error_code":"SESSION_REQUIRED","success":false,"message":"bind caller"});
        let server = StubDaemon::start(vec![
            Response::json(409, json!({"detail": detail})),
            Response::json(409, json!({"detail": "other"})),
            Response {
                status: 500,
                body: "plain error".into(),
                delay: Default::default(),
            },
            Response {
                status: 409,
                body: "invalid json".into(),
                delay: Default::default(),
            },
            Response::json(201, json!({"created":true})),
        ])?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                assert_eq!(
                    dialer.request("GET", "/session", None, Default::default()),
                    detail
                );
                for expected in [
                    "HTTP 409: {\"detail\":\"other\"}",
                    "HTTP 500: plain error",
                    "HTTP 409: invalid json",
                    "HTTP 201: {\"created\":true}",
                ] {
                    assert_eq!(
                        dialer.request("GET", "/error", None, Default::default()),
                        json!({"success":false,"error":expected})
                    );
                }
                Ok(())
            },
        )
    })
}

#[test]
fn unavailable_and_timeout_results_are_typed() -> anyhow::Result<()> {
    isolated(|root| {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        let address = listener.local_addr()?;
        drop(listener);
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(format!("http://{address}")),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let result = dialer.request("GET", "/refused", None, Default::default());
                assert_eq!(result["error_code"], "DAEMON_UNAVAILABLE");
                assert_eq!(
                    result["error"],
                    format!(
                        "Gobby daemon HTTP control plane is unavailable at http://{address}: connection failed. Start it with `gobby start`, or check `gobby status`."
                    )
                );
                Ok(())
            },
        )?;
        let server = StubDaemon::start(vec![
            Response::json(
                503,
                json!({"status":"unavailable", "backend":{"state":"stopped", "target":"python"}}),
            ),
            Response::json(503, json!({"ordinary":"error"})),
            Response {
                status: 200,
                body: "{}".into(),
                delay: std::time::Duration::from_millis(150),
            },
        ])?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let unavailable = dialer.request("GET", "/unavailable", None, Default::default());
                assert_eq!(unavailable["error_code"], "DAEMON_UNAVAILABLE");
                assert!(
                    unavailable["error"]
                        .as_str()
                        .unwrap()
                        .contains("backend stopped")
                );
                assert_eq!(
                    dialer.request("GET", "/ordinary", None, Default::default()),
                    json!({"success":false,"error":"HTTP 503: {\"ordinary\":\"error\"}"})
                );
                let timeout = dialer.request(
                    "POST",
                    "/api/mcp/gobby-agents/tools/spawn_agent/",
                    Some(&json!({})),
                    RequestOptions {
                        timeout: std::time::Duration::from_millis(30),
                        ..Default::default()
                    },
                );
                assert_eq!(timeout["error_code"], "REQUEST_TIMEOUT");
                assert_eq!(
                    timeout["error"],
                    "Gobby daemon request timed out after 0.03s while calling /api/mcp/gobby-agents/tools/spawn_agent/. Retry spawn_agent with the same task_id to recover the accepted run, or call list_running_agents with that task_id."
                );
                Ok(())
            },
        )
    })
}

#[test]
fn dialer_survives_daemon_restart() -> anyhow::Result<()> {
    isolated(|root| {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        let address = listener.local_addr()?;
        drop(listener);
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(format!("http://{address}")),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                let options = || RequestOptions {
                    preflight: true,
                    ..Default::default()
                };
                assert_eq!(
                    dialer.request("GET", "/before", None, options())["error_code"],
                    "DAEMON_UNAVAILABLE"
                );
                {
                    let server = StubDaemon::bind(
                        address,
                        (0..3)
                            .map(|_| Response::json(200, json!({"ok":true})))
                            .collect(),
                    )?;
                    assert_eq!(
                        dialer.request("GET", "/one", None, options()),
                        json!({"ok":true})
                    );
                    assert_eq!(
                        dialer.request("GET", "/two", None, options()),
                        json!({"ok":true})
                    );
                    let requests = server.requests();
                    assert_eq!(
                        requests.iter().map(|r| r.path.as_str()).collect::<Vec<_>>(),
                        ["/api/health", "/one", "/two"]
                    );
                }
                assert_eq!(
                    dialer.request("GET", "/down", None, options())["error_code"],
                    "DAEMON_UNAVAILABLE"
                );
                let server = StubDaemon::bind(
                    address,
                    (0..2)
                        .map(|_| Response::json(200, json!({"restarted":true})))
                        .collect(),
                )?;
                assert_eq!(
                    dialer.request("GET", "/after", None, options()),
                    json!({"restarted":true})
                );
                assert_eq!(server.requests()[0].path, "/api/health");
                Ok(())
            },
        )
    })
}

#[test]
#[cfg(unix)]
fn managed_envelope_credential_is_reread_per_request() -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    isolated(|root| {
        let envelope = root.join("grant.json");
        let write = |token: &str| -> anyhow::Result<()> {
            fs::write(
                &envelope,
                serde_json::to_vec(
                    &json!({"managed_api_token":token,"principal":{"kind":"agent_run","execution_id":"run","session_id":"seat","project_id":"project"}}),
                )?,
            )?;
            fs::set_permissions(&envelope, fs::Permissions::from_mode(0o600))?;
            Ok(())
        };
        write("first")?;
        let server = StubDaemon::start((0..2).map(|_| Response::json(200, json!({}))).collect())?;
        temp_env::with_vars(
            [
                ("GOBBY_DAEMON_URL", Some(server.url().as_str())),
                ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", envelope.to_str()),
                ("GOBBY_AGENT_RUN_ID", Some("run")),
                ("GOBBY_SESSION_ID", Some("seat")),
                ("GOBBY_PROJECT_ID", Some("project")),
            ],
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                assert_eq!(
                    dialer.request("GET", "/first", None, Default::default()),
                    json!({})
                );
                write("renewed")?;
                assert_eq!(
                    dialer.request("GET", "/renewed", None, Default::default()),
                    json!({})
                );
                let requests = server.requests();
                assert_eq!(requests[0].headers["authorization"], "Bearer first");
                assert_eq!(requests[1].headers["authorization"], "Bearer renewed");
                assert_eq!(requests[1].headers["x-gobby-caller-project-id"], "project");
                fs::remove_file(&envelope)?;
                assert_eq!(
                    dialer.request("GET", "/missing", None, Default::default())["success"],
                    false
                );
                assert_eq!(
                    server.requests().len(),
                    2,
                    "must not send a cached capability after envelope removal"
                );
                Ok(())
            },
        )
    })
}

#[test]
fn unmanaged_credential_is_cached_until_unauthorized() -> anyhow::Result<()> {
    isolated(|root| {
        let server = StubDaemon::start((0..2).map(|_| Response::json(200, json!({}))).collect())?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                fs::write(root.join("bootstrap.yaml"), "api_key: changed\n")?;
                dialer.request("GET", "/first", None, Default::default());
                dialer.request("GET", "/second", None, Default::default());
                assert!(
                    server
                        .requests()
                        .iter()
                        .all(|request| request.headers["authorization"] == "Bearer first")
                );
                assert_eq!(server.requests().len(), 2);
                Ok(())
            },
        )
    })
}

#[test]
fn requests_do_not_hold_a_global_io_lock() -> anyhow::Result<()> {
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    isolated(|root| {
        let server = StubDaemon::start(vec![
            Response {
                status: 200,
                body: "{\"slow\":true}".into(),
                delay: Duration::from_millis(600),
            },
            Response::json(200, json!({"fast":true})),
        ])?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(server.url()),
            || -> anyhow::Result<()> {
                let dialer = Arc::new(Dialer::with_context(root, Default::default())?);
                let slow = dialer.clone();
                let worker = std::thread::spawn(move || {
                    slow.request("GET", "/slow", None, Default::default())
                });
                server.wait_for_requests(1);
                let started = Instant::now();
                assert_eq!(
                    dialer.request("GET", "/fast", None, Default::default()),
                    json!({"fast":true})
                );
                assert!(
                    started.elapsed() < Duration::from_millis(400),
                    "fast request waited for slow I/O"
                );
                assert_eq!(worker.join().unwrap(), json!({"slow":true}));
                Ok(())
            },
        )
    })
}

#[test]
fn endpoint_changes_and_query_parameters_are_followed() -> anyhow::Result<()> {
    isolated(|root| {
        let first = StubDaemon::start(vec![Response::json(200, json!({"first":true}))])?;
        let second = StubDaemon::start(vec![Response::json(200, json!({"second":true}))])?;
        temp_env::with_var(
            "GOBBY_DAEMON_URL",
            Some(first.url()),
            || -> anyhow::Result<()> {
                let dialer = Dialer::with_context(root, Default::default())?;
                assert_eq!(
                    dialer.request("GET", "/first", None, Default::default()),
                    json!({"first":true})
                );
                temp_env::with_var("GOBBY_DAEMON_URL", Some(second.url()), || {
                    assert_eq!(
                        dialer.request(
                            "GET",
                            "/second",
                            None,
                            RequestOptions {
                                params: &[("intent", "a b&c")],
                                ..Default::default()
                            }
                        ),
                        json!({"second":true})
                    );
                });
                assert_eq!(first.requests().len(), 1);
                assert_eq!(second.requests()[0].path, "/second?intent=a+b%26c");
                Ok(())
            },
        )
    })
}
