//! `gport start <chunk id>` contract tests (module `start` of the `suite` program).
//!
//! Each test serves canned daemon JSON from a stub HTTP server on 127.0.0.1, points
//! `GOBBY_DAEMON_URL` at it, and runs the built `gport` in a temp git repository whose
//! `docs/port/map-v2.json` holds fixture chunks, committed on `0.9.0`.
//!
//! Daemon shapes served, taken from `src/gobby/servers/routes/tasks.py` and
//! `src/gobby/storage/tasks/_models.py`:
//! - `GET /api/tasks/%23<seq>` and `GET /api/tasks/<uuid>` return the full record (`to_dict`):
//!   `ref`, `id`, `seq_num`, `title`, `description`, `parent_task_id`, top-level
//!   `closed_reason` and `closed_commit_sha`, and `state` (`name`, `is_closed`,
//!   `closed_reason`, `closed_commit_sha`). A missing task is a 404 with `{"detail": ...}`.
//! - `GET /api/tasks?parent_task_id=<uuid>&limit=N&offset=N` returns
//!   `{"tasks": [...], "total", "stats", "limit", "offset"}`. Each row is a brief one
//!   (`to_brief`): no `description`, no top-level `closed_reason` or `closed_commit_sha`,
//!   though its `state` object still carries both. `limit` is 1 to 1000 (default 50),
//!   and `total` counts every match, not the page.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{Map, Value, json};
use tempfile::TempDir;

use crate::support::{Repo, Run, gport_env};

const EPIC_SEQ: u64 = 21542;
const PROJECT_ID: &str = "00000000-0000-4000-8000-0000000000aa";
const SEQ_ALPHA: u64 = 21601;
const SEQ_BETA: u64 = 21602;
const SEQ_CHECK: u64 = 21603;
const SEQ_TARGET: u64 = 21604;
const SEQ_DECOY: u64 = 21605;

// ---------------------------------------------------------------------------
// Stub daemon
// ---------------------------------------------------------------------------

/// One task in the stub daemon. Children of the epic carry `parent` = the epic's id.
#[derive(Clone)]
struct Task {
    seq: u64,
    title: String,
    description: String,
    parent: Option<String>,
    closed: Option<Closed>,
}

#[derive(Clone)]
struct Closed {
    reason: String,
    commit: Option<String>,
}

impl Task {
    fn closed_as(mut self, reason: &str, commit: Option<&str>) -> Self {
        self.closed = Some(Closed {
            reason: reason.to_owned(),
            commit: commit.map(str::to_owned),
        });
        self
    }
}

fn id_of(seq: u64) -> String {
    format!("00000000-0000-4000-8000-{seq:012}")
}

fn epic() -> Task {
    Task {
        seq: EPIC_SEQ,
        title: "Gobby 1.0 port".to_owned(),
        description: "Epic fixture.".to_owned(),
        parent: None,
        closed: None,
    }
}

/// A child of the epic, open, with the given description.
fn child(seq: u64, title: &str, description: &str) -> Task {
    Task {
        seq,
        title: title.to_owned(),
        description: description.to_owned(),
        parent: Some(id_of(EPIC_SEQ)),
        closed: None,
    }
}

/// The brief heading a chunk's task carries: `# <chunk id>: <title>`.
fn brief(chunk: &str, title: &str) -> String {
    format!("# {chunk}: {title}\n\nFixture brief for {chunk}.")
}

/// A child of the epic whose brief names `chunk` and whose title is `title`.
fn chunk_task(seq: u64, chunk: &str, title: &str) -> Task {
    child(seq, title, &brief(chunk, title))
}

/// The target chunk's own task, open, titled like its map entry.
fn target_task() -> Task {
    chunk_task(SEQ_TARGET, "FX-TARGET", "Target chunk")
}

fn state_json(task: &Task) -> Value {
    let closed = task.closed.as_ref();
    let name = if closed.is_some() { "closed" } else { "open" };
    json!({
        "name": name,
        "is_closed": closed.is_some(),
        "is_claimed": false,
        "is_blocked": false,
        "is_escalated": false,
        "hold_state": null,
        "closed_reason": closed.map(|c| c.reason.clone()),
        "closed_commit_sha": closed.and_then(|c| c.commit.clone()),
    })
}

