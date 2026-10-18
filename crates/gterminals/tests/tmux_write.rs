//! Acceptance tests for the Rust tmux write runtime (#24569, plan 2.11).
//!
//! A recording fake stands in for the tmux binary, so every argv is asserted
//! against the Python oracle (`src/gobby/terminals/tmux_runtime.py`) without a
//! tmux server.

use std::io;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use gobby_terminals::repository::{Backend, Ownership, State, Terminal};
use gobby_terminals::tmux_write::{CommandOutput, TmuxCommands, TmuxWrite};
use gobby_terminals::write::{BoxFuture, Delivery, Kind, Runtime, Stage, Step, WriteFailure};
use serde_json::{Value, json};
use uuid::Uuid;

const SOCKET: &str = "/var/folders/gobby-test/tmux/default";
const PANE: &str = "%7";
const GENERATION_FORMAT: &str = "#{pid}\t#{start_time}";
const FLAGS_FORMAT: &str = "#{cursor_keys_flag}|#{keypad_cursor_flag}|#{bracket_paste_flag}";

/// Recording tmux: answers each generation query from `generations` in order,
/// repeating the last answer, and answers the flag query with no flags set.
struct FakeTmux {
    generations: Vec<String>,
    generation_reads: StdMutex<usize>,
    calls: StdMutex<Vec<Vec<String>>>,
}

impl FakeTmux {
    fn new(generations: &[&str]) -> Arc<Self> {
        Arc::new(Self {
            generations: generations
                .iter()
                .map(|value| (*value).to_owned())
                .collect(),
            generation_reads: StdMutex::new(0),
            calls: StdMutex::new(Vec::new()),
        })
    }

    fn calls(&self) -> Vec<Vec<String>> {
        self.calls.lock().expect("calls lock").clone()
    }
}

impl TmuxCommands for FakeTmux {
    fn run<'a>(
        &'a self,
        argv: &'a [String],
        _timeout: Duration,
    ) -> BoxFuture<'a, io::Result<CommandOutput>> {
        Box::pin(async move {
            self.calls.lock().expect("calls lock").push(argv.to_vec());
            let stdout = match argv.last().map(String::as_str) {
                Some(GENERATION_FORMAT) => {
                    let mut reads = self.generation_reads.lock().expect("reads lock");
                    let index = (*reads).min(self.generations.len() - 1);
                    *reads += 1;
                    format!("{}\n", self.generations[index])
                }
                Some(FLAGS_FORMAT) => "0|0|0\n".to_owned(),
                _ => String::new(),
            };
            Ok(CommandOutput {
                status: 0,
                stdout,
                stderr: String::new(),
            })
        })
    }
}

fn tmux_row(locator: Value) -> Terminal {
    let now: DateTime<Utc> = "2026-10-18T01:00:00Z".parse().expect("fixture timestamp");
    Terminal {
        id: Uuid::new_v4(),
        backend: Backend::Tmux,
        ownership: Ownership::External,
        state: State::Live,
        spawn_key: None,
        machine_id: Uuid::new_v4(),
        project_id: Uuid::new_v4(),
        session_id: None,
        agent_run_id: None,
        locator: Some(locator),
        locator_key: None,
        session_name: Some("work".to_owned()),
        window_id: None,
        title: None,
        host_epoch: None,
        rows: Some(40),
        cols: Some(120),
        attempt_generation: 1,
        attempt_started_at: now,
        process: None,
        unresolved_writes: json!({}),
        automatic_write_quarantined_at: None,
        automatic_write_quarantine_action_key: None,
        created_at: now,
        updated_at: now,
        liveness_at: None,
    }
}

fn locator(server_pid: i64, server_start_time: i64) -> Value {
    json!({
        "socket_path": SOCKET,
        "pane_id": PANE,
        "server_pid": server_pid,
        "server_start_time": server_start_time,
    })
}

fn step(kind: Kind, payload: &str, submit: bool) -> Step {
    Step {
        kind,
        payload: payload.to_owned(),
        submit,
    }
}

