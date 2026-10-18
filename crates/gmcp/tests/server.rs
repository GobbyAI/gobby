//! `gmcp` over stdio, checked against the Python wrapper's captured fixtures.

mod stub_daemon;

use anyhow::{Context, Result, bail};
use gobby_mcp::contract::WRAPPER_CONTRACT;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};
use stub_daemon::{Response, StubDaemon};

const TOOLS_LIST: &str = include_str!("../contracts/tools_list.v1.json");
const WIRE_CASES: &str = include_str!("../contracts/wire_cases.v1.json");
const SHAPING_CASES: &str = include_str!("../contracts/shaping_cases.v1.json");
const PROTOCOL_VERSION: &str = "2025-11-25";
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(10);
/// The wire cases capture the Python wrapper against this port, so it stays fixed.
const FIXTURE_DAEMON_PORT: u16 = 61040;

fn fixture(text: &str) -> Result<Value> {
    serde_json::from_str(text).context("fixture is valid JSON")
}

fn tasks_listing() -> Value {
    json!({
        "success": true,
        "tools": [{
            "name": "create_task",
            "description": "Create a task",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
        }],
    })
}

/// Normalizes what the test must not compare exactly: the crate version, and the
/// pretty-printed text block, whose object order the wire format leaves open.
fn canonical(mut value: Value) -> Value {
    if let Some(version) = value.pointer_mut("/serverInfo/version") {
        *version = json!("<version>");
    }
    if let Some(items) = value.pointer_mut("/content").and_then(Value::as_array_mut) {
        for item in items {
            if let Some(text) = item.get("text").and_then(Value::as_str).map(str::to_owned) {
                item["text"] = serde_json::from_str(&text).unwrap_or(json!(text));
            }
        }
    }
    value
}

/// The working directory belongs to the host, so both sides compare a placeholder.
fn with_cwd_placeholder(requests: Vec<Value>) -> Vec<Value> {
    requests
        .into_iter()
        .map(|mut request| {
            if let Some(body) = request.get_mut("body").and_then(Value::as_object_mut)
                && body.contains_key("cwd")
            {
                body.insert("cwd".into(), json!("<cwd>"));
            }
            request
        })
        .collect()
}

/// A stdio `gmcp` child that speaks newline-delimited JSON-RPC.
struct Gmcp {
    child: Child,
    stdin: Option<ChildStdin>,
    messages: mpsc::Receiver<Value>,
    next_id: u64,
}

