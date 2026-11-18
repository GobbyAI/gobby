//! Contract tests for `gport shapes`: per-tool call counts and argument-key sets, never a value.
//!
//! The spec fixes the `Skipped sources` heading and the exit codes and names no other output
//! text. So each test finds a tool by its label and reads the text up to the next label: the
//! count is a standalone number there, and the keys appear there in sorted order. Every run sets
//! HOME and GOBBY_HOME to empty temp directories and passes `--transcripts`, so nothing real is
//! read. Runs that do not test the daemon pass `--no-daemon`.

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tempfile::TempDir;

use crate::support::{Repo, Run, gport_env};

/// The tool labels in the main fixture, already in sorted order.
const LABELS: [&str; 4] = ["docs:lookup", "read_note", "search_notes", "write_note"];

/// Every secret value in the fixture starts with this prefix, so no output may contain it.
const SECRET: &str = "SECRET-";

/// The daemon's tool metrics answer: per-tool `call_count` under each server, as the MCP tools
/// listing returns it with `include_metrics=true`.
const METRICS_BODY: &str = r#"{"success":true,"tools":{"metricsrv":[{"name":"probe_tool","call_count":42,"success_rate":1.0,"avg_latency_ms":3.5}]}}"#;

/// A daemon metrics answer whose tool name is not name-shaped. It must count as unnamed.
const UNNAMED_METRICS_BODY: &str =
    r#"{"success":true,"tools":{"metricsrv":[{"name":"bad name SECRET-METRIC","call_count":5}]}}"#;

/// A git checkout to run from, plus scratch space for transcripts and empty homes.
struct Sandbox {
    repo: Repo,
    scratch: TempDir,
}

impl Sandbox {
    fn new() -> Self {
        Self {
            repo: Repo::new(),
            scratch: TempDir::new().expect("scratch dir"),
        }
    }

    fn join(&self, name: &str) -> PathBuf {
        self.scratch.path().join(name)
    }

    /// Runs `gport shapes <args>` with empty HOME and GOBBY_HOME and the given daemon URL.
    fn run(&self, args: &[String], daemon_url: &str) -> Run {
        let home = self.join("home");
        let gobby_home = self.join("gobby-home");
        std::fs::create_dir_all(&home).expect("empty home");
        std::fs::create_dir_all(&gobby_home).expect("empty gobby home");
        let mut owned = vec!["shapes".to_owned()];
        owned.extend(args.iter().cloned());
        let argv: Vec<&str> = owned.iter().map(String::as_str).collect();
        let home = home.to_string_lossy().into_owned();
        let gobby_home = gobby_home.to_string_lossy().into_owned();
        gport_env(
            self.repo.path(),
            &argv,
            &[
                ("HOME", home.as_str()),
                ("GOBBY_HOME", gobby_home.as_str()),
                ("GOBBY_DAEMON_URL", daemon_url),
            ],
        )
    }
}

/// `--transcripts <root>` for each root, followed by the extra flags.
fn shapes_args(roots: &[PathBuf], flags: &[&str]) -> Vec<String> {
    let mut out = Vec::new();
    for root in roots {
        out.push("--transcripts".to_owned());
        out.push(root.to_string_lossy().into_owned());
    }
    out.extend(flags.iter().map(|flag| (*flag).to_owned()));
    out
}

/// A daemon base URL whose port has no listener.
fn closed_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind probe port");
    let port = listener.local_addr().expect("probe address").port();
    drop(listener);
    format!("http://127.0.0.1:{port}")
}

/// What the stub saw: every accepted connection, and each request line that arrived.
#[derive(Debug)]
struct Seen {
    connections: usize,
    requests: Vec<String>,
}

/// A detached HTTP stub on 127.0.0.1 that answers every request with `body`.
struct Stub {
    url: String,
    connections: Arc<AtomicUsize>,
    requests: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
}

impl Stub {
    fn start(body: &'static str) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub");
        listener.set_nonblocking(true).expect("nonblocking stub");
        let url = format!("http://{}", listener.local_addr().expect("stub address"));
        let stub = Self {
            url,
            connections: Arc::new(AtomicUsize::new(0)),
            requests: Arc::new(Mutex::new(Vec::new())),
            stop: Arc::new(AtomicBool::new(false)),
        };
        let connections = Arc::clone(&stub.connections);
        let requests = Arc::clone(&stub.requests);
        let stop = Arc::clone(&stub.stop);
        let _worker = thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        connections.fetch_add(1, Ordering::SeqCst);
                        serve(stream, body, &requests);
                    }
                    Err(_) => thread::sleep(Duration::from_millis(10)),
                }
            }
        });
        stub
    }

    /// Stops accepting and returns what arrived so far.
    fn finish(self) -> Seen {
        self.stop.store(true, Ordering::SeqCst);
        Seen {
            connections: self.connections.load(Ordering::SeqCst),
            requests: self.requests.lock().expect("requests").clone(),
        }
    }
}

