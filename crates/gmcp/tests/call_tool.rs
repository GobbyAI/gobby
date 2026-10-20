use anyhow::Context;
use anyhow::Result;
use gobby_mcp::call_tool::{guarded_call, prepare};
use gobby_mcp::canonical::canonicalize;
use serde_json::Value;
use serde_json::json;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

struct Client {
    child: Child,
    stdin: ChildStdin,
    messages: mpsc::Receiver<Value>,
}

impl Client {
    fn start(url: &str, home: &std::path::Path) -> Result<Self> {
        std::fs::write(home.join("bootstrap.yaml"), "api_key: test\n")?;
        let mut child = Command::new(env!("CARGO_BIN_EXE_gmcp"))
            .current_dir(home)
            .env("GOBBY_HOME", home)
            .env("GOBBY_DAEMON_URL", url)
            .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env_remove("GOBBY_AGENT_API_TOKEN")
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_SESSION_ID")
            .env_remove("GOBBY_PROJECT_ID")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let stdout = child.stdout.take().context("stdout")?;
        let (sender, messages) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(value) = serde_json::from_str(&line)
                    && sender.send(value).is_err()
                {
                    break;
                }
            }
        });
        let stdin = child.stdin.take().context("stdin")?;
        Ok(Self {
            child,
            stdin,
            messages,
        })
    }

    fn send(&mut self, value: Value) -> Result<()> {
        writeln!(self.stdin, "{value}")?;
        self.stdin.flush()?;
        Ok(())
    }

    fn next(&self) -> Result<Value> {
        Ok(self.messages.recv_timeout(Duration::from_secs(5))?)
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn read_http(stream: &mut TcpStream) -> Result<(String, String, Value)> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut reader = BufReader::new(stream);
    let mut head = String::new();
    reader.read_line(&mut head)?;
    let mut length = 0;
    loop {
        let mut line = String::new();
        anyhow::ensure!(
            reader.read_line(&mut line)? > 0,
            "unexpected EOF in stub request headers"
        );
        if line == "\r\n" {
            break;
        }
        if let Some((key, value)) = line.split_once(':')
            && key.eq_ignore_ascii_case("content-length")
        {
            length = value.trim().parse()?;
        }
        head.push_str(&line);
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    let path = head
        .split_whitespace()
        .nth(1)
        .context("request path")?
        .to_owned();
    Ok((
        path,
        head.to_lowercase(),
        if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&body)?
        },
    ))
}

fn respond(stream: &mut TcpStream, value: Value) -> Result<()> {
    let body = value.to_string();
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )?;
    Ok(())
}

