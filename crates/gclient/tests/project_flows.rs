//! New/Open project registration and default-workspace attachment.
use super::mock_daemon;

use crossterm::event::{KeyCode, KeyEvent};
use gobby_client::app::{
    open_new_project_dialog, open_open_project_dialog, project_dialog_key, submit_new_project,
    submit_open_project, ModalOutcome, Workspace,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::{Chrome, Mode};
use mock_daemon::MockDaemon;
use serde_json::{json, Value};

fn project(path: &std::path::Path) -> Value {
    json!({"id": "project-new", "name": "new", "display_name": "new",
        "checkout": {"machine_id": "m-local", "root_path": path}})
}

#[tokio::test]
async fn active_project_path_can_be_reselected_after_superseding_intent() {
    use gobby_client::app::{
        apply_project_attachment, dispatch_project_navigation, resume_project_navigation, JobResult,
    };
    let mock = MockDaemon::start("local-token").await;
    let home = tempfile::tempdir().expect("isolated home");
    let first = home.path().join("first");
    let other = home.path().join("other");
    let release = mock.enqueue_held("POST", "/api/projects/init", 200, project(&first));
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    submit_new_project(&mut workspace, &mut chrome, first.to_str().expect("path"))
        .await
        .expect("first intent");
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        mock.wait_for_request(|request| request.target == "/api/projects/init"),
    )
    .await
    .expect("first registration started");
    submit_new_project(&mut workspace, &mut chrome, other.to_str().expect("path"))
        .await
        .expect("other intent");
    submit_new_project(&mut workspace, &mut chrome, first.to_str().expect("path"))
        .await
        .expect("reselect first");
    release.notify_one();
    let outcome = receiver.recv().await.expect("first result");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, true, attachment);
    resume_project_navigation(&mut workspace, &mut chrome, &sender)
        .await
        .expect("resume latest intent");
    mock_daemon::project_jobs::apply_navigation(&mut workspace, &mut chrome).await;
    assert_eq!(
        workspace.project_id(),
        Some("project-new"),
        "reselected in-flight path opens"
    );
    assert!(chrome.dialog.is_none());
    assert!(!other.exists(), "superseded path never creates a checkout");
    assert_eq!(init_requests(&mock).len(), 1);
    mock.shutdown().await;
}

#[tokio::test]
async fn same_project_path_can_be_retried_after_connection_generation_changes() {
    use gobby_client::app::{
        apply_project_attachment, dispatch_project_navigation, resume_project_navigation, JobResult,
    };
    use gobby_client::daemon::Daemon;
    let mock = MockDaemon::start("local-token").await;
    let home = tempfile::tempdir().expect("isolated checkout");
    let root = home.path().canonicalize().expect("root");
    assert!(std::process::Command::new("git")
        .arg("-C")
        .arg(&root)
        .arg("init")
        .output()
        .expect("git init")
        .status
        .success());
    let row = project(&root);
    let release = mock.enqueue_held("GET", "/api/projects", 200, json!([row.clone()]));
    mock.enqueue("GET", "/api/projects", 200, json!([row]));
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().join("state"));
    let mut chrome = Chrome::dark();
    open_open_project_dialog(&mut chrome);
    let path = root.to_str().expect("path");
    submit_open_project(&mut workspace, &mut chrome, path)
        .await
        .expect("first intent");
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        mock.wait_for_request(|request| request.target == "/api/projects"),
    )
    .await
    .expect("first lookup held");
    let previous = workspace.daemon().generation();
    let current = workspace
        .daemon()
        .reconnect(previous)
        .await
        .expect("new connection");
    assert_ne!(previous, current);
    submit_open_project(&mut workspace, &mut chrome, path)
        .await
        .expect("retry same path");
    assert!(
        dispatch_project_navigation(&mut workspace, &sender),
        "old-generation path cannot suppress retry"
    );
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), receiver.recv())
        .await
        .expect("retry completes")
        .expect("outcome");
    assert_eq!(outcome.tag.generation, current);
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, true, attachment);
    resume_project_navigation(&mut workspace, &mut chrome, &sender)
        .await
        .expect("resume retry");
    assert_eq!(workspace.project_id(), Some("project-new"));
    assert!(chrome.dialog.is_none());
    release.notify_one();
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), receiver.recv())
        .await
        .expect("old request completes")
        .expect("outcome");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, false, attachment);
    assert_eq!(workspace.project_id(), Some("project-new"));
    mock.shutdown().await;
}