async fn run_write(
    generations: &[&str],
    row: &Terminal,
    step: Step,
) -> (Result<Delivery, WriteFailure>, Vec<Vec<String>>) {
    let fake = FakeTmux::new(generations);
    let runtime = TmuxWrite::new(fake.clone());
    let result = runtime.write(row, &step).await;
    let calls = fake.calls();
    for argv in &calls {
        assert_eq!(argv[0], "tmux");
        assert_eq!(argv[1], "-S", "every call addresses a socket: {argv:?}");
        assert_eq!(
            argv[2], SOCKET,
            "every call uses the recorded socket: {argv:?}"
        );
    }
    (result, calls)
}

async fn run_resize(
    generations: &[&str],
    row: &Terminal,
    rows: i32,
    cols: i32,
) -> (Result<(), WriteFailure>, Vec<Vec<String>>) {
    let fake = FakeTmux::new(generations);
    let runtime = TmuxWrite::new(fake.clone());
    let result = runtime.resize(row, rows, cols).await;
    (result, fake.calls())
}

async fn run_release(
    generations: &[&str],
    row: &Terminal,
) -> (Result<(), WriteFailure>, Vec<Vec<String>>) {
    let fake = FakeTmux::new(generations);
    let runtime = TmuxWrite::new(fake.clone());
    let result = runtime.release_size(row).await;
    (result, fake.calls())
}

/// Calls that change state. Generation and flag queries are `display-message`.
fn writes(calls: &[Vec<String>]) -> Vec<Vec<String>> {
    calls
        .iter()
        .filter(|argv| argv[3] != "display-message")
        .cloned()
        .collect()
}

/// Arguments after `tmux -S <socket>`, for each call.
fn args(calls: &[Vec<String>]) -> Vec<Vec<&str>> {
    calls
        .iter()
        .map(|argv| argv[3..].iter().map(String::as_str).collect())
        .collect()
}

/// The paste buffer name is per-call; assert its shape and return it.
fn buffer_name(sent: &[Vec<String>]) -> &str {
    let name = sent[0][5].as_str();
    assert!(name.starts_with("gobby-"), "buffer name shape: {name}");
    name
}