fn full_json(task: &Task) -> Value {
    json!({
        "ref": format!("#{}", task.seq),
        "id": id_of(task.seq),
        "seq_num": task.seq,
        "project_id": PROJECT_ID,
        "title": task.title,
        "description": task.description,
        "parent_task_id": task.parent,
        "state": state_json(task),
        "priority": 2,
        "task_type": "task",
        "closed_reason": task.closed.as_ref().map(|c| c.reason.clone()),
        "closed_commit_sha": task.closed.as_ref().and_then(|c| c.commit.clone()),
        "closed_at": null,
        "labels": null,
        "commits": null,
        "category": null,
    })
}

fn brief_json(task: &Task) -> Value {
    json!({
        "ref": format!("#{}", task.seq),
        "id": id_of(task.seq),
        "seq_num": task.seq,
        "title": task.title,
        "state": state_json(task),
        "priority": 2,
        "task_type": "task",
        "parent_task_id": task.parent,
        "path_cache": null,
        "category": null,
    })
}

/// A stub daemon on 127.0.0.1 with an OS-assigned port. Its accept loop runs on a background
/// thread for the life of the test process.
struct Stub {
    url: String,
    requests: Arc<Mutex<Vec<String>>>,
}

impl Stub {
    fn serve(tasks: Vec<Task>) -> Self {
        Self::serve_with(tasks, Paging::HONEST)
    }

    fn serve_with(tasks: Vec<Task>, paging: Paging) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub daemon");
        let port = listener.local_addr().expect("stub address").port();
        let tasks = Arc::new(tasks);
        let requests = Arc::new(Mutex::new(Vec::new()));
        let log = Arc::clone(&requests);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let tasks = Arc::clone(&tasks);
                let log = Arc::clone(&log);
                thread::spawn(move || handle(stream, &tasks, paging, &log));
            }
        });
        Self {
            url: format!("http://127.0.0.1:{port}"),
            requests,
        }
    }

    /// How many `GET /api/tasks` calls asked for children of some parent.
    fn child_list_calls(&self) -> usize {
        let log = self.requests.lock().expect("request log");
        log.iter().filter(|target| is_child_listing(target)).count()
    }
}

/// Whether a request target lists the children of some parent.
fn is_child_listing(target: &str) -> bool {
    target.starts_with("/api/tasks?") && target.contains("parent_task_id=")
}

/// How the stub answers `GET /api/tasks?parent_task_id=...`.
#[derive(Clone, Copy)]
enum Paging {
    /// Honours `offset` and `limit`, returns at most `max_rows` rows per page, and reports
    /// `total` as the number of children when `report_total` is set.
    Honest { max_rows: usize, report_total: bool },
    /// Ignores `offset` and `limit`. Every listing returns the first `rows` children in fixture
    /// order, so the daemon repeats itself. `total` is reported only when it is given.
    Repeating { rows: usize, total: Option<u64> },
}

impl Paging {
    /// Honours `offset` and `limit` with no row clamp, and reports `total`.
    const HONEST: Self = Self::Honest {
        max_rows: 1000,
        report_total: true,
    };
}

fn handle(stream: TcpStream, tasks: &[Task], paging: Paging, log: &Mutex<Vec<String>>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let Ok(read_half) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(read_half);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
        return;
    }
    loop {
        let mut header = String::new();
        match reader.read_line(&mut header) {
            Ok(0) | Err(_) => break,
            Ok(_) if header.trim_end().is_empty() => break,
            Ok(_) => {}
        }
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default();
    let target = parts.next().unwrap_or_default().to_owned();
    log.lock().expect("request log").push(target.clone());
    let (status, body) = route(method, &target, tasks, paging);
    let body = body.to_string();
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        405 => "Method Not Allowed",
        _ => "Unprocessable Entity",
    };
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let mut stream = stream;
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

