use super::{parse, verbs::Action, CommandEnv};

fn action(args: &[&str], env: &CommandEnv) -> Result<Action, super::CommandError> {
    Action::parse(parse(args.iter().map(|s| (*s).to_owned()).collect())?, env)
}

#[test]
fn pane_env_supplies_default_refs() {
    let env = CommandEnv {
        pane_ref: Some("0:1:2:3".into()),
        workspace_id: Some("workspace-id".into()),
        tab_id: Some("tab-id".into()),
    };
    let split = action(&["split", "--right"], &env).expect("split");
    assert!(matches!(split, Action::Split { pane, .. } if pane == "0:1:2:3"));
    let list = action(&["list"], &env).expect("list");
    assert!(matches!(list, Action::List { workspace } if workspace == "workspace-id"));
    let send = action(&["send-keys", "hello", "--enter"], &env).expect("send");
    assert!(
        matches!(send, Action::Op { op: crate::daemon::WorkspaceOp::PaneSendText { pane, text, .. }, .. }
        if pane == "0:1:2:3" && text == "hello")
    );
    let local = action(&["new-tab", "--project", "project-id"], &env).expect("local tab");
    assert!(matches!(local, Action::NewTab { prefix: Some(prefix), .. } if prefix == "0:1"));
    let other = action(
        &[
            "new-tab",
            "--project",
            "project-id",
            "--workspace",
            "other-id",
        ],
        &env,
    )
    .expect("other workspace");
    assert!(matches!(other, Action::NewTab { prefix: None, .. }));
}

#[test]
fn missing_ref_outside_a_pane_is_usage_error() {
    let error = action(&["capture-pane"], &CommandEnv::default()).expect_err("missing ref");
    assert_eq!(error.code, 2);
    let error = action(&["list"], &CommandEnv::default()).expect_err("missing workspace");
    assert_eq!(error.code, 2);
}

#[test]
fn positional_text_may_start_with_option_prefix() {
    let send = action(
        &["send-keys", "0:1:2:3", "--", "--version"],
        &CommandEnv::default(),
    )
    .expect("literal text");
    assert!(matches!(send,
        Action::Op { op: crate::daemon::WorkspaceOp::PaneSendText { text, .. }, .. }
        if text == "--version"));
}

#[test]
fn uuid_tab_ref_can_be_typed_without_changing_it() {
    let tab = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let kill = action(&["kill", tab, "--kind", "tab"], &CommandEnv::default()).expect("tab close");
    assert!(matches!(kill,
        Action::Op { op: crate::daemon::WorkspaceOp::TabClose { tab: ref passed, .. }, .. }
        if passed == tab));
}