#[tokio::test]
async fn tmux_write_is_socket_and_generation_scoped() {
    let live = locator(4242, 1_700_000_000);
    let row = tmux_row(live);

    // Text with no submit pastes through a named buffer and deletes it.
    let (result, calls) =
        run_write(&["4242\t1700000000"], &row, step(Kind::Text, "hi", false)).await;
    assert_eq!(result, Ok(Delivery::Delivered));
    let sent = writes(&calls);
    let name = buffer_name(&sent);
    assert_eq!(
        args(&sent),
        vec![
            vec!["set-buffer", "-b", name, "--", "hi"],
            vec!["paste-buffer", "-d", "-p", "-b", name, "-t", PANE],
            vec!["delete-buffer", "-b", name],
        ]
    );

    // A named key is sent by tmux key name.
    let (result, calls) =
        run_write(&["4242\t1700000000"], &row, step(Kind::Key, "enter", false)).await;
    assert_eq!(result, Ok(Delivery::Delivered));
    assert_eq!(
        args(&writes(&calls)),
        vec![vec!["send-keys", "-t", PANE, "Enter"]]
    );

    // Raw input is sent as hex bytes, in 512-byte chunks.
    let (result, calls) =
        run_write(&["4242\t1700000000"], &row, step(Kind::Input, "ls", false)).await;
    assert_eq!(result, Ok(Delivery::Delivered));
    assert_eq!(
        args(&writes(&calls)),
        vec![vec!["send-keys", "-t", PANE, "-H", "6c", "73"]]
    );

    // Paste pastes the text through the same named buffer.
    let (result, calls) = run_write(
        &["4242\t1700000000"],
        &row,
        step(Kind::Paste, "pasted", false),
    )
    .await;
    assert_eq!(result, Ok(Delivery::Delivered));
    let sent = writes(&calls);
    let name = buffer_name(&sent);
    assert_eq!(
        args(&sent),
        vec![
            vec!["set-buffer", "-b", name, "--", "pasted"],
            vec!["paste-buffer", "-d", "-p", "-b", name, "-t", PANE],
            vec!["delete-buffer", "-b", name],
        ]
    );

    // Submit re-checks the generation after the Enter delay, then sends Enter.
    let (result, calls) = run_write(
        &["4242\t1700000000", "4242\t1700000000"],
        &row,
        step(Kind::Text, "hi", true),
    )
    .await;
    assert_eq!(result, Ok(Delivery::Delivered));
    let sent = writes(&calls);
    let name = buffer_name(&sent);
    assert_eq!(
        args(&sent),
        vec![
            vec!["set-buffer", "-b", name, "--", "hi"],
            vec!["paste-buffer", "-d", "-p", "-b", name, "-t", PANE],
            vec!["delete-buffer", "-b", name],
            vec!["send-keys", "-t", PANE, "Enter"],
        ]
    );

    // A generation mismatch refuses every kind before any state-changing call.
    for generations in [&["4241\t1700000000"][..], &["4242\t1700000001"][..]] {
        for probe in [
            step(Kind::Text, "hi", false),
            step(Kind::Key, "enter", false),
            step(Kind::Input, "ls", false),
            step(Kind::Paste, "pasted", false),
        ] {
            let (result, calls) = run_write(generations, &row, probe.clone()).await;
            let failure = result.expect_err("mismatched generation refuses");
            assert_eq!(failure.stage, Stage::None, "{probe:?} stage");
            assert!(
                writes(&calls).is_empty(),
                "{probe:?} sent after mismatch: {calls:?}"
            );
        }
    }

    // A mismatch found after the Enter delay refuses at stage partial, with no Enter.
    let (result, calls) = run_write(
        &["4242\t1700000000", "4242\t1700000002"],
        &row,
        step(Kind::Text, "hi", true),
    )
    .await;
    let failure = result.expect_err("mismatch after delay refuses");
    assert_eq!(failure.stage, Stage::Partial);
    let sent = writes(&calls);
    assert!(
        sent.iter().all(|argv| argv[3] != "send-keys"),
        "no Enter after mismatch: {sent:?}"
    );

    // A row with no external tmux socket refuses before any call.
    let mut gobby_owned = tmux_row(locator(4242, 1_700_000_000));
    gobby_owned.ownership = Ownership::Gobby;
    let (result, calls) = run_write(
        &["4242\t1700000000"],
        &gobby_owned,
        step(Kind::Key, "enter", false),
    )
    .await;
    assert_eq!(result.expect_err("non-external refuses").stage, Stage::None);
    assert!(
        calls.is_empty(),
        "no call for a non-external row: {calls:?}"
    );

    let no_socket = tmux_row(
        json!({ "pane_id": PANE, "server_pid": 4242, "server_start_time": 1_700_000_000 }),
    );
    let (result, calls) = run_write(
        &["4242\t1700000000"],
        &no_socket,
        step(Kind::Key, "enter", false),
    )
    .await;
    assert_eq!(
        result.expect_err("missing socket refuses").stage,
        Stage::None
    );
    assert!(calls.is_empty(), "no call without a socket: {calls:?}");
}

#[tokio::test]
async fn tmux_resize_and_release_size_match_python() {
    let row = tmux_row(locator(4242, 1_700_000_000));

    // Matching generation: manual window size, then the target size.
    let (result, calls) = run_resize(&["4242\t1700000000"], &row, 40, 120).await;
    assert_eq!(result, Ok(()));
    assert_eq!(
        args(&writes(&calls)),
        vec![
            vec!["set-option", "-w", "-t", PANE, "window-size", "manual"],
            vec!["resize-window", "-t", PANE, "-x", "120", "-y", "40"],
        ]
    );

    let (result, calls) = run_release(&["4242\t1700000000"], &row).await;
    assert_eq!(result, Ok(()));
    assert_eq!(
        args(&writes(&calls)),
        vec![vec!["set-option", "-wu", "-t", PANE, "window-size"]]
    );

    // A generation mismatch returns Ok without sending, as Python does.
    let (result, calls) = run_resize(&["4241\t1700000000"], &row, 40, 120).await;
    assert_eq!(result, Ok(()));
    assert!(
        writes(&calls).is_empty(),
        "resize sent after mismatch: {calls:?}"
    );

    let (result, calls) = run_release(&["4241\t1700000000"], &row).await;
    assert_eq!(result, Ok(()));
    assert!(
        writes(&calls).is_empty(),
        "release sent after mismatch: {calls:?}"
    );

    // Dimensions outside 1..=1024 refuse before any tmux call.
    let (result, calls) = run_resize(&["4242\t1700000000"], &row, 0, 120).await;
    assert!(result.is_err(), "zero rows refuses");
    assert!(
        calls.is_empty(),
        "no call for invalid dimensions: {calls:?}"
    );
}
