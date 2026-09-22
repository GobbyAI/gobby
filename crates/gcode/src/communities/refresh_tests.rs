use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::index::api::{self, IndexWriteMode};
use crate::index::indexer::{IndexDegradation, IndexOutcome, refresh_communities};
use crate::models::{ImportRelation, IndexedFile};
use crate::{db, models};

use super::{LabelSource, StoredCommunity, read_for_context, refresh_project_communities};

#[test]
#[serial_test::serial(serial_db)]
fn refresh_writes_rows_watermark_and_signature() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("refresh-writes");
    let root = Path::new("/tmp").join(&project_id);
    seed_file(&mut conn, &project_id, &root, "pkg/a.py", &["pkg.b"]);
    seed_file(&mut conn, &project_id, &root, "pkg/b.py", &["pkg.a"]);
    let ctx = test_context(database_url, &project_id, ProjectIndexScope::Single);

    let report = refresh_project_communities(&mut conn, &ctx).expect("refresh communities");

    assert_eq!(report.communities, 1);
    assert_eq!(report.new_ids, 1);
    assert!(!report.skipped_unchanged);
    let state = project_state(&mut conn, &project_id);
    assert_eq!(state.0, 1);
    assert!(state.1.is_some());
    let rows = raw_rows(&mut conn, &project_id);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].members, vec!["pkg/a.py", "pkg/b.py"]);
    assert_eq!(rows[0].representatives, vec!["pkg/a.py", "pkg/b.py"]);
}

#[test]
#[serial_test::serial(serial_db)]
fn unchanged_partition_skips_write() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("skip-write");
    let root = Path::new("/tmp").join(&project_id);
    seed_file(&mut conn, &project_id, &root, "pkg/a.py", &["pkg.b"]);
    seed_file(&mut conn, &project_id, &root, "pkg/b.py", &["pkg.a"]);
    let ctx = test_context(database_url, &project_id, ProjectIndexScope::Single);
    refresh_project_communities(&mut conn, &ctx).expect("first refresh");
    let before = raw_rows(&mut conn, &project_id)[0].refreshed_at;

    let report = refresh_project_communities(&mut conn, &ctx).expect("second refresh");

    assert!(report.skipped_unchanged);
    assert_eq!(report.changed, 0);
    assert_eq!(raw_rows(&mut conn, &project_id)[0].refreshed_at, before);
}

#[test]
#[serial_test::serial(serial_db)]
fn refresh_failure_degrades_index_outcome() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("degraded");
    let mut ctx = test_context(database_url, &project_id, ProjectIndexScope::Single);
    ctx.project_id = "not-a-uuid".to_string();
    let mut outcome = IndexOutcome::default();

    refresh_communities(&mut conn, &ctx, &mut outcome);

    assert!(outcome.communities.is_none());
    assert!(matches!(
        outcome.degraded.as_slice(),
        [IndexDegradation::CommunityRefreshFailed { message }]
            if message.contains("invalid uuid id")
    ));
}

#[test]
#[serial_test::serial(serial_db)]
fn read_for_context_prefers_overlay_rows() {
    let (mut conn, database_url, parent_id, _parent_cleanup) = seeded_project("read-parent");
    let (_, _, overlay_id, _overlay_cleanup) = seeded_project("read-overlay");
    store_partition(
        &mut conn,
        &parent_id,
        vec![stored_row(
            &parent_id,
            4,
            &["parent.py"],
            "1111111111111111",
        )],
        4,
        "parent",
    );
    store_partition(
        &mut conn,
        &overlay_id,
        vec![stored_row(
            &overlay_id,
            9,
            &["overlay.py"],
            "2222222222222222",
        )],
        9,
        "overlay",
    );
    let ctx = overlay_context(database_url, &parent_id, &overlay_id);

    let overlay = read_for_context(&mut conn, &ctx).expect("read overlay rows");
    assert_eq!(overlay[0].project_id, overlay_id);
    assert_eq!(overlay[0].community_id, 9);

    conn.execute(
        "DELETE FROM code_communities WHERE project_id = $1",
        &[&db::id_param(&overlay_id).expect("overlay id")],
    )
    .expect("delete overlay rows");
    conn.execute(
        "UPDATE code_indexed_project_states SET partition_signature = NULL WHERE project_id = $1",
        &[&db::id_param(&overlay_id).expect("overlay id")],
    )
    .expect("reset overlay signature");
    let fallback = read_for_context(&mut conn, &ctx).expect("read parent fallback");
    assert_eq!(fallback[0].project_id, parent_id);
    assert_eq!(fallback[0].community_id, 4);
}

