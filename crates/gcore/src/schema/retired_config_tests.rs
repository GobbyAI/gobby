use super::assets::MIGRATIONS;
use super::runner::SchemaRunner;
use super::runner_tests::{DATABASE_TEST_LOCK, scratch_database};

#[test]
fn retired_terminal_config_rows_are_deleted() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
        return Ok(());
    };
    let index = MIGRATIONS
        .iter()
        .position(|migration| migration.version == 502)
        .unwrap_or(MIGRATIONS.len());
    SchemaRunner::with_migrations_for_test(&mut client, "public", &MIGRATIONS[..index])?.apply()?;
    for key in [
        "tmux",
        "tmux.enabled",
        "tmux.attach_history_lines",
        "terminal_host.tmux_attach_history_max_bytes",
        "terminal_host.tmux_poll_interval_ms",
        "terminal_host.tmux_poll_backoff_ceiling_ms",
        "terminal_host.max_attached_terminals",
        "terminals.attach_history_lines",
        "terminal_host.max_attachments_total",
    ] {
        client.execute(
            "INSERT INTO config_store(key, value) VALUES ($1, '700')",
            &[&key],
        )?;
    }
    let before: i64 = client
        .query_one("SELECT revision FROM config_state WHERE id = true", &[])?
        .get(0);
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let remaining: Vec<String> = client
        .query("SELECT key FROM config_store ORDER BY key", &[])?
        .iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(
        remaining,
        [
            "terminal_host.max_attachments_total",
            "terminals.attach_history_lines"
        ]
    );
    let after: i64 = client
        .query_one("SELECT revision FROM config_state WHERE id = true", &[])?
        .get(0);
    assert_eq!(after, before + 1);
    assert_eq!(
        SchemaRunner::new(&mut client, "public")?
            .apply()?
            .migrations_applied,
        0
    );
    let rerun_revision: i64 = client
        .query_one("SELECT revision FROM config_state WHERE id = true", &[])?
        .get(0);
    assert_eq!(rerun_revision, after);
    Ok(())
}

#[test]
fn non_native_terminal_rows_are_deleted() -> anyhow::Result<()> {
    let _serial = DATABASE_TEST_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some((_database, mut client)) = scratch_database()? else {
        return Ok(());
    };
    let index = MIGRATIONS
        .iter()
        .position(|migration| migration.version == 502)
        .unwrap_or(MIGRATIONS.len());
    SchemaRunner::with_migrations_for_test(&mut client, "public", &MIGRATIONS[..index])?.apply()?;
    client.batch_execute(
        "INSERT INTO users (id, email, name, password_hash)
         VALUES ('99999999-9999-4999-8999-999999999999', 'terminal-migration@test.local', 'test', 'x');
         INSERT INTO machines (id, hostname, owner_user_id)
         VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'test-node',
                 '99999999-9999-4999-8999-999999999999');
         INSERT INTO projects (id, name)
         VALUES ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'terminal-migration');
         INSERT INTO sessions (id, external_id, machine_id, source, project_id)
         VALUES ('cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'terminal-migration',
                 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'test',
                 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
         INSERT INTO terminals (id, backend, ownership, state, spawn_key, machine_id, project_id)
         VALUES ('dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'tmux', 'gobby', 'pending', 'retired',
                 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
                ('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', 'native', 'gobby', 'pending', 'retained',
                 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
         INSERT INTO agent_runs (id, machine_id, parent_session_id, provider, prompt, terminal_id)
         VALUES ('ffffffff-ffff-4fff-8fff-ffffffffffff', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'test', 'test',
                 'dddddddd-dddd-4ddd-8ddd-dddddddddddd');",
    )?;
    SchemaRunner::new(&mut client, "public")?.apply()?;
    let backends: Vec<String> = client
        .query("SELECT backend FROM terminals ORDER BY backend", &[])?
        .iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(backends, ["native"]);
    let cleared: bool = client
        .query_one(
            "SELECT terminal_id IS NULL FROM agent_runs WHERE id = \
             'ffffffff-ffff-4fff-8fff-ffffffffffff'",
            &[],
        )?
        .get(0);
    assert!(cleared);
    Ok(())
}
