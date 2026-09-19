//! The chrome names a terminal, it does not print its primary key.
//!
//! These tests pin the D1 label ladder against a real daemon payload: the name
//! the user gave the pane, then the provider of the session bound to it, then
//! the command in its foreground, then the literal `shell`. The last rung is a
//! literal, which is what makes the guarantee at the bottom of this file
//! possible — no rung can be an id, at any width.
//!
//! `title` is deliberately not a rung, and the rows here carry misleading ones
//! to prove it: the daemon fills `title` from `window_name or pane_title or
//! session_name`, so it is `zsh` for one pane and `75`, `[tmux]` or a whole
//! session banner for the next.

mod mock_daemon;

use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::ui::chrome::{attention_label, Chrome, RowState};
use gobby_client::ui::sidebar::{session_rows, TERMINAL_ROW};
use gobby_client::ui::sidebar_rows::SidebarRow;
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::Instant;

const PROJECT_ID: &str = "project-1";
const AGENT: &str = "7c1dc6ef-5050-476f-86f9-baf787efcbb3";
const SHELL: &str = "fe6f6dc6-6b00-4bf0-b5ac-e69a0b73b80c";
const ABSENT: &str = "910ed674-bb52-404e-bd67-5372f69a9cab";
const SESSION: &str = "8c97a3b9-a98d-4c92-b35b-0fddeb48570f";

/// A live tmux row exactly as `/api/terminals` ships it. `title` is null rather
/// than absent when the pane never reported one, which is the shape the daemon
/// actually sends, and `command` is null for a row the daemon could not probe.
fn tmux_row(terminal_id: &str, title: Value, pane: &str, command: Value) -> Value {
    json!({
        "terminal_id": terminal_id,
        "state": "live",
        "backend": "tmux",
        "title": title,
        "command": command,
        "attach": {
            "backend": "tmux",
            "frame_host_epoch": "host-epoch",
            "host_socket": "/tmp/gobby-frames.sock",
            "host_terminal_id": null,
            "socket_path": "/tmp/tmux-501/default",
            "pane_id": pane,
            "server_pid": 2212,
            "server_start_time": 1788478295,
        }
    })
}

/// A native row: no tmux pane, so the foreground command is the only thing
/// about it the daemon can observe.
fn native_row(terminal_id: &str, command: Value) -> Value {
    json!({
        "terminal_id": terminal_id,
        "state": "live",
        "backend": "native",
        "title": null,
        "command": command,
    })
}

/// The attention-roster entry that lists `terminal_id` in the sessions section.
fn entry(terminal_id: &str, backend: &str) -> Value {
    json!({
        "entry_id": format!("run:{terminal_id}"),
        "terminal": {"terminal_id": terminal_id, "backend": backend},
    })
}

/// The same entry with the provider the daemon resolved for its session, which
/// is rung 2 of the ladder.
fn entry_with_provider(terminal_id: &str, backend: &str, provider: &str) -> Value {
    let mut entry = entry(terminal_id, backend);
    entry["provider"] = json!(provider);
    entry
}

/// Reconcile a roster and an attention roster, then report the sessions rows
/// the sidebar drew and the label each attention entry resolved to.
async fn sidebar(rows: Vec<Value>, attention: Vec<Value>) -> (Vec<SidebarRow>, Vec<String>) {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": rows,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 0, "entries": attention}),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project(PROJECT_ID);
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("roster reconcile");

    let chrome = Chrome::dark();
    let drawn = session_rows(&workspace, &chrome);
    let labels = attention
        .iter()
        .map(|entry| attention_label(&workspace, entry["entry_id"].as_str().expect("entry id")))
        .collect();

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
    (drawn, labels)
}

/// Rung 3, and the rung the daemon had to grow a new mechanism to serve: with
/// no name of its own and no session bound to it, a terminal is called after
/// whatever is running in it. The titles here are what tmux actually reports
/// for such panes, and none of them reaches the row.
#[tokio::test]
async fn the_foreground_command_names_a_terminal_with_no_name_of_its_own() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, json!("75"), "%533", json!("nvim")),
            native_row(SHELL, json!("cargo")),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(
        labels,
        ["nvim %533", "cargo"],
        "the tmux row keeps its address; neither row shows the daemon's title"
    );
    assert_eq!(named, labels);
}

/// Rung 2 outranks rung 3. A terminal hosting a coding session is called after
/// the provider driving it, because `codex` says what the row *is* where the
/// foreground command only says what it is doing this second — the same row
/// would read `node` a moment later.
#[tokio::test]
async fn a_bound_session_is_named_by_its_provider_over_its_command() {
    let (roster, named) = sidebar(
        vec![tmux_row(
            AGENT,
            json!("gobby-codex-d0"),
            "%533",
            json!("node"),
        )],
        vec![entry_with_provider(AGENT, "tmux", "codex")],
    )
    .await;

    assert_eq!(roster[0].label, "codex %533");
    assert_eq!(named, ["codex %533"]);
}

