//! The Native write runtime over its own authenticated host connection.

mod common;

use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gobby_terminals::control::PROTOCOL_VERSION;
use gobby_terminals::host::{
    EpochAuthority, EpochBinding, HostFailure, HostOptions, HostSupervisor,
};
use gobby_terminals::native_write::NativeWrite;
use gobby_terminals::repository::Terminal;
use gobby_terminals::write::{
    BatchOp, BatchTarget, Delivery, Kind, Runtime, Stage, Step, WriteFailure,
};
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::task::JoinHandle;
use tokio::time::Instant;

const EPOCH: &str = "epoch-E";
const TOKEN: &str = "fixture-token";
const MACHINE: &str = "10000000-0000-4000-8000-000000000001";

/// What the fixture host does with one request other than hello and ping.
enum Answer {
    Reply(Value),
    Hangup,
}

type Script = Arc<dyn Fn(&Value) -> Answer + Send + Sync>;

#[derive(Debug, Clone)]
struct Logged {
    at: Instant,
    connection: usize,
    request: Value,
}

/// A gterm control socket served in-process beside a live process named
/// gterm, so the opener's pid identity check passes or fails for real.
struct FakeHost {
    dir: TempDir,
    process: Child,
    log: Arc<Mutex<Vec<Logged>>>,
    connections: Arc<AtomicUsize>,
    task: JoinHandle<()>,
}

impl FakeHost {
    fn new(script: impl Fn(&Value) -> Answer + Send + Sync + 'static) -> Self {
        Self::start(Arc::new(script), false)
    }

    fn lying_about_pid(script: impl Fn(&Value) -> Answer + Send + Sync + 'static) -> Self {
        Self::start(Arc::new(script), true)
    }

    fn start(script: Script, bad_pid: bool) -> Self {
        let dir = TempDir::new().unwrap();
        let binary = dir.path().join("gterm");
        std::fs::copy("/bin/sleep", &binary).unwrap();
        #[cfg(target_os = "macos")]
        assert!(
            Command::new("/usr/bin/codesign")
                .args(["--force", "--sign", "-"])
                .arg(&binary)
                .output()
                .unwrap()
                .status
                .success(),
            "identity fixture executable must be signed before launch"
        );
        let process = Command::new(&binary).arg("600").spawn().unwrap();
        let pid = process.id();
        std::fs::write(dir.path().join("gterm.pid"), pid.to_string()).unwrap();
        std::fs::write(dir.path().join("gterm-control.token"), format!("{TOKEN}\n")).unwrap();
        let listener = UnixListener::bind(dir.path().join("gterm-control.sock")).unwrap();
        let log = Arc::new(Mutex::new(Vec::new()));
        let connections = Arc::new(AtomicUsize::new(0));
        let ping_pid = if bad_pid { pid + 1 } else { pid };
        let task = tokio::spawn(serve(
            listener,
            ping_pid,
            script,
            log.clone(),
            connections.clone(),
        ));
        Self {
            dir,
            process,
            log,
            connections,
            task,
        }
    }

    fn options(&self) -> HostOptions {
        HostOptions {
            socket_dir: self.dir.path().to_owned(),
            binary_path: self.dir.path().join("gterm"),
            machine_id: MACHINE.into(),
            health_interval: Duration::from_secs(60),
            ..HostOptions::default()
        }
    }

    fn token_path(&self) -> PathBuf {
        self.dir.path().join("gterm-control.token")
    }

    fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }

    fn logged(&self, method: &str) -> Vec<Logged> {
        self.log
            .lock()
            .unwrap()
            .iter()
            .filter(|entry| entry.request["method"] == method)
            .cloned()
            .collect()
    }

    /// Every `write` request addressed to one host terminal, in arrival order.
    fn writes_to(&self, host_terminal_id: &str) -> Vec<Logged> {
        self.logged("write")
            .into_iter()
            .filter(|entry| entry.request["host_terminal_id"] == host_terminal_id)
            .collect()
    }
}

