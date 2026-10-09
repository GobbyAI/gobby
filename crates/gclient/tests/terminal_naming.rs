//! The chrome names a terminal and uses a short ID when no name is available.
//!
//! These tests pin the label ladder against a real daemon payload: the name
//! the user gave the pane, then the command in its foreground, then a short
//! terminal ID. The provider belongs on the agent row, not the terminal row.
//!
//! `title` is deliberately not a rung, and the rows here carry misleading ones
//! to prove it: the daemon fills `title` from `window_name or pane_title or
//! session_name`, so it is `zsh` for one pane and `75`, `[tmux]` or a whole
//! session banner for the next.

mod mock_daemon;

use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::ui::chrome::{attention_label, Chrome, RowState};
use gobby_client::ui::sidebar::{agent_rows, terminal_rows, TERMINAL_ROW};
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

/// The same entry with the provider the daemon resolved for its session.
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

    let mut chrome = Chrome::dark();
    // Naming checks compare agent rows, without project heading rows.
    chrome.sidebar.all_sessions = false;
    let mut drawn = agent_rows(&workspace, &chrome);
    drawn.extend(terminal_rows(&workspace, &chrome));
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

/// Rung 2, and the rung the daemon had to grow a new mechanism to serve: with
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
        ["nvim", "cargo"],
        "neither row shows the daemon's title"
    );
    assert!(roster[0].reference.is_empty());
    assert_eq!(roster[0].height(), 3);
    assert_eq!(named, labels);
}

/// A managed run still uses the terminal's foreground command as its row name.
/// The provider belongs on the agent row, not the terminal row.
#[tokio::test]
async fn a_managed_run_is_named_by_its_command_without_a_provider_token() {
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

    assert_eq!(roster[0].label, "node");
    assert_eq!(named, ["node"]);
}

/// The daemon supplies the spawned shell when no foreground job is active.
#[tokio::test]
async fn a_terminal_with_no_name_or_session_reads_its_daemon_command() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, Value::Null, "%3", json!("zsh")),
            native_row(SHELL, json!("zsh")),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["zsh", "zsh"]);
    assert!(roster[0].reference.is_empty());
    assert!(roster[1].reference.is_empty());
    assert_eq!(named, labels);
}

/// Commands collide — most panes on this machine are running a shell. Stable
/// row IDs keep equal displayed commands distinct without showing addresses.
#[tokio::test]
async fn two_terminals_running_the_same_command_keep_distinct_row_ids() {
    let (roster, _) = sidebar(
        vec![
            tmux_row(AGENT, json!("15"), "%0", json!("zsh")),
            tmux_row(SHELL, json!("[tmux]"), "%7", json!("zsh")),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "tmux")],
    )
    .await;

    assert!(roster.iter().all(|row| row.label == "zsh"), "{roster:?}");
    assert_ne!(roster[0].id, roster[1].id);
    assert!(roster.iter().all(|row| row.reference.is_empty()));
}

/// When both name and command are absent, the short ID distinguishes terminal
/// rows and attention labels without displaying a raw terminal UUID.
#[tokio::test]
async fn unnamed_terminals_use_distinct_short_ids_without_exposing_uuids() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, Value::Null, "%3", Value::Null),
            native_row(SHELL, Value::Null),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, [&AGENT[..8], &SHELL[..8]]);
    assert_eq!(named, labels);

    let rendered: Vec<&str> = roster
        .iter()
        .flat_map(|row| {
            [
                row.label.as_str(),
                row.definition.as_str(),
                row.reference.as_str(),
                row.detail.as_str(),
                row.model_slug.as_str(),
            ]
        })
        .chain(named.iter().map(String::as_str))
        .collect();
    for id in [AGENT, SHELL, ABSENT] {
        assert!(
            rendered.iter().all(|text| !text.contains(id)),
            "`{id}` reached the chrome: {rendered:?}"
        );
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
        rows[0].label, "node",
        "the agent row is named by its terminal's command"
    );
    assert!(rows[0].reference.is_empty());
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
async fn an_attention_row_for_an_unknown_terminal_reads_as_its_short_id() {
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
    assert_eq!(rows[0].detail, "", "no directory reported");
    assert_eq!(named, [&ABSENT[..8]]);
}
