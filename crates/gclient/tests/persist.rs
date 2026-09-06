//! Atomic workspace snapshot persistence and restore.

use gobby_client::persist::{
    load_snapshot, save_snapshot, LayoutNode, SplitAxis, WorkspaceSnapshot,
};
use gobby_client::Workspace;
use std::fs;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn snapshot(project_id: &str, terminal_ids: &[&str]) -> WorkspaceSnapshot {
    let panes = terminal_ids
        .iter()
        .map(|terminal_id| LayoutNode::Pane {
            terminal_id: (*terminal_id).to_string(),
        })
        .collect();
    WorkspaceSnapshot {
        project_id: project_id.to_string(),
        layout: LayoutNode::Split {
            axis: SplitAxis::Horizontal,
            children: panes,
        },
        tab_order: terminal_ids.iter().map(|id| (*id).to_string()).collect(),
        focused_terminal_id: terminal_ids.first().map(|id| (*id).to_string()),
    }
}

#[test]
fn workspace_round_trip_and_corrupt_file() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let expected = snapshot("proj-1", &["live-1", "dead-1"]);
    let path = save_snapshot(&home, &expected).expect("save");
    assert_eq!(load_snapshot(&home, "proj-1").unwrap(), expected);

    fs::write(&path, b"{broken").unwrap();
    let recovered = load_snapshot(&home, "proj-1").expect("corruption is quarantined");
    assert_eq!(recovered, WorkspaceSnapshot::empty("proj-1"));
    assert!(!path.exists());
    let quarantined = fs::read_dir(path.parent().unwrap())
        .unwrap()
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.starts_with("workspace.json.corrupt-"))
        .count();
    assert_eq!(quarantined, 1);

    save_snapshot(&home, &expected).expect("save restore fixture");
    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home);
    ws.daemon_mut().set_live_terminals(vec!["live-1".into()]);
    ws.restore_project("proj-1").expect("restore");
    assert_eq!(ws.tab_order(), vec!["live-1"]);
    assert_eq!(ws.focused_terminal_id(), Some("live-1"));
    assert_eq!(ws.pane_count(), 1);
}

#[test]
fn workspace_write_is_atomic() {
    let dir = tempfile::tempdir().unwrap();
    let home = Arc::new(dir.path().join("home"));
    let initial = snapshot("proj-race", &["initial"]);
    let path = save_snapshot(&home, &initial).expect("initial save");
    let active = Arc::new(AtomicUsize::new(4));

    let writers: Vec<_> = (0..4)
        .map(|writer| {
            let home = Arc::clone(&home);
            let active = Arc::clone(&active);
            std::thread::spawn(move || {
                let result = (|| {
                    for generation in 0..50 {
                        let terminal = format!("writer-{writer}-{generation}");
                        save_snapshot(&home, &snapshot("proj-race", &[&terminal]))?;
                    }
                    Ok::<_, std::io::Error>(())
                })();
                active.fetch_sub(1, Ordering::SeqCst);
                result
            })
        })
        .collect();

    while active.load(Ordering::SeqCst) > 0 {
        let bytes = fs::read(&path).expect("snapshot remains readable");
        let observed: WorkspaceSnapshot =
            serde_json::from_slice(&bytes).expect("reader sees complete JSON");
        assert_eq!(observed.project_id, "proj-race");
        assert_eq!(observed.tab_order.len(), 1);
    }
    for writer in writers {
        writer.join().unwrap().expect("concurrent save");
    }

    let temp_files = fs::read_dir(path.parent().unwrap())
        .unwrap()
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.ends_with(".tmp"))
        .collect::<Vec<_>>();
    assert!(
        temp_files.is_empty(),
        "temporary files remain: {temp_files:?}"
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let before = load_snapshot(&home, "proj-race").unwrap();
        let parent = path.parent().unwrap();
        let original_mode = fs::metadata(parent).unwrap().permissions().mode();
        fs::set_permissions(parent, fs::Permissions::from_mode(0o500)).unwrap();
        let failed = save_snapshot(&home, &snapshot("proj-race", &["replacement"]));
        fs::set_permissions(parent, fs::Permissions::from_mode(original_mode)).unwrap();
        assert!(
            failed.is_err(),
            "injected pre-replacement failure must surface"
        );
        assert_eq!(load_snapshot(&home, "proj-race").unwrap(), before);
    }
}

#[test]
fn workspace_mutations_persist_atomically() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home.clone());
    ws.select_project("proj-mutations");

    ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    let pane_b = ws.open_terminal("term-b", "native", "epoch-a").unwrap();
    let opened = load_snapshot(&home, "proj-mutations").unwrap();
    assert_eq!(opened.tab_order, vec!["term-a", "term-b"]);

    ws.focus_pane(pane_b).unwrap();
    let focused = load_snapshot(&home, "proj-mutations").unwrap();
    assert_eq!(focused.focused_terminal_id.as_deref(), Some("term-b"));

    ws.set_tab_order(&["term-b", "term-a"]).unwrap();
    let reordered = load_snapshot(&home, "proj-mutations").unwrap();
    assert_eq!(reordered.tab_order, vec!["term-b", "term-a"]);
}