#[test]
fn ordinary_call_completes_during_wait() -> Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let url = format!("http://{}", listener.local_addr()?);
    let (arrived, held) = mpsc::channel();
    let (release, released) = mpsc::channel();
    let (requests, recorded) = mpsc::channel();
    let server = thread::spawn(move || -> Result<()> {
        let mut pending = None;
        // The first wrapper call captures the inventory; later calls reuse it.
        for _ in 0..3 {
            let (mut stream, _) = listener.accept()?;
            let (path, headers, body) = read_http(&mut stream)?;
            requests.send((path.clone(), headers, body))?;
            if path == "/api/mcp/tools/call" {
                pending = Some(stream);
                arrived.send(())?;
            } else if path == "/api/mcp/bridge/tool-timeouts" {
                respond(&mut stream, json!({"success":true,"tool_timeouts":{}}))?;
            } else {
                respond(
                    &mut stream,
                    json!({"success":true,"result":{"ordinary":true}}),
                )?;
            }
        }
        released.recv_timeout(Duration::from_secs(5))?;
        respond(
            &mut pending.context("held wait socket")?,
            json!({"success":true,"result":{"wait":true}}),
        )
    });
    let home = tempfile::tempdir()?;
    let mut client = Client::start(&url, home.path())?;
    client.send(json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}))?;
    assert_eq!(client.next()?["id"], 1);
    client.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))?;
    client.send(json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"call_tool",
        "arguments":{"server_name":"gobby-agents","tool_name":"wait_for_output","arguments":{"timeout":30},
        "session_id":"#wait","project_id":"wait-project","preflight_enabled":false}}}))?;
    held.recv_timeout(Duration::from_secs(5))
        .context("wait request did not arrive")?;
    client.send(
        json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"call_tool",
        "arguments":{"server_name":"example","tool_name":"echo","arguments":{"value":"ordinary"},
        "session_id":"#ordinary","project_id":"ordinary-project","preflight_enabled":false}}}),
    )?;
    let ordinary = client.next()?;
    assert_eq!(
        ordinary["id"], 3,
        "ordinary response must precede held wait"
    );
    assert_eq!(
        ordinary["result"]["structuredContent"]["result"]["ordinary"],
        true
    );
    let calls: Vec<_> = (0..3)
        .map(|_| recorded.recv_timeout(Duration::from_secs(5)))
        .collect::<std::result::Result<_, _>>()?;
    let ordinary_http = calls
        .iter()
        .find(|(path, _, _)| path == "/api/mcp/example/tools/echo")
        .context("ordinary route")?;
    assert!(
        ordinary_http
            .1
            .contains("x-gobby-session-id: #ordinary\r\n")
    );
    assert!(
        ordinary_http
            .1
            .contains("x-gobby-project-id: ordinary-project\r\n")
    );
    assert_eq!(ordinary_http.2, json!({"value":"ordinary"}));
    let wait_http = calls
        .iter()
        .find(|(path, _, _)| path == "/api/mcp/tools/call")
        .context("wait route")?;
    assert!(wait_http.1.contains("x-gobby-session-id: #wait\r\n"));
    assert!(wait_http.1.contains("x-gobby-project-id: wait-project\r\n"));
    release.send(())?;
    let wait = client.next()?;
    assert_eq!(wait["id"], 2);
    assert_eq!(wait["result"]["structuredContent"]["result"]["wait"], true);
    server.join().expect("stub daemon worker")?;
    Ok(())
}

#[test]
fn canonicalization_matches_shared_cases() -> Result<()> {
    let corpus: Value =
        serde_json::from_str(include_str!("../contracts/call_tool_wrapper_cases.v1.json"))?;
    for case in corpus["cases"].as_array().expect("case array") {
        let actual = canonicalize(&case["input"]);
        if let Some(error) = case.get("error") {
            assert_eq!(
                actual,
                Err(error.as_str().expect("error text").to_owned()),
                "{}",
                case["name"]
            );
        } else {
            assert_eq!(actual, Ok(case["output"].clone()), "{}", case["name"]);
        }
    }
    Ok(())
}

#[test]
fn wait_tool_timeout_is_capped_with_metadata() -> Result<()> {
    let call = prepare(
        &json!({"server_name":"gobby-agents", "tool_name":"wait_for_output",
        "arguments":{"timeout":900}, "intent":"wait here"}),
    )?;
    assert_eq!(call.path, "/api/mcp/tools/call");
    assert_eq!(call.http_timeout, Duration::from_secs(330));
    assert_eq!(
        call.body,
        json!({"server_name":"gobby-agents", "tool_name":"wait_for_output",
        "arguments":{"timeout":300.0}, "intent":"wait here"})
    );
    assert_eq!(
        call.finish(json!({"success":true})),
        json!({"success":true,
        "requested_timeout_seconds":900.0, "effective_timeout_seconds":300.0,
        "wait_timeout_capped_by_mcp_wrapper":true})
    );
    Ok(())
}