/// Rung 4. No name, no session, and a daemon that could not read a foreground
/// command — a tmux row over REST, whose pane pid is never persisted. The row
/// still has to render, and what it renders is a word, not an id.
#[tokio::test]
async fn a_terminal_with_no_name_session_or_command_reads_as_the_shell_it_is() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, Value::Null, "%3", Value::Null),
            native_row(SHELL, Value::Null),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["shell %3", "shell"]);
    assert_eq!(named, labels);
}

/// Commands collide — most panes on this machine are running a shell. The
/// address is what keeps two of them apart, exactly as it did when the ladder
/// still named them after their title.
#[tokio::test]
async fn two_terminals_running_the_same_command_stay_distinguishable_by_address() {
    let (roster, _) = sidebar(
        vec![
            tmux_row(AGENT, json!("15"), "%0", json!("zsh")),
            tmux_row(SHELL, json!("[tmux]"), "%7", json!("zsh")),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "tmux")],
    )
    .await;

    assert!(roster.iter().all(|row| row.label.starts_with("zsh ")));
    assert_ne!(roster[0].label, roster[1].label);
    assert!(roster[0].label.ends_with("%0"));
    assert!(roster[1].label.ends_with("%7"));
}

/// The guarantee the ladder exists for. Every rung above the last can be
/// missing at once, and the row still never shows a terminal id — not whole,
/// and not truncated to the leading segment that used to be the fallback and
/// rendered four identical rows for four different terminals.
#[tokio::test]
async fn no_terminal_id_reaches_the_sidebar_at_any_width() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, Value::Null, "%3", Value::Null),
            native_row(SHELL, Value::Null),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let rendered: Vec<&str> = roster
        .iter()
        .flat_map(|row| {
            std::iter::once(row.label.as_str()).chain(row.tokens.iter().map(String::as_str))
        })
        .chain(named.iter().map(String::as_str))
        .collect();
    for id in [AGENT, SHELL, ABSENT] {
        for width in [id.len(), 8, 4] {
            let prefix = &id[..width];
            assert!(
                rendered.iter().all(|text| !text.contains(prefix)),
                "`{prefix}` reached the chrome: {rendered:?}"
            );
        }
    }
}

/// Every live attention entry the daemon emits is keyed `session:<uuid>`, and
/// only the roster row says which terminal hosts that session. Matching the two
/// as strings resolves nothing, which left the entry showing a session UUID and
/// left its row unmarked while its session sat blocked.
#[tokio::test]
async fn an_attention_row_keyed_by_session_names_the_terminal_that_hosts_it() {
    let mut row = tmux_row(AGENT, json!("gobby-codex-d0"), "%533", json!("node"));
    row["session_id"] = json!(SESSION);
    let (rows, _) = sidebar(
        vec![row],
        vec![json!({
            "entry_id": format!("session:{SESSION}"),
            "terminal": {"terminal_id": AGENT, "backend": "tmux"},
            "provider": "codex",
            "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
        })],
    )
    .await;

    assert_eq!(
        rows[0].label, "codex %533",
        "the agent row carries the address-qualified terminal name"
    );
    assert_eq!(
        rows[0].state,
        RowState::Attention,
        "a blocked session must mark the row the user can act on"
    );
}

/// An entry can still name a subject no roster row claims — a run in another
/// project, or a session whose terminal has already gone. It gets no sessions
/// row, and with no pane to ask, the ladder has only its last rung left. The
/// roster terminal no entry names still lists, as a bare terminal under its
/// own name.
#[tokio::test]
async fn an_attention_row_for_an_unknown_terminal_reads_as_a_shell() {
    let (rows, named) = sidebar(
        vec![tmux_row(AGENT, json!("75"), "%533", json!("nvim"))],
        vec![json!({"entry_id": format!("blocked:{ABSENT}"), "kind": "blocked"})],
    )
    .await;

    let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
    assert_eq!(
        ids,
        [format!("{TERMINAL_ROW}{AGENT}").as_str()],
        "the terminal-less entry drew a row: {rows:?}"
    );
    assert_eq!(rows[0].label, "nvim");
    assert!(
        rows[0].tokens.iter().any(|token| token == "%533"),
        "a bare terminal keeps its address as a token: {:?}",
        rows[0].tokens
    );
    assert_eq!(named, ["shell"]);
}