/// Reads one request, records its request line, and writes `body` as a 200 JSON response.
fn serve(stream: TcpStream, body: &str, requests: &Mutex<Vec<String>>) {
    stream.set_nonblocking(false).expect("blocking connection");
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("read timeout");
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    let mut header = String::new();
    loop {
        header.clear();
        match reader.read_line(&mut header) {
            Ok(0) | Err(_) => break,
            Ok(_) if header == "\r\n" || header == "\n" => break,
            Ok(_) => {}
        }
    }
    requests
        .lock()
        .expect("requests")
        .push(request_line.trim_end().to_owned());
    let mut stream = reader.into_inner();
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
}

/// The path of a request line, without its query string.
fn request_path(request_line: &str) -> &str {
    let target = request_line.split_whitespace().nth(1).unwrap_or("");
    target.split('?').next().unwrap_or("")
}

/// Writes one JSON-lines file, creating its parent directories.
fn write_lines(path: &Path, lines: &[String]) {
    std::fs::create_dir_all(path.parent().expect("parent dir")).expect("transcript dir");
    let mut text = lines.join("\n");
    text.push('\n');
    std::fs::write(path, text).expect("write transcript");
}

fn record(value: &Value) -> String {
    value.to_string()
}

/// A Claude Code assistant record holding one tool_use block.
fn claude_call(name: &str, input: Value) -> Value {
    json!({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": input}
    ]}})
}

/// A Claude Code call through the gobby proxy: the target sits in the input, not the name.
fn proxy_call(server: &str, tool: &str, arguments: Value) -> Value {
    claude_call(
        "mcp__gobby__call_tool",
        json!({"server_name": server, "tool_name": tool, "arguments": arguments}),
    )
}

/// A Codex function call; its arguments are a JSON object encoded as a string.
fn codex_call(name: &str, arguments: Value) -> Value {
    json!({"type": "response_item", "payload": {
        "type": "function_call", "name": name, "arguments": arguments.to_string()
    }})
}

/// Writes the main fixture and returns (Claude Code projects root, Codex sessions root).
///
/// Claude has a top-level file and a nested file. Codex has one dated rollout. Each secret value
/// starts with `SECRET-`, and every tool's keys are split across files, unsorted in the source.
/// Expected counts: read_note 7, write_note 13, search_notes 2, docs:lookup 5 (proxy calls).
fn write_fixture(sandbox: &Sandbox) -> (PathBuf, PathBuf) {
    let claude = sandbox.join("claude-projects");
    let codex = sandbox.join("codex-sessions");

    let mut top = Vec::new();
    for i in 0..3 {
        top.push(record(&claude_call(
            "read_note",
            json!({"output_style": format!("SECRET-STYLE-{i}"), "note_id": format!("SECRET-NOTE-{i}")}),
        )));
    }
    top.push(record(&json!({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "SECRET-TEXT-BLOCK thinking aloud"},
        {"type": "tool_use", "name": "write_note",
         "input": {"tags": ["SECRET-TAG"], "body_text": "SECRET-BODY-MIXED"}},
        {"type": "tool_use", "name": "write_note", "input": {"body_text": "SECRET-BODY-SECOND"}},
        {"type": "tool_use", "name": "read_note", "input": {"note_id": "SECRET-NOTE-MIXED"}}
    ]}})));
    for i in 0..3 {
        top.push(record(&proxy_call(
            "docs",
            "lookup",
            json!({"query": format!("SECRET-QUERY-{i}"), "limit": 10}),
        )));
    }
    top.push(record(&json!({"type": "user", "message": {"role": "user",
        "content": "SECRET-USER-TEXT please"}})));
    write_lines(&claude.join("alpha/session-one.jsonl"), &top);

    let mut nested = vec!["this line is not json {SECRET-RAW-LINE".to_owned()];
    for i in 0..2 {
        nested.push(record(&claude_call(
            "read_note",
            json!({"include_body": true, "note_id": format!("SECRET-NOTE-N2-{i}")}),
        )));
    }
    for i in 0..4 {
        nested.push(record(&claude_call(
            "write_note",
            json!({"body_text": format!("SECRET-BODY-N2-{i}")}),
        )));
    }
    nested.push(record(&claude_call(
        "search_notes",
        json!({"query": "SECRET-SEARCH-N2"}),
    )));
    for i in 0..2 {
        nested.push(record(&proxy_call(
            "docs",
            "lookup",
            json!({"query": format!("SECRET-QUERY-N2-{i}"), "offset": 2}),
        )));
    }
    nested.push(record(
        &json!({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "no tool here SECRET-PLAIN"}
        ]}}),
    ));
    write_lines(&claude.join("alpha/nested/session-two.jsonl"), &nested);

    let mut rollout = vec![record(&codex_call(
        "read_note",
        json!({"note_id": "SECRET-NOTE-CODEX"}),
    ))];
    for i in 0..7 {
        rollout.push(record(&codex_call(
            "write_note",
            json!({"title": format!("SECRET-TITLE-CODEX-{i}"), "body_text": "SECRET-BODY-CODEX"}),
        )));
    }
    rollout.push(record(&codex_call(
        "search_notes",
        json!({"query": "SECRET-SEARCH-CODEX", "limit": 3}),
    )));
    rollout.push(record(&json!({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": "SECRET-CODEX-MSG"
    }})));
    write_lines(&codex.join("2026/11/18/rollout-one.jsonl"), &rollout);

    (claude, codex)
}