#[tokio::test]
async fn reselecting_a_pending_worktree_creation_reuses_its_success() {
    use gobby_client::app::{
        apply_project_attachment, create_worktree, dispatch_project_navigation,
        resume_project_navigation, JobResult,
    };
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["existing"], "existing")]);
    let release = mock.enqueue_held("POST", "/api/source-control/worktrees", 200, json!({"id":"new-tree", "project_id":"project-1", "branch_name":"first", "worktree_path":"/repo-first", "status":"active", "workspace_role":"client"}));
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial model");
    let mut chrome = Chrome::dark();
    gobby_client::app::sync_live_chrome(&mut workspace, &mut chrome);
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    chrome.dialog = Some(Dialog::NewWorktree {
        project_id: "project-1".into(),
        branch: "first".into(),
        base: String::new(),
        cursor: 5,
        base_focused: false,
        error: None,
    });
    create_worktree(
        &mut workspace,
        &mut chrome,
        &sender,
        "project-1",
        "first",
        None,
    )
    .await
    .expect("first");
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        mock.wait_for_request(|request| {
            request.method == "POST" && request.target == "/api/source-control/worktrees"
        }),
    )
    .await
    .expect("first creation held");
    create_worktree(
        &mut workspace,
        &mut chrome,
        &sender,
        "project-1",
        "other",
        None,
    )
    .await
    .expect("other");
    create_worktree(
        &mut workspace,
        &mut chrome,
        &sender,
        "project-1",
        "first",
        None,
    )
    .await
    .expect("reselect first");
    release.notify_one();
    let outcome = receiver.recv().await.expect("creation completes");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("creation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, true, attachment);
    resume_project_navigation(&mut workspace, &mut chrome, &sender)
        .await
        .expect("open created tree");
    assert!(
        !dispatch_project_navigation(&mut workspace, &sender),
        "reselect consumes the existing successful creation instead of creating the branch twice"
    );
    assert!(chrome.dialog.is_none());
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|request| request.method == "POST"
                && request.target == "/api/source-control/worktrees")
            .count(),
        1
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn attachment_failure_preserves_unrelated_dialog_and_names_target() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("workspace_attach");
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("first");
    let mut chrome = Chrome::dark();
    chrome.dialog = Some(Dialog::Alerts { scroll: 0 });
    gobby_client::app::focus_project(&mut workspace, &mut chrome, "second")
        .await
        .expect("queue");
    mock_daemon::project_jobs::apply_navigation(&mut workspace, &mut chrome).await;
    assert_eq!(workspace.project_id(), Some("first"));
    assert_eq!(chrome.dialog, Some(Dialog::Alerts { scroll: 0 }));
    assert!(chrome
        .toasts
        .iter()
        .any(|toast| toast.toast.title.contains("project second")));
    mock.shutdown().await;
}

#[tokio::test]
async fn attachment_from_retired_generation_cannot_install_snapshot() {
    use gobby_client::app::{
        apply_project_attachment, dispatch_project_navigation, focus_project, JobResult,
    };
    let mock = MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    focus_project(&mut workspace, &mut chrome, "second")
        .await
        .expect("queue");
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    let outcome = receiver.recv().await.expect("attachment outcome");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    // This is the generation verdict supplied by the loop's outcome reducer.
    apply_project_attachment(&mut workspace, &mut chrome, false, attachment);
    assert!(workspace.workspace_model().is_none());
    assert_eq!(workspace.project_id(), None);
    assert!(chrome.toasts.is_empty());
    assert!(!dispatch_project_navigation(&mut workspace, &sender));
    mock.shutdown().await;
}

