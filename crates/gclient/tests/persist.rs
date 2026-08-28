//! 3.3.4 workspace snapshot restore.

use gobby_client::persist::{save_snapshot, WorkspaceSnapshot};
use gobby_client::Workspace;

#[test]
fn restore_drops_dead_terminals() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let snapshot = WorkspaceSnapshot {
        project_id: "proj-1".into(),
        terminal_ids: vec!["live-1".into(), "dead-1".into()],
        focus: Some("live-1".into()),
    };
    save_snapshot(&home, &snapshot).expect("save");
    let path = home.join("client").join("proj-1").join("workspace.json");
    assert!(path.is_file());

    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home);
    ws.daemon_mut().set_live_terminals(vec!["live-1".into()]);
    ws.restore_project("proj-1").expect("restore");
    let ids = ws.roster_terminal_ids();
    assert!(ids.contains(&"live-1".to_string()));
    assert!(!ids.contains(&"dead-1".to_string()));
    assert_eq!(ws.pane_count(), 1);
}
