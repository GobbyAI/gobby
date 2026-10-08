//! New/Open project registration and default-workspace attachment.
mod mock_daemon;

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
    assert!(
        matches!(&chrome.dialog, Some(Dialog::NewProject { error: Some(error), .. }) if error.contains("not empty"))
    );
    open_open_project_dialog(&mut chrome);
    submit_open_project(&mut workspace, &mut chrome, root.to_str().expect("path"))
        .await
        .expect("inline error");
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
