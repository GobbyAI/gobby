#![cfg(unix)]

use anyhow::Result;
use gobby_core::bootstrap::parse_hub_database_bootstrap;
use gobby_daemon::node_commands::{ExitReason, ReportExit, WorkspaceMode};
use gobby_daemon::nodes::agents::{
    NodeAgents,
    state::{AgentState, ExitRecord, RunPhase, RunRecord, WorkspaceRecord},
};
use gobby_daemon::state::NodeServices;
use std::{fs, os::unix::fs::MetadataExt, os::unix::fs::PermissionsExt, path::Path};

fn workspace(root: &Path, reused: bool) -> WorkspaceRecord {
    WorkspaceRecord {
        mode: WorkspaceMode::Worktree,
        path: root.join("workspace"),
        branch_name: "agent/run".into(),
        base_branch: "main".into(),
        source_root: root.join("source"),
        reused,
    }
}

fn run(phase: RunPhase, workspace: Option<WorkspaceRecord>) -> RunRecord {
    RunRecord {
        terminal_id: "terminal".into(),
        phase,
        accepted_at_ms: 100,
        workspace,
        host_terminal_id: Some("host-terminal".into()),
    }
}

fn exit(run_id: &str, ended_at_ms: u64) -> ExitRecord {
    ExitRecord {
        exit: ReportExit {
            run_id: run_id.into(),
            terminal_id: "finished-terminal".into(),
            launched: true,
            exit_code: Some(0),
            reason: ExitReason::Exited,
            workspace: None,
            sandbox: None,
        },
        ended_at_ms,
    }
}

#[tokio::test]
async fn agent_state_round_trips() -> Result<()> {
    let home = tempfile::tempdir()?;
    let agents = NodeAgents::open(home.path())?;
    let path = home.path().join("node/agents.json");
    assert!(path.is_file(), "opening absent state must persist it");
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            agents.changed.notified()
        )
        .await
        .is_ok(),
        "initial commit must notify consumers"
    );
    assert_eq!(
        fs::metadata(path.parent().unwrap())?.permissions().mode() & 0o777,
        0o700
    );
    let old_file = fs::File::open(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644))?;
    let expected = {
        let mut state = agents.state.lock().await;
        state.hub_epoch = Some(7);
        state.host_epoch = 3;
        state.host_epoch_id = Some("host-epoch".into());
        state.runs.insert(
            "live".into(),
            run(RunPhase::Launched, Some(workspace(home.path(), false))),
        );
        state
            .removals
            .insert("old".into(), workspace(home.path(), false));
        state.cancelled.insert("cancelled".into(), 123);
        state.exits.push(exit("finished", 200));
        assert_eq!(state.live_and_accepted(), 1);
        state.commit(&agents.home, &agents.changed)?;
        state.clone()
    };
    assert_ne!(
        old_file.metadata()?.ino(),
        fs::metadata(&path)?.ino(),
        "commit must replace by rename"
    );
    assert_eq!(fs::metadata(&path)?.permissions().mode() & 0o777, 0o600);
    assert!(!home.path().join("node/agents.json.tmp").exists());
    assert!(
        tokio::time::timeout(std::time::Duration::from_secs(1), agents.changed.notified())
            .await
            .is_ok()
    );
    drop(agents);
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(*reopened.state.lock().await, expected);
    Ok(())
}

#[tokio::test]
async fn revision_never_repeats_after_restart() -> Result<()> {
    let home = tempfile::tempdir()?;
    let mut previous = 0;
    for _ in 0..4 {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        let revision = state.next_revision(&agents.home, &agents.changed)?;
        assert!(revision > previous, "revision must survive every reopen");
        let persisted: AgentState =
            serde_json::from_slice(&fs::read(home.path().join("node/agents.json"))?)?;
        assert_eq!(
            persisted.revision, revision,
            "value must be durable before return"
        );
        previous = revision;
    }
    Ok(())
}