fn route(method: &str, target: &str, tasks: &[Task], paging: Paging) -> (u16, Value) {
    if method != "GET" {
        return (405, json!({"detail": "Method Not Allowed"}));
    }
    let (raw_path, raw_query) = target.split_once('?').unwrap_or((target, ""));
    let path = pct_decode(raw_path);
    if path == "/api/tasks" {
        return list(tasks, &parse_query(raw_query), paging);
    }
    if let Some(reference) = path.strip_prefix("/api/tasks/") {
        return match find(tasks, reference) {
            Some(task) => (200, full_json(task)),
            None => (
                404,
                json!({"detail": format!("Task not found: {reference}")}),
            ),
        };
    }
    (404, json!({"detail": "Not Found"}))
}

/// Resolve `#<seq>` (raw or percent-decoded) or a task id.
fn find<'a>(tasks: &'a [Task], reference: &str) -> Option<&'a Task> {
    match reference.strip_prefix('#') {
        Some(number) => {
            let seq: u64 = number.parse().ok()?;
            tasks.iter().find(|task| task.seq == seq)
        }
        None => tasks.iter().find(|task| id_of(task.seq) == reference),
    }
}

/// The list route: filter by `parent_task_id` when given, then answer the page the paging mode
/// allows. Children keep their fixture order, so the test controls which page holds each one.
fn list(tasks: &[Task], query: &HashMap<String, String>, paging: Paging) -> (u16, Value) {
    let limit = match page_number(query, "limit", 50, 1, 1000) {
        Ok(value) => value,
        Err(detail) => return (422, json!({"detail": detail})),
    };
    let offset = match page_number(query, "offset", 0, 0, usize::MAX) {
        Ok(value) => value,
        Err(detail) => return (422, json!({"detail": detail})),
    };
    let rows: Vec<&Task> = tasks
        .iter()
        .filter(|task| match query.get("parent_task_id") {
            Some(parent) => task.parent.as_deref() == Some(parent.as_str()),
            None => true,
        })
        .collect();
    let (page, total): (Vec<Value>, Option<u64>) = match paging {
        Paging::Honest {
            max_rows,
            report_total,
        } => {
            let page: Vec<Value> = rows
                .iter()
                .skip(offset)
                .take(limit.min(max_rows))
                .map(|task| brief_json(task))
                .collect();
            let total = u64::try_from(rows.len()).ok().filter(|_| report_total);
            (page, total)
        }
        Paging::Repeating { rows: count, total } => {
            let page: Vec<Value> = rows
                .iter()
                .take(count)
                .map(|task| brief_json(task))
                .collect();
            (page, total)
        }
    };
    let mut body = json!({"tasks": page, "stats": {}, "limit": limit, "offset": offset});
    if let Some(total) = total {
        body["total"] = json!(total);
    }
    (200, body)
}

fn page_number(
    query: &HashMap<String, String>,
    key: &str,
    default: usize,
    min: usize,
    max: usize,
) -> Result<usize, String> {
    let Some(raw) = query.get(key) else {
        return Ok(default);
    };
    match raw.parse::<usize>() {
        Ok(value) if (min..=max).contains(&value) => Ok(value),
        _ => Err(format!("{key} must be an integer from {min} to {max}")),
    }
}

fn parse_query(raw: &str) -> HashMap<String, String> {
    raw.split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            (pct_decode(key), pct_decode(value))
        })
        .collect()
}

