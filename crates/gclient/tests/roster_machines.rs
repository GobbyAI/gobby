//! 3.2 gclient attributes agents by the roster's machine and project.
//!
//! The hub-wide roster names each entry's machine and project. These tests
//! script roster entries whose session and run rows are absent from the
//! fetched sidebar windows, so only the roster can place them.

use gobby_client::ui::chrome::{Chrome, RowState};
use gobby_client::ui::sidebar::{agent_rows, machine_rows, ALL_MACHINES};
use gobby_client::Workspace;
use serde_json::{json, Value};

const LOCAL_MACHINE: &str = "m-local";
const REMOTE_MACHINE: &str = "m-remote";
const PROJECT_A: &str = "proj-a";
const PROJECT_B: &str = "proj-b";

/// A session roster entry as the daemon ships it, hosted on `machine` in
/// `project`, optionally blocked on an actionable prompt.
fn entry(session: &str, machine: &str, project: &str, blocked: bool) -> Value {
    let attention = blocked.then(|| {
        json!({
            "attention_id": format!("att-{session}"),
            "kind": "actionable",
            "fingerprint": format!("fp-{session}"),
        })
    });
    json!({
        "entry_id": format!("session:{session}"),
        "session_id": session,
        "machine_id": machine,
        "project_id": project,
        "lifecycle_status": "active",
        "attention": attention,
        "terminal": {"terminal_id": format!("term-{session}"), "backend": "native"},
    })
}

/// A workspace on `LOCAL_MACHINE` focused on `PROJECT_A`, with no session or
/// run rows fetched, holding `entries` as its roster.
fn workspace(entries: Vec<Value>) -> Workspace {
    let mut ws = Workspace::scripted();
    ws.set_local_machine(LOCAL_MACHINE);
    ws.daemon_mut().set_roster(json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": entries,
    }));
    ws.select_project(PROJECT_A);
    ws.reconcile_subscribe_first().expect("reconcile roster");
    ws
}

fn agent_ids(ws: &Workspace, chrome: &Chrome) -> Vec<String> {
    agent_rows(ws, chrome)
        .into_iter()
        .map(|row| row.id)
        .filter(|id| id.starts_with("session:"))
        .collect()
}

#[test]
fn agents_use_roster_machine() {
    let ws = workspace(vec![entry("s-remote", REMOTE_MACHINE, PROJECT_A, false)]);

    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == "session:s-remote")
        .expect("roster entry becomes an agent");

    assert_eq!(agent.machine_id, REMOTE_MACHINE);
    assert!(
        ws.sidebar().machines.iter().any(|m| m == REMOTE_MACHINE),
        "the roster machine is listed: {:?}",
        ws.sidebar().machines
    );
}

#[test]
fn remote_machine_row_shows_attention() {
    let ws = workspace(vec![
        entry("s-local", LOCAL_MACHINE, PROJECT_A, false),
        entry("s-remote", REMOTE_MACHINE, PROJECT_A, true),
    ]);
    let chrome = Chrome::dark();

    let states: Vec<(String, RowState)> = machine_rows(&ws, &chrome)
        .into_iter()
        .map(|row| (row.id, row.state))
        .collect();

    assert!(
        states.contains(&(REMOTE_MACHINE.to_string(), RowState::Attention)),
        "the remote machine row rolls up its blocked agent: {states:?}"
    );
    assert!(
        !states.contains(&(LOCAL_MACHINE.to_string(), RowState::Attention)),
        "the local machine row carries no remote attention: {states:?}"
    );
}

#[test]
fn agents_use_roster_project() {
    let ws = workspace(vec![
        entry("s-local", LOCAL_MACHINE, PROJECT_A, false),
        entry("s-remote", REMOTE_MACHINE, PROJECT_B, false),
    ]);
    let mut chrome = Chrome::dark();
    chrome.sidebar.all_projects = false;
    chrome.sidebar.machine_filter = Some(ALL_MACHINES.to_string());

    let remote = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == "session:s-remote")
        .expect("roster entry becomes an agent");

    assert_eq!(remote.project_id, PROJECT_B);
    assert_eq!(agent_ids(&ws, &chrome), ["session:s-local"]);
}

#[test]
fn new_window_defaults_to_current_machine() {
    let ws = workspace(vec![
        entry("s-local", LOCAL_MACHINE, PROJECT_A, false),
        entry("s-remote", REMOTE_MACHINE, PROJECT_A, false),
    ]);
    let mut chrome = Chrome::dark();

    assert_eq!(agent_ids(&ws, &chrome), ["session:s-local"]);

    chrome.sidebar.machine_filter = Some(ALL_MACHINES.to_string());
    let mut every = agent_ids(&ws, &chrome);
    every.sort();
    assert_eq!(every, ["session:s-local", "session:s-remote"]);
}
