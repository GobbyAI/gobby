//! The chrome names a terminal, it does not print its primary key.
//!
//! Every roster row the daemon ships carries a `title` and, for tmux, the pane
//! address the user already types (`%533`). These tests pin the naming ladder
//! against a real daemon payload: title, then address, then a short id.

mod mock_daemon;

use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::ui::chrome::{Chrome, RowState};
use gobby_client::ui::sidebar_rows::{attention_rows, roster_rows, SidebarRow};
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

/// Reconcile a roster and an attention roster, then report the sidebar the
/// chrome would draw from them.
async fn sidebar(rows: Vec<Value>, attention: Vec<Value>) -> (Vec<SidebarRow>, Vec<SidebarRow>) {
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
    let drawn = (
        roster_rows(&workspace, &chrome),
        attention_rows(&workspace, &chrome),
    );

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
    drawn
}

/// The complaint this fixes: a roster of UUIDs says nothing about which
/// terminal is which, and the sidebar truncates them all to the same prefix.
#[tokio::test]
async fn a_named_terminal_shows_its_title_and_tmux_address_rather_than_its_uuid() {
    let (roster, _) = sidebar(
        vec![
            tmux_row(AGENT, json!("gobby-codex-d0"), "%533"),
            tmux_row(SHELL, json!("zsh"), "%0"),
        ],
        Vec::new(),
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["gobby-codex-d0", "zsh"]);
    // The detail column reads `<backend> <address> <state>`; the mock decides
    // the backend on the attach reply, so pin the address by its position.
    assert_eq!(
        roster[0].detail.split_whitespace().nth(1),
        Some("%533"),
        "the address belongs beside the backend that owns it: {}",
        roster[0].detail
    );
    assert!(
        roster.iter().all(|row| !row.label.contains(AGENT)
            && !row.label.contains(SHELL)
            && !row.detail.contains(AGENT)
            && !row.detail.contains(SHELL)),
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
        Vec::new(),
    )
    .await;

    assert_eq!(roster[0].label, roster[1].label);
    assert_ne!(roster[0].detail, roster[1].detail);
    assert!(roster[0].detail.contains("%0"));
    assert!(roster[1].detail.contains("%7"));
}

/// A row the daemon never titled still has to render. tmux lends its address;
/// a native row has neither, so only a short id is left.
#[tokio::test]
async fn an_untitled_terminal_falls_back_to_its_address_and_then_to_a_short_id() {
    let (roster, _) = sidebar(
        vec![tmux_row(AGENT, Value::Null, "%3"), native_row(SHELL)],
        Vec::new(),
    )
    .await;

    let labels: Vec<&str> = roster.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["%3", "fe6f6dc6"]);
}

/// Every live attention entry the daemon emits is keyed `session:<uuid>`, and
/// only the roster row says which terminal hosts that session. Matching the two
/// as strings resolves nothing, which left the entry showing a session UUID and
/// left the terminal's own row unmarked while its session sat blocked.
#[tokio::test]
async fn an_attention_row_keyed_by_session_names_the_terminal_that_hosts_it() {
    let mut row = tmux_row(AGENT, json!("gobby-codex-d0"), "%533");
    row["session_id"] = json!(SESSION);
    let (roster, attention) = sidebar(
        vec![row],
        vec![json!({"entry_id": format!("session:{SESSION}"), "kind": "blocked"})],
    )
    .await;

    assert_eq!(attention[0].label, "gobby-codex-d0");
    assert_eq!(
        roster[0].state,
        RowState::Attention,
        "a blocked session must mark the roster row the user can act on"
    );
}

/// An entry can still name a subject no roster row claims — a run in another
/// project, or a session whose terminal has already gone.
#[tokio::test]
async fn an_attention_row_for_an_unknown_terminal_shortens_its_id() {
    let (_, attention) = sidebar(
        vec![tmux_row(AGENT, json!("gobby-codex-d0"), "%533")],
        vec![json!({"entry_id": format!("blocked:{ABSENT}"), "kind": "blocked"})],
    )
    .await;

    let labels: Vec<&str> = attention.iter().map(|row| row.label.as_str()).collect();
    assert_eq!(labels, ["910ed674"]);
}