#[test]
fn ordinary_and_extended_tools_use_wrapper_routes() -> Result<()> {
    let call = prepare(&json!({"server_name":"server/name", "tool_name":"echo?",
        "arguments":{"value":1}, "intent":"a".repeat(1100),
        "session_id":"#1", "project_id":"project", "preflight_enabled":false}))?;
    assert_eq!(call.path, "/api/mcp/server%2Fname/tools/echo%3F");
    assert_eq!(call.http_timeout, Duration::from_secs(30));
    assert_eq!(call.body, json!({"value":1}));
    assert_eq!(call.params, vec![("intent".to_owned(), "a".repeat(1024))]);
    assert_eq!(call.session_id.as_deref(), Some("#1"));
    assert_eq!(call.project_id.as_deref(), Some("project"));
    assert!(!call.preflight);
    let extended = prepare(&json!({"server_name":"gobby-agents", "tool_name":"spawn_agent"}))?;
    assert_eq!(extended.http_timeout, Duration::from_secs(300));
    assert_eq!(extended.guard_timeout, Some(300.0));
    assert!(extended.preflight);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn heartbeat_reports_progress_while_waiting() {
    for has_token in [true, false] {
        let progress = Arc::new(Mutex::new(Vec::new()));
        let observed = Arc::clone(&progress);
        let (release, held) = tokio::sync::oneshot::channel();
        let worker = tokio::spawn(async move {
            guarded_call(
                async move { held.await.expect("release") },
                "wait_for_output",
                Some(30.0),
                has_token.then_some(move |notification| {
                    observed.lock().expect("progress lock").push(notification);
                    std::future::ready(())
                }),
            )
            .await
        });
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_secs(16)).await;
        tokio::task::yield_now().await;
        let expected = if has_token {
            vec![json!({"progress":15.0,"total":30.0,
            "message":"wait_for_output still waiting for daemon result"})]
        } else {
            vec![]
        };
        assert_eq!(*progress.lock().expect("progress lock"), expected);
        release
            .send(json!({"success":true}))
            .expect("waiting request");
        assert_eq!(worker.await.expect("guard worker"), json!({"success":true}));
        tokio::time::advance(Duration::from_secs(30)).await;
        assert_eq!(*progress.lock().expect("progress lock"), expected);
    }
}

#[tokio::test(start_paused = true)]
async fn guard_timeout_returns_background_result() {
    let (release, held) = tokio::sync::oneshot::channel();
    let (finished, completion) = tokio::sync::oneshot::channel();
    let worker = tokio::spawn(async move {
        guarded_call(
            async move {
                let result = held.await.expect("release background request");
                finished.send(()).expect("completion receiver");
                result
            },
            "wait_for_output",
            Some(2.0),
            Some(|_| std::future::ready(())),
        )
        .await
    });
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_secs(6)).await;
    assert!(
        !worker.is_finished(),
        "guard includes five seconds of grace"
    );
    tokio::time::advance(Duration::from_secs(2)).await;
    assert_eq!(
        worker.await.expect("guard result"),
        json!({"success":true,"completed":false,
        "timeout_seconds":2.0,"effective_timeout_seconds":2.0,"mcp_wrapper_timeout":true,
        "background_call_continues":true,"tool_name":"wait_for_output"})
    );
    release
        .send(json!({"success":true}))
        .expect("background request must remain alive");
    completion.await.expect("background completion");
}

#[tokio::test(start_paused = true)]
async fn cancelled_guard_stops_heartbeat() {
    let progress = Arc::new(Mutex::new(Vec::new()));
    let observed = Arc::clone(&progress);
    let (release, held) = tokio::sync::oneshot::channel();
    let (finished, completion) = tokio::sync::oneshot::channel();
    let worker = tokio::spawn(async move {
        guarded_call(
            async move {
                let result = held.await.expect("release background request");
                finished.send(()).expect("completion receiver");
                result
            },
            "wait_for_output",
            Some(300.0),
            Some(move |notification| {
                observed.lock().expect("progress lock").push(notification);
                std::future::ready(())
            }),
        )
        .await
    });
    tokio::task::yield_now().await;
    worker.abort();
    assert!(worker.await.expect_err("cancelled guard").is_cancelled());
    release
        .send(json!({"success":true}))
        .expect("background request remains alive");
    completion.await.expect("background request completed");
    tokio::time::advance(Duration::from_secs(16)).await;
    tokio::task::yield_now().await;
    assert!(
        progress.lock().expect("progress lock").is_empty(),
        "cancelled guard must stop its heartbeat"
    );
}