#[test]
#[serial_test::serial(serial_db)]
fn replace_is_atomic_under_concurrent_read() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("atomic-read");
    store_partition(
        &mut conn,
        &project_id,
        vec![stored_row(&project_id, 1, &["old.py"], "1111111111111111")],
        1,
        "old",
    );
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let replace = db::begin_replace(&mut conn, &machine_id, &project_id).expect("begin replace");
    let (sender, receiver) = mpsc::channel();
    let reader_url = database_url.clone();
    let reader_project = project_id.clone();
    let reader = thread::spawn(move || {
        let mut reader = db::connect_readwrite(&reader_url).expect("reader connection");
        sender
            .send(raw_rows(&mut reader, &reader_project))
            .expect("send visible rows");
    });
    let visible = receiver
        .recv_timeout(Duration::from_secs(2))
        .expect("reader is not blocked by uncommitted replacement");
    assert_eq!(visible[0].members, vec!["old.py"]);
    replace
        .commit(
            vec![stored_row(&project_id, 1, &["new.py"], "2222222222222222")],
            1,
            "new",
        )
        .expect("commit replacement");
    reader.join().expect("reader thread");
    assert_eq!(raw_rows(&mut conn, &project_id)[0].members, vec!["new.py"]);
}

#[test]
#[serial_test::serial(serial_db)]
fn label_written_during_refresh_is_not_lost() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("label-race");
    store_partition(
        &mut conn,
        &project_id,
        vec![stored_row(&project_id, 1, &["old.py"], "1111111111111111")],
        1,
        "old",
    );
    let project_uuid = db::id_param(&project_id).expect("project id");
    conn.execute(
        "UPDATE code_communities
         SET label = 'model-before', label_source = 'model', label_confidence = 0.8,
             label_model = 'labeler-v1', labeled_signature = member_signature
         WHERE project_id = $1 AND community_id = 1",
        &[&project_uuid],
    )
    .expect("commit model label before refresh lock");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let replace = db::begin_replace(&mut conn, &machine_id, &project_id).expect("begin replace");
    assert_eq!(replace.prior()[0].label, "model-before");

    let (started_sender, started_receiver) = mpsc::channel();
    let (done_sender, done_receiver) = mpsc::channel();
    let writer_url = database_url.clone();
    let writer_project = project_id.clone();
    let writer = thread::spawn(move || {
        let mut writer = db::connect_readwrite(&writer_url).expect("writer connection");
        started_sender.send(()).expect("signal update start");
        let updated = writer
            .execute(
                "UPDATE code_communities
                 SET label = 'model-late', label_source = 'model', label_confidence = 0.9,
                     label_model = 'labeler-v2', labeled_signature = member_signature
                 WHERE project_id = $1 AND community_id = 1
                   AND member_signature = '1111111111111111'",
                &[&db::id_param(&writer_project).expect("writer project id")],
            )
            .expect("guarded label update");
        done_sender.send(updated).expect("send update count");
    });
    started_receiver.recv().expect("writer started");
    assert!(
        done_receiver
            .recv_timeout(Duration::from_millis(100))
            .is_err()
    );
    let mut replacement = replace.prior()[0].clone();
    replacement.members = vec!["new.py".to_string()];
    replacement.member_signature = "2222222222222222".to_string();
    replacement.refreshed_at = SystemTime::now();
    replace
        .commit(vec![replacement], 1, "new")
        .expect("commit replacement");
    assert_eq!(
        done_receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("late update completes"),
        0
    );
    writer.join().expect("writer thread");
    let stored = raw_rows(&mut conn, &project_id);
    assert_eq!(stored[0].label, "model-before");
    assert_ne!(
        stored[0].labeled_signature.as_deref(),
        Some(stored[0].member_signature.as_str())
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn overlay_refresh_seeds_prior_rows_from_parent() {
    let (mut conn, database_url, parent_id, _parent_cleanup) = seeded_project("seed-parent");
    let (_, _, overlay_id, _overlay_cleanup) = seeded_project("seed-overlay");
    let root = Path::new("/tmp").join(&parent_id);
    seed_file(&mut conn, &parent_id, &root, "pkg/a.py", &["pkg.b"]);
    seed_file(&mut conn, &parent_id, &root, "pkg/b.py", &["pkg.a"]);
    let parent_ctx = test_context(database_url.clone(), &parent_id, ProjectIndexScope::Single);
    refresh_project_communities(&mut conn, &parent_ctx).expect("refresh parent");
    let parent_uuid = db::id_param(&parent_id).expect("parent id");
    conn.execute(
        "UPDATE code_communities
         SET label = 'parent model', label_source = 'model', label_confidence = 0.93,
             label_model = 'labeler-v1', labeled_signature = member_signature
         WHERE project_id = $1",
        &[&parent_uuid],
    )
    .expect("seed parent model label");
    conn.execute(
        "UPDATE code_indexed_project_states
         SET community_id_watermark = 40
         WHERE project_id = $1",
        &[&parent_uuid],
    )
    .expect("raise parent watermark");
    let ctx = overlay_context(database_url, &parent_id, &overlay_id);

    let report = refresh_project_communities(&mut conn, &ctx).expect("refresh overlay");

    assert!(!report.skipped_unchanged);
    let rows = raw_rows(&mut conn, &overlay_id);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].community_id, 1);
    assert_eq!(rows[0].label, "parent model");
    assert_eq!(rows[0].label_source, LabelSource::Model);
    assert_eq!(project_state(&mut conn, &overlay_id).0, 40);
}

