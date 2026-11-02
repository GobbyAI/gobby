//! Regressions for the second review's lifecycle and shared-store findings.
use super::*;
use gobby_daemon::nodes::agents::{launch::reconcile, trust::seed_trust};
use std::{
    sync::{Arc, mpsc},
    thread,
};

#[tokio::test]
async fn reconcile_keeps_runs_launched_after_inventory_snapshot() -> Result<()> {
    let home = tempfile::tempdir()?;
    let agents = Arc::new(NodeAgents::open(home.path())?);
    let host = fake_gterm::FakeGterm::start(home.path()).await?;
    {
        let mut state = agents.state.lock().await;
        state
            .runs
            .insert("old-missing".into(), run(RunPhase::Launched, None));
        state.runs.insert(
            "new".into(),
            run(RunPhase::Preparing, Some(workspace(home.path(), false))),
        );
        state.commit(home.path(), &agents.changed)?;
    }
    host.pause_next_list_reply();
    let reconciling = tokio::spawn({
        let agents = agents.clone();
        async move { reconcile(&agents).await }
    });
    tokio::time::timeout(Duration::from_secs(5), host.wait_for_list_snapshot()).await?;
    {
        // Publish a launch after the host captured its inventory. That inventory
        // can prove absence only for runs already launched when the list began.
        let mut state = agents.state.lock().await;
        state.runs.get_mut("new").context("new run")?.phase = RunPhase::Launched;
        state.commit(home.path(), &agents.changed)?;
    }
    host.release_list_reply();
    tokio::time::timeout(Duration::from_secs(5), reconciling).await???;
    let state = persisted_state(home.path())?;
    assert!(
        state.runs.contains_key("new"),
        "stale inventory retired a newer live run"
    );
    assert!(
        !state.removals.contains_key("new"),
        "live workspace queued for deletion"
    );
    assert!(!state.exits.iter().any(|exit| exit.exit.run_id == "new"));
    assert!(
        state
            .exits
            .iter()
            .any(|exit| exit.exit.run_id == "old-missing")
    );
    Ok(())
}

#[tokio::test]
async fn restart_exit_keeps_workspace_metadata_and_cleanup_ownership() -> Result<()> {
    for reused in [false, true] {
        for cancelled in [false, true] {
            let home = tempfile::tempdir()?;
            let expected_workspace = workspace(home.path(), reused);
            {
                let agents = NodeAgents::open(home.path())?;
                let mut state = agents.state.lock().await;
                state.runs.insert(
                    "interrupted".into(),
                    run(RunPhase::Preparing, Some(expected_workspace.clone())),
                );
                state.pending_exits.insert("interrupted".into(), Some(7));
                if cancelled {
                    state.cancelled.insert("interrupted".into(), 1);
                }
                state.commit(home.path(), &agents.changed)?;
            }
            let agents = NodeAgents::open(home.path())?;
            let state = agents.state.lock().await;
            let report = &state.exits.first().context("recovered exit")?.exit;
            assert!(report.launched);
            assert_eq!(report.exit_code, Some(7));
            assert_eq!(
                report.reason,
                if cancelled {
                    ExitReason::Cancelled
                } else {
                    ExitReason::Exited
                }
            );
            let reported = report
                .workspace
                .as_ref()
                .context("recovery lost workspace metadata")?;
            assert_eq!(reported.path, expected_workspace.path.display().to_string());
            assert_eq!(reported.branch_name, expected_workspace.branch_name);
            assert_eq!(reported.base_branch, expected_workspace.base_branch);
            assert!(
                state.removals.is_empty(),
                "a launched workspace is queued for removal only after its exit is acknowledged"
            );
            assert_eq!(
                state.runs.get("interrupted").map(|run| run.phase),
                Some(RunPhase::Exited)
            );
            assert!(state.pending_exits.is_empty());
            assert_eq!(persisted_state(home.path())?, *state);
        }
    }
    Ok(())
}

fn seeders_respect_shared_lock(provider: &'static str) -> Result<()> {
    let home = tempfile::tempdir()?;
    let config = match provider {
        "claude" => home.path().join(".claude.json"),
        "codex" => home.path().join(".codex/config.toml"),
        _ => unreachable!(),
    };
    fs::create_dir_all(config.parent().context("config parent")?)?;
    let original = if provider == "claude" {
        "{\"foreign\":1}"
    } else {
        "foreign = 1\n"
    };
    fs::write(&config, original)?;
    fs::set_permissions(&config, fs::Permissions::from_mode(0o600))?;
    let old_file = fs::File::open(&config)?;
    let lock = config.with_file_name(format!(
        "{}.lock",
        config
            .file_name()
            .context("config filename")?
            .to_string_lossy()
    ));
    fs::create_dir(&lock)?;
    let (started, starts) = mpsc::channel();
    let (finished, finishes) = mpsc::channel();
    let mut workers = Vec::new();
    for id in ["one", "two"] {
        let home = home.path().to_path_buf();
        fs::create_dir(home.join(id))?;
        let started = started.clone();
        let finished = finished.clone();
        workers.push(thread::spawn(move || {
            started.send(()).expect("signal start");
            let result = seed_trust(provider, &home, &home.join(id));
            finished.send(()).expect("signal finish");
            result
        }));
    }
    for _ in 0..2 {
        starts.recv_timeout(Duration::from_secs(5))?;
    }
    let completed_while_locked = finishes.recv_timeout(Duration::from_millis(250)).is_ok();
    // Model a CLI writer's update under the shared lock. Each seeder must read
    // this fresh version after acquiring the lock, then preserve the other seed.
    fs::write(
        &config,
        if provider == "claude" {
            "{\"foreign\":2}"
        } else {
            "foreign = 2\n"
        },
    )?;
    fs::remove_dir(&lock)?;
    for worker in workers {
        worker.join().expect("seeder panicked")?;
    }
    assert!(
        !completed_while_locked,
        "{provider} seeder bypassed the shared config lock"
    );
    assert_ne!(
        old_file.metadata()?.ino(),
        fs::metadata(&config)?.ino(),
        "config was overwritten in place"
    );
    assert_eq!(fs::metadata(&config)?.permissions().mode() & 0o777, 0o600);
    let text = fs::read_to_string(&config)?;
    if provider == "claude" {
        let data: Value = serde_json::from_str(&text)?;
        assert_eq!(data["foreign"], 2);
        for id in ["one", "two"] {
            assert_eq!(
                data["projects"][home.path().join(id).display().to_string()]["hasTrustDialogAccepted"],
                true
            );
        }
    } else {
        let data: toml_edit::DocumentMut = text.parse()?;
        assert_eq!(data["foreign"].as_integer(), Some(2));
        for id in ["one", "two"] {
            assert_eq!(
                data["projects"][&home.path().join(id).display().to_string()]["trust_level"]
                    .as_str(),
                Some("trusted")
            );
        }
    }
    Ok(())
}

#[test]
fn claude_trust_serializes_with_shared_writers() -> Result<()> {
    let result = seeders_respect_shared_lock("claude");
    assert!(result.is_ok(), "{result:?}");
    result
}

#[test]
fn codex_trust_serializes_concurrent_seeds_and_replaces_atomically() -> Result<()> {
    let result = seeders_respect_shared_lock("codex");
    assert!(result.is_ok(), "{result:?}");
    result
}