#[tokio::test]
async fn cancelled_repeated_project_submission_cannot_change_focus() {
    use gobby_client::app::{apply_project_attachment, dispatch_project_navigation, JobResult};
    let mock = MockDaemon::start("local-token").await;
    let home = tempfile::tempdir().expect("isolated home");
    let root = home.path().join("new-checkout");
    let release = mock.enqueue_held("POST", "/api/projects/init", 200, project(&root));
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().join("state"));
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    let path = root.to_str().expect("path");
    submit_new_project(&mut workspace, &mut chrome, path)
        .await
        .expect("queue");
    submit_new_project(&mut workspace, &mut chrome, path)
        .await
        .expect("coalesce pending");
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        mock.wait_for_request(|request| request.target == "/api/projects/init"),
    )
    .await
    .expect("registration started");
    submit_new_project(&mut workspace, &mut chrome, path)
        .await
        .expect("coalesce active");
    assert!(!dispatch_project_navigation(&mut workspace, &sender));
    assert_eq!(
        project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Esc)),
        ModalOutcome::Close
    );
    release.notify_one();
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), receiver.recv())
        .await
        .expect("job completes")
        .expect("outcome");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, true, attachment);
    assert_eq!(
        workspace.project_id(),
        None,
        "cancelled result cannot select a project"
    );
    assert!(
        workspace.workspace_model().is_none(),
        "cancelled result cannot install a workspace"
    );
    assert!(chrome.dialog.is_none());
    assert_eq!(
        init_requests(&mock).len(),
        1,
        "one registration for repeated submissions"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn superseded_project_attachment_cannot_overwrite_latest_choice() {
    use gobby_client::app::{
        apply_project_attachment, dispatch_project_navigation, focus_project, JobResult,
    };
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("first", &[(&["terminal-first"], "terminal-first")]);
    mock.seed_other_workspace("second", &[(&["terminal-second"], "terminal-second")]);
    let latest = mock.seed_other_workspace("third", &[(&["terminal-third"], "terminal-third")]);
    let release = mock.hold_ws("workspace_attach", |request| {
        request["project_id"] == "second"
    });
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("first");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    let original = workspace
        .workspace_model()
        .expect("model")
        .workspace
        .id
        .clone();
    let mut chrome = Chrome::dark();
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    focus_project(&mut workspace, &mut chrome, "second")
        .await
        .expect("queue second");
    assert!(dispatch_project_navigation(&mut workspace, &sender));
    tokio::time::timeout(
        std::time::Duration::from_secs(10),
        mock.wait_for_request(|request| {
            request.body.as_ref().is_some_and(|body| {
                body["type"] == "workspace_attach" && body["project_id"] == "second"
            })
        }),
    )
    .await
    .expect("second attach started");
    focus_project(&mut workspace, &mut chrome, "third")
        .await
        .expect("queue third");
    assert!(
        !dispatch_project_navigation(&mut workspace, &sender),
        "attachments are serialized"
    );
    release.notify_one();
    let outcome = receiver.recv().await.expect("second result");
    let JobResult::ProjectAttached(attachment) = outcome.result else {
        panic!("navigation result");
    };
    apply_project_attachment(&mut workspace, &mut chrome, true, attachment);
    assert_eq!(workspace.project_id(), Some("first"));
    assert_eq!(
        workspace.workspace_model().expect("model").workspace.id,
        original
    );
    mock_daemon::project_jobs::apply_navigation(&mut workspace, &mut chrome).await;
    assert_eq!(workspace.project_id(), Some("third"));
    assert_eq!(
        workspace.workspace_model().expect("model").workspace.id,
        latest
    );
    assert_eq!(
        project_attaches(&mock)
            .iter()
            .map(|request| request["project_id"].as_str().expect("project"))
            .collect::<Vec<_>>(),
        ["second", "third"]
    );
    mock.shutdown().await;
}

fn init_requests(mock: &MockDaemon) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|r| r.method == "POST" && r.target == "/api/projects/init")
        .filter_map(|r| r.body)
        .collect()
}