#[test]
#[serial_test::serial(serial_db)]
fn overlay_first_refresh_commits_when_partition_matches_parent() {
    let (mut conn, database_url, parent_id, _parent_cleanup) = seeded_project("match-parent");
    let (_, _, overlay_id, _overlay_cleanup) = seeded_project("match-overlay");
    let root = Path::new("/tmp").join(&parent_id);
    seed_file(&mut conn, &parent_id, &root, "pkg/a.py", &["pkg.b"]);
    seed_file(&mut conn, &parent_id, &root, "pkg/b.py", &["pkg.a"]);
    let parent_ctx = test_context(database_url.clone(), &parent_id, ProjectIndexScope::Single);
    refresh_project_communities(&mut conn, &parent_ctx).expect("refresh parent");
    let parent_signature = project_state(&mut conn, &parent_id).1;
    let ctx = overlay_context(database_url, &parent_id, &overlay_id);

    let first = refresh_project_communities(&mut conn, &ctx).expect("first overlay refresh");
    let second = refresh_project_communities(&mut conn, &ctx).expect("second overlay refresh");

    assert!(!first.skipped_unchanged);
    assert!(!raw_rows(&mut conn, &overlay_id).is_empty());
    assert_eq!(project_state(&mut conn, &overlay_id).1, parent_signature);
    assert!(second.skipped_unchanged);
}

#[test]
#[serial_test::serial(serial_db)]
fn overlay_seed_locks_parent_state_row() {
    let (mut conn, database_url, parent_id, _parent_cleanup) = seeded_project("lock-parent");
    let (_, _, overlay_id, _overlay_cleanup) = seeded_project("lock-overlay");
    store_partition(
        &mut conn,
        &parent_id,
        vec![stored_row(&parent_id, 3, &["old.py"], "1111111111111111")],
        3,
        "old",
    );
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let parent_replace =
        db::begin_replace(&mut conn, &machine_id, &parent_id).expect("lock parent state");
    let (started_sender, started_receiver) = mpsc::channel();
    let (done_sender, done_receiver) = mpsc::channel();
    let seed_url = database_url.clone();
    let seed_overlay = overlay_id.clone();
    let seed_parent = parent_id.clone();
    let seed_machine = machine_id.clone();
    let seeder = thread::spawn(move || {
        let mut seed_conn = db::connect_readwrite(&seed_url).expect("seed connection");
        let mut overlay_replace = db::begin_replace(&mut seed_conn, &seed_machine, &seed_overlay)
            .expect("begin overlay replacement");
        started_sender.send(()).expect("signal seed start");
        overlay_replace
            .seed_from_parent(&seed_parent)
            .expect("seed parent rows");
        let result = (
            overlay_replace.watermark(),
            overlay_replace.prior()[0].community_id,
        );
        overlay_replace.skip().expect("rollback seed transaction");
        done_sender.send(result).expect("send seed result");
    });
    started_receiver.recv().expect("seeder started");
    assert!(
        done_receiver
            .recv_timeout(Duration::from_millis(100))
            .is_err()
    );
    parent_replace
        .commit(
            vec![stored_row(
                &parent_id,
                8,
                &["committed.py"],
                "2222222222222222",
            )],
            8,
            "committed",
        )
        .expect("commit parent replacement");
    assert_eq!(
        done_receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("seed observes committed parent"),
        (8, 8)
    );
    seeder.join().expect("seeder thread");
}

