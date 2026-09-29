mod mock_daemon;

use mock_daemon::MockDaemon;
use serde_json::{json, Value};
use std::fs;
use std::process::{Command, Output};

const PROJECT: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const VERBS: [&str; 11] = [
    "list",
    "new-tab",
    "split",
    "resize",
    "title",
    "select",
    "send-keys",
    "capture-pane",
    "wait-for-output",
    "kill",
    "help",
];

fn gclient(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_gclient"))
        .args(args)
        .env_remove("GOBBY_PANE_REF")
        .env_remove("GOBBY_WORKSPACE_ID")
        .env_remove("GOBBY_TAB_ID")
        .output()
        .expect("run gclient")
}

#[test]
fn help_documents_one_workspace_contract_for_every_verb() {
    let output = gclient(&["help"]);
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    let help = stdout(&output);
    for verb in VERBS {
        assert!(help.contains(&format!("\n  {verb}")), "{verb}");
    }
    assert!(help.contains("list [--workspace REF]"));
    assert!(help.contains("new-tab --project NAME|ID [--workspace REF]"));
    assert!(help.contains("select [REF] [--workspace REF]"));
    assert!(help.contains("Every verb takes --workspace REF"));
    assert!(help.contains("defaults to GOBBY_WORKSPACE_ID"));
    assert!(help.contains("other verbs resolve a short ID in it"));
    assert!(help.contains("an explicit --workspace\nrefuses a REF outside it"));
    assert!(help.contains("Omitted pane REF uses GOBBY_PANE_REF"));
    assert!(help.contains("gclient VERB --help shows one verb."));
}

#[test]
fn every_verb_has_its_own_help() {
    for verb in VERBS {
        for args in [vec![verb, "--help"], vec![verb, "-h"], vec!["help", verb]] {
            let output = gclient(&args);
            assert_eq!(
                output.status.code(),
                Some(0),
                "{args:?}: {}",
                stderr(&output)
            );
            let help = stdout(&output);
            assert!(
                help.starts_with(&format!("usage: gclient {verb}")),
                "{args:?}: {help}"
            );
            assert!(
                help.contains("Every verb takes --workspace REF"),
                "{args:?}"
            );
        }
    }
    let send_keys = stdout(&gclient(&["send-keys", "--help"]));
    assert!(send_keys.starts_with("usage: gclient send-keys [REF] TEXT [--enter]\n"));
    assert!(send_keys.contains("check it with capture-pane before retrying"));

    for args in [
        vec!["help", "--workspace", "0"],
        vec!["help", "send-keys", "--workspace", "0", "--json"],
        vec!["help", "--daemon-url", "http://x", "--token-file", "t"],
    ] {
        let output = gclient(&args);
        assert_eq!(
            output.status.code(),
            Some(0),
            "{args:?}: {}",
            stderr(&output)
        );
    }

    let unknown = gclient(&["help", "bogus"]);
    assert_eq!(unknown.status.code(), Some(2));
    assert_eq!(stderr(&unknown), "gclient help: unknown verb: bogus\n");
}

#[tokio::test(flavor = "multi_thread")]
async fn capture_pane_takes_workspace_like_list() {
    let daemon = MockDaemon::start("command-token").await;
    let seeded = daemon.seed_workspace(PROJECT, &[(&["terminal-a"], "first")]);
    let listed = invoke(&daemon, &["list", "--workspace", "default", "--json"]).await;
    assert_eq!(listed.status.code(), Some(0), "{}", stderr(&listed));
    let workspace = &serde_json::from_str::<Value>(&stdout(&listed)).unwrap()["workspace"];
    let node = workspace["node_ref"].as_u64().unwrap_or(0);
    let number = workspace["ref"].as_u64().expect("workspace ref");
    let inside = format!("{node}:{number}:1:0");
    let outside = format!("{node}:{}:1:0", number + 7);

    for reference in [inside.as_str(), seeded[0].1[0].as_str()] {
        let output = invoke(
            &daemon,
            &["capture-pane", reference, "--workspace", "default"],
        )
        .await;
        assert_eq!(
            output.status.code(),
            Some(0),
            "{reference}: {}",
            stderr(&output)
        );
        assert_eq!(stdout(&output), "mock pane output\n");
        assert_eq!(
            daemon.workspace_requests().last().unwrap()["op"],
            "pane.read"
        );
    }

    let sent = daemon.workspace_requests().len();
    let output = invoke(
        &daemon,
        &["capture-pane", &outside, "--workspace", "default"],
    )
    .await;
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        stderr(&output),
        format!("gclient capture-pane: {outside} is not in workspace default\n")
    );
    assert_eq!(daemon.workspace_requests().len(), sent);
}

#[tokio::test(flavor = "multi_thread")]
async fn short_id_resolves_in_the_workspace_never_as_its_name() {
    let daemon = MockDaemon::start("command-token").await;
    daemon.seed_workspace(PROJECT, &[(&["terminal-a"], "first")]);
    let output = invoke(&daemon, &["capture-pane", "abcd", "--workspace", "default"]).await;
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        stderr(&output),
        "gclient capture-pane: no pane in workspace default starts with abcd\n"
    );
    assert!(daemon.workspace_requests().is_empty());

    let output = invoke(&daemon, &["send-keys", "abcd", "hello"]).await;
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr(&output).contains("short ID abcd needs --workspace REF or GOBBY_WORKSPACE_ID"));
    assert!(daemon.workspace_requests().is_empty());
}