/// The text for `label`: from its first occurrence to the next listed label, or to the end.
fn section<'a>(stdout: &'a str, label: &str, labels: &[&str]) -> Option<&'a str> {
    let start = stdout.find(label)?;
    let body = start + label.len();
    let end = labels
        .iter()
        .copied()
        .filter(|other| *other != label)
        .filter_map(|other| stdout[body..].find(other).map(|at| body + at))
        .min()
        .unwrap_or(stdout.len());
    Some(&stdout[start..end])
}

/// The section for `label`, panicking with the whole output when the label is not listed.
fn tool_text<'a>(stdout: &'a str, label: &str, labels: &[&str]) -> &'a str {
    section(stdout, label, labels)
        .unwrap_or_else(|| panic!("`{label}` is not listed; stdout:\n{stdout}"))
}

/// True when `text` holds `number` as a standalone token.
fn has_count(text: &str, number: u32) -> bool {
    let want = number.to_string();
    text.split(|c: char| !c.is_ascii_alphanumeric())
        .any(|token| token == want)
}

/// Asserts `keys` appear in `text` in the given order, and that none of `absent` appears.
fn assert_keys(text: &str, keys: &[&str], absent: &[&str]) {
    let mut at = 0;
    for &key in keys {
        match text[at..].find(key) {
            Some(found) => at += found + key.len(),
            None => panic!("key `{key}` is missing or out of sorted order in:\n{text}"),
        }
    }
    for &key in absent {
        assert!(
            !text.contains(key),
            "key `{key}` must not be listed for this tool:\n{text}"
        );
    }
}

/// The text after the `Skipped sources` heading, or None when the heading is absent.
fn skipped_section(stdout: &str) -> Option<&str> {
    const HEADING: &str = "Skipped sources";
    stdout.find(HEADING).map(|at| &stdout[at + HEADING.len()..])
}

/// Contract: each tool's count is its calls across every file and both record shapes, and a
/// proxy call counts under its target, never under the proxy tool's own name.
#[test]
fn counts_each_tool_across_files_and_both_shapes() {
    let sandbox = Sandbox::new();
    let (claude, codex) = write_fixture(&sandbox);
    let run = sandbox.run(
        &shapes_args(&[claude, codex], &["--no-daemon"]),
        &closed_url(),
    );
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    for (label, count) in [
        ("docs:lookup", 5),
        ("read_note", 7),
        ("search_notes", 2),
        ("write_note", 13),
    ] {
        let text = tool_text(&run.stdout, label, &LABELS);
        assert!(
            has_count(text, count),
            "`{label}` should count {count}; section:\n{text}"
        );
    }
    assert!(
        !run.stdout.contains("mcp__gobby__call_tool"),
        "the proxy tool must not be counted under its own name:\n{}",
        run.stdout
    );
}

/// Contract: each tool lists the sorted union of the argument keys across all of its calls,
/// and a proxy call lists only the keys of its arguments object.
#[test]
fn lists_sorted_union_of_argument_keys_per_tool() {
    let sandbox = Sandbox::new();
    let (claude, codex) = write_fixture(&sandbox);
    let run = sandbox.run(
        &shapes_args(&[claude, codex], &["--no-daemon"]),
        &closed_url(),
    );
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let out = &run.stdout;
    assert_keys(
        tool_text(out, "read_note", &LABELS),
        &["include_body", "note_id", "output_style"],
        &["body_text", "tags", "title"],
    );
    assert_keys(
        tool_text(out, "write_note", &LABELS),
        &["body_text", "tags", "title"],
        &["note_id", "include_body", "output_style"],
    );
    assert_keys(
        tool_text(out, "search_notes", &LABELS),
        &["limit", "query"],
        &["note_id", "tags"],
    );
    assert_keys(
        tool_text(out, "docs:lookup", &LABELS),
        &["limit", "offset", "query"],
        &["server_name", "tool_name", "note_id"],
    );
}