fn project_attaches(mock: &MockDaemon) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|r| r.method == "WS")
        .filter_map(|r| r.body)
        .filter(|r| {
            r.get("type").and_then(Value::as_str) == Some("workspace_attach")
                && r.get("project_id").is_some()
        })
        .collect()
}

#[test]
fn project_actions_open_distinct_path_dialogs() {
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    assert_eq!(chrome.mode, Mode::ProjectDialog);
    assert!(matches!(chrome.dialog, Some(Dialog::NewProject { .. })));
    assert_eq!(
        project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Enter)),
        ModalOutcome::InitProject("~/".into())
    );
    open_open_project_dialog(&mut chrome);
    assert!(matches!(chrome.dialog, Some(Dialog::OpenProject { .. })));
    assert_eq!(
        project_dialog_key(&mut chrome, &KeyEvent::from(KeyCode::Enter)),
        ModalOutcome::OpenProject("~/".into())
    );
}

#[test]
fn project_dialogs_render_actions_and_wrapped_recovery_in_dark_and_light() {
    use gobby_client::theme::{Theme, ThemeKind};
    use gobby_client::ui::dialogs::render_dialog;
    use ratatui::{backend::TestBackend, Terminal};

    for appearance in [ThemeKind::Dark, ThemeKind::Light] {
        for (create, title, button) in [
            (true, "New project", "Create"),
            (false, "Open project", "Open"),
        ] {
            let mut chrome = Chrome::new(Theme::new(appearance));
            let error = Some(
                "Registration failed: name already exists. Checkout kept; retry with Open project."
                    .into(),
            );
            chrome.dialog = Some(if create {
                Dialog::NewProject {
                    path: "/scratch/project".into(),
                    cursor: 16,
                    error,
                }
            } else {
                Dialog::OpenProject {
                    path: "/scratch/project".into(),
                    cursor: 16,
                    error,
                }
            });
            let mut terminal = Terminal::new(TestBackend::new(80, 20)).expect("terminal");
            terminal
                .draw(|frame| {
                    render_dialog(frame, frame.area(), &chrome);
                })
                .expect("render");
            let screen: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|cell| cell.symbol())
                .collect();
            assert!(screen.contains(title), "{screen}");
            assert!(screen.contains(button), "{screen}");
            assert!(
                screen.contains("Open project."),
                "recovery must remain visible: {screen}"
            );
            assert!(
                screen.contains("/scratch/project"),
                "path remains visible: {screen}"
            );
        }
    }
}