fn pct_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let escaped = if bytes[index] == b'%' && index + 2 < bytes.len() {
            std::str::from_utf8(&bytes[index + 1..index + 3])
                .ok()
                .and_then(|hex| u8::from_str_radix(hex, 16).ok())
        } else {
            None
        };
        match escaped {
            Some(byte) => {
                out.push(byte);
                index += 3;
            }
            None => {
                out.push(bytes[index]);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A loopback port nothing listens on: bind, note the port, then close the listener.
fn closed_port_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind probe");
    let port = listener.local_addr().expect("probe address").port();
    drop(listener);
    format!("http://127.0.0.1:{port}")
}

// ---------------------------------------------------------------------------
// Fixture map and repository
// ---------------------------------------------------------------------------

fn chunk(id: &str, title: &str, kind: &str, depends_on: &[&str]) -> Value {
    json!({
        "id": id,
        "title": title,
        "kind": kind,
        "goal": "Fixture chunk.",
        "ledger_selectors": [],
        "engines": [],
        "depends_on": depends_on,
        "oracle": "Fixture oracle.",
        "size": "S",
        "decision_refs": [],
        "seam_gaps": [],
        "tree_nodes": [],
        "python_edits": "None.",
        "registration": "None.",
        "risks": "None.",
    })
}

/// The map as a file: every top-level key of the real map, then the chunks.
fn map_text(chunks: Vec<Value>) -> String {
    let mut top = Map::new();
    for key in [
        "angle",
        "branching",
        "cutover_model",
        "json_path",
        "live_checkout_constraints",
        "python_strategy",
        "report_path",
        "rollback_and_packaging",
        "summary",
    ] {
        top.insert(key.to_owned(), json!("Fixture."));
    }
    for key in [
        "answers_25341",
        "assumptions",
        "chunk_protocol",
        "coverage_selfcheck",
        "decisions",
        "open_problems",
        "start_gate",
    ] {
        top.insert(key.to_owned(), json!([]));
    }
    top.insert("chunks".to_owned(), Value::Array(chunks));
    serde_json::to_string_pretty(&Value::Object(top)).expect("map json")
}

/// Chunks `FX-ALPHA` (engine), `FX-BETA` (engine), `FX-CHECK` (checkpoint), `FX-TARGET`
/// (engine, depending on `target_deps`) and `FX-ORPHAN` (engine, depending on `FX-ALPHA`).
fn standard_map(target_deps: &[&str]) -> String {
    map_text(vec![
        chunk("FX-ALPHA", "Alpha chunk", "engine", &[]),
        chunk("FX-BETA", "Beta chunk", "engine", &[]),
        chunk("FX-CHECK", "Checkpoint chunk", "checkpoint", &[]),
        chunk("FX-TARGET", "Target chunk", "engine", target_deps),
        chunk("FX-ORPHAN", "Orphan chunk", "engine", &["FX-ALPHA"]),
    ])
}

/// Commit the standard map on `0.9.0`; returns that commit.
fn commit_map(repo: &Repo, target_deps: &[&str]) -> String {
    repo.write("docs/port/map-v2.json", &standard_map(target_deps));
    repo.commit("fixture: chunk map")
}

/// A commit on a side branch off `from`, with `0.9.0` checked out again afterwards.
fn side_commit(repo: &Repo, from: &str) -> String {
    repo.git(&["checkout", "-q", "-b", "side-work", from]);
    let sha = repo.commit("side work");
    repo.git(&["checkout", "-q", "0.9.0"]);
    sha
}

/// Run `gport start <chunk>` against the daemon at `daemon_url`, with an empty GOBBY_HOME so
/// that no token or bootstrap file is read.
fn start(repo: &Repo, daemon_url: &str, chunk: &str) -> Run {
    let home = TempDir::new().expect("temp GOBBY_HOME");
    let home_path = home.path().to_str().expect("utf-8 temp path");
    gport_env(
        repo.path(),
        &["start", chunk],
        &[("GOBBY_DAEMON_URL", daemon_url), ("GOBBY_HOME", home_path)],
    )
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

fn detail(run: &Run) -> String {
    format!(
        "exit {}\nstdout:\n{}\nstderr:\n{}",
        run.code, run.stdout, run.stderr
    )
}

fn assert_exit(run: &Run, code: i32) {
    assert_eq!(run.code, code, "gport start exit code\n{}", detail(run));
}

fn assert_names(run: &Run, id: &str) {
    assert!(
        run.stdout.contains(id),
        "stdout should name {id}\n{}",
        detail(run)
    );
}

fn assert_no_launch_base(run: &Run) {
    assert!(
        !run.stdout.contains("launch base"),
        "a refusal prints no launch base\n{}",
        detail(run)
    );
}

fn assert_launch_base(run: &Run, head: &str) {
    assert!(
        run.stdout.contains(&format!("launch base {head}")),
        "stdout should print `launch base {head}`\n{}",
        detail(run)
    );
}

fn assert_line_per_dependency(run: &Run, deps: &[&str]) {
    for dep in deps {
        assert!(
            run.stdout.lines().any(|line| line.contains(dep)),
            "stdout should have a line for dependency {dep}\n{}",
            detail(run)
        );
    }
}

fn assert_incomplete(run: &Run, received: u64, total: u64) {
    let line = format!("the child listing is incomplete: received {received} of {total} children");
    assert!(
        run.stderr.contains(&line),
        "stderr should say `{line}`\n{}",
        detail(run)
    );
}

fn assert_tree_clean(repo: &Repo) {
    let status = repo.git(&["status", "--porcelain", "--untracked-files=all"]);
    assert!(
        status.is_empty(),
        "start must write no file, but git status shows:\n{status}"
    );
}

// ---------------------------------------------------------------------------
// Contracts
// ---------------------------------------------------------------------------

/// Contract: when every dependency is closed as done with a commit that is an ancestor of
/// `0.9.0`, `gport start` exits 0, prints one line per dependency and `launch base <full
/// 0.9.0 sha>`, and writes no file.
#[test]
fn dependencies_done_and_landed_pass_with_launch_base() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA", "FX-BETA"]);
    let alpha = repo.commit("land alpha");
    let beta = repo.commit("land beta");
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
        chunk_task(SEQ_BETA, "FX-BETA", "Beta chunk").closed_as("done", Some(beta.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-ALPHA", "FX-BETA"]);
    assert_launch_base(&run, &head);
    assert_tree_clean(&repo);
}

/// Contract: a dependency whose kind is `checkpoint` and that is closed as done needs no
/// commit, so the start passes when its `closed_commit_sha` is absent.
#[test]
fn checkpoint_dependency_done_without_commit_passes() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-CHECK"]);
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_CHECK, "FX-CHECK", "Checkpoint chunk").closed_as("done", None),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-CHECK"]);
    assert_launch_base(&run, &head);
    assert_tree_clean(&repo);
}

/// Contract: an open dependency refuses with exit 1, names its chunk id, and prints no
/// launch base.
#[test]
fn open_dependency_refuses_naming_it() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk"),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_no_launch_base(&run);
}