impl Gmcp {
    fn spawn(daemon_url: &str, home: &Path) -> Result<Self> {
        std::fs::write(home.join("bootstrap.yaml"), "api_key: test\n")?;
        let mut child = Command::new(env!("CARGO_BIN_EXE_gmcp"))
            .current_dir(home)
            .env("GOBBY_HOME", home)
            .env("GOBBY_DAEMON_URL", daemon_url)
            .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env_remove("GOBBY_AGENT_API_TOKEN")
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_SESSION_ID")
            .env_remove("GOBBY_PROJECT_ID")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("spawn gmcp")?;
        let stdout = child.stdout.take().context("gmcp stdout")?;
        let (sender, messages) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Ok(message) = serde_json::from_str::<Value>(&line)
                    && sender.send(message).is_err()
                {
                    break;
                }
            }
        });
        let stdin = child.stdin.take();
        Ok(Self { child, stdin, messages, next_id: 1 })
    }

    fn send(&mut self, message: &Value) -> Result<()> {
        let stdin = self.stdin.as_mut().context("gmcp stdin is closed")?;
        writeln!(stdin, "{message}")?;
        stdin.flush()?;
        Ok(())
    }

    fn request(&mut self, method: &str, params: Value) -> Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        Ok(id)
    }

    fn response(&self, id: u64) -> Result<Value> {
        loop {
            let message = self
                .messages
                .recv_timeout(RESPONSE_TIMEOUT)
                .with_context(|| format!("no response to request {id}"))?;
            if message.get("id") == Some(&json!(id)) {
                if let Some(error) = message.get("error") {
                    bail!("request {id} failed: {error}");
                }
                return Ok(message["result"].clone());
            }
        }
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        let id = self.request(method, params)?;
        self.response(id)
    }

    fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value> {
        self.call("tools/call", json!({"name": name, "arguments": arguments}))
    }

    fn initialize(&mut self) -> Result<Value> {
        let params = json!({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fixture", "version": "0"},
        });
        let result = self.call("initialize", params)?;
        self.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))?;
        Ok(result)
    }

    fn close_stdin(&mut self) {
        self.stdin = None;
    }

    fn wait_for_exit(&mut self, limit: Duration) -> Result<ExitStatus> {
        let deadline = Instant::now() + limit;
        loop {
            if let Some(status) = self.child.try_wait()? {
                return Ok(status);
            }
            if Instant::now() >= deadline {
                bail!("gmcp did not exit within {limit:?} of stdin closing");
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for Gmcp {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A URL whose port has nothing listening, so every dial is refused.
fn closed_url() -> Result<String> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(format!("http://127.0.0.1:{port}"))
}

#[test]
fn initialize_does_no_io() -> Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    listener.set_nonblocking(true)?;
    let url = format!("http://{}", listener.local_addr()?);
    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&url, home.path())?;

    let result = gmcp.initialize()?;

    let expected = fixture(WIRE_CASES)?["initialize"].clone();
    assert_eq!(canonical(result), canonical(expected));
    match listener.accept() {
        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(()),
        other => bail!("initialize touched the daemon listener: {other:?}"),
    }
}

#[test]
fn tools_list_matches_fixture() -> Result<()> {
    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&closed_url()?, home.path())?;
    gmcp.initialize()?;

    let result = gmcp.call("tools/list", json!({}))?;

    assert_eq!(result, fixture(TOOLS_LIST)?);
    Ok(())
}

#[test]
fn tools_forward_like_python_wrapper() -> Result<()> {
    let wire = fixture(WIRE_CASES)?;
    for case in wire["forwarded"].as_array().context("forwarded cases")? {
        let daemon = StubDaemon::start((0..8).map(|_| Response::json(200, json!({"success": true}))).collect())?;
        let home = tempfile::tempdir()?;
        let mut gmcp = Gmcp::spawn(&daemon.url(), home.path())?;
        gmcp.initialize()?;

        gmcp.call_tool(case["tool"].as_str().context("tool name")?, case["arguments"].clone())?;

        let requests: Vec<_> = daemon
            .requests()
            .into_iter()
            .filter(|request| !request.path.starts_with("/api/mcp/bridge/ready"))
            .collect();
        let header = WRAPPER_CONTRACT.protocol_version_header.to_ascii_lowercase();
        for request in &requests {
            assert_eq!(request.headers.get(&header), Some(&WRAPPER_CONTRACT.protocol_version));
        }
        let observed = requests
            .into_iter()
            .map(|request| {
                let (path, query) = request.path.split_once('?').unwrap_or((&request.path, ""));
                let query: BTreeMap<&str, &str> = query
                    .split('&')
                    .filter(|pair| !pair.is_empty())
                    .map(|pair| pair.split_once('=').unwrap_or((pair, "")))
                    .collect();
                json!({
                    "method": request.method,
                    "path": path,
                    "query": query,
                    "body": request.body,
                })
            })
            .collect();
        let expected = case["requests"].as_array().context("expected requests")?.clone();
        assert_eq!(
            with_cwd_placeholder(observed),
            with_cwd_placeholder(expected),
            "tool {}",
            case["tool"]
        );
    }
    Ok(())
}