impl Drop for FakeHost {
    fn drop(&mut self) {
        self.task.abort();
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

async fn serve(
    listener: UnixListener,
    ping_pid: u32,
    script: Script,
    log: Arc<Mutex<Vec<Logged>>>,
    connections: Arc<AtomicUsize>,
) {
    while let Ok((stream, _)) = listener.accept().await {
        let connection = connections.fetch_add(1, Ordering::SeqCst) + 1;
        let (script, log) = (script.clone(), log.clone());
        tokio::spawn(async move {
            let (reader, mut writer) = stream.into_split();
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let request: Value = serde_json::from_str(&line).unwrap();
                log.lock().unwrap().push(Logged {
                    at: Instant::now(),
                    connection,
                    request: request.clone(),
                });
                let mut reply = match request["method"].as_str() {
                    Some("hello") if request["control_token"] == TOKEN => json!({
                        "ok": true, "protocol_version": PROTOCOL_VERSION,
                        "host_epoch": EPOCH, "version": "fixture", "capabilities": [],
                    }),
                    Some("hello") => json!({"ok": false, "error": "invalid_token"}),
                    Some("ping") => json!({
                        "ok": true, "host_epoch": EPOCH, "version": "fixture", "host_pid": ping_pid,
                    }),
                    _ => match script(&request) {
                        Answer::Reply(reply) => reply,
                        Answer::Hangup => return,
                    },
                };
                reply["id"] = request["id"].clone();
                let mut bytes = serde_json::to_vec(&reply).unwrap();
                bytes.push(b'\n');
                if writer.write_all(&bytes).await.is_err() {
                    return;
                }
            }
        });
    }
}

struct NoBindings;

impl EpochAuthority for NoBindings {
    fn bindings<'a>(
        &'a self,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(Vec::new()) })
    }
}

fn ok() -> Answer {
    Answer::Reply(json!({"ok": true, "written": true}))
}

fn refuse(code: &str) -> Answer {
    Answer::Reply(
        json!({"ok": false, "written": false, "error": code, "code": code, "stage": "none"}),
    )
}

/// A Native row on `host_terminal_id` recorded under `epoch`.
fn row(host_terminal_id: Option<&str>, epoch: Option<&str>) -> Terminal {
    let mut terminal = common::terminal();
    terminal.locator = host_terminal_id.map(|id| json!({"host_terminal_id": id}));
    terminal.host_epoch = epoch.map(str::to_owned);
    terminal
}

fn step(kind: Kind, payload: &str, submit: bool) -> Step {
    Step {
        kind,
        payload: payload.into(),
        submit,
    }
}

/// A request's verb fields, without the per-connection id and sequence.
fn body(entry: &Logged) -> Value {
    let mut request = entry.request.clone();
    let object = request.as_object_mut().unwrap();
    object.remove("id");
    object.remove("operation_seq");
    request
}

fn refusal(outcome: Result<Delivery, WriteFailure>) -> (Stage, String) {
    match outcome {
        Err(failure) => (failure.stage, failure.code),
        other => panic!("expected a refusal, got {other:?}"),
    }
}

/// The host refuses or drops each write by the host terminal it names.
fn by_terminal(request: &Value) -> Answer {
    let enter = request["kind"] == "key";
    match request["host_terminal_id"].as_str() {
        Some("busy") => refuse("pty_busy"),
        Some("enter-gone") if enter => refuse("terminal_gone"),
        Some("text-lost") if !enter => Answer::Hangup,
        Some("enter-lost") if enter => Answer::Hangup,
        _ => ok(),
    }
}