#[test]
#[serial_test::serial(serial_db)]
fn invalidate_removes_community_rows_with_project_state() {
    let (mut conn, _database_url, project_id, _cleanup) = seeded_project("invalidate");
    store_partition(
        &mut conn,
        &project_id,
        vec![stored_row(&project_id, 1, &["old.py"], "1111111111111111")],
        1,
        "old",
    );

    crate::index::indexer::invalidate(&mut conn, &project_id, None).expect("invalidate project");

    let project_uuid = db::id_param(&project_id).expect("project id");
    let machine_uuid =
        db::id_param(&gobby_core::machine::read_local_machine_id().expect("machine id"))
            .expect("machine uuid");
    let communities: i64 = conn
        .query_one(
            "SELECT count(*) FROM code_communities WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .expect("count communities")
        .get(0);
    let states: i64 = conn
        .query_one(
            "SELECT count(*) FROM code_indexed_project_states WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .expect("count project states")
        .get(0);
    assert_eq!((communities, states), (0, 0));
}

#[test]
#[serial_test::serial(serial_db)]
fn edge_change_without_membership_change_rewrites_rows() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("edge-change");
    let root = Path::new("/tmp").join(&project_id);
    seed_clique(&mut conn, &project_id, &root, "a", true);
    seed_clique(&mut conn, &project_id, &root, "b", false);
    let ctx = test_context(database_url, &project_id, ProjectIndexScope::Single);
    refresh_project_communities(&mut conn, &ctx).expect("first refresh");
    let before = raw_rows(&mut conn, &project_id);
    assert_eq!(before.len(), 2);

    let mut a0_imports = (1..5)
        .map(|index| format!("pkg.a{index}"))
        .collect::<Vec<_>>();
    a0_imports.push("pkg.b0".to_string());
    replace_imports(&mut conn, &project_id, "pkg/a0.py", &a0_imports);
    let a1_imports = (0..5)
        .filter(|index| *index != 1)
        .map(|index| format!("pkg.a{index}"))
        .collect::<Vec<_>>();
    replace_imports(&mut conn, &project_id, "pkg/a1.py", &a1_imports);
    let report = refresh_project_communities(&mut conn, &ctx).expect("edge-only refresh");
    let after = raw_rows(&mut conn, &project_id);

    assert!(!report.skipped_unchanged);
    assert_eq!(
        before
            .iter()
            .map(|row| &row.member_signature)
            .collect::<Vec<_>>(),
        after
            .iter()
            .map(|row| &row.member_signature)
            .collect::<Vec<_>>()
    );
    assert!(before.iter().zip(&after).any(|(left, right)| {
        left.internal_edges != right.internal_edges && left.cohesion != right.cohesion
    }));
    assert!(
        before
            .iter()
            .zip(&after)
            .any(|(left, right)| left.representatives != right.representatives)
    );
    assert!(
        before
            .iter()
            .zip(&after)
            .any(|(left, right)| left.boundary != right.boundary)
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn incremental_latency_experiment_reports_after() {
    let (Ok(root), Ok(file), Ok(project_id)) = (
        env::var("GCODE_LATENCY_CORPUS_ROOT"),
        env::var("GCODE_LATENCY_FILE"),
        env::var("GCODE_LATENCY_PROJECT_ID"),
    ) else {
        return;
    };
    let root = PathBuf::from(root);
    let database_url = crate::test_env::postgres_test_database_url("latency experiment");
    let mut conn = db::connect_readwrite(&database_url).expect("connect latency database");
    crate::test_env::seed_test_checkout(&mut conn, &project_id, &root)
        .expect("register latency test checkout");
    let ctx = latency_context(database_url, &project_id, &root);
    let request = || crate::index::indexer::IndexRequest {
        project_root: root.clone(),
        path_filter: None,
        explicit_files: vec![PathBuf::from(&file)],
        full: false,
        require_cpp_semantics: false,
        sync_projections: false,
    };

    crate::index::indexer::index_files(
        request(),
        &ctx,
        crate::index::indexer::IndexOptions::default(),
    )
    .expect("warm community refresh");
    for run in 1..=5 {
        let started = std::time::Instant::now();
        let outcome = crate::index::indexer::index_files(
            request(),
            &ctx,
            crate::index::indexer::IndexOptions::default(),
        )
        .expect("incremental single-file index");
        assert!(
            outcome
                .communities
                .as_deref()
                .is_some_and(|report| report.skipped_unchanged),
            "unchanged latency samples must exercise the partition-signature fast path"
        );
        println!(
            "latency mode=after run={run} elapsed_ms={} indexed={} skipped={} communities={:?}",
            started.elapsed().as_millis(),
            outcome.indexed_files,
            outcome.skipped_files,
            outcome.communities
        );
    }
}

#[test]
#[serial_test::serial(serial_db)]
fn churn_evidence_experiment() {
    let (Ok(source_root), Ok(anchor), Ok(project_id), Ok(corpus)) = (
        env::var("GCODE_CHURN_CORPUS_ROOT"),
        env::var("GCODE_CHURN_ANCHOR"),
        env::var("GCODE_CHURN_PROJECT_ID"),
        env::var("GCODE_CHURN_CORPUS"),
    ) else {
        return;
    };
    let database_url = crate::test_env::postgres_test_database_url("churn evidence experiment");
    let mut conn = db::connect_readwrite(&database_url).expect("connect churn database");
    let source_root = PathBuf::from(source_root);
    let scratch = tempfile::tempdir().expect("create disposable corpus copy");
    let root = scratch.path().join("corpus");
    copy_source_tree(&source_root, &root).expect("copy corpus source files");
    let _checkout_restore = rebind_checkout(&mut conn, &database_url, &project_id, &root);
    let ctx = latency_context(database_url, &project_id, &root);

    let first = run_churn_index(&ctx, &root, vec![PathBuf::from(&anchor)]);
    let rows_after_first = raw_rows(&mut conn, &project_id);
    let second = run_churn_index(&ctx, &root, vec![PathBuf::from(&anchor)]);
    let unchanged = raw_rows(&mut conn, &project_id);
    assert!(
        first
            .communities
            .as_deref()
            .is_some_and(|report| report.skipped_unchanged)
    );
    assert!(
        second
            .communities
            .as_deref()
            .is_some_and(|report| report.skipped_unchanged)
    );
    assert_eq!(rows_after_first, unchanged);
    println!(
        "churn corpus={corpus} phase=unchanged first={:?} second={:?} rows={}",
        first.communities,
        second.communities,
        unchanged.len()
    );

    let selected = unchanged
        .iter()
        .filter(|row| {
            row.members
                .iter()
                .filter(|member| member.ends_with(".py") && root.join(member).is_file())
                .count()
                >= 4
        })
        .max_by_key(|row| row.member_count)
        .expect("community with four Python files")
        .clone();
    let machine_id =
        db::id_param(&gobby_core::machine::read_local_machine_id().expect("local machine id"))
            .expect("machine UUID");
    let project_uuid = db::id_param(&project_id).expect("project UUID");
    conn.execute(
        "UPDATE code_communities
         SET label = 'Q1.4 model label', label_source = 'model',
             label_confidence = 0.99, label_model = 'q1.4-evidence',
             labeled_signature = member_signature, labeled_at = NOW()
         WHERE machine_id = $1 AND project_id = $2 AND community_id = $3",
        &[&machine_id, &project_uuid, &selected.community_id],
    )
    .expect("seed model label before rename");
    let labeled = read_for_context(&mut conn, &ctx).expect("read seeded model label");
    let labeled = labeled
        .iter()
        .find(|row| row.community_id == selected.community_id)
        .expect("seeded community remains readable");
    assert_eq!(labeled.label_source, LabelSource::Model);
    assert!(!labeled.label_stale);

    let renamed = selected
        .members
        .iter()
        .filter(|member| member.ends_with(".py") && root.join(member).is_file())
        .take(3)
        .enumerate()
        .map(|(index, old)| {
            let new = evidence_renamed_path(old, index);
            fs::rename(root.join(old), root.join(&new)).expect("rename community member");
            (old.clone(), new)
        })
        .collect::<Vec<_>>();
    assert_eq!(renamed.len(), 3);
    let rename_paths = renamed
        .iter()
        .flat_map(|(old, new)| [PathBuf::from(old), PathBuf::from(new)])
        .collect::<Vec<_>>();
    let rename_outcome = run_churn_index(&ctx, &root, rename_paths);
    let after_rename = raw_rows(&mut conn, &project_id);
    print_churn_diff(&corpus, "rename-three", &unchanged, &after_rename);
    let renamed_raw = after_rename
        .iter()
        .find(|row| row.community_id == selected.community_id)
        .expect("renamed community keeps its id");
    assert_eq!(renamed_raw.label_source, LabelSource::Model);
    assert_ne!(renamed_raw.member_signature, selected.member_signature);
    let renamed_view = read_for_context(&mut conn, &ctx).expect("read stale model label");
    let renamed_view = renamed_view
        .iter()
        .find(|row| row.community_id == selected.community_id)
        .expect("renamed community remains readable");
    assert!(renamed_view.label_stale);
    assert_eq!(renamed_view.label_source, LabelSource::Deterministic);
    println!(
        "churn corpus={corpus} phase=rename-three report={:?} community_id={} label_stale={} stored_label_source={} read_label_source={}",
        rename_outcome.communities,
        selected.community_id,
        renamed_view.label_stale,
        renamed_raw.label_source.as_str(),
        renamed_view.label_source.as_str()
    );

    let selected_module = python_module_name(&renamed[0].1);
    let added = format!("q14_{corpus}_added.py");
    fs::write(root.join(&added), format!("import {selected_module}\n"))
        .expect("write one added file");
    let add_outcome = run_churn_index(&ctx, &root, vec![PathBuf::from(&added)]);
    let after_add = raw_rows(&mut conn, &project_id);
    print_churn_diff(&corpus, "add-one", &after_rename, &after_add);
    assert!(after_add.iter().any(|row| row.members.contains(&added)));
    println!(
        "churn corpus={corpus} phase=add-one report={:?} added={added}",
        add_outcome.communities
    );

    let retired = after_add
        .iter()
        .filter(|row| row.member_count == 1)
        .find_map(|row| {
            let member = row.members.first()?;
            (member.ends_with(".py") && root.join(member).is_file())
                .then(|| (row.community_id, member.clone()))
        })
        .expect("single-file community to retire");
    fs::remove_file(root.join(&retired.1)).expect("delete singleton community member");
    let unrelated_a = format!("q14_{corpus}_unrelated_a.py");
    let unrelated_b = format!("q14_{corpus}_unrelated_b.py");
    let unrelated_a_module = python_module_name(&unrelated_a);
    let unrelated_b_module = python_module_name(&unrelated_b);
    fs::write(
        root.join(&unrelated_a),
        format!("import {unrelated_b_module}\n"),
    )
    .expect("write unrelated member A");
    fs::write(
        root.join(&unrelated_b),
        format!("import {unrelated_a_module}\n"),
    )
    .expect("write unrelated member B");
    let retire_outcome = run_churn_index(
        &ctx,
        &root,
        vec![
            PathBuf::from(&retired.1),
            PathBuf::from(&unrelated_a),
            PathBuf::from(&unrelated_b),
        ],
    );
    let after_retire = raw_rows(&mut conn, &project_id);
    print_churn_diff(
        &corpus,
        "retire-and-add-unrelated",
        &after_add,
        &after_retire,
    );
    assert!(after_retire.iter().all(|row| row.community_id != retired.0));
    let unrelated = after_retire
        .iter()
        .find(|row| row.members.contains(&unrelated_a) && row.members.contains(&unrelated_b))
        .expect("unrelated files form a new community");
    assert_ne!(unrelated.community_id, retired.0);
    println!(
        "churn corpus={corpus} phase=retire-and-add-unrelated report={:?} retired_id={} new_id={} deleted={} new_members={:?}",
        retire_outcome.communities, retired.0, unrelated.community_id, retired.1, unrelated.members
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn stale_model_label_reads_as_deterministic() {
    let (mut conn, database_url, project_id, _cleanup) = seeded_project("stale-label");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    let machine_uuid = db::id_param(&machine_id).expect("machine uuid");
    let project_uuid = db::id_param(&project_id).expect("project uuid");
    let members = vec!["src/api.rs".to_string(), "src/model.rs".to_string()];
    let representatives = vec!["src/api.rs".to_string()];
    let candidates = vec!["api".to_string(), "model".to_string()];
    let boundary = "[]";
    conn.execute(
        "INSERT INTO code_communities (
            machine_id, project_id, community_id, member_count, members,
            representatives, internal_edges, cohesion, boundary, member_signature,
            label_deterministic, label, label_source, label_confidence, label_model,
            label_candidates, labeled_signature
         ) VALUES (
            $1, $2, 7, 2, $3, $4, 1, 1.0, $5::text::jsonb, '1111111111111111',
            'api', 'domain model', 'model', 0.91, 'labeler-v1', $6,
            '2222222222222222'
         )",
        &[
            &machine_uuid,
            &project_uuid,
            &members,
            &representatives,
            &boundary,
            &candidates,
        ],
    )
    .expect("seed stale model label");

    let ctx = test_context(database_url, &project_id, ProjectIndexScope::Single);
    let rows = read_for_context(&mut conn, &ctx).expect("read communities");

    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].label, "api");
    assert_eq!(rows[0].label_source, LabelSource::Deterministic);
    assert_eq!(rows[0].label_confidence, None);
    assert_eq!(rows[0].label_model, None);
    assert!(rows[0].label_stale);

    let stored = conn
        .query_one(
            "SELECT label, label_source, label_confidence, label_model
             FROM code_communities
             WHERE machine_id = $1 AND project_id = $2 AND community_id = 7",
            &[&machine_uuid, &project_uuid],
        )
        .expect("read stored row");
    assert_eq!(stored.get::<_, String>(0), "domain model");
    assert_eq!(stored.get::<_, String>(1), "model");
    assert_eq!(stored.get::<_, Option<f64>>(2), Some(0.91));
    assert_eq!(
        stored.get::<_, Option<String>>(3).as_deref(),
        Some("labeler-v1")
    );
}

fn seeded_project(prefix: &str) -> (postgres::Client, String, String, ProjectCleanup) {
    let database_url = crate::test_env::postgres_test_database_url("community refresh tests");
    let mut conn = db::connect_readwrite(&database_url).expect("connect to test database");
    let project_id = unique_test_uuid(prefix);
    cleanup_project(&mut conn, &project_id).expect("pre-clean project");
    let root = PathBuf::from(format!("/tmp/{project_id}"));
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    api::upsert_project_seed(
        &mut conn,
        &machine_id,
        &project_id,
        &root,
        IndexWriteMode::Overlay,
    )
    .expect("seed indexed project");
    let cleanup = ProjectCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    (conn, database_url, project_id, cleanup)
}

fn test_context(database_url: String, project_id: &str, index_scope: ProjectIndexScope) -> Context {
    Context {
        database_url,
        project_root: Path::new("/tmp").join(project_id),
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope,
    }
}

fn overlay_context(database_url: String, parent_id: &str, overlay_id: &str) -> Context {
    let parent_root = Path::new("/tmp").join(parent_id);
    let overlay_root = Path::new("/tmp").join(overlay_id);
    test_context(
        database_url,
        parent_id,
        ProjectIndexScope::Overlay {
            overlay_project_id: overlay_id.to_string(),
            overlay_root,
            parent_project_id: parent_id.to_string(),
            parent_root,
        },
    )
}

fn latency_context(database_url: String, project_id: &str, root: &Path) -> Context {
    Context {
        database_url,
        project_root: root.to_path_buf(),
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    }
}

fn seed_file(
    conn: &mut postgres::Client,
    project_id: &str,
    root: &Path,
    file_path: &str,
    modules: &[&str],
) {
    let content_hash = format!("hash-{}", file_path.replace('/', "-"));
    let file = IndexedFile {
        id: IndexedFile::make_id(project_id, file_path, &content_hash),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        language: "python".to_string(),
        content_hash: content_hash.clone(),
        symbol_count: 0,
        byte_size: 1,
        indexed_at: String::new(),
    };
    api::upsert_file(conn, &file).expect("seed indexed file");
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    api::file_state::upsert_file_state(conn, &machine_id, &file, root, IndexWriteMode::Overlay)
        .expect("seed file state");
    let imports = modules
        .iter()
        .map(|module| ImportRelation {
            file_path: file_path.to_string(),
            module_name: (*module).to_string(),
        })
        .collect::<Vec<_>>();
    api::upsert_imports(conn, project_id, file_path, &content_hash, &imports)
        .expect("seed imports");
}

fn replace_imports(
    conn: &mut postgres::Client,
    project_id: &str,
    file_path: &str,
    modules: &[String],
) {
    let content_hash = format!("hash-{}", file_path.replace('/', "-"));
    let imports = modules
        .iter()
        .map(|module| ImportRelation {
            file_path: file_path.to_string(),
            module_name: module.clone(),
        })
        .collect::<Vec<_>>();
    api::upsert_imports(conn, project_id, file_path, &content_hash, &imports)
        .expect("replace imports");
}

fn seed_clique(
    conn: &mut postgres::Client,
    project_id: &str,
    root: &Path,
    prefix: &str,
    omit_first_pair: bool,
) {
    for source in 0..5 {
        let modules = (0..5)
            .filter(|target| *target != source)
            .filter(|target| !(omit_first_pair && source < 2 && *target < 2))
            .map(|target| format!("pkg.{prefix}{target}"))
            .collect::<Vec<_>>();
        let refs = modules.iter().map(String::as_str).collect::<Vec<_>>();
        seed_file(
            conn,
            project_id,
            root,
            &format!("pkg/{prefix}{source}.py"),
            &refs,
        );
    }
}

fn store_partition(
    conn: &mut postgres::Client,
    project_id: &str,
    rows: Vec<StoredCommunity>,
    watermark: i32,
    signature: &str,
) {
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    db::begin_replace(conn, &machine_id, project_id)
        .expect("begin stored partition")
        .commit(rows, watermark, signature)
        .expect("store partition");
}

fn stored_row(
    project_id: &str,
    community_id: i32,
    members: &[&str],
    member_signature: &str,
) -> StoredCommunity {
    StoredCommunity {
        machine_id: gobby_core::machine::read_local_machine_id().expect("machine id"),
        project_id: project_id.to_string(),
        community_id,
        member_count: members.len(),
        members: members.iter().map(|member| (*member).to_string()).collect(),
        representatives: members.iter().map(|member| (*member).to_string()).collect(),
        internal_edges: members.len().saturating_sub(1),
        cohesion: 1.0,
        boundary: Vec::new(),
        member_signature: member_signature.to_string(),
        label_deterministic: members[0].to_string(),
        label: members[0].to_string(),
        label_source: LabelSource::Deterministic,
        label_confidence: None,
        label_model: None,
        label_candidates: vec![members[0].to_string()],
        labeled_signature: None,
        labeled_at: None,
        label_attempted_at: None,
        refreshed_at: SystemTime::now(),
        label_stale: false,
    }
}

fn raw_rows(conn: &mut postgres::Client, project_id: &str) -> Vec<StoredCommunity> {
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine id");
    db::read_project_communities(conn, &machine_id, project_id).expect("read stored communities")
}

fn project_state(conn: &mut postgres::Client, project_id: &str) -> (i32, Option<String>) {
    let machine_id =
        db::id_param(&gobby_core::machine::read_local_machine_id().expect("machine id"))
            .expect("machine uuid");
    let project_id = db::id_param(project_id).expect("project uuid");
    let row = conn
        .query_one(
            "SELECT community_id_watermark, partition_signature
             FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &project_id],
        )
        .expect("read project state");
    (row.get(0), row.get(1))
}

fn run_churn_index(ctx: &Context, root: &Path, explicit_files: Vec<PathBuf>) -> IndexOutcome {
    crate::index::indexer::index_files(
        crate::index::indexer::IndexRequest {
            project_root: root.to_path_buf(),
            path_filter: None,
            explicit_files,
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        ctx,
        crate::index::indexer::IndexOptions::default(),
    )
    .expect("run churn index")
}

fn copy_source_tree(source: &Path, destination: &Path) -> std::io::Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if matches!(
            name.as_ref(),
            ".git"
                | ".gobby"
                | ".venv"
                | "node_modules"
                | "target"
                | "dist"
                | "build"
                | "__pycache__"
        ) || name.starts_with(".evidence-")
        {
            continue;
        }
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            continue;
        }
        let target = destination.join(entry.file_name());
        if file_type.is_dir() {
            copy_source_tree(&entry.path(), &target)?;
        } else if source_file_for_evidence(&entry.path()) {
            fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

fn source_file_for_evidence(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| matches!(name, ".gitignore" | "pyproject.toml" | "Cargo.toml"))
        || path
            .extension()
            .and_then(|extension| extension.to_str())
            .is_some_and(|extension| {
                matches!(
                    extension,
                    "c" | "cc"
                        | "cpp"
                        | "cs"
                        | "go"
                        | "h"
                        | "hpp"
                        | "java"
                        | "js"
                        | "jsx"
                        | "kt"
                        | "kts"
                        | "mjs"
                        | "php"
                        | "py"
                        | "rb"
                        | "rs"
                        | "scala"
                        | "sh"
                        | "swift"
                        | "ts"
                        | "tsx"
                )
            })
}

fn rebind_checkout(
    conn: &mut postgres::Client,
    database_url: &str,
    project_id: &str,
    root: &Path,
) -> CheckoutRootRestore {
    let machine_id =
        db::id_param(&gobby_core::machine::read_local_machine_id().expect("local machine id"))
            .expect("machine UUID");
    let project_uuid = db::id_param(project_id).expect("project UUID");
    let row = conn
        .query_one(
            "SELECT root_path FROM project_checkouts
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &project_uuid],
        )
        .expect("read original checkout root");
    let original_root = row.get::<_, String>(0);
    let root = root.to_string_lossy().to_string();
    assert_eq!(
        conn.execute(
            "UPDATE project_checkouts SET root_path = $3
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_id, &project_uuid, &root],
        )
        .expect("bind disposable checkout"),
        1
    );
    CheckoutRootRestore {
        database_url: database_url.to_string(),
        machine_id,
        project_id: project_uuid,
        original_root,
    }
}