/// Contract: a dependency closed with a reason other than `done` refuses with exit 1 and
/// names its chunk id. Its commit is an ancestor of `0.9.0`, so the reason is the only
/// failure.
#[test]
fn dependency_closed_with_other_reason_refuses() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk")
            .closed_as("superseded", Some(alpha.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_no_launch_base(&run);
}

/// Contract: a dependency whose `closed_commit_sha` exists but is not an ancestor of `0.9.0`
/// (a commit on a side branch) refuses with exit 1 and names its chunk id.
#[test]
fn dependency_commit_off_integration_branch_refuses() {
    let repo = Repo::new();
    let base = commit_map(&repo, &["FX-ALPHA"]);
    let side = side_commit(&repo, &base);
    repo.git(&["cat-file", "-e", side.as_str()]);
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(side.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_no_launch_base(&run);
}

/// Contract: a dependency of a kind other than `checkpoint`, closed as done with no
/// `closed_commit_sha`, refuses with exit 1 and names its chunk id.
#[test]
fn non_checkpoint_dependency_without_commit_refuses() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", None),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_no_launch_base(&run);
}

/// Contract: every failing dependency is printed before exit 1, not only the first one.
#[test]
fn every_failing_dependency_is_reported() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA", "FX-BETA"]);
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk"),
        chunk_task(SEQ_BETA, "FX-BETA", "Beta chunk").closed_as("done", None),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_names(&run, "FX-BETA");
    assert_no_launch_base(&run);
}

