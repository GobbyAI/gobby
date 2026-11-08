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