fn evidence_renamed_path(path: &str, index: usize) -> String {
    let path = Path::new(path);
    let stem = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .expect("UTF-8 file stem");
    let extension = path
        .extension()
        .and_then(|extension| extension.to_str())
        .expect("file extension");
    path.with_file_name(format!("{stem}_q14_renamed_{index}.{extension}"))
        .to_string_lossy()
        .to_string()
}

fn python_module_name(path: &str) -> String {
    let path = path.strip_prefix("src/").unwrap_or(path);
    path.strip_suffix(".py")
        .unwrap_or(path)
        .replace('/', ".")
        .trim_end_matches(".__init__")
        .to_string()
}

fn print_churn_diff(
    corpus: &str,
    phase: &str,
    before: &[StoredCommunity],
    after: &[StoredCommunity],
) {
    let before = before
        .iter()
        .map(|row| (row.community_id, row))
        .collect::<BTreeMap<_, _>>();
    let after = after
        .iter()
        .map(|row| (row.community_id, row))
        .collect::<BTreeMap<_, _>>();
    let ids = before
        .keys()
        .chain(after.keys())
        .copied()
        .collect::<BTreeSet<_>>();
    let diffs = ids
        .into_iter()
        .filter_map(|community_id| {
            let previous = before.get(&community_id).copied();
            let current = after.get(&community_id).copied();
            let previous_value = previous.map(community_evidence);
            let current_value = current.map(community_evidence);
            (previous_value != current_value).then(|| {
                let previous_members = previous
                    .map(|row| row.members.iter().cloned().collect::<BTreeSet<_>>())
                    .unwrap_or_default();
                let current_members = current
                    .map(|row| row.members.iter().cloned().collect::<BTreeSet<_>>())
                    .unwrap_or_default();
                serde_json::json!({
                    "community_id": community_id,
                    "members_added": current_members.difference(&previous_members).collect::<Vec<_>>(),
                    "members_removed": previous_members.difference(&current_members).collect::<Vec<_>>(),
                    "before": previous_value,
                    "after": current_value,
                })
            })
        })
        .collect::<Vec<_>>();
    println!(
        "churn-diff corpus={corpus} phase={phase} {}",
        serde_json::to_string(&diffs).expect("serialize churn row diffs")
    );
}