/// Contract: the chunk's task is the epic's child whose description starts with
/// `# <chunk id>:`, even when its title differs from the map title. A child titled like the
/// map but briefed for another chunk does not stop the search, so the start passes.
#[test]
fn chunk_task_found_by_heading_when_title_differs() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let stub = Stub::serve(vec![
        epic(),
        child(SEQ_DECOY, "Target chunk", &brief("FX-OTHER", "Other work")),
        child(
            SEQ_TARGET,
            "Retitled target work",
            &brief("FX-TARGET", "Target chunk"),
        ),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-ALPHA"]);
    assert_launch_base(&run, &head);
}

/// Contract: a chunk with no child task whose description starts with `# <chunk id>:` refuses
/// with exit 1 and names the chunk id. Its dependency is healthy, so the missing child is the
/// only failure.
#[test]
fn chunk_without_child_task_refuses_naming_it() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-ORPHAN");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ORPHAN");
    assert_no_launch_base(&run);
}

/// Contract: a chunk id absent from `docs/port/map-v2.json` is refused: exit 1, with the id
/// named on stdout.
#[test]
fn chunk_absent_from_map_refuses_naming_it() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ]);
    let run = start(&repo, &stub.url, "FX-NOT-IN-MAP");
    assert_exit(&run, 1);
    assert_names(&run, "FX-NOT-IN-MAP");
    assert_no_launch_base(&run);
}

/// Contract: a dependency with no child task whose description starts with `# <dep id>:`
/// refuses with exit 1 and names the dependency's chunk id.
#[test]
fn dependency_without_child_task_refuses_naming_it() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve(vec![epic(), target_task()]);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 1);
    assert_names(&run, "FX-ALPHA");
    assert_no_launch_base(&run);
}

/// Contract: children are read across every page of `GET /api/tasks?parent_task_id=...`. The
/// chunk's task and its dependency sit past the first 1,000 children, so the start can only
/// pass if the client follows the paging. The stub honours any legal `limit`.
#[test]
fn children_spread_over_two_pages_are_all_seen() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let mut tasks = vec![epic()];
    tasks.extend((0..1001u64).map(|n| child(30_000 + n, &format!("Filler {n}"), "Filler task.")));
    tasks.push(target_task());
    tasks.push(
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    );
    let stub = Stub::serve(tasks);
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-ALPHA"]);
    assert_launch_base(&run, &head);
    assert!(
        stub.child_list_calls() >= 2,
        "paging needs at least two child list calls, saw {}\n{}",
        stub.child_list_calls(),
        detail(&run)
    );
}

/// Contract: when the daemon cannot be reached, `gport start` exits 2 (an input or transport
/// error), not 1. The precondition runs the same fixture against a live stub and expects exit
/// 0, so this test fails until the verb works, and the exit 2 comes only from the closed port.
#[test]
fn unreachable_daemon_exits_2() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve(vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ]);
    let live = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&live, 0);
    let run = start(&repo, &closed_port_url(), "FX-TARGET");
    assert_exit(&run, 2);
    assert_no_launch_base(&run);
}

/// `count` open children of the epic that no chunk names, each with a fresh sequence number.
fn fillers(count: u64) -> Vec<Task> {
    (0..count)
        .map(|n| child(30_000 + n, &format!("Filler {n}"), "Filler task."))
        .collect()
}