#[tokio::test]
async fn native_write_matches_python_stage_semantics() {
    let host = FakeHost::new(by_terminal);
    let runtime = NativeWrite::new(host.options());

    // Text with submit is the text without submit, the Enter delay, then Enter.
    let outcome = runtime
        .write(
            &row(Some("ht-1"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert_eq!(outcome, Ok(Delivery::Delivered));
    let writes = host.writes_to("ht-1");
    assert_eq!(writes.len(), 2, "text then Enter: {writes:?}");
    assert_eq!(
        body(&writes[0]),
        json!({"method": "write", "host_terminal_id": "ht-1", "kind": "text",
            "encoding": "utf8-b64", "data": "eA==", "submit": false})
    );
    assert_eq!(
        body(&writes[1]),
        json!({"method": "write", "host_terminal_id": "ht-1", "kind": "key",
            "encoding": "utf8-b64", "data": "ZW50ZXI="})
    );
    assert!(
        writes[1].at - writes[0].at >= Duration::from_secs(1),
        "Enter must wait the Enter delay"
    );

    // One step of every other shape is one write; only text carries submit.
    for (kind, name, field) in [
        (Kind::Text, "text", Some(false)),
        (Kind::Key, "key", None),
        (Kind::Paste, "paste", None),
        (Kind::Input, "input", None),
    ] {
        let terminal = format!("single-{name}");
        let outcome = runtime
            .write(&row(Some(&terminal), Some(EPOCH)), &step(kind, "x", false))
            .await;
        assert_eq!(outcome, Ok(Delivery::Delivered), "{name}");
        let writes = host.writes_to(&terminal);
        assert_eq!(writes.len(), 1, "{name}: {writes:?}");
        let mut expected = json!({"method": "write", "host_terminal_id": terminal,
            "kind": name, "encoding": "utf8-b64", "data": "eA=="});
        if let Some(submit) = field {
            expected["submit"] = json!(submit);
        }
        assert_eq!(body(&writes[0]), expected, "{name}");
    }

    // A first-write host error refuses with stage none and sends no Enter;
    // a single-step host error is the same typed refusal.
    let outcome = runtime
        .write(
            &row(Some("busy"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert_eq!(refusal(outcome), (Stage::None, "pty_busy".into()));
    let outcome = runtime
        .write(
            &row(Some("busy"), Some(EPOCH)),
            &step(Kind::Paste, "x", false),
        )
        .await;
    assert_eq!(refusal(outcome), (Stage::None, "pty_busy".into()));
    assert_eq!(
        host.writes_to("busy").len(),
        2,
        "no Enter follows a refused text"
    );

    // An Enter-step host error refuses with stage partial.
    let outcome = runtime
        .write(
            &row(Some("enter-gone"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert_eq!(refusal(outcome), (Stage::Partial, "terminal_gone".into()));

    // A connection lost after either send is uncertain, and the next write
    // reopens an authenticated connection.
    let before = host.connections();
    let outcome = runtime
        .write(
            &row(Some("text-lost"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert!(
        matches!(outcome, Ok(Delivery::Uncertain { .. })),
        "{outcome:?}"
    );
    assert_eq!(
        host.writes_to("text-lost").len(),
        1,
        "no Enter after a lost text"
    );
    let outcome = runtime
        .write(
            &row(Some("enter-lost"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert!(
        matches!(outcome, Ok(Delivery::Uncertain { .. })),
        "{outcome:?}"
    );
    assert_eq!(host.writes_to("enter-lost").len(), 2);
    let outcome = runtime
        .write(
            &row(Some("after-loss"), Some(EPOCH)),
            &step(Kind::Key, "enter", false),
        )
        .await;
    assert_eq!(outcome, Ok(Delivery::Delivered));
    assert_eq!(
        host.connections(),
        before + 2,
        "each loss reopens one connection"
    );

    // A row from another epoch, a row without one, and a row without a host
    // terminal id refuse with stage none before any byte.
    for (terminal, epoch, code) in [
        ("stale", Some("epoch-OLD"), "host_epoch_changed"),
        ("unrecorded", None, "host_epoch_changed"),
    ] {
        let outcome = runtime
            .write(&row(Some(terminal), epoch), &step(Kind::Text, "x", true))
            .await;
        assert_eq!(refusal(outcome), (Stage::None, code.into()), "{terminal}");
        assert!(host.writes_to(terminal).is_empty(), "{terminal} sent bytes");
    }
    let writes = host.logged("write").len();
    let outcome = runtime
        .write(&row(None, Some(EPOCH)), &step(Kind::Text, "x", false))
        .await;
    assert_eq!(
        refusal(outcome),
        (Stage::None, "host_terminal_id_missing".into())
    );
    assert_eq!(host.logged("write").len(), writes);

    // An unavailable host refuses with stage none.
    let empty = TempDir::new().unwrap();
    let absent = NativeWrite::new(HostOptions {
        socket_dir: empty.path().to_owned(),
        machine_id: MACHINE.into(),
        ..HostOptions::default()
    });
    let outcome = absent
        .write(
            &row(Some("ht-1"), Some(EPOCH)),
            &step(Kind::Text, "x", true),
        )
        .await;
    assert_eq!(refusal(outcome).0, Stage::None);
}

fn fixture(name: &str) -> Value {
    let path = PathBuf::from(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../gterminal/tests/fixtures/wire_golden"
    ))
    .join(name);
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn target(result_id: &str, terminal: Terminal, operations: &[(Kind, &str, u64)]) -> BatchTarget {
    BatchTarget {
        result_id: result_id.into(),
        terminal,
        operations: operations
            .iter()
            .map(|(kind, payload, delay_ms)| BatchOp {
                kind: *kind,
                payload: (*payload).into(),
                delay_ms: *delay_ms,
            })
            .collect(),
    }
}

/// Each result's id and outcome, in the order returned.
fn outcomes(results: Vec<gobby_terminals::write::BatchResult>) -> Vec<(String, Value)> {
    results
        .into_iter()
        .map(|result| {
            let outcome = match result.outcome {
                Ok(Delivery::Delivered) => json!("delivered"),
                Ok(Delivery::Uncertain { .. }) => json!("uncertain"),
                Err(failure) => json!([format!("{:?}", failure.stage), failure.code]),
            };
            (result.result_id, outcome)
        })
        .collect()
}

/// The host answers a batch by the first target's host terminal.
fn by_first_target(request: &Value) -> Answer {
    match request["targets"][0]["host_terminal_id"].as_str() {
        Some("ht-1") => Answer::Reply(json!({"ok": true, "results": [
            {"recipient_id": "r1", "host_terminal_id": "ht-1", "ok": true, "written": true},
            {"recipient_id": "r2", "host_terminal_id": "ht-2", "ok": false, "written": false,
                "error": "write_queue_unavailable", "stage": "partial"},
            {"recipient_id": "r3", "host_terminal_id": "ht-3", "ok": false,
                "error": "not_found", "stage": "none"},
        ]})),
        Some("upgrading") => Answer::Reply(json!({"ok": false, "error": "host_upgrading"})),
        Some("short") => Answer::Reply(json!({"ok": true, "results": []})),
        _ => Answer::Hangup,
    }
}

#[tokio::test]
async fn native_batch_matches_gterm_write_batch_fixture() {
    let host = FakeHost::new(by_first_target);
    let runtime = NativeWrite::new(host.options());
    let batch = runtime
        .batch()
        .expect("the Native runtime has an atomic batch");
    let live = |id: &str| row(Some(id), Some(EPOCH));

    // The request is the gterm fixture, and each target keeps its result.
    let results = batch
        .write_batch(vec![
            target("r1", live("ht-1"), &[(Kind::Text, "x", 0)]),
            target("r2", live("ht-2"), &[(Kind::Key, "enter", 15)]),
            target("r3", live("ht-3"), &[(Kind::Text, "y", 0)]),
        ])
        .await
        .expect("a delivered batch has a result per target");
    assert_eq!(
        outcomes(results),
        vec![
            ("r1".into(), json!("delivered")),
            ("r2".into(), json!(["Partial", "write_queue_unavailable"])),
            ("r3".into(), json!(["None", "not_found"])),
        ]
    );
    let sent = host.logged("write_batch");
    assert_eq!(sent.len(), 1, "one host request per batch");
    let mut expected = fixture("control_write_batch.json");
    expected["id"] = sent[0].request["id"].clone();
    expected["operation_seq"] = sent[0].request["operation_seq"].clone();
    assert_eq!(sent[0].request, expected);

    // A connection lost after send leaves only the sent target uncertain;
    // targets refused before dispatch keep their stage-none refusal.
    let results = batch
        .write_batch(vec![
            target("unlocated", row(None, Some(EPOCH)), &[(Kind::Text, "x", 0)]),
            target(
                "stale",
                row(Some("stale"), Some("epoch-OLD")),
                &[(Kind::Text, "x", 0)],
            ),
            target("lost", live("lost"), &[(Kind::Text, "x", 0)]),
        ])
        .await
        .expect("a lost batch still reports each target");
    assert_eq!(
        outcomes(results),
        vec![
            (
                "unlocated".into(),
                json!(["None", "host_terminal_id_missing"])
            ),
            ("stale".into(), json!(["None", "host_epoch_changed"])),
            ("lost".into(), json!("uncertain")),
        ]
    );
    let sent = host.logged("write_batch");
    assert_eq!(sent.len(), 2);
    assert_eq!(
        sent[1].request["targets"],
        json!([{"recipient_id": "lost", "host_terminal_id": "lost", "operations": [
            {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0}]}]),
        "refused targets are never sent"
    );

    // A whole-request host refusal refuses every sent target with stage none,
    // and a reply without per-target results leaves them uncertain.
    let results = batch
        .write_batch(vec![
            target("a", live("upgrading"), &[(Kind::Text, "x", 0)]),
            target("b", live("upgrading-2"), &[(Kind::Text, "x", 0)]),
        ])
        .await
        .expect("a refused batch reports each target");
    assert_eq!(
        outcomes(results),
        vec![
            ("a".into(), json!(["None", "host_upgrading"])),
            ("b".into(), json!(["None", "host_upgrading"])),
        ]
    );
    let results = batch
        .write_batch(vec![target("c", live("short"), &[(Kind::Text, "x", 0)])])
        .await
        .expect("an undecodable reply reports each target");
    assert_eq!(outcomes(results), vec![("c".into(), json!("uncertain"))]);
}

#[tokio::test]
async fn open_control_authenticates_an_independent_connection() {
    let host = FakeHost::new(|_| ok());
    let token = std::fs::read(host.token_path()).unwrap();
    let modified = std::fs::metadata(host.token_path())
        .unwrap()
        .modified()
        .unwrap();
    let supervisor = HostSupervisor::new(host.options(), Arc::new(NoBindings));
    supervisor
        .start()
        .await
        .expect("the supervisor adopts the fixture host");

    let runtime = NativeWrite::new(host.options());
    let outcome = runtime
        .write(
            &row(Some("ht-1"), Some(EPOCH)),
            &step(Kind::Text, "x", false),
        )
        .await;
    assert_eq!(outcome, Ok(Delivery::Delivered));
    assert_eq!(
        host.connections(),
        2,
        "the runtime opens its own connection"
    );
    let hellos = host.logged("hello");
    assert_eq!(hellos.len(), 2, "{hellos:?}");
    for hello in &hellos {
        assert_eq!(hello.request["protocol_version"], PROTOCOL_VERSION);
        assert_eq!(hello.request["control_token"], TOKEN);
    }
    let pinged: Vec<usize> = host
        .logged("ping")
        .iter()
        .map(|entry| entry.connection)
        .collect();
    assert!(
        pinged.contains(&hellos[1].connection),
        "the opener verifies the host pid"
    );
    let writes = host.writes_to("ht-1");
    assert_eq!(writes.len(), 1);
    assert_eq!(
        writes[0].connection, hellos[1].connection,
        "the write rides its own connection"
    );
    assert_ne!(hellos[0].connection, hellos[1].connection);
    assert_eq!(
        std::fs::read(host.token_path()).unwrap(),
        token,
        "the token is never rotated"
    );
    assert_eq!(
        std::fs::metadata(host.token_path())
            .unwrap()
            .modified()
            .unwrap(),
        modified
    );
    supervisor.stop().await;

    // A missing token refuses before any write and is never created.
    let tokenless = FakeHost::new(|_| ok());
    std::fs::remove_file(tokenless.token_path()).unwrap();
    let outcome = NativeWrite::new(tokenless.options())
        .write(
            &row(Some("ht-1"), Some(EPOCH)),
            &step(Kind::Text, "x", false),
        )
        .await;
    assert_eq!(refusal(outcome).0, Stage::None);
    assert!(
        !tokenless.token_path().exists(),
        "the opener never creates the token"
    );
    assert!(tokenless.logged("write").is_empty());

    // A host whose ping names another pid is refused before any write.
    let impostor = FakeHost::lying_about_pid(|_| ok());
    let outcome = NativeWrite::new(impostor.options())
        .write(
            &row(Some("ht-1"), Some(EPOCH)),
            &step(Kind::Text, "x", false),
        )
        .await;
    assert_eq!(refusal(outcome).0, Stage::None);
    assert!(impostor.logged("write").is_empty());
}