/// Contract: a proxy call is counted under '<server>:<tool>' with the keys of its arguments
/// object, for any server and tool names.
#[test]
fn proxy_call_counts_under_its_target_with_argument_keys() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("proxy-root");
    write_lines(
        &root.join("only.jsonl"),
        &[record(&proxy_call(
            "alpha_srv",
            "beta_tool",
            json!({"zeta_key": "SECRET-ZETA", "alpha_key": "SECRET-ALPHA"}),
        ))],
    );
    let run = sandbox.run(&shapes_args(&[root], &["--no-daemon"]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let labels = ["alpha_srv:beta_tool"];
    let text = tool_text(&run.stdout, "alpha_srv:beta_tool", &labels);
    assert!(
        has_count(text, 1),
        "the proxy target should count 1:\n{text}"
    );
    assert_keys(
        text,
        &["alpha_key", "zeta_key"],
        &["server_name", "tool_name"],
    );
    assert!(
        !run.stdout.contains("mcp__gobby__call_tool"),
        "the proxy tool must not be listed:\n{}",
        run.stdout
    );
}

/// Contract: no argument value appears in stdout or stderr. The fixture's values all start
/// with `SECRET-`, and that prefix must not appear in either stream.
#[test]
fn no_argument_value_appears_in_stdout_or_stderr() {
    let sandbox = Sandbox::new();
    let (claude, codex) = write_fixture(&sandbox);
    let run = sandbox.run(
        &shapes_args(&[claude, codex], &["--no-daemon"]),
        &closed_url(),
    );
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        run.stdout.contains("read_note"),
        "the run must list tools before values can be checked:\n{}",
        run.stdout
    );
    assert!(
        !run.stdout.contains(SECRET),
        "an argument value leaked to stdout:\n{}",
        run.stdout
    );
    assert!(
        !run.stderr.contains(SECRET),
        "an argument value leaked to stderr:\n{}",
        run.stderr
    );
}