#[test]
fn wire_results_match_fixture() -> Result<()> {
    let wire = fixture(WIRE_CASES)?;
    let calls = wire["calls"].as_object().context("calls")?;

    let daemon = StubDaemon::start(vec![Response::json(200, tasks_listing())])?;
    let home = tempfile::tempdir()?;
    let mut reachable = Gmcp::spawn(&daemon.url(), home.path())?;
    reachable.initialize()?;
    let listed = reachable.call_tool("list_tools", json!({"server_name": "gobby-tasks"}))?;
    assert_eq!(canonical(listed), canonical(calls["list_tools_success"].clone()));

    TcpListener::bind(("127.0.0.1", FIXTURE_DAEMON_PORT))
        .context("the fixture port must be free for the unavailable case")?;
    let down_home = tempfile::tempdir()?;
    let mut down = Gmcp::spawn(&format!("http://localhost:{FIXTURE_DAEMON_PORT}"), down_home.path())?;
    down.initialize()?;
    let unavailable = down.call_tool("list_mcp_servers", json!({}))?;
    assert_eq!(canonical(unavailable), canonical(calls["daemon_unavailable"].clone()));
    let init_project = down.call_tool("init_project", json!({"name": "demo"}))?;
    assert_eq!(canonical(init_project), canonical(calls["init_project"].clone()));
    Ok(())
}

#[test]
fn result_type_is_complete_on_list_and_call_envelopes() -> Result<()> {
    let daemon = StubDaemon::start((0..8).map(|_| Response::json(200, tasks_listing())).collect())?;
    let home = tempfile::tempdir()?;
    let mut reachable = Gmcp::spawn(&daemon.url(), home.path())?;
    reachable.initialize()?;
    let listed = reachable.call("tools/list", json!({}))?;
    assert_eq!(listed["resultType"], "complete");
    let listing = reachable.call_tool("list_tools", json!({"server_name": "gobby-tasks"}))?;
    assert_eq!(listing["resultType"], "complete");

    let down_home = tempfile::tempdir()?;
    let mut down = Gmcp::spawn(&closed_url()?, down_home.path())?;
    down.initialize()?;
    let unavailable = down.call_tool("list_mcp_servers", json!({}))?;
    assert_eq!(unavailable["resultType"], "complete");
    let init_project = down.call_tool("init_project", json!({"name": "demo"}))?;
    assert_eq!(init_project["resultType"], "complete");
    Ok(())
}

#[test]
fn bridge_ready_follows_wrapper_schedule() -> Result<()> {
    let wire = fixture(WIRE_CASES)?;
    let delays: Vec<f64> = wire["bridge_ready"]["delays_seconds"]
        .as_array()
        .context("delays")?
        .iter()
        .map(|delay| delay.as_f64().context("delay seconds"))
        .collect::<Result<_>>()?;
    let conflict = json!({"detail": {"success": false, "error_code": "SESSION_REQUIRED"}});
    let daemon = StubDaemon::start(vec![
        Response::json(409, conflict.clone()),
        Response::json(409, conflict),
        Response::json(200, json!({"success": true})),
    ])?;
    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&daemon.url(), home.path())?;
    gmcp.initialize()?;
    assert!(daemon.requests().is_empty(), "readiness waits for the first tools/list");

    gmcp.call("tools/list", json!({}))?;
    let started = Instant::now();
    let mut arrivals = Vec::new();
    for count in 1..=3 {
        daemon.wait_for_requests(count);
        arrivals.push(started.elapsed());
    }
    let tolerance = Duration::from_millis(50);
    assert!(arrivals[1] + tolerance >= Duration::from_secs_f64(delays[1]));
    assert!(arrivals[2] - arrivals[1] + tolerance >= Duration::from_secs_f64(delays[2]));
    Ok(())
}

#[test]
fn binary_version_and_clean_exit() -> Result<()> {
    let output = Command::new(env!("CARGO_BIN_EXE_gmcp")).arg("--version").output()?;
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout)?.trim(),
        format!("gmcp {}", env!("CARGO_PKG_VERSION"))
    );

    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&closed_url()?, home.path())?;
    gmcp.initialize()?;
    gmcp.close_stdin();
    let status = gmcp.wait_for_exit(Duration::from_secs(2))?;
    assert!(status.success());
    Ok(())
}

