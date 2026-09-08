//! The chrome names a terminal, it does not print its primary key.
//!
//! Every roster row the daemon ships carries a `title` and, for tmux, the pane
//! address the user already types (`%533`). These tests pin the naming ladder
//! against a real daemon payload: title, then address, then a short id. The
//! sidebar names a terminal on the agent row of its attention-roster entry.

mod mock_daemon;

use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::ui::chrome::{attention_label, Chrome, RowState};
use gobby_client::ui::sidebar::agent_rows;
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
/// actually sends.
fn tmux_row(terminal_id: &str, title: Value, pane: &str) -> Value {
    json!({
        "terminal_id": terminal_id,
        "state": "live",
        "backend": "tmux",
        "title": title,
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

/// A native row: no tmux pane, so nothing but its title can name it.
fn native_row(terminal_id: &str) -> Value {
    json!({
        "terminal_id": terminal_id,
        "state": "live",
        "backend": "native",
        "title": null,
    })
}

/// The attention-roster entry that lists `terminal_id` in the agents section.
fn entry(terminal_id: &str, backend: &str) -> Value {
    json!({
        "entry_id": format!("run:{terminal_id}"),
        "terminal": {"terminal_id": terminal_id, "backend": backend},
    })
}

/// Reconcile a roster and an attention roster, then report the agent rows
/// the chrome would draw from them and what it calls each attention entry.
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
    let drawn = agent_rows(&workspace, &chrome);
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

/// The complaint this fixes: a roster of UUIDs says nothing about which
/// terminal is which, and the sidebar truncates them all to the same prefix.
#[tokio::test]
async fn a_named_terminal_shows_its_title_and_tmux_address_rather_than_its_uuid() {
    let (roster, named) = sidebar(
        vec![
            tmux_row(AGENT, json!("gobby-codex-d0"), "%533"),
            tmux_row(SHELL, json!("zsh"), "%0"),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "tmux")],
    )
    .await;

    // An agent row is address-qualified: `<title> <address>`, and the
    // attention entry on it is called the same.
    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["gobby-codex-d0 %533", "zsh %0"]);
    assert_eq!(named, labels);
    assert!(
        roster.iter().all(|row| !row.label.contains(AGENT)
            && !row.label.contains(SHELL)
            && row
                .tokens
                .iter()
                .all(|token| !token.contains(AGENT) && !token.contains(SHELL))),
        "a terminal UUID reached the sidebar: {roster:?}"
    );
}

/// Titles collide — tmux names most panes after whatever is running in them.
/// The address is what keeps two of them apart.
#[tokio::test]
async fn two_terminals_sharing_a_title_stay_distinguishable_by_address() {
    let (roster, _) = sidebar(
        vec![
            tmux_row(AGENT, json!("zsh"), "%0"),
            tmux_row(SHELL, json!("zsh"), "%7"),
        ],
        vec![entry(AGENT, "tmux"), entry(SHELL, "tmux")],
    )
    .await;

    assert!(roster.iter().all(|row| row.label.starts_with("zsh ")));
    assert_ne!(roster[0].label, roster[1].label);
    assert!(roster[0].label.ends_with("%0"));
    assert!(roster[1].label.ends_with("%7"));
}

/// A row the daemon never titled still has to render. tmux lends its address;
/// a native row has neither, so only a short id is left.
#[tokio::test]
async fn an_untitled_terminal_falls_back_to_its_address_and_then_to_a_short_id() {
    let (roster, _) = sidebar(
        vec![tmux_row(AGENT, Value::Null, "%3"), native_row(SHELL)],
        vec![entry(AGENT, "tmux"), entry(SHELL, "native")],
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["%3", "fe6f6dc6"]);
}

/// Every live attention entry the daemon emits is keyed `session:<uuid>`, and
/// only the roster row says which terminal hosts that session. Matching the two
/// as strings resolves nothing, which left the entry showing a session UUID and
/// left its row unmarked while its session sat blocked.
#[tokio::test]
async fn an_attention_row_keyed_by_session_names_the_terminal_that_hosts_it() {
    let mut row = tmux_row(AGENT, json!("gobby-codex-d0"), "%533");
    row["session_id"] = json!(SESSION);
    let (rows, _) = sidebar(
        vec![row],
        vec![json!({
            "entry_id": format!("session:{SESSION}"),
            "terminal": {"terminal_id": AGENT, "backend": "tmux"},
            "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
        })],
    )
    .await;

    assert_eq!(
        rows[0].label, "gobby-codex-d0 %533",
        "the agent row carries the address-qualified terminal name"
    );
    assert_eq!(
        rows[0].state,
        RowState::Attention,
        "a blocked session must mark the row the user can act on"
    );
}

/// An entry can still name a subject no roster row claims — a run in another
/// project, or a session whose terminal has already gone. It gets no agent
/// row, and the chrome calls it by a short id.
#[tokio::test]
async fn an_attention_row_for_an_unknown_terminal_shortens_its_id() {
    let (rows, named) = sidebar(
        vec![tmux_row(AGENT, json!("gobby-codex-d0"), "%533")],
        vec![json!({"entry_id": format!("blocked:{ABSENT}"), "kind": "blocked"})],
    )
    .await;

    assert!(
        rows.is_empty(),
        "a terminal-less entry drew a row: {rows:?}"
    );
    assert_eq!(named, ["910ed674"]);
}