fn community_evidence(row: &StoredCommunity) -> serde_json::Value {
    serde_json::json!({
        "community_id": row.community_id,
        "member_count": row.member_count,
        "representatives": row.representatives,
        "internal_edges": row.internal_edges,
        "cohesion": row.cohesion,
        "boundary": row.boundary,
        "member_signature": row.member_signature,
        "label_deterministic": row.label_deterministic,
        "label": row.label,
        "label_source": row.label_source.as_str(),
        "label_confidence": row.label_confidence,
        "label_model": row.label_model,
        "label_candidates": row.label_candidates,
        "labeled_signature": row.labeled_signature,
    })
}

fn unique_test_uuid(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &models::CODE_INDEX_UUID_NAMESPACE,
        format!("community-{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
}

struct ProjectCleanup {
    database_url: String,
    project_id: String,
}

struct CheckoutRootRestore {
    database_url: String,
    machine_id: uuid::Uuid,
    project_id: uuid::Uuid,
    original_root: String,
}

impl Drop for CheckoutRootRestore {
    fn drop(&mut self) {
        if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
            let _ = conn.execute(
                "UPDATE project_checkouts SET root_path = $3
                 WHERE machine_id = $1 AND project_id = $2",
                &[&self.machine_id, &self.project_id, &self.original_root],
            );
            let _ = conn.execute(
                "UPDATE code_indexed_project_states SET root_path = $3
                 WHERE machine_id = $1 AND project_id = $2",
                &[&self.machine_id, &self.project_id, &self.original_root],
            );
        }
    }
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            let _ = cleanup_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project_id = db::id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_communities WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    Ok(())
}
