//! Atomic workspace snapshot persistence and restore.

use gobby_client::persist::{
    load_snapshot, save_snapshot, LayoutNode, SplitAxis, TabSnapshot, WorkspaceSnapshot,
};
use gobby_client::prefs::{load_prefs, prefs_path, save_prefs, PrefsError};
use gobby_client::ui::settings::{ClientPrefs, PassthroughModifier};
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use std::fs;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn pane(terminal_id: &str) -> LayoutNode {
    LayoutNode::Pane {
        terminal_id: terminal_id.to_string(),
    }
}

fn split(children: Vec<LayoutNode>) -> LayoutNode {
    LayoutNode::Split {
        axis: SplitAxis::Horizontal,
        ratio: 0.5,
        children,
    }
}

fn tab(layout: LayoutNode, focused: &str) -> TabSnapshot {
    TabSnapshot {
        title: String::new(),
        layout,
        focused: Some(focused.to_string()),
    }
}

/// One tab per terminal, the first one active and focused.
fn snapshot(project_id: &str, terminal_ids: &[&str]) -> WorkspaceSnapshot {
    WorkspaceSnapshot {
        project_id: project_id.to_string(),
        tabs: terminal_ids.iter().map(|id| tab(pane(id), id)).collect(),
        active_tab: 0,
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
    let tabs = ws.restore_project("proj-1").expect("restore");
    assert_eq!(
        tabs.tabs.len(),
        1,
        "the tab of the dead terminal is dropped"
    );
    assert_eq!(ws.tab_order(), vec!["live-1"]);
    assert_eq!(ws.focused_terminal_id(), Some("live-1"));
    assert_eq!(ws.pane_count(), 1);
}

/// 2.2.3: a snapshot with split tabs round-trips, and the restore keeps a
/// live split intact, collapses a split around a vanished terminal, drops a
/// tab with none left, and rewrites the file without the vanished ones.
#[test]
fn snapshot_restores_tab_layouts() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let expected = WorkspaceSnapshot {
        project_id: "proj".to_string(),
        tabs: vec![
            tab(split(vec![pane("live-1"), pane("dead-1")]), "dead-1"),
            tab(split(vec![pane("live-2"), pane("live-3")]), "live-3"),
            tab(pane("dead-2"), "dead-2"),
        ],
        active_tab: 1,
        focused_terminal_id: Some("live-3".to_string()),
    };
    save_snapshot(&home, &expected).expect("save");
    assert_eq!(load_snapshot(&home, "proj").expect("load"), expected);

    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home.clone());
    ws.daemon_mut()
        .set_live_terminals(vec!["live-1".into(), "live-2".into(), "live-3".into()]);
    let tabs = ws.restore_project("proj").expect("restore");
    assert_eq!(tabs.tabs.len(), 2, "the tab holding only dead-2 is dropped");
    assert_eq!(
        tabs.tabs[0].layout.pane_count(),
        1,
        "the split collapses to live-1"
    );
    assert_eq!(
        tabs.tabs[1].layout.pane_count(),
        2,
        "the live split stays intact"
    );
    assert_eq!(tabs.active_tab, 1);
    let focused = tabs.tabs[1].focused_pane().expect("focused slot");
    assert_eq!(ws.pane(focused).terminal_id, "live-3");
    assert_eq!(ws.focused_terminal_id(), Some("live-3"));
    assert_eq!(ws.pane_count(), 3);

    let rewritten = load_snapshot(&home, "proj").expect("rewritten snapshot");
    assert_eq!(rewritten.tabs.len(), 2);
    assert_eq!(rewritten.terminal_ids(), ["live-1", "live-2", "live-3"]);
    assert_eq!(
        rewritten.tabs[0].focused.as_deref(),
        Some("live-1"),
        "focus falls to the surviving slot"
    );
    assert_eq!(rewritten.active_tab, 1);
    assert_eq!(rewritten.focused_terminal_id.as_deref(), Some("live-3"));
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
        assert_eq!(observed.tabs.len(), 1);
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

