mod mock_daemon;

use gobby_client::app::{plan_arrange, ArrangeLayout};
use mock_daemon::MockDaemon;
use serde_json::Value;
use std::fs;
use std::process::{Command, Output};

const PROJECT: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OTHER_PROJECT: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const TERMINALS: &[&str] = &["a", "b", "c"];

#[derive(Clone, Copy, Debug)]
enum Scope {
    Flag,
    Inherited,
    RefOnly,
}

/// The tab under test, in a parked workspace other than `default`.
struct Other {
    workspace: String,
    tab: String,
    reference: String,
    panes: Vec<String>,
}

#[tokio::test(flavor = "multi_thread")]
async fn numeric_tab_ref_arranges_by_flag_inherited_workspace_or_its_own_ref() {
    for scope in [Scope::Flag, Scope::Inherited, Scope::RefOnly] {
        let daemon = MockDaemon::start("command-token").await;
        let seeded = daemon.seed_workspace(PROJECT, &[(TERMINALS, "a")]);
        let (tab, panes) = &seeded[0];
        let listed = listing(&daemon, "default").await;
        let reference = numeric_tab_ref(&listed, 0);
        let id = listed["workspace"]["id"].as_str().expect("workspace id");
        let (flags, env) = match scope {
            Scope::Flag => (vec!["--workspace", "default"], vec![]),
            Scope::Inherited => (vec![], vec![("GOBBY_WORKSPACE_ID", id)]),
            Scope::RefOnly => (vec![], vec![]),
        };
        let mut args = vec!["arrange", reference.as_str(), "--layout", "tiled"];
        args.extend(flags);
        let output = invoke(&daemon, &args, &env).await;
        assert_eq!(
            output.status.code(),
            Some(0),
            "{scope:?}: {}",
            stderr(&output)
        );
        assert_tiled_plan(&daemon, tab, panes);
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn tab_uuid_in_the_inherited_workspace_arranges_there() {
    let daemon = MockDaemon::start("command-token").await;
    let other = seed_other(&daemon).await;
    let output = invoke(
        &daemon,
        &["arrange", &other.tab, "--layout", "tiled"],
        &[("GOBBY_WORKSPACE_ID", &other.workspace)],
    )
    .await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert_tiled_plan(&daemon, &other.tab, &other.panes);
}

#[tokio::test(flavor = "multi_thread")]
async fn omitted_tab_arranges_the_pane_tab_in_the_inherited_workspace() {
    let daemon = MockDaemon::start("command-token").await;
    let other = seed_other(&daemon).await;
    let output = invoke(
        &daemon,
        &["arrange", "--layout", "tiled"],
        &[
            ("GOBBY_WORKSPACE_ID", &other.workspace),
            ("GOBBY_TAB_ID", &other.tab),
        ],
    )
    .await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert_tiled_plan(&daemon, &other.tab, &other.panes);
}

#[tokio::test(flavor = "multi_thread")]
async fn tab_outside_the_explicit_workspace_is_refused_with_no_layout_ops() {
    let daemon = MockDaemon::start("command-token").await;
    let other = seed_other(&daemon).await;
    for reference in [other.tab.as_str(), other.reference.as_str()] {
        let output = invoke(
            &daemon,
            &[
                "arrange",
                reference,
                "--layout",
                "tiled",
                "--workspace",
                "default",
            ],
            &[],
        )
        .await;
        assert_eq!(
            output.status.code(),
            Some(2),
            "{reference}: {}",
            stderr(&output)
        );
        assert_eq!(
            stderr(&output),
            format!("gclient arrange: {reference} is not in workspace default\n")
        );
        assert!(layout_ops(&daemon).is_empty(), "{reference}: no layout ops");
    }
}

/// The home workspace stays current: one tab of its own. The other project's
/// workspace is parked with three panes. Reading it attaches it, so this
/// reselects home by id; the mock restores by id, not by name.
async fn seed_other(daemon: &MockDaemon) -> Other {
    daemon.seed_workspace(PROJECT, &[(&["home"], "home")]);
    let home = listing(daemon, "default").await;
    let home_id = home["workspace"]["id"]
        .as_str()
        .expect("home id")
        .to_owned();
    let workspace = daemon.seed_other_workspace(OTHER_PROJECT, &[(TERMINALS, "a")]);
    let listed = listing(daemon, &workspace).await;
    let tab = listed["tabs"][0]["id"].as_str().expect("tab id").to_owned();
    let panes = pane_ids(&listed, &tab);
    let reference = numeric_tab_ref(&listed, 0);
    listing(daemon, &home_id).await;
    Other {
        workspace,
        tab,
        reference,
        panes,
    }
}

async fn listing(daemon: &MockDaemon, workspace: &str) -> Value {
    let output = invoke(daemon, &["list", "--workspace", workspace, "--json"], &[]).await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    serde_json::from_str(&stdout(&output)).expect("list JSON")
}

/// The numeric `node:workspace:tab` REF of the tab at `index` in `listed`.
fn numeric_tab_ref(listed: &Value, index: usize) -> String {
    let node = listed["workspace"]["node_ref"].as_u64().unwrap_or(0);
    let workspace = listed["workspace"]["ref"].as_u64().expect("workspace ref");
    let tab = listed["tabs"][index]["ref"].as_u64().expect("tab ref");
    format!("{node}:{workspace}:{tab}")
}

fn pane_ids(listed: &Value, tab: &str) -> Vec<String> {
    listed["panes"]
        .as_array()
        .expect("panes")
        .iter()
        .filter(|pane| pane["tab_id"] == tab)
        .map(|pane| pane["id"].as_str().expect("pane id").to_owned())
        .collect()
}

fn layout_ops(daemon: &MockDaemon) -> Vec<Value> {
    daemon
        .workspace_requests()
        .into_iter()
        .filter(|op| matches!(op["op"].as_str(), Some("pane.move" | "pane.resize")))
        .collect()
}

fn assert_tiled_plan(daemon: &MockDaemon, tab: &str, panes: &[String]) {
    let actual = layout_ops(daemon);
    let expected = plan_arrange(ArrangeLayout::Tiled, tab, panes);
    assert_eq!(actual.len(), expected.len(), "complete tiled plan");
    for (actual, expected) in actual.iter().zip(expected) {
        let expected = serde_json::to_value(expected).expect("serialize GUI operation");
        for (key, value) in expected.as_object().expect("operation object") {
            assert_eq!(actual.get(key), Some(value), "{key}");
        }
    }
}

async fn invoke(daemon: &MockDaemon, args: &[&str], env: &[(&str, &str)]) -> Output {
    let directory = tempfile::tempdir().expect("token directory");
    let token_file = directory.path().join("token");
    fs::write(&token_file, "command-token").expect("token file");
    let url = daemon.url().to_owned();
    let args = args.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    let env = env
        .iter()
        .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
        .collect::<Vec<_>>();
    tokio::task::spawn_blocking(move || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_gclient"));
        command
            .arg(&args[0])
            .arg("--daemon-url")
            .arg(url)
            .arg("--token-file")
            .arg(token_file)
            .args(&args[1..]);
        command
            .env_remove("GOBBY_PANE_REF")
            .env_remove("GOBBY_WORKSPACE_ID")
            .env_remove("GOBBY_TAB_ID")
            .envs(env);
        command.output().expect("run gclient")
    })
    .await
    .expect("join gclient")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout UTF-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr UTF-8")
}