async fn invoke(daemon: &MockDaemon, args: &[&str]) -> Output {
    let directory = tempfile::tempdir().expect("token directory");
    let token_file = directory.path().join("token");
    fs::write(&token_file, "command-token").expect("token file");
    let url = daemon.url().to_owned();
    let args = args.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
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
            .env_remove("GOBBY_TAB_ID");
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

#[tokio::test(flavor = "multi_thread")]
async fn each_verb_sends_its_workspace_op() {
    let daemon = MockDaemon::start("command-token").await;
    let seeded = daemon.seed_workspace(PROJECT, &[(&["terminal-a"], "first")]);
    let (tab, panes) = &seeded[0];
    let pane = &panes[0];
    let cases = [
        (
            vec!["new-tab", "--workspace", "default", "--project", PROJECT],
            "tab.create",
        ),
        (vec!["split", pane, "--right"], "pane.split"),
        (vec!["resize", pane, "0.5"], "pane.resize"),
        (vec!["title", "0:0:0", "renamed"], "tab.rename"),
        (
            vec!["select", pane, "--workspace", "default", "--tab-ref", tab],
            "workspace.set_focus_hints",
        ),
        (
            vec!["send-keys", pane, "hello", "--enter"],
            "pane.send_text",
        ),
        (vec!["capture-pane", pane], "pane.read"),
        (
            vec!["wait-for-output", pane, "--pattern", "ready"],
            "pane.wait_for_output",
        ),
    ];
    for (args, expected) in cases {
        let output = invoke(&daemon, &args).await;
        assert_eq!(
            output.status.code(),
            Some(0),
            "{args:?}: {}",
            stderr(&output)
        );
        assert_eq!(
            daemon
                .workspace_requests()
                .last()
                .and_then(|v| v["op"].as_str()),
            Some(expected)
        );
    }
    let creation = daemon
        .workspace_requests()
        .into_iter()
        .find(|v| v["op"] == "tab.create")
        .expect("tab.create request");
    assert!(creation.get("role").is_none());
    assert!(creation.get("runbook").is_none());
    let output = invoke(&daemon, &["list", "--workspace", "default"]).await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert!(stdout(&output).contains(tab));
    let output = invoke(&daemon, &["kill", pane]).await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert_eq!(
        daemon.workspace_requests().last().unwrap()["op"],
        "pane.close"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn json_prints_reply_result_verbatim() {
    let daemon = MockDaemon::start("command-token").await;
    let seeded = daemon.seed_workspace(PROJECT, &[(&["terminal-a"], "first")]);
    let output = invoke(&daemon, &["capture-pane", &seeded[0].1[0], "--json"]).await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert_eq!(
        serde_json::from_str::<Value>(&stdout(&output)).unwrap(),
        json!({"text": "mock pane output"})
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn project_name_resolves_before_new_tab() {
    let daemon = MockDaemon::start("command-token").await;
    daemon.enqueue(
        "GET",
        "/api/projects",
        200,
        json!([{
            "id": PROJECT,
            "name": "sample",
            "display_name": "Sample",
            "checkout": null,
            "session_count": 0,
            "last_activity_at": null
        }]),
    );
    let output = invoke(
        &daemon,
        &["new-tab", "--workspace", "default", "--project", "sample"],
    )
    .await;
    assert_eq!(output.status.code(), Some(0), "{}", stderr(&output));
    assert_eq!(
        daemon.workspace_requests().last().unwrap()["project_id"],
        PROJECT
    );
    assert!(daemon
        .requests()
        .iter()
        .any(|request| request.target == "/api/projects"));
}

#[tokio::test(flavor = "multi_thread")]
async fn refused_op_exits_one_with_code_and_reason() {
    let daemon = MockDaemon::start("command-token").await;
    daemon.enqueue_workspace_refusal("forbidden", "pane is protected");
    let output = invoke(&daemon, &["send-keys", "0:1:2:3", "hello"]).await;
    assert_eq!(output.status.code(), Some(1));
    assert!(stderr(&output).contains("forbidden: pane is protected"));
}

#[tokio::test(flavor = "multi_thread")]
async fn send_keys_timeout_is_visible_and_fails() {
    let daemon = MockDaemon::start("command-token").await;
    daemon.suppress_ws("workspace_op");
    let output = invoke(&daemon, &["send-keys", "0:1:2:3", "hello"]).await;
    assert_eq!(output.status.code(), Some(3));
    assert_eq!(stdout(&output), "");
    let error = stderr(&output);
    assert!(
        error.starts_with("gclient send-keys: Daemon did not answer workspace_op in time."),
        "{error}"
    );
    assert!(error.contains("text sent to a pane may already be there"));
    assert!(error.contains("`gclient capture-pane REF` before retrying"));
}

#[tokio::test(flavor = "multi_thread")]
async fn token_failure_exits_three() {
    let daemon = MockDaemon::start("different-token").await;
    let output = invoke(&daemon, &["list", "--workspace", "default"]).await;
    assert_eq!(output.status.code(), Some(3));
}

#[tokio::test(flavor = "multi_thread")]
async fn wait_for_output_exit_codes_follow_reason() {
    let daemon = MockDaemon::start("command-token").await;
    for (reason, status) in [("matched", 0), ("timeout", 1), ("pane_lost", 2)] {
        daemon.enqueue_workspace_result(json!({"matched": reason == "matched", "reason": reason}));
        let output = invoke(
            &daemon,
            &["wait-for-output", "0:1:2:3", "--pattern", "ready"],
        )
        .await;
        assert_eq!(
            output.status.code(),
            Some(status),
            "{reason}: {}",
            stderr(&output)
        );
    }
}