#[tokio::test]
async fn new_project_registers_created_checkout_then_attaches_default_workspace() {
    let mock = MockDaemon::start("local-token").await;
    let parent = tempfile::tempdir().expect("parent");
    let root = parent
        .path()
        .canonicalize()
        .expect("parent path")
        .join("new");
    let row = project(&root);
    mock.enqueue("POST", "/api/projects/init", 200, row.clone());
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, json!([row.clone()]));
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(parent.path().join("gobby-home"));
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    submit_new_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("create");
    mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
    assert!(root.join(".git").is_dir());
    assert_eq!(init_requests(&mock), [json!({"path": root})]);
    assert_eq!(workspace.project_id(), Some("project-new"));
    assert!(chrome.dialog.is_none());
    let attaches = project_attaches(&mock);
    assert_eq!(attaches.len(), 1);
    assert_eq!(attaches[0]["project_id"], "project-new");
    assert!(
        attaches[0].get("workspace").is_none(),
        "attach chooses the project's default workspace"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn open_project_registers_only_unregistered_checkout() {
    for registered in [false, true] {
        let mock = MockDaemon::start("local-token").await;
        let parent = tempfile::tempdir().expect("parent");
        let root = parent.path().canonicalize().expect("path");
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(&root)
            .arg("init")
            .output()
            .expect("git init");
        assert!(status.status.success());
        let row = project(&root);
        mock.enqueue(
            "GET",
            "/api/projects",
            200,
            if registered {
                json!([row.clone()])
            } else {
                json!([])
            },
        );
        if !registered {
            mock.enqueue("POST", "/api/projects/init", 200, row.clone());
        }
        for _ in 0..3 {
            mock.enqueue("GET", "/api/projects", 200, json!([row.clone()]));
        }
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect");
        let mut workspace = Workspace::live(daemon);
        workspace.set_gobby_home(parent.path().join("gobby-home"));
        if registered {
            workspace.select_project("project-new");
        }
        let mut chrome = Chrome::dark();
        open_open_project_dialog(&mut chrome);
        submit_open_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
            .await
            .expect("open");
        mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
        assert_eq!(
            init_requests(&mock),
            if registered {
                vec![]
            } else {
                vec![json!({"path": root})]
            }
        );
        assert_eq!(workspace.project_id(), Some("project-new"));
        assert!(chrome.dialog.is_none());
        assert_eq!(project_attaches(&mock).len(), 1);
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn registration_failure_keeps_checkout_and_does_not_attach() {
    let mock = MockDaemon::start("local-token").await;
    let parent = tempfile::tempdir().expect("parent");
    let root = parent.path().canonicalize().expect("path").join("new");
    mock.enqueue(
        "POST",
        "/api/projects/init",
        400,
        json!({"detail": "registration refused"}),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    submit_new_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("inline error");
    mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
    assert!(
        matches!(&chrome.dialog, Some(Dialog::NewProject { error: Some(error), .. }) if error.contains("Registration failed") && error.contains("Open project"))
    );
    assert!(root.join(".git").is_dir());
    assert_eq!(workspace.project_id(), None);
    assert!(project_attaches(&mock).is_empty());
    assert_eq!(init_requests(&mock), [json!({"path": root})]);
    // The initialized checkout can be retried through Open; no second git init.
    open_open_project_dialog(&mut chrome);
    mock.enqueue("GET", "/api/projects", 200, json!([]));
    mock.enqueue(
        "POST",
        "/api/projects/init",
        400,
        json!({"detail": "still refused"}),
    );
    submit_open_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("inline error");
    mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
    assert!(
        matches!(&chrome.dialog, Some(Dialog::OpenProject { error: Some(error), .. }) if error.contains("Registration failed"))
    );
    assert!(project_attaches(&mock).is_empty());
    mock.shutdown().await;
}

#[tokio::test]
async fn invalid_paths_show_errors_without_registration_or_attach() {
    let mock = MockDaemon::start("local-token").await;
    let parent = tempfile::tempdir().expect("parent");
    let root = parent.path().canonicalize().expect("path");
    let file = root.join("keep");
    std::fs::write(&file, "keep content").expect("existing content");
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect");
    let mut workspace = Workspace::live(daemon);
    let mut chrome = Chrome::dark();
    open_new_project_dialog(&mut chrome);
    submit_new_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("inline error");
    mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
    assert!(
        matches!(&chrome.dialog, Some(Dialog::NewProject { error: Some(error), .. }) if error.contains("not empty"))
    );
    open_open_project_dialog(&mut chrome);
    submit_open_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("inline error");
    mock_daemon::project_jobs::settle_navigation(&mut workspace, &mut chrome).await;
    assert!(
        matches!(&chrome.dialog, Some(Dialog::OpenProject { error: Some(error), .. }) if error.contains("Cannot verify git checkout"))
    );
    assert_eq!(
        std::fs::read_to_string(file).expect("preserved"),
        "keep content"
    );
    assert!(init_requests(&mock).is_empty());
    assert!(project_attaches(&mock).is_empty());
    mock.shutdown().await;
}