#[test]
fn stdin_close_exits_with_outstanding_wait() -> Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let url = format!("http://{}", listener.local_addr()?);
    let (held_sender, held) = mpsc::channel();
    let (release_sender, release) = mpsc::channel::<()>();
    thread::spawn(move || -> Result<()> {
        let (mut stream, _) = listener.accept()?;
        let mut head = Vec::new();
        let mut byte = [0_u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte)?;
            head.push(byte[0]);
        }
        held_sender.send(())?;
        // The response is withheld until the test releases it.
        let _ = release.recv();
        stream.write_all(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 17\r\nConnection: close\r\n\r\n{\"success\": true}",
        )?;
        Ok(())
    });

    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&url, home.path())?;
    gmcp.initialize()?;
    gmcp.request("tools/call", json!({"name": "list_mcp_servers", "arguments": {}}))?;
    held.recv_timeout(Duration::from_secs(5))
        .context("the daemon never received the wait request")?;

    gmcp.close_stdin();
    let status = gmcp.wait_for_exit(Duration::from_secs(2))?;
    // Only after the child has exited does the held response go out.
    release_sender.send(())?;
    assert!(status.success());
    Ok(())
}

/// Runs one shaping case: the stub answers the daemon calls in order, and the wrapper must make
/// the fixture's requests and return the fixture's envelope.
fn shaping_case_matches_fixture(name: &str) -> Result<()> {
    let shaping = fixture(SHAPING_CASES)?;
    let case = shaping["cases"]
        .as_array()
        .context("shaping cases")?
        .iter()
        .find(|case| case["name"] == name)
        .with_context(|| format!("no shaping case named {name}"))?;
    let responses: Vec<Response> = case["responses"]
        .as_array()
        .context("shaping responses")?
        .iter()
        .map(|body| Response::json(200, body.clone()))
        .collect();
    let daemon = StubDaemon::start(responses)?;
    let home = tempfile::tempdir()?;
    let mut gmcp = Gmcp::spawn(&daemon.url(), home.path())?;
    gmcp.initialize()?;
    let result = gmcp.call_tool(
        case["tool"].as_str().context("tool name")?,
        case["arguments"].clone(),
    )?;

    let observed: Vec<Value> = daemon
        .requests()
        .into_iter()
        .filter(|request| !request.path.starts_with("/api/mcp/bridge/ready"))
        .map(|request| {
            let (path, query) = request.path.split_once('?').unwrap_or((&request.path, ""));
            let query: BTreeMap<&str, &str> = query
                .split('&')
                .filter(|pair| !pair.is_empty())
                .map(|pair| pair.split_once('=').unwrap_or((pair, "")))
                .collect();
            json!({
                "method": request.method,
                "path": path,
                "query": query,
                "body": request.body,
            })
        })
        .collect();
    let expected = case["requests"]
        .as_array()
        .context("expected requests")?
        .clone();
    assert_eq!(
        with_cwd_placeholder(observed),
        with_cwd_placeholder(expected),
        "requests for {name}"
    );
    assert_eq!(
        canonical(result),
        canonical(case["envelope"].clone()),
        "envelope for {name}"
    );
    Ok(())
}

#[test]
fn shaping_list_mcp_servers_compacts_rows() -> Result<()> {
    shaping_case_matches_fixture("list_mcp_servers_compacts_rows")
}

#[test]
fn shaping_list_tools_strips_none_for_one_server() -> Result<()> {
    shaping_case_matches_fixture("list_tools_strips_none_for_one_server")
}

#[test]
fn shaping_list_tools_aggregates_servers_without_a_name() -> Result<()> {
    shaping_case_matches_fixture("list_tools_aggregates_servers_without_a_name")
}

#[test]
fn shaping_get_tool_schema_reshapes_the_answer() -> Result<()> {
    shaping_case_matches_fixture("get_tool_schema_reshapes_the_answer")
}

#[test]
fn shaping_get_tool_schema_keeps_only_the_error() -> Result<()> {
    shaping_case_matches_fixture("get_tool_schema_keeps_only_the_error")
}