/// Contract: a line that is not JSON, a blank line, and a record with no tool call are skipped
/// without error, and the tool calls around them are still counted.
#[test]
fn skips_malformed_and_toolless_lines_without_error() {
    let sandbox = Sandbox::new();
    let junk = sandbox.join("junk-root");
    write_lines(
        &junk.join("junk.jsonl"),
        &[
            "not json at all {".to_owned(),
            String::new(),
            record(&json!({"type": "user", "message": {"role": "user",
                "content": "SECRET-JUNK-USER"}})),
            record(&json!({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": "SECRET-JUNK-MSG"}})),
        ],
    );
    let mixed = sandbox.join("mixed-root");
    write_lines(
        &mixed.join("mixed.jsonl"),
        &[
            "garbage {".to_owned(),
            record(&claude_call(
                "read_note",
                json!({"note_id": "SECRET-MIXED-NOTE"}),
            )),
            String::new(),
        ],
    );
    let run = sandbox.run(
        &shapes_args(&[junk, mixed], &["--no-daemon"]),
        &closed_url(),
    );
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        !run.stderr.contains("panicked"),
        "skipped lines must not panic:\n{}",
        run.stderr
    );
    let labels = ["read_note"];
    let text = tool_text(&run.stdout, "read_note", &labels);
    assert!(
        has_count(text, 1),
        "the one valid call should count 1:\n{text}"
    );
}

/// Contract: a missing transcript root is listed under a `Skipped sources` heading, the other
/// roots are still counted, and the exit is 0.
#[test]
fn missing_transcript_root_is_listed_as_skipped_and_exits_zero() {
    let sandbox = Sandbox::new();
    let (claude, _codex) = write_fixture(&sandbox);
    let missing = sandbox.join("missing-root");
    let run = sandbox.run(
        &shapes_args(&[claude, missing], &["--no-daemon"]),
        &closed_url(),
    );
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let skipped = skipped_section(&run.stdout)
        .unwrap_or_else(|| panic!("no `Skipped sources` heading; stdout:\n{}", run.stdout));
    assert!(
        skipped.contains("missing-root"),
        "the missing root must be listed under the heading:\n{skipped}"
    );
    let text = tool_text(&run.stdout, "read_note", &LABELS);
    assert!(
        has_count(text, 6),
        "the Claude root alone counts read_note 6:\n{text}"
    );
}

/// Contract: with `--no-daemon`, no daemon is contacted (the stub at GOBBY_DAEMON_URL receives
/// no connection), the metrics source is listed as skipped, and the exit is 0.
#[test]
fn no_daemon_contacts_nothing_and_lists_metrics_as_skipped() {
    let sandbox = Sandbox::new();
    let (claude, _codex) = write_fixture(&sandbox);
    let stub = Stub::start(METRICS_BODY);
    let run = sandbox.run(&shapes_args(&[claude], &["--no-daemon"]), &stub.url);
    let seen = stub.finish();
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert_eq!(
        seen.connections, 0,
        "--no-daemon must not contact the daemon: {seen:?}"
    );
    let skipped = skipped_section(&run.stdout)
        .unwrap_or_else(|| panic!("no `Skipped sources` heading; stdout:\n{}", run.stdout));
    assert!(
        skipped.to_lowercase().contains("metrics"),
        "the metrics source must be listed as skipped:\n{skipped}"
    );
}

/// Contract: when the daemon is unreachable (a closed port, an empty GOBBY_HOME) and
/// `--no-daemon` is not passed, the metrics route is listed under `Skipped sources`, the
/// transcript counts still print, and the exit is 0.
#[test]
fn unreachable_daemon_is_listed_as_skipped_and_exits_zero() {
    let sandbox = Sandbox::new();
    let (claude, _codex) = write_fixture(&sandbox);
    let run = sandbox.run(&shapes_args(&[claude], &[]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let skipped = skipped_section(&run.stdout)
        .unwrap_or_else(|| panic!("no `Skipped sources` heading; stdout:\n{}", run.stdout));
    assert!(
        skipped.to_lowercase().contains("metrics"),
        "the unreachable metrics route must be listed as skipped:\n{skipped}"
    );
    let text = tool_text(&run.stdout, "read_note", &LABELS);
    assert!(
        has_count(text, 6),
        "the transcript counts must still print:\n{text}"
    );
}

/// Contract: output is deterministic. Two runs over the same roots print identical stdout and
/// stderr, and tools appear sorted by name.
#[test]
fn output_is_deterministic_and_sorted_by_tool_name() {
    let sandbox = Sandbox::new();
    let (claude, codex) = write_fixture(&sandbox);
    let args = shapes_args(&[claude, codex], &["--no-daemon"]);
    let url = closed_url();
    let first = sandbox.run(&args, &url);
    let second = sandbox.run(&args, &url);
    assert_eq!(first.code, 0, "exit code; stderr: {}", first.stderr);
    assert_eq!(
        first.stdout, second.stdout,
        "stdout differs between two runs"
    );
    assert_eq!(
        first.stderr, second.stderr,
        "stderr differs between two runs"
    );
    let positions: Vec<usize> = LABELS
        .iter()
        .map(|label| {
            first
                .stdout
                .find(label)
                .unwrap_or_else(|| panic!("`{label}` is not listed:\n{}", first.stdout))
        })
        .collect();
    assert!(
        positions.windows(2).all(|pair| pair[0] < pair[1]),
        "tools must appear sorted by name; positions {positions:?} in:\n{}",
        first.stdout
    );
}

/// Contract: with the daemon reachable, its tool metrics route answers, and each tool's
/// `call_count` appears on that tool's line. The route is `GET /api/mcp/tools` (the query is
/// not checked).
#[test]
fn daemon_tool_metrics_counts_appear_in_output() {
    let sandbox = Sandbox::new();
    let empty = sandbox.join("empty-root");
    std::fs::create_dir_all(&empty).expect("empty transcript root");
    let stub = Stub::start(METRICS_BODY);
    let run = sandbox.run(&shapes_args(&[empty], &[]), &stub.url);
    let seen = stub.finish();
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        seen.requests
            .iter()
            .any(|line| request_path(line).starts_with("/api/mcp/tools")),
        "the metrics route was not requested: {seen:?}"
    );
    let label = "metricsrv:probe_tool";
    let text = tool_text(&run.stdout, label, &[label]);
    assert!(
        has_count(text, 42),
        "the daemon's call_count 42 should appear on the `{label}` line:\n{text}"
    );
    if let Some(skipped) = skipped_section(&run.stdout) {
        assert!(
            !skipped.to_lowercase().contains("metrics"),
            "a reachable metrics route must not be listed as skipped:\n{skipped}"
        );
    }
}

/// A non-loopback daemon endpoint is refused before any request is built. A request that went out
/// would wait out the daemon timeout and list `unreachable`, so the refusal line together with the
/// fast return shows that no request was sent.
#[test]
fn remote_daemon_endpoint_is_refused_before_any_request() {
    let sandbox = Sandbox::new();
    let empty = sandbox.join("empty-root");
    std::fs::create_dir_all(&empty).expect("empty transcript root");
    let started = Instant::now();
    let run = sandbox.run(&shapes_args(&[empty], &[]), "http://192.0.2.1:9");
    let elapsed = started.elapsed();
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let skipped = skipped_section(&run.stdout)
        .unwrap_or_else(|| panic!("no `Skipped sources` heading; stdout:\n{}", run.stdout));
    assert!(
        skipped.contains("remote daemon endpoint refused"),
        "a remote endpoint must be refused and listed as skipped:\n{skipped}"
    );
    assert!(
        elapsed < Duration::from_secs(4),
        "a refused endpoint must not wait on the network; the run took {elapsed:?}"
    );
}

/// Calls behind a symlink below the root are not counted; the root's own calls still are.
#[cfg(unix)]
#[test]
fn symlinks_below_the_root_are_not_followed() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("claude-projects");
    let outside = sandbox.join("outside");
    write_lines(
        &root.join("own/session.jsonl"),
        &[record(&claude_call("root_tool", json!({"kept": 1})))],
    );
    write_lines(
        &outside.join("dir/session.jsonl"),
        &[record(&claude_call(
            "linked_dir_tool",
            json!({"hidden": 1}),
        ))],
    );
    write_lines(
        &outside.join("file.jsonl"),
        &[record(&claude_call(
            "linked_file_tool",
            json!({"hidden": 1}),
        ))],
    );
    std::os::unix::fs::symlink(outside.join("dir"), root.join("linked-dir"))
        .expect("link to a directory");
    std::os::unix::fs::symlink(outside.join("file.jsonl"), root.join("linked-file.jsonl"))
        .expect("link to a file");
    let run = sandbox.run(&shapes_args(&[root], &[]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        run.stdout.contains("root_tool"),
        "the root's own calls must count:\n{}",
        run.stdout
    );
    for hidden in ["linked_dir_tool", "linked_file_tool"] {
        assert!(
            !run.stdout.contains(hidden),
            "`{hidden}` sits behind a symlink below the root and must not be counted:\n{}",
            run.stdout
        );
    }
}

/// The root named through a symlink is still scanned: only links below the root are skipped.
#[cfg(unix)]
#[test]
fn a_symlinked_root_is_still_scanned() {
    let sandbox = Sandbox::new();
    let real = sandbox.join("real-projects");
    write_lines(
        &real.join("session.jsonl"),
        &[record(&claude_call("root_link_tool", json!({"kept": 1})))],
    );
    let link = sandbox.join("linked-root");
    std::os::unix::fs::symlink(&real, &link).expect("link to the root");
    let run = sandbox.run(&shapes_args(&[link], &[]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let text = tool_text(&run.stdout, "root_link_tool", &["root_link_tool"]);
    assert!(
        has_count(text, 1),
        "the root named through a symlink must be scanned:\n{text}"
    );
    let skipped = skipped_section(&run.stdout).unwrap_or("");
    assert!(
        !skipped.contains("symlink"),
        "the root itself is followed, so no symlink line may be listed:\n{}",
        run.stdout
    );
}

/// Keys that are not key-shaped are counted, never printed. A 64-character key still prints.
#[test]
fn argument_keys_that_are_not_identifiers_are_counted_not_printed() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("keys-root");
    let longest = "k".repeat(64);
    let too_long = "x".repeat(65);
    let mut input = serde_json::Map::new();
    input.insert("plainKey".to_owned(), json!(1));
    input.insert(longest.clone(), json!(2));
    input.insert("a key with spaces SECRET-DICT".to_owned(), json!(3));
    input.insert("1starts_with_digit".to_owned(), json!(4));
    input.insert(too_long.clone(), json!(5));
    write_lines(
        &root.join("session.jsonl"),
        &[record(&claude_call("shape_probe", Value::Object(input)))],
    );
    let run = sandbox.run(&shapes_args(&[root], &[]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let text = tool_text(&run.stdout, "shape_probe", &["shape_probe"]);
    let expected = format!("{longest}, plainKey, +3 other keys");
    assert!(
        text.contains(&expected),
        "key-shaped keys print and the rest are counted:\n{text}"
    );
    assert!(
        !run.stdout.contains("SECRET-DICT"),
        "a content key must not be printed:\n{}",
        run.stdout
    );
    assert!(
        !run.stdout.contains("starts_with_digit"),
        "a key that starts with a digit must not be printed:\n{}",
        run.stdout
    );
    assert!(
        !run.stdout.contains(&too_long),
        "a key over 64 characters must not be printed:\n{}",
        run.stdout
    );
}

/// Tool labels that are not name-shaped count together under one `(unnamed)` line.
#[test]
fn tool_labels_that_are_not_name_shaped_count_under_one_unnamed_line() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("labels-root");
    let lines = [
        record(&claude_call("bad name SECRET-LABEL", json!({"a": 1}))),
        record(&claude_call("", json!({}))),
        record(&proxy_call("docs", "look up SECRET-LABEL-2", json!({}))),
        record(&claude_call("ok_tool", json!({"x": 1}))),
    ];
    write_lines(&root.join("session.jsonl"), &lines);
    let run = sandbox.run(&shapes_args(&[root], &[]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        !run.stdout.contains("SECRET-LABEL"),
        "a label that is not name-shaped must not be printed:\n{}",
        run.stdout
    );
    let unnamed: Vec<&str> = run
        .stdout
        .lines()
        .filter(|line| line.starts_with("(unnamed)"))
        .collect();
    assert_eq!(
        unnamed.len(),
        1,
        "the unnamed labels must share one line:\n{}",
        run.stdout
    );
    let columns: Vec<&str> = unnamed[0].split_whitespace().collect();
    assert_eq!(
        columns.get(1).copied(),
        Some("3"),
        "three unnamed calls:\n{}",
        run.stdout
    );
    assert!(
        run.stdout.contains("ok_tool"),
        "a name-shaped tool must still be listed:\n{}",
        run.stdout
    );
}

/// A daemon tool name that is not name-shaped counts under the same `(unnamed)` line.
#[test]
fn daemon_labels_that_are_not_name_shaped_count_under_one_unnamed_line() {
    let sandbox = Sandbox::new();
    let empty = sandbox.join("empty-root");
    std::fs::create_dir_all(&empty).expect("empty transcript root");
    let stub = Stub::start(UNNAMED_METRICS_BODY);
    let run = sandbox.run(&shapes_args(&[empty], &[]), &stub.url);
    stub.finish();
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    assert!(
        !run.stdout.contains("SECRET-METRIC"),
        "a daemon label that is not name-shaped must not be printed:\n{}",
        run.stdout
    );
    let row = run
        .stdout
        .lines()
        .find(|line| line.starts_with("(unnamed)"))
        .unwrap_or_else(|| panic!("no `(unnamed)` line; stdout:\n{}", run.stdout));
    let columns: Vec<&str> = row.split_whitespace().collect();
    assert_eq!(
        columns.get(2).copied(),
        Some("5"),
        "the daemon's count must land on the unnamed line:\n{row}"
    );
}

/// Argument keys that are not key-shaped, because they hold a dot or a hyphen, are counted and
/// never printed. Only the plain key prints.
#[test]
fn argument_keys_with_dots_or_hyphens_are_counted_not_printed() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("dotted-keys-root");
    let mut input = serde_json::Map::new();
    input.insert("plainKey".to_owned(), json!(1));
    input.insert("notes-for-alice.txt".to_owned(), json!(2));
    input.insert("api.internal.example.com".to_owned(), json!(3));
    input.insert("deadbeef-0000-4000-8000-000000000000".to_owned(), json!(4));
    write_lines(
        &root.join("session.jsonl"),
        &[record(&claude_call("key_probe", Value::Object(input)))],
    );
    let run = sandbox.run(&shapes_args(&[root], &["--no-daemon"]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let text = tool_text(&run.stdout, "key_probe", &["key_probe"]);
    assert!(
        text.contains("plainKey, +3 other keys"),
        "only the plain key prints and the three others are counted:\n{text}"
    );
    for hidden in [
        "notes-for-alice.txt",
        "api.internal.example.com",
        "deadbeef-0000-4000-8000-000000000000",
    ] {
        assert!(
            !run.stdout.contains(hidden),
            "key `{hidden}` is not key-shaped and must not be printed:\n{}",
            run.stdout
        );
    }
}

/// A dot is allowed in a tool label part after its first character, as in a dotted tool name, so
/// dotted labels print under their own names. A label outside that shape still counts under the
/// shared `(unnamed)` line and is not printed: a space, `@`, `/`, a leading digit or dot, non-ASCII
/// text, more than one `:`, or a part over 64 bytes.
#[test]
fn dotted_tool_labels_count_under_unnamed_and_are_not_printed() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("dotted-labels-root");
    let edge = format!("a{}", "b".repeat(63));
    let folded = [
        "a@b.probe".to_owned(),
        "probe/with.slash".to_owned(),
        "1st.probe".to_owned(),
        ".leading.probe".to_owned(),
        "caf\u{e9}.probe".to_owned(),
        "a:b:c".to_owned(),
        format!("a{}", "b".repeat(64)),
    ];
    let mut lines = vec![
        record(&claude_call("tools.dotted_probe", json!({"a": 1}))),
        record(&proxy_call("some-srv", "dotted.probe", json!({"b": 1}))),
        record(&claude_call(&edge, json!({"c": 1}))),
    ];
    lines.extend(
        folded
            .iter()
            .map(|label| record(&claude_call(label, json!({"d": 1})))),
    );
    write_lines(&root.join("session.jsonl"), &lines);
    let run = sandbox.run(&shapes_args(&[root], &["--no-daemon"]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let row = |label: &str| {
        run.stdout
            .lines()
            .find(|line| line.starts_with(label))
            .unwrap_or_else(|| panic!("no `{label}` line; stdout:\n{}", run.stdout))
    };
    for printed in ["tools.dotted_probe", "some-srv:dotted.probe", edge.as_str()] {
        let columns: Vec<&str> = row(printed).split_whitespace().collect();
        assert_eq!(
            columns.get(1).copied(),
            Some("1"),
            "dotted label `{printed}` is name-shaped and must print under its own name:\n{}",
            run.stdout
        );
    }
    for hidden in &folded {
        assert!(
            !run.stdout.contains(hidden.as_str()),
            "label `{hidden}` is outside the name shape and must not be printed:\n{}",
            run.stdout
        );
    }
    let columns: Vec<&str> = row("(unnamed)").split_whitespace().collect();
    assert_eq!(
        columns.get(1).copied(),
        Some("7"),
        "the seven labels outside the name shape must count under the unnamed line:\n{}",
        run.stdout
    );
}

/// Guard, green before and after the label rule: a hyphen is allowed inside a label part, so a
/// hyphenated server name and an `mcp__<server>__<tool>` name still print under their own labels.
#[test]
fn hyphenated_server_and_mcp_style_names_still_print() {
    let sandbox = Sandbox::new();
    let root = sandbox.join("guard-labels-root");
    let lines = [
        record(&proxy_call("some-srv", "some_tool", json!({"k": 1}))),
        record(&claude_call("mcp__some-server__tool", json!({"k": 1}))),
    ];
    write_lines(&root.join("session.jsonl"), &lines);
    let run = sandbox.run(&shapes_args(&[root], &["--no-daemon"]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let labels = ["mcp__some-server__tool", "some-srv:some_tool"];
    for label in labels {
        let text = tool_text(&run.stdout, label, &labels);
        assert!(
            has_count(text, 1),
            "`{label}` should print and count 1:\n{text}"
        );
    }
}

/// Symlinks below a root are not followed. Each root that holds some reports its own count on one
/// `Skipped sources` line; a root with none gets no line. Link targets are never printed.
#[cfg(unix)]
#[test]
fn symlinks_below_each_root_are_counted_per_root_in_skipped_sources() {
    let sandbox = Sandbox::new();
    let outside = sandbox.join("outside");
    write_lines(
        &outside.join("target/session.jsonl"),
        &[record(&claude_call("hidden_probe", json!({"x": 1})))],
    );
    write_lines(
        &outside.join("target.jsonl"),
        &[record(&claude_call("hidden_probe", json!({"x": 1})))],
    );
    let alpha = sandbox.join("alpha-root");
    write_lines(
        &alpha.join("own/session.jsonl"),
        &[record(&claude_call("alpha_probe", json!({"x": 1})))],
    );
    std::os::unix::fs::symlink(outside.join("target"), alpha.join("linked-dir"))
        .expect("link to a directory");
    std::os::unix::fs::symlink(
        outside.join("target.jsonl"),
        alpha.join("linked-file.jsonl"),
    )
    .expect("link to a file");
    let beta = sandbox.join("beta-root");
    write_lines(
        &beta.join("session.jsonl"),
        &[record(&claude_call("beta_probe", json!({"x": 1})))],
    );
    std::os::unix::fs::symlink(outside.join("target"), beta.join("linked-dir"))
        .expect("link to a directory");
    let gamma = sandbox.join("gamma-root");
    write_lines(
        &gamma.join("session.jsonl"),
        &[record(&claude_call("gamma_probe", json!({"x": 1})))],
    );
    let roots = [alpha.clone(), beta.clone(), gamma.clone()];
    let run = sandbox.run(&shapes_args(&roots, &["--no-daemon"]), &closed_url());
    assert_eq!(run.code, 0, "exit code; stderr: {}", run.stderr);
    let skipped = skipped_section(&run.stdout)
        .unwrap_or_else(|| panic!("no `Skipped sources` heading; stdout:\n{}", run.stdout));
    let alpha_line = format!("{}: 2 symlinks not followed", alpha.display());
    let beta_line = format!("{}: 1 symlink not followed", beta.display());
    assert!(
        skipped.lines().any(|line| line.trim() == alpha_line),
        "the first root holds two links and needs its own line `{alpha_line}`:\n{skipped}"
    );
    assert!(
        skipped.lines().any(|line| line.trim() == beta_line),
        "the second root holds one link and needs its own line `{beta_line}`:\n{skipped}"
    );
    assert!(
        !skipped.contains(&gamma.display().to_string()),
        "a root with no symlinks must get no line:\n{skipped}"
    );
    assert!(
        !run.stdout.contains("hidden_probe"),
        "calls behind links must not be counted:\n{}",
        run.stdout
    );
    for hidden in ["outside", "linked-"] {
        assert!(
            !run.stdout.contains(hidden),
            "`{hidden}` is a link target or link name and must not be printed:\n{}",
            run.stdout
        );
    }
}