/// Opening and focusing terminals writes nothing; the snapshot is the tab
/// set the caller hands `persist_workspace`.
#[test]
fn workspace_persists_the_tab_set_it_is_given() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");
    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home.clone());
    ws.select_project("proj-mutations");

    let pane_a = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    let pane_b = ws.open_terminal("term-b", "native", "epoch-a").unwrap();
    ws.focus_pane(pane_b).unwrap();
    assert_eq!(
        load_snapshot(&home, "proj-mutations")
            .expect_err("nothing is written until a tab set is persisted")
            .kind(),
        std::io::ErrorKind::NotFound
    );

    let mut chrome = Chrome::dark();
    chrome.open_pane(pane_a, "a");
    chrome.open_pane_below(pane_b, "b");
    ws.persist_workspace(chrome.tabs()).expect("persist");
    let saved = load_snapshot(&home, "proj-mutations").unwrap();
    assert_eq!(saved.tabs.len(), 1);
    assert!(
        matches!(
            &saved.tabs[0].layout,
            LayoutNode::Split { axis: SplitAxis::Vertical, children, .. } if children.len() == 2
        ),
        "{:?}",
        saved.tabs[0].layout
    );
    assert_eq!(saved.terminal_ids(), ["term-a", "term-b"]);
    assert_eq!(saved.focused_terminal_id.as_deref(), Some("term-b"));
}

#[test]
fn prefs_round_trip_and_reject_unknown_keys() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().join("home");

    let defaults = load_prefs(&home).expect("missing file yields defaults");
    assert_eq!(defaults, ClientPrefs::default());
    assert!(defaults.mouse_capture);

    let prefs = ClientPrefs {
        theme: "light".to_string(),
        keybinds: "/tmp/keys.toml".to_string(),
        mouse_capture: false,
        pane_gaps: false,
        sidebar_width: 32,
        right_click_passthrough_modifier: PassthroughModifier::Alt,
        ..ClientPrefs::default()
    };
    let path = save_prefs(&home, &prefs).expect("save");
    assert_eq!(path, prefs_path(&home));
    assert_eq!(path, home.join("client").join("prefs.toml"));
    let text = fs::read_to_string(&path).unwrap();
    assert!(text.starts_with("[ui]\n"), "{text}");
    assert!(text.contains("mouse_capture = false\n"), "{text}");
    assert!(
        text.contains("right_click_passthrough_modifier = \"alt\"\n"),
        "{text}"
    );
    assert!(
        text.contains("[keymap]\npath = \"/tmp/keys.toml\"\n"),
        "{text}"
    );
    assert!(!text.contains("layout"), "{text}");
    assert_eq!(load_prefs(&home).unwrap(), prefs);
    let leftovers = fs::read_dir(path.parent().unwrap())
        .unwrap()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
        .count();
    assert_eq!(leftovers, 0);

    fs::write(&path, "[ui]\nsidebar_width = 40\n").unwrap();
    let partial = load_prefs(&home).expect("every key is optional");
    assert_eq!(
        partial,
        ClientPrefs {
            sidebar_width: 40,
            ..ClientPrefs::default()
        }
    );

    fs::write(&path, "[ui]\nmouse_captre = false\n").unwrap();
    let error = load_prefs(&home).expect_err("unknown key must fail");
    assert!(matches!(error, PrefsError::Parse(_)), "{error:?}");
    let message = error.to_string();
    assert!(message.contains("mouse_captre"), "{message}");
    assert!(message.contains("line 2"), "{message}");

    fs::write(&path, "[keymapp]\npath = \"\"\n").unwrap();
    let message = load_prefs(&home)
        .expect_err("unknown table must fail")
        .to_string();
    assert!(message.contains("keymapp"), "{message}");
}
