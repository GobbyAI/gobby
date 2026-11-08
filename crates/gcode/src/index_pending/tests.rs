use super::*;
use std::fs;

#[test]
fn persisted_batch_is_taken_until_it_is_settled() {
    let root = tempfile::tempdir().expect("temp root");
    let dir = root.path().join("project");
    persist(
        &dir,
        &[PathBuf::from("src/a.rs"), PathBuf::from("src/b.rs")],
    )
    .expect("persist batch");

    let batches = take(&dir).expect("take batches");
    assert_eq!(
        batches.len(),
        1,
        "the next index run must take the persisted batch"
    );
    assert_eq!(
        batches[0].batch,
        Batch::Paths {
            paths: vec![PathBuf::from("src/a.rs"), PathBuf::from("src/b.rs")],
        }
    );

    settle(&batches[0]).expect("settle batch");
    assert!(
        take(&dir).expect("take after settle").is_empty(),
        "a settled batch must not be taken again"
    );
}

#[test]
fn whole_tree_marker_is_taken_until_it_is_settled() {
    let root = tempfile::tempdir().expect("temp root");
    let dir = root.path().join("project");
    persist_whole_tree(&dir, Some(Path::new("src")), true).expect("persist marker");

    let batches = take(&dir).expect("take markers");
    assert_eq!(
        batches.len(),
        1,
        "the next index run must take the whole-tree marker"
    );
    assert_eq!(
        batches[0].batch,
        Batch::WholeTree {
            path_filter: Some(PathBuf::from("src")),
            full: true,
        }
    );

    settle(&batches[0]).expect("settle marker");
    assert!(
        take(&dir).expect("take after settle").is_empty(),
        "a settled marker must not be taken again"
    );
}

#[test]
fn each_persist_writes_its_own_batch() {
    let root = tempfile::tempdir().expect("temp root");
    let dir = root.path().join("project");
    persist(&dir, &[PathBuf::from("src/a.rs")]).expect("first batch");
    persist(&dir, &[PathBuf::from("src/b.rs")]).expect("second batch");

    let mut paths: Vec<PathBuf> = take(&dir)
        .expect("take batches")
        .into_iter()
        .flat_map(|batch| match batch.batch {
            Batch::Paths { paths } => paths,
            Batch::WholeTree { .. } => Vec::new(),
        })
        .collect();
    paths.sort();
    assert_eq!(
        paths,
        vec![PathBuf::from("src/a.rs"), PathBuf::from("src/b.rs")],
        "a second persist must not overwrite the first batch"
    );
}

#[test]
fn a_missing_queue_dir_has_no_batches() {
    let root = tempfile::tempdir().expect("temp root");
    assert!(
        take(&root.path().join("never-written"))
            .expect("take from a missing dir")
            .is_empty()
    );
}

#[test]
fn an_unreadable_batch_stays_on_disk() {
    let root = tempfile::tempdir().expect("temp root");
    let dir = root.path().join("project");
    fs::create_dir_all(&dir).expect("queue dir");
    fs::write(dir.join("batch-corrupt.json"), b"{not json").expect("corrupt batch");

    assert!(take(&dir).expect("take past a corrupt batch").is_empty());
    assert!(
        dir.join("batch-corrupt.json").exists(),
        "an unreadable batch must stay queued for inspection, never be deleted"
    );
}

#[test]
fn pending_dir_accepts_a_project_uuid() {
    let project_id = "9c1f5a2e-3b7d-4e8a-8f10-5d2c6b7a9e41";
    let dir = pending_dir(project_id).expect("a UUID project id names a queue dir");
    assert!(
        dir.ends_with(Path::new("code-index-pending").join(project_id)),
        "the queue dir must sit under code-index-pending/<project id>, got {}",
        dir.display()
    );
}

#[test]
fn pending_dir_rejects_ids_that_are_not_uuids() {
    for project_id in ["", "..", "../../escape", "/etc/gobby-escape", "not-a-uuid"] {
        assert!(
            pending_dir(project_id).is_err(),
            "project id {project_id:?} must not name a queue dir"
        );
    }
}

#[test]
fn queued_paths_must_stay_within_the_project_root() {
    let root = Path::new("/work/project");
    assert!(path_stays_within_root(root, Path::new("src/lib.rs")));
    assert!(path_stays_within_root(
        root,
        Path::new("/work/project/src/lib.rs")
    ));
    assert!(!path_stays_within_root(root, Path::new("")));
    assert!(!path_stays_within_root(root, Path::new("..")));
    assert!(!path_stays_within_root(root, Path::new("src/../../x")));
    assert!(!path_stays_within_root(root, Path::new("/work/other/x.rs")));
    assert!(!path_stays_within_root(
        root,
        Path::new("/work/project2/x.rs")
    ));
    assert!(!path_stays_within_root(
        root,
        Path::new("/work/project/../secret.rs")
    ));
}

#[test]
fn a_batch_with_an_escaping_entry_is_not_contained() {
    let root = Path::new("/work/project");
    let whole_tree = |path_filter: Option<PathBuf>| Batch::WholeTree {
        path_filter,
        full: true,
    };
    assert!(batch_stays_within_root(root, &whole_tree(None)));
    assert!(batch_stays_within_root(
        root,
        &whole_tree(Some(PathBuf::from("src")))
    ));
    assert!(!batch_stays_within_root(
        root,
        &whole_tree(Some(PathBuf::from("../escape")))
    ));

    let explicit = |entries: &[&str]| Batch::Paths {
        paths: entries.iter().copied().map(PathBuf::from).collect(),
    };
    assert!(batch_stays_within_root(root, &explicit(&["src/a.rs"])));
    assert!(!batch_stays_within_root(
        root,
        &explicit(&["src/a.rs", "../escape.rs"])
    ));
}