#[test]
fn prepare_refuses_unreadable_agent_state() -> Result<()> {
    let home = tempfile::tempdir()?;
    let state_path = home.path().join("node/agents.json");
    fs::create_dir(state_path.parent().unwrap())?;
    fs::write(&state_path, b"{broken")?;
    let bootstrap = parse_hub_database_bootstrap(
        "datastore_mode: remote\nhub_daemon_url: http://127.0.0.1:1\napi_key: gobby_00000000000000000000000000000000000000000002CZclj\n",
    )?.expect("node bootstrap");
    let prepared =
        NodeServices::prepare(&bootstrap, &home.path().join("bootstrap.yaml"), home.path());
    assert!(prepared.is_err(), "corrupt state must prevent node startup");
    let message = format!("{:#}", prepared.err().unwrap());
    assert!(
        message.contains(&state_path.display().to_string()),
        "{message}"
    );
    assert!(message.contains("move it aside to reset"), "{message}");
    assert_eq!(fs::read(&state_path)?, b"{broken");
    Ok(())
}

#[tokio::test]
async fn cancel_records_survive_restart_and_expire() -> Result<()> {
    let home = tempfile::tempdir()?;
    let now = 2 * 24 * 60 * 60 * 1_000;
    let cutoff = now - 24 * 60 * 60 * 1_000;
    {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        state.cancelled.insert("expired".into(), cutoff - 1);
        state.cancelled.insert("boundary".into(), cutoff);
        state.cancelled.insert("recent".into(), now);
        state.cancelled.insert("future".into(), now + 1);
        state.commit(&agents.home, &agents.changed)?;
    }
    let agents = NodeAgents::open(home.path())?;
    {
        let mut state = agents.state.lock().await;
        assert_eq!(
            state.cancelled.len(),
            4,
            "open must retain cancellation records"
        );
        state.prune_cancelled(now);
        assert!(!state.cancelled.contains_key("expired"));
        assert!(state.cancelled.contains_key("boundary"));
        assert!(state.cancelled.contains_key("recent"));
        assert!(state.cancelled.contains_key("future"));
        state.commit(&agents.home, &agents.changed)?;
    }
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(reopened.state.lock().await.cancelled.len(), 3);
    Ok(())
}

#[tokio::test]
async fn restart_fails_unlaunched_runs() -> Result<()> {
    let home = tempfile::tempdir()?;
    {
        let agents = NodeAgents::open(home.path())?;
        let mut state = agents.state.lock().await;
        state.exits.push(exit("previous", 1));
        state
            .removals
            .insert("existing-removal".into(), workspace(home.path(), false));
        state
            .runs
            .insert("accepted".into(), run(RunPhase::Accepted, None));
        state.runs.insert(
            "preparing".into(),
            run(RunPhase::Preparing, Some(workspace(home.path(), false))),
        );
        state.runs.insert(
            "reused".into(),
            run(RunPhase::Preparing, Some(workspace(home.path(), true))),
        );
        state.runs.insert(
            "launched".into(),
            run(RunPhase::Launched, Some(workspace(home.path(), false))),
        );
        assert_eq!(state.live_and_accepted(), 4);
        state.commit(&agents.home, &agents.changed)?;
    }
    let recovered = {
        let agents = NodeAgents::open(home.path())?;
        let state = agents.state.lock().await;
        assert_eq!(state.live_and_accepted(), 1);
        assert!(state.runs.contains_key("launched"));
        assert_eq!(state.removals.len(), 2);
        assert!(state.removals.contains_key("preparing"));
        assert!(state.removals.contains_key("existing-removal"));
        assert!(!state.removals.contains_key("reused"));
        assert_eq!(state.exits.len(), 4);
        assert_eq!(state.exits[0].exit.run_id, "previous");
        assert!(
            state
                .exits
                .windows(2)
                .all(|pair| pair[0].ended_at_ms <= pair[1].ended_at_ms)
        );
        for recovered in &state.exits[1..] {
            assert_eq!(recovered.exit.reason, ExitReason::PrepareFailed);
            assert!(!recovered.exit.launched);
            assert!(recovered.exit.workspace.is_none());
            assert!(recovered.exit.exit_code.is_none());
        }
        state.clone()
    };
    let reopened = NodeAgents::open(home.path())?;
    assert_eq!(
        *reopened.state.lock().await,
        recovered,
        "recovery must persist and be idempotent"
    );
    Ok(())
}