/// Contract: a daemon that clamps each page to 500 rows and reports a total of 1,500 is read in
/// full. The wanted chunk sits at index 1,200, past the second page, so the start passes only if
/// the client keeps paging by the rows it has received: listings at offsets 0, 500 and 1,000.
#[test]
fn children_clamped_to_500_rows_per_page_are_all_seen() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let mut children = fillers(1_500);
    children[0] =
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str()));
    children[1_200] = target_task();
    let mut tasks = vec![epic()];
    tasks.extend(children);
    let stub = Stub::serve_with(
        tasks,
        Paging::Honest {
            max_rows: 500,
            report_total: true,
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-ALPHA"]);
    assert_launch_base(&run, &head);
    assert_eq!(
        stub.child_list_calls(),
        3,
        "listings at offsets 0, 500 and 1000 should read all 1,500 children\n{}",
        detail(&run)
    );
}

/// Contract: a child listing with no numeric total is an input error. The start exits 2 and
/// names the missing total, after exactly one listing.
#[test]
fn listing_without_total_is_an_input_error() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let stub = Stub::serve_with(
        vec![
            epic(),
            target_task(),
            chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk")
                .closed_as("done", Some(alpha.as_str())),
        ],
        Paging::Honest {
            max_rows: 1000,
            report_total: false,
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 2);
    assert!(
        run.stderr.contains("the child listing has no total"),
        "stderr should name the missing total\n{}",
        detail(&run)
    );
    assert_eq!(
        stub.child_list_calls(),
        1,
        "a listing without a total is refused after one request\n{}",
        detail(&run)
    );
    assert_no_launch_base(&run);
}

/// Contract: a daemon that ignores `offset` and returns its one row on every call is stopped by
/// the second listing, which adds no unseen child. The listing is incomplete, so the start exits
/// 2 and says how many of the total arrived.
#[test]
fn offset_ignoring_daemon_repeating_one_row_is_incomplete() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    repo.commit("later work");
    let stub = Stub::serve_with(
        vec![epic(), target_task()],
        Paging::Repeating {
            rows: 1,
            total: Some(1_000_000),
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 2);
    assert_incomplete(&run, 1, 1_000_000);
    assert_eq!(
        stub.child_list_calls(),
        2,
        "the paging should stop at the first repeated page\n{}",
        detail(&run)
    );
    assert_no_launch_base(&run);
    assert_tree_clean(&repo);
}

/// Contract: a daemon that ignores `offset` and returns the same three rows on every call is
/// stopped by the second listing, which adds no unseen child. The listing reports 2,500 but
/// holds three, so the start exits 2 and says three of the 2,500 arrived.
#[test]
fn offset_ignoring_daemon_repeating_three_rows_is_incomplete() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    repo.commit("later work");
    let mut tasks = vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk"),
    ];
    tasks.extend(fillers(2_498));
    let stub = Stub::serve_with(
        tasks,
        Paging::Repeating {
            rows: 3,
            total: Some(2_500),
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 2);
    assert_incomplete(&run, 3, 2_500);
    assert_eq!(
        stub.child_list_calls(),
        2,
        "the paging should stop at the first repeated page\n{}",
        detail(&run)
    );
    assert_no_launch_base(&run);
}

/// Contract: child listing requests stop at the 1,000-request ceiling whatever the total says.
/// The daemon clamps each page to one row and reports 1,500 children, so 1,000 requests reach
/// only 1,000 of them. The start exits 2 after the 1,000th request and makes no further one.
#[test]
fn child_listing_ceiling_stops_at_1000_requests() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let mut tasks = vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ];
    tasks.extend(fillers(1_498));
    let stub = Stub::serve_with(
        tasks,
        Paging::Honest {
            max_rows: 1,
            report_total: true,
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 2);
    assert_incomplete(&run, 1_000, 1_500);
    assert_eq!(
        stub.child_list_calls(),
        1_000,
        "the ceiling is 1,000 child listing requests\n{}",
        detail(&run)
    );
    assert_no_launch_base(&run);
}

/// Contract: a listing that needs exactly the 1,000-request ceiling passes. One row per page and
/// a total of 1,000 take 1,000 requests, and the last of them reaches the total.
#[test]
fn child_listing_exactly_at_the_1000_request_ceiling_passes() {
    let repo = Repo::new();
    commit_map(&repo, &["FX-ALPHA"]);
    let alpha = repo.commit("land alpha");
    repo.commit("later work");
    let head = repo.git(&["rev-parse", "0.9.0"]);
    let mut tasks = vec![
        epic(),
        target_task(),
        chunk_task(SEQ_ALPHA, "FX-ALPHA", "Alpha chunk").closed_as("done", Some(alpha.as_str())),
    ];
    tasks.extend(fillers(998));
    let stub = Stub::serve_with(
        tasks,
        Paging::Honest {
            max_rows: 1,
            report_total: true,
        },
    );
    let run = start(&repo, &stub.url, "FX-TARGET");
    assert_exit(&run, 0);
    assert_line_per_dependency(&run, &["FX-ALPHA"]);
    assert_launch_base(&run, &head);
    assert_eq!(
        stub.child_list_calls(),
        1_000,
        "one row per page over 1,000 children takes 1,000 requests\n{}",
        detail(&run)
    );
}
