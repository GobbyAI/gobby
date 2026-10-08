use super::*;

#[test]
#[serial_test::serial(serial_db)]
fn replacement_bounds_statement_latency_for_worktree_sized_partition() -> anyhow::Result<()> {
    let (mut conn, _, project_id, _cleanup) = seeded_project("persist-roundtrips");
    // Shadow only this connection's community table. The delay and statement
    // counter disappear with the connection, including on an assertion failure.
    conn.batch_execute(
        "CREATE TEMP TABLE code_communities
             (LIKE public.code_communities INCLUDING ALL);
         CREATE TEMP TABLE persist_statements (id bigserial);
         CREATE FUNCTION pg_temp.delay_community_statement() RETURNS trigger
             LANGUAGE plpgsql AS $$
             BEGIN
                 PERFORM pg_sleep(0.01);
                 INSERT INTO pg_temp.persist_statements DEFAULT VALUES;
                 RETURN NULL;
             END $$;
         CREATE TRIGGER community_statement_latency
             AFTER INSERT ON pg_temp.code_communities
             FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.delay_community_statement();",
    )?;
    let rows = (1..=2_570)
        .map(|id| {
            let member = format!("file_{id}.py");
            let mut row = stored_row(&project_id, id, &[&member], &format!("{id:016x}"));
            row.boundary = vec![(id + 1, 2)];
            row.refreshed_at = UNIX_EPOCH + Duration::from_secs(1_700_000_000);
            if id % 2 == 0 {
                row.label = format!("model-{id}");
                row.label_source = LabelSource::Model;
                row.label_confidence = Some(0.8);
                row.label_model = Some("fixture-model".to_string());
                row.labeled_signature = Some(row.member_signature.clone());
                row.labeled_at = Some(UNIX_EPOCH + Duration::from_secs(1_700_000_000));
                row.label_attempted_at = row.labeled_at;
            }
            row
        })
        .collect::<Vec<_>>();
    let expected = rows.clone();
    let started = Instant::now();
    store_partition(&mut conn, &project_id, rows, 2_570, "worktree-sized");
    let elapsed = started.elapsed();
    let statements: i64 = conn
        .query_one("SELECT count(*) FROM pg_temp.persist_statements", &[])?
        .get(0);
    println!("community_persist rows=2570 statements={statements} elapsed={elapsed:?}");
    assert!(
        elapsed < Duration::from_secs(10),
        "serial statement latency exhausted the incremental-index budget: {elapsed:?}"
    );
    assert!(
        statements <= 16,
        "too many database round trips: {statements}"
    );
    let stored = raw_rows(&mut conn, &project_id);
    assert_eq!(stored.len(), expected.len());
    for (actual, expected) in stored.iter().zip(&expected) {
        assert_eq!(actual.machine_id, expected.machine_id);
        assert_eq!(actual.project_id, expected.project_id);
        assert_eq!(actual.community_id, expected.community_id);
        assert_eq!(actual.member_count, expected.member_count);
        assert_eq!(actual.members, expected.members);
        assert_eq!(actual.representatives, expected.representatives);
        assert_eq!(actual.internal_edges, expected.internal_edges);
        assert_eq!(actual.cohesion, expected.cohesion);
        assert_eq!(actual.boundary, expected.boundary);
        assert_eq!(actual.member_signature, expected.member_signature);
        assert_eq!(actual.label_deterministic, expected.label_deterministic);
        assert_eq!(actual.label, expected.label);
        assert_eq!(actual.label_source, expected.label_source);
        assert_eq!(actual.label_confidence, expected.label_confidence);
        assert_eq!(actual.label_model, expected.label_model);
        assert_eq!(actual.label_candidates, expected.label_candidates);
        assert_eq!(actual.labeled_signature, expected.labeled_signature);
        assert_eq!(actual.labeled_at, expected.labeled_at);
        assert_eq!(actual.label_attempted_at, expected.label_attempted_at);
        assert_eq!(actual.refreshed_at, expected.refreshed_at);
    }
    assert_eq!(
        project_state(&mut conn, &project_id),
        (2_570, Some("worktree-sized".into()))
    );
    Ok(())
}

#[test]
#[serial_test::serial(serial_db)]
fn replacement_rolls_back_all_batches_when_later_row_is_invalid() -> anyhow::Result<()> {
    let (mut conn, _, project_id, _cleanup) = seeded_project("persist-rollback");
    let original = stored_row(&project_id, 1, &["old.py"], "1111111111111111");
    store_partition(&mut conn, &project_id, vec![original], 1, "old");
    let mut rows = (1..=300)
        .map(|id| stored_row(&project_id, id, &["new.py"], "2222222222222222"))
        .collect::<Vec<_>>();
    rows[299].member_count = 2;
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let result =
        db::begin_replace(&mut conn, &machine_id, &project_id)?.commit(rows, 300, "invalid");
    assert!(result.is_err(), "invalid row must reject the replacement");
    let stored = raw_rows(&mut conn, &project_id);
    assert_eq!(stored.len(), 1);
    assert_eq!(stored[0].members, ["old.py"]);
    assert_eq!(
        project_state(&mut conn, &project_id),
        (1, Some("old".into()))
    );
    Ok(())
}
